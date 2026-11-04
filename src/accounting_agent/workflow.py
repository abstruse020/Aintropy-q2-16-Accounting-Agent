"""Workflow manager: sync (agent proposes), review (human decides), chat (read-only Q&A)."""
import uuid

from . import db, llm
from .db import fmt
from .ledger import AkauntingLedger, DEFAULT_CATEGORIES, LedgerError, get_ledger
from .tools import hold_reasons, post_transaction

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


def _describe(t):
    """The header of one review item: what the transaction is and why it is here."""
    print(f"\n#{t['id']}  {t['date']}  {t['account']}  {fmt(t['amount_minor'])}  {t['description']}")
    why = t["review_reason"] or "; ".join(hold_reasons(t["amount_minor"], t["confidence"])) or "not recorded"
    print(f"   Held because: {why}")
    if t["category"]:
        extra = f" - {t['rationale']}" if t["rationale"] else ""
        print(f"   Agent suggests: {t['category']} ({t['kind']}), confidence {t['confidence']:.2f}{extra}")
    else:
        print("   Agent made no suggestion.")


def _choose(t, cats, needed, options, input_fn):
    """Ask the human. Returns a category name, 'skip', or None if input ended."""
    print(f"   Pick a category ({needed}):")
    for i, name in enumerate(options, 1):
        print(f"     {i:>2}) {name}" + ("   <- agent's pick" if name == t["category"] else ""))
    accept = f'[Enter]=accept "{t["category"]}", ' if t["category"] else ""
    while True:
        try:
            choice = input_fn(f"   {accept}number or name=choose another, s=skip > ").strip()
        except EOFError:
            return None
        if choice.lower() == "s":
            return "skip"
        if choice == "":
            if t["category"] in options:
                return t["category"]
            print("   There is no suggestion to accept. Pick a category, or s to skip.")
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        match = next((c for c in cats if c.lower() == choice.lower()), None)
        if match is None:
            print(f"   '{choice}' is not in the list. Type a number from 1 to {len(options)} or a name.")
        elif cats[match] != needed:
            print(f"   {match} is an {cats[match]} category, but this is an {needed}. Pick from the list.")
        else:
            return match


def review(conn, accept_all=False, input_fn=input):
    """Human step. Shows the agent's suggestion and the valid categories; the human decides."""
    rows = conn.execute("SELECT * FROM transactions WHERE state='needs_review' ORDER BY date, id").fetchall()
    if not rows:
        print("[review] nothing to review")
    cats = get_ledger(conn).categories()
    for t in rows:
        _describe(t)
        needed = "expense" if t["amount_minor"] < 0 else "income"     # money out vs money in
        options = sorted(n for n, typ in cats.items() if typ == needed)
        if accept_all:
            if not t["category"]:
                print("   -> left in queue (no suggestion to accept)")
                continue
            category = t["category"]
        else:
            category = _choose(t, cats, needed, options, input_fn)
            if category is None:
                print("\n[review] input ended; remaining items stay in the queue")
                break
        if category == "skip":
            conn.execute("UPDATE transactions SET state='skipped' WHERE id=?", (t["id"],))
            conn.commit()
            db.audit(conn, "human", "skip", {"txn_id": t["id"]})
            print("   -> skipped")
            continue
        kind = cats[category]
        conn.execute("UPDATE transactions SET state='approved', kind=?, category=? WHERE id=?",
                     (kind, category, t["id"]))
        conn.commit()
        db.audit(conn, "human", "approve", {"txn_id": t["id"], "category": category, "kind": kind,
                                            "agent_suggested": t["category"]})
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
