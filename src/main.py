"""Accounting agent CLI.

  python src/main.py init                  load the sample bank statement
  python src/main.py sync                  agent categorizes new transactions
  python src/main.py review [--accept-all] human approves what the agent was unsure about
  python src/main.py chat "question"       ask about your spending (read-only); no question = interactive
  python src/main.py seed-categories       create default categories in Akaunting (optional)
  python src/main.py status                state counts and consistency checks

Ledger: FakeLedger (SQLite) by default. To use a running Akaunting, export AKAUNTING_URL,
AKAUNTING_USER and AKAUNTING_PASSWORD (the user needs the `read-api` permission).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from accounting_agent import db, workflow          # noqa: E402
from accounting_agent.ledger import LedgerError      # noqa: E402
from accounting_agent.llm import LLMError          # noqa: E402
from accounting_agent.providers import CsvStatementProvider   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    p = argparse.ArgumentParser(description="Local accounting agent (Claude via `claude -p`).")
    p.add_argument("--db", default=os.environ.get("ACCT_DB", os.path.join(ROOT, "accounting.db")))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="load a bank statement CSV")
    s.add_argument("--csv", default=os.path.join(ROOT, "data", "statement.csv"))
    s.add_argument("--reset", action="store_true", help="delete the database first")
    sub.add_parser("sync", help="run the agent over new transactions")
    s = sub.add_parser("review", help="human review of flagged transactions")
    s.add_argument("--accept-all", action="store_true", help="non-interactive: accept the agent's suggestions")
    s = sub.add_parser("chat", help="ask questions about your spending")
    s.add_argument("question", nargs="?")
    sub.add_parser("seed-categories", help="create the default categories in Akaunting (needs AKAUNTING_URL)")
    sub.add_parser("status", help="show state and checks")
    a = p.parse_args(argv)

    if a.cmd == "init" and a.reset and os.path.exists(a.db):
        os.remove(a.db)
    conn = db.connect(a.db)
    try:
        if a.cmd == "init":
            added, skipped = db.ingest(conn, CsvStatementProvider(a.csv))
            print(f"[init] {added} new, {skipped} already present")
        elif a.cmd == "sync":
            print("[sync] state:", workflow.sync(conn, a.db))
        elif a.cmd == "review":
            print("\n[review] state:", workflow.review(conn, accept_all=a.accept_all))
        elif a.cmd == "chat":
            if a.question:
                print(workflow.chat_once(a.question, a.db)[0])
            else:
                sid = None
                while True:
                    try:
                        q = input("you> ").strip()
                    except EOFError:
                        break
                    if q in ("", "exit", "quit"):
                        break
                    ans, sid = workflow.chat_once(q, a.db, sid)
                    print(ans)
        elif a.cmd == "seed-categories":
            workflow.seed_categories(conn)
        elif a.cmd == "status":
            workflow.status(conn)
    except (LLMError, LedgerError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
