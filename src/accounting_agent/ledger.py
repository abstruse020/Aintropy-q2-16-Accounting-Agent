"""Ledger adapters. Both implement the same small interface:

    categories()                       -> {name: 'expense'|'income'}  (the only valid choices)
    post(number, date, account, amount_minor, kind, category, description) -> ref
          raises DuplicateNumber if `number` was already posted
    find_by_number(number)             -> ref | None
    search(query, category, start, end, limit) -> [entry dicts]
    spend_report(start, end)           -> totals

FakeLedger (default): SQLite, no network, keeps only what the pipeline needs.
AkauntingLedger: a running Akaunting instance over its REST API (stdlib only).
Select with env vars: AKAUNTING_URL, AKAUNTING_USER, AKAUNTING_PASSWORD.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .db import fmt

# Seed set for FakeLedger, and for `seed-categories` on a fresh Akaunting.
DEFAULT_CATEGORIES = {
    "Groceries": "expense",
    "Dining": "expense",
    "Transport": "expense",
    "Utilities": "expense",
    "Rent": "expense",
    "Subscriptions": "expense",
    "Shopping": "expense",
    "Health": "expense",
    "Salary": "income",
    "Other Income": "income",
}


class LedgerError(Exception):
    """The ledger is unreachable or rejected a request. Retryable later."""


class DuplicateNumber(Exception):
    pass


def aggregate(rows):
    """rows: iterable of (kind, category, signed amount_minor) -> report dict."""
    per_cat, spent, income = {}, 0, 0
    for kind, category, minor in rows:
        if kind == "expense":
            spent += -minor
            c = per_cat.setdefault(category, [0, 0])
            c[0] += -minor
            c[1] += 1
        elif kind == "income":
            income += minor
    ordered = sorted(per_cat.items(), key=lambda kv: -kv[1][0])
    return {"expenses_by_category": {k: {"total": fmt(v[0]), "count": v[1]} for k, v in ordered},
            "total_spent": fmt(spent), "total_income": fmt(income), "net": fmt(income - spent)}


class FakeLedger:
    name = "fake"

    def __init__(self, conn):
        self.conn = conn

    def categories(self):
        return dict(DEFAULT_CATEGORIES)

    def post(self, number, date, account, amount_minor, kind, category, description):
        if category not in DEFAULT_CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        try:
            cur = self.conn.execute(
                "INSERT INTO ledger_entries (number, date, account, amount_minor, kind, category, description)"
                " VALUES (?,?,?,?,?,?,?)",
                (number, date, account, amount_minor, kind, category, description),
            )
        except Exception as e:  # sqlite3.IntegrityError on the UNIQUE number
            if "UNIQUE" in str(e):
                raise DuplicateNumber(number) from e
            raise
        self.conn.commit()
        return cur.lastrowid

    def find_by_number(self, number):
        row = self.conn.execute("SELECT id FROM ledger_entries WHERE number=?", (number,)).fetchone()
        return row["id"] if row else None

    def _where(self, start, end):
        sql, args = " WHERE 1=1", []
        if start:
            sql += " AND date>=?"
            args.append(start)
        if end:
            sql += " AND date<=?"
            args.append(end)
        return sql, args

    def search(self, query=None, category=None, start=None, end=None, limit=50):
        where, args = self._where(start, end)
        if query:
            where += " AND description LIKE ?"
            args.append(f"%{query}%")
        if category:
            where += " AND category=?"
            args.append(category)
        rows = self.conn.execute(f"SELECT * FROM ledger_entries{where} ORDER BY date, id LIMIT ?",
                                 args + [int(limit)])
        return [{"date": r["date"], "description": r["description"], "amount": fmt(r["amount_minor"]),
                 "account": r["account"], "kind": r["kind"], "category": r["category"]} for r in rows]

    def spend_report(self, start=None, end=None):
        where, args = self._where(start, end)
        return aggregate((r["kind"], r["category"], r["amount_minor"]) for r in
                         self.conn.execute(f"SELECT kind, category, amount_minor FROM ledger_entries{where}", args))


class ApiError(LedgerError):
    def __init__(self, status, payload):
        self.status, self.payload = status, payload if isinstance(payload, dict) else {}
        self.errors = self.payload.get("errors") or {}
        super().__init__(f"Akaunting API {status}: {self.payload.get('message') or self.payload or 'no detail'}")


class AkauntingLedger:
    """Akaunting REST adapter. Verified against Akaunting 3.x running in Docker:
    HTTP Basic auth, single default company (no company_id needed), paginated
    {data, meta} responses, `number` unique per company (HTTP 422 on duplicates).
    """
    name = "akaunting"
    FALLBACK_PAYMENT_METHOD = "offline-payments.bank_transfer.2"

    def __init__(self, base_url, user, password, payment_method=None, timeout=30):
        self.base = base_url.rstrip("/") + "/api"
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout
        self._payment_method = payment_method
        self._cats = None        # name -> (id, type), income/expense only
        self._cat_names = None   # id -> name, all types
        self._accounts = None    # name.lower() -> (id, currency)
        self._account_names = None

    # ---- http
    def _request(self, method, path, params=None, body=None):
        url = self.base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(
            url, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except ValueError:
                payload = {}
            if e.code in (401, 403):
                raise LedgerError(f"Akaunting rejected the credentials (HTTP {e.code}); the user needs `read-api`")
            raise ApiError(e.code, payload)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LedgerError(f"cannot reach Akaunting at {self.base}: {e}")
        return json.loads(raw) if raw else {}

    def _list(self, path, params=None):
        out, page = [], 1
        while True:
            resp = self._request("GET", path, dict(params or {}, limit=100, page=page))
            out += resp.get("data", [])
            if page >= (resp.get("meta") or {}).get("last_page", 1):
                return out
            page += 1

    # ---- reference data (cached per process)
    def _load_categories(self):
        rows = self._list("/categories")
        self._cat_names = {c["id"]: c["name"] for c in rows}
        self._cats = {c["name"]: (c["id"], c["type"]) for c in rows
                      if c["type"] in ("expense", "income") and c.get("enabled", True)}

    def categories(self):
        if self._cats is None:
            self._load_categories()
        return {n: t for n, (_, t) in self._cats.items()}

    def ensure_categories(self, wanted):
        """Create missing categories in Akaunting (explicit `seed-categories` command only)."""
        have = self.categories()
        created = []
        for name, typ in wanted.items():
            if name not in have:
                self._request("POST", "/categories", body={"name": name, "type": typ, "color": "#6b7280", "enabled": 1})
                created.append(name)
        self._cats = None
        return created

    def _load_accounts(self):
        rows = self._list("/accounts")
        self._accounts = {a["name"].lower(): (a["id"], a["currency_code"]) for a in rows}
        self._account_names = {a["id"]: a["name"] for a in rows}

    def _account(self, name):
        if self._accounts is None:
            self._load_accounts()
        if name.lower() not in self._accounts:     # bank account named in the statement: create it
            currency = next(iter(self._accounts.values()), (None, "USD"))[1]
            number = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-") or "ACCT"
            self._request("POST", "/accounts", body={"name": name, "number": number, "currency_code": currency,
                                                     "opening_balance": 0, "type": "bank", "enabled": 1})
            self._load_accounts()
        return self._accounts[name.lower()]

    def _pay_method(self):
        if self._payment_method is None:
            code = None
            try:
                for s in self._list("/settings"):
                    if s.get("key") == "offline-payments.methods":
                        methods = json.loads(s["value"])
                        code = next((m["code"] for m in methods if "bank" in m["code"]), methods[0]["code"])
            except (LedgerError, ValueError, KeyError, IndexError):
                pass
            self._payment_method = code or self.FALLBACK_PAYMENT_METHOD
        return self._payment_method

    # ---- LedgerClient interface
    def post(self, number, date, account, amount_minor, kind, category, description):
        if self._cats is None:
            self._load_categories()
        if category not in self._cats or self._cats[category][1] != kind:
            raise ValueError(f"{kind} category {category!r} does not exist in Akaunting")
        account_id, currency = self._account(account)
        body = {"type": kind, "number": number, "account_id": account_id, "paid_at": f"{date} 00:00:00",
                "amount": fmt(abs(amount_minor)), "currency_code": currency, "currency_rate": 1,
                "category_id": self._cats[category][0], "payment_method": self._pay_method(),
                "description": description}
        try:
            return self._request("POST", "/transactions", body=body)["data"]["id"]
        except ApiError as e:
            if e.status == 422 and "number" in e.errors:
                raise DuplicateNumber(number)
            if e.status == 422 and "payment_method" in e.errors:
                raise LedgerError(f"{e}. On a fresh Docker install run: docker exec akaunting php artisan cache:clear")
            raise

    def find_by_number(self, number):
        for t in self._request("GET", "/transactions", {"search": f"number:{number}"}).get("data", []):
            if t["number"] == number:
                return t["id"]
        return None

    def _entries(self):
        if self._cat_names is None:
            self._load_categories()
        if self._accounts is None:
            self._load_accounts()
        out = []
        for t in self._list("/transactions"):
            if t["type"] not in ("expense", "income"):      # skip transfers, splits, etc.
                continue
            minor = abs(int(round(float(t["amount"]) * 100)))
            out.append({"date": t["paid_at"][:10], "description": t.get("description") or t["number"],
                        "amount_minor": -minor if t["type"] == "expense" else minor,
                        "account": self._account_names.get(t["account_id"], f"account #{t['account_id']}"),
                        "kind": t["type"], "category": self._cat_names.get(t["category_id"], f"category #{t['category_id']}"),
                        "id": t["id"]})
        return sorted(out, key=lambda e: (e["date"], e["id"]))

    @staticmethod
    def _in_range(e, start, end):
        return (not start or e["date"] >= start) and (not end or e["date"] <= end)

    def search(self, query=None, category=None, start=None, end=None, limit=50):
        rows = [e for e in self._entries() if self._in_range(e, start, end)
                and (not query or query.lower() in e["description"].lower())
                and (not category or e["category"] == category)]
        return [{"date": e["date"], "description": e["description"], "amount": fmt(e["amount_minor"]),
                 "account": e["account"], "kind": e["kind"], "category": e["category"]} for e in rows[:int(limit)]]

    def spend_report(self, start=None, end=None):
        return aggregate((e["kind"], e["category"], e["amount_minor"])
                         for e in self._entries() if self._in_range(e, start, end))


AKAUNTING_ENV = ("AKAUNTING_URL", "AKAUNTING_USER", "AKAUNTING_PASSWORD", "AKAUNTING_PAYMENT_METHOD")


_akaunting_instances = {}


def get_ledger(conn):
    """FakeLedger unless AKAUNTING_URL is set. The Akaunting adapter is reused per
    process so its category/account lookups are cached across tool calls."""
    url = os.environ.get("AKAUNTING_URL")
    if not url:
        return FakeLedger(conn)
    user, pw = os.environ.get("AKAUNTING_USER"), os.environ.get("AKAUNTING_PASSWORD")
    if not user or not pw:
        raise LedgerError("AKAUNTING_URL is set but AKAUNTING_USER / AKAUNTING_PASSWORD are not")
    key = (url, user, pw)
    if key not in _akaunting_instances:
        _akaunting_instances[key] = AkauntingLedger(url, user, pw, os.environ.get("AKAUNTING_PAYMENT_METHOD"))
    return _akaunting_instances[key]
