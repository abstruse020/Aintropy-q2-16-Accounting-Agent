"""Workflow manager: sync (agent proposes), review (human decides), chat (read-only Q&A)."""
import uuid

from . import db, llm
from .db import fmt
from .ledger import AkauntingLedger, DEFAULT_CATEGORIES, LedgerError, get_ledger
from .tools import post_transaction

MAX_ROUNDS = 5


def counts(conn):
    return {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM transactions GROUP BY state")}


def retry_approved(conn):
    """Resume: post anything approved earlier but not posted (e.g. the ledger was down)."""
    for r in conn.execute("SELECT id FROM transactions WHERE state='approved' ORDER BY id").fetchall():
        try:
            post_transaction(conn, r["id"], actor="system")
            print(f"[sync] posted previously approved #{r['id']}")
        except LedgerError as e:
            print(f"[sync] ledger still unavailable: {e}")
            return


def sync(conn, db_path):
    """Run the agent until nothing is left in state 'new' (bounded)."""
    retry_approved(conn)
    for rnd in range(1, MAX_ROUNDS + 1):
        pending = counts(conn).get("new", 0)
        if pending == 0:
            break
        print(f"[sync] round {rnd}: {pending} pending")
        data = llm.run_claude("Process all pending transactions now.", "sync", db_path, llm.SYNC_PROMPT)
        db.audit(conn, "agent:sync", "run", {"round": rnd}, {"cost_usd": data.get("total_cost_usd"),
                                                             "summary": str(data.get("result"))[:300]})
        print(f"[sync] agent: {str(data.get('result')).strip()[:300]}")
    left = counts(conn).get("new", 0)
    if left:
        print(f"[sync] {left} transaction(s) still unprocessed after {MAX_ROUNDS} rounds; run sync again")
    return counts(conn)


def review(conn, accept_all=False, input_fn=input):
    """Human step. Each item shows the agent's suggestion; the human decides."""
    rows = conn.execute("SELECT * FROM transactions WHERE state='needs_review' ORDER BY date, id").fetchall()
    cats = get_ledger(conn).categories()
    if not rows:
        print("[review] nothing to review")
    for t in rows:
        sug = f"{t['kind']}/{t['category']} ({t['confidence']:.2f})" if t["category"] else "no suggestion"
        print(f"\n#{t['id']} {t['date']} {t['account']} {fmt(t['amount_minor']):>10}  {t['description']}")
        print(f"   agent: {sug}  | {t['rationale'] or ''}")
        if accept_all:
            if not t["category"]:
                print("   -> left in queue (no suggestion to accept)")
                continue
            choice = ""
        else:
            choice = input_fn("   [enter]=accept, category name=override, s=skip > ").strip()
        if choice.lower() == "s":
            conn.execute("UPDATE transactions SET state='skipped' WHERE id=?", (t["id"],))
            conn.commit()
            db.audit(conn, "human", "skip", {"txn_id": t["id"]})
            continue
        category = t["category"] if not choice else next((c for c in cats if c.lower() == choice.lower()), None)
        if category is None:
            print("   unknown category; left in queue")
            continue
        if not choice and not t["category"]:
            print("   nothing to accept; left in queue")
            continue
        kind = cats[category]
        conn.execute("UPDATE transactions SET state='approved', kind=?, category=? WHERE id=?",
                     (kind, category, t["id"]))
        conn.commit()
        db.audit(conn, "human", "approve", {"txn_id": t["id"], "category": category, "kind": kind})
        try:
            post_transaction(conn, t["id"], actor="human")
            print(f"   -> posted as {kind}/{category}")
        except LedgerError as e:
            print(f"   -> approved, but the ledger is unavailable ({e}); `sync` will retry")
    return counts(conn)


def chat_once(prompt, db_path, session=None):
    """One chat turn. Read-only tools only. Returns (answer, session_id)."""
    if session is None:
        sid = str(uuid.uuid4())
        data = llm.run_claude(prompt, "chat", db_path, llm.CHAT_PROMPT, session_id=sid)
    else:
        sid = session
        data = llm.run_claude(prompt, "chat", db_path, llm.CHAT_PROMPT, resume=sid)
    return str(data.get("result")).strip(), sid


def seed_categories(conn):
    """Create the default categories in Akaunting (it ships with only Deposit/Sales/Other)."""
    ledger = get_ledger(conn)
    if not isinstance(ledger, AkauntingLedger):
        print("[seed] FakeLedger already has the default categories; set AKAUNTING_URL to seed Akaunting")
        return
    created = ledger.ensure_categories(DEFAULT_CATEGORIES)
    print(f"[seed] created {len(created)} categories in Akaunting: {', '.join(created) or 'none (all present)'}")


def status(conn):
    c = counts(conn)
    ledger = get_ledger(conn)
    print(f"ledger: {ledger.name}")
    print("transactions by state:", c or "none")
    posted = conn.execute("SELECT idempotency_key FROM transactions WHERE state='posted'").fetchall()
    missing = [r["idempotency_key"] for r in posted if ledger.find_by_number(r["idempotency_key"]) is None]
    print(f"checks: posted transactions={len(posted)}, missing from ledger={len(missing)}")
