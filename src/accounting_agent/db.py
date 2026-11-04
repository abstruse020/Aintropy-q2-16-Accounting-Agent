"""SQLite store: pipeline state, the fake ledger, and the audit log (one file)."""
import csv
import hashlib
import json
import re
import sqlite3
import time
from decimal import Decimal, ROUND_HALF_UP

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT NOT NULL UNIQUE,
    date            TEXT NOT NULL,
    description     TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,          -- signed integer cents
    account         TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'new',
        -- new -> posted | needs_review -> (human) -> posted | skipped
    kind            TEXT,                      -- expense | income
    category        TEXT,
    confidence      REAL,
    rationale       TEXT,
    idempotency_key TEXT UNIQUE,               -- frozen at first post attempt
    ledger_ref      INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id           INTEGER PRIMARY KEY,
    number       TEXT NOT NULL UNIQUE,         -- same role as Akaunting's `number`
    date         TEXT NOT NULL,
    account      TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    category     TEXT NOT NULL,
    description  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    actor  TEXT NOT NULL,                      -- agent:sync | agent:chat | human | system
    action TEXT NOT NULL,
    args   TEXT,
    result TEXT
);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def audit(conn, actor, action, args=None, result=None):
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, args, result) VALUES (?,?,?,?,?)",
        (time.time(), actor, action, json.dumps(args), json.dumps(result)),
    )
    conn.commit()


def to_minor(text):
    """'-1,234.50' -> -123450. Decimal, never float."""
    d = Decimal(str(text).replace(",", "").replace("$", "").strip())
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(minor):
    return f"{Decimal(minor) / 100:.2f}"


def _normalize(desc):
    return re.sub(r"\s+", " ", desc.strip().upper())


def ingest_csv(conn, path):
    """Load a bank statement CSV (date,description,amount,account).

    Idempotent: the fingerprint includes an occurrence index, so two identical
    purchases on one day stay distinct, and re-loading the same file adds nothing.
    """
    seen = {}
    added = skipped = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            minor = to_minor(row["amount"])
            base = "|".join([row["account"].strip(), row["date"].strip(), str(minor), _normalize(row["description"])])
            idx = seen.get(base, 0)
            seen[base] = idx + 1
            fp = hashlib.sha256(f"{base}|{idx}".encode()).hexdigest()
            cur = conn.execute(
                "INSERT OR IGNORE INTO transactions (fingerprint, date, description, amount_minor, account)"
                " VALUES (?,?,?,?,?)",
                (fp, row["date"].strip(), row["description"].strip(), minor, row["account"].strip()),
            )
            added += cur.rowcount
            skipped += 1 - cur.rowcount
    conn.commit()
    audit(conn, "system", "ingest", {"file": str(path)}, {"added": added, "already_present": skipped})
    return added, skipped
