"""Tests for the deterministic parts (no LLM, no key needed)."""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from accounting_agent import db, ledger, llm, mcp_server, tools, workflow     # noqa: E402
from accounting_agent.providers import BankProvider, BankRow, CsvStatementProvider   # noqa: E402
from accounting_agent.ledger import FakeLedger         # noqa: E402

CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "statement.csv")


class Base(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k) for k in ledger.AKAUNTING_ENV if k in os.environ}   # hermetic: FakeLedger only
        self.dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.dir.name, "t.db"))
        db.ingest(self.conn, CsvStatementProvider(CSV))

    def tearDown(self):
        os.environ.update(self._env)
        self.conn.close()
        self.dir.cleanup()

    def tid(self, like):
        return self.conn.execute("SELECT id FROM transactions WHERE description LIKE ?", (f"%{like}%",)).fetchone()[0]


class TestProviders(Base):
    def test_csv_provider_returns_canonical_rows(self):
        rows = CsvStatementProvider(CSV).fetch()
        self.assertEqual(len(rows), 19)
        self.assertEqual(rows[0], BankRow("2026-10-01", "ACME CORP PAYROLL", 320000, "Checking"))

    def test_any_provider_plugs_in(self):
        class Fixed(BankProvider):
            name = "fixed"
            def fetch(self):
                return [BankRow("2026-11-01", "NEW SHOP", -1250, "Savings")]
        added, _ = db.ingest(self.conn, Fixed())
        self.assertEqual(added, 1)
        self.assertEqual(self.conn.execute("SELECT amount_minor FROM transactions WHERE description='NEW SHOP'")
                         .fetchone()[0], -1250)

    def test_base_provider_must_be_implemented(self):
        with self.assertRaises(NotImplementedError):
            BankProvider().fetch()


class TestSecrets(unittest.TestCase):
    def test_no_secret_on_the_claude_command_line(self):
        saved = dict(os.environ)
        try:
            os.environ.update(AKAUNTING_URL="http://x", AKAUNTING_USER="me@example.com",
                              AKAUNTING_PASSWORD="hunter2-very-secret", CLAUDE_KEY="tok-very-secret")
            cmd = llm.build_command("hi", "sync", "/tmp/x.db", "sys")
        finally:
            os.environ.clear()
            os.environ.update(saved)
        joined = " ".join(cmd)
        for secret in ("hunter2-very-secret", "tok-very-secret"):
            self.assertNotIn(secret, joined)
        self.assertIn("${AKAUNTING_PASSWORD}", joined)       # a reference, expanded by claude from its environment


class TestIngest(Base):
    def test_reload_adds_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        added, _ = db.ingest(self.conn, CsvStatementProvider(CSV))
        self.assertEqual(added, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], before)

    def test_identical_same_day_purchases_stay_distinct(self):
        n = self.conn.execute("SELECT COUNT(*) FROM transactions WHERE description LIKE '%BLUE BOTTLE%'").fetchone()[0]
        self.assertEqual(n, 2)

    def test_money_is_exact(self):
        self.assertEqual(db.to_minor("-1,234.50"), -123450)
        self.assertEqual(db.to_minor("0.10") + db.to_minor("0.20"), 30)


class TestGate(Base):
    def test_rejects_unknown_category_and_wrong_sign(self):
        t = self.tid("NETFLIX")
        with self.assertRaises(tools.ToolError):
            tools.propose_category(self.conn, t, "expense", "Made Up", 0.99)
        with self.assertRaises(tools.ToolError):
            tools.propose_category(self.conn, t, "income", "Salary", 0.99)      # negative amount
        with self.assertRaises(tools.ToolError):
            tools.propose_category(self.conn, t, "expense", "Salary", 0.99)     # type mismatch

    def test_confident_small_expense_is_posted(self):
        t = self.tid("NETFLIX")
        out = tools.propose_category(self.conn, t, "expense", "Subscriptions", 0.95)
        self.assertEqual(out["status"], "posted")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0], 1)

    def test_low_confidence_and_large_go_to_review(self):
        self.assertEqual(tools.propose_category(self.conn, self.tid("AMZN"), "expense", "Shopping", 0.5)["status"],
                         "needs_review")
        self.assertEqual(tools.propose_category(self.conn, self.tid("DELTA"), "expense", "Transport", 0.99)["status"],
                         "needs_review")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0], 0)

    def test_review_reason_is_stored_and_readable(self):
        tools.propose_category(self.conn, self.tid("DELTA"), "expense", "Transport", 0.99)
        tools.propose_category(self.conn, self.tid("AMZN"), "expense", "Shopping", 0.5)
        tools.flag_for_review(self.conn, self.tid("PAYPAL"), "who is J Smith?")
        why = {r["id"]: r["review_reason"] for r in self.conn.execute("SELECT id, review_reason FROM transactions")}
        self.assertIn("640.00 is at or above the 500.00", why[self.tid("DELTA")])
        self.assertIn("confidence 0.50 is below", why[self.tid("AMZN")])
        self.assertIn("who is J Smith?", why[self.tid("PAYPAL")])

    def test_old_database_gets_the_new_column(self):
        path = os.path.join(self.dir.name, "old.db")
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, fingerprint TEXT, date TEXT, description TEXT,"
                  " amount_minor INTEGER, account TEXT, state TEXT, kind TEXT, category TEXT, confidence REAL,"
                  " rationale TEXT, idempotency_key TEXT, ledger_ref INTEGER, attempts INTEGER, last_error TEXT)")
        c.commit(); c.close()
        cols = {r[1] for r in db.connect(path).execute("PRAGMA table_info(transactions)")}
        self.assertIn("review_reason", cols)

    def test_transfer_is_not_a_kind(self):
        with self.assertRaises(tools.ToolError):
            tools.propose_category(self.conn, self.tid("NETFLIX"), "transfer", "Transfer", 0.99)

    def test_cannot_propose_twice(self):
        t = self.tid("NETFLIX")
        tools.propose_category(self.conn, t, "expense", "Subscriptions", 0.95)
        with self.assertRaises(tools.ToolError):
            tools.propose_category(self.conn, t, "expense", "Shopping", 0.95)

    def test_post_is_idempotent_and_recovers(self):
        t = self.tid("NETFLIX")
        tools.propose_category(self.conn, t, "expense", "Subscriptions", 0.95)
        # simulate a crash after the ledger write but before the state update
        self.conn.execute("UPDATE transactions SET state='approved', ledger_ref=NULL WHERE id=?", (t,))
        self.conn.commit()
        tools.post_transaction(self.conn, t)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT state FROM transactions WHERE id=?", (t,)).fetchone()[0], "posted")


class TestReview(Base):
    def _queue(self):
        tools.propose_category(self.conn, self.tid("AMZN"), "expense", "Shopping", 0.5)      # has a suggestion
        tools.flag_for_review(self.conn, self.tid("PAYPAL"), "who is J Smith?")               # has none

    def _run(self, answers):
        it = iter(answers)
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            workflow.review(self.conn, input_fn=lambda _p: next(it))
        return buf.getvalue()

    def _row(self, like):
        return self.conn.execute("SELECT * FROM transactions WHERE description LIKE ?", (f"%{like}%",)).fetchone()

    def test_screen_shows_suggestion_and_numbered_choices(self):
        self._queue()
        out = self._run(["", "s"])
        self.assertIn("Agent suggests: Shopping (expense), confidence 0.50", out)
        self.assertIn("1) Dining", out)
        self.assertIn("<- agent's pick", out)
        self.assertIn("Agent made no suggestion.", out)
        self.assertNotIn("Salary", out)                      # money out: income categories are not offered

    def test_enter_accepts_number_and_name_override(self):
        self._queue()
        self._run(["", "2"])                                 # accept Shopping; pick option 2 for PayPal
        self.assertEqual(self._row("AMZN")["category"], "Shopping")
        self.assertEqual(self._row("AMZN")["state"], "posted")
        self.assertEqual(self._row("PAYPAL")["category"], sorted(
            n for n, t in ledger.DEFAULT_CATEGORIES.items() if t == "expense")[1])    # option 2 = Groceries

    def test_override_by_name_and_wrong_type_is_rejected(self):
        self._queue()
        out = self._run(["salary", "dining", "health"])      # Salary refused for an expense, then Dining; then PayPal
        self.assertIn("Salary is an income category, but this is an expense", out)
        self.assertEqual(self._row("AMZN")["category"], "Dining")
        self.assertEqual(self._row("PAYPAL")["category"], "Health")

    def test_enter_without_suggestion_asks_again_and_bad_input_reprompts(self):
        self._queue()
        out = self._run(["s", "", "99", "nonsense", "s"])
        self.assertIn("There is no suggestion to accept", out)
        self.assertIn("'99' is not in the list", out)
        self.assertIn("'nonsense' is not in the list", out)
        self.assertEqual(self._row("AMZN")["state"], "skipped")
        self.assertEqual(self._row("PAYPAL")["state"], "skipped")

    def test_ending_input_leaves_items_queued(self):
        self._queue()
        import contextlib
        def eof(_p):
            raise EOFError
        with contextlib.redirect_stdout(io.StringIO()):
            workflow.review(self.conn, input_fn=eof)
        self.assertEqual(self._row("AMZN")["state"], "needs_review")


class TestModes(Base):
    def test_chat_mode_has_no_write_tools(self):
        names = set(tools.tools_for("chat"))
        self.assertTrue({"spend_report", "search_ledger"} <= names)
        self.assertFalse({"propose_category", "flag_for_review", "list_pending_transactions"} & names)
        with self.assertRaises(tools.ToolError):
            tools.call_tool(self.conn, "chat", "propose_category", {"txn_id": 1})

    def test_spend_report_totals(self):
        for like, cat in (("NETFLIX", "Subscriptions"), ("CHIPOTLE", "Dining")):
            tools.propose_category(self.conn, self.tid(like), "expense", cat, 0.95)
        rep = FakeLedger(self.conn).spend_report()
        self.assertEqual(rep["total_spent"], "28.94")   # 15.49 + 13.45, exact
        self.assertIn("Dining", rep["expenses_by_category"])


class TestMCP(Base):
    def test_protocol_roundtrip(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "list_categories", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "propose_category", "arguments": {"txn_id": 1, "kind": "expense", "category": "Nope", "confidence": 1}}},
        ]
        out = io.StringIO()
        mcp_server.serve(self.conn, "sync", io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out)
        resp = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in resp], [1, 2, 3, 4])          # notification got no reply
        self.assertEqual(resp[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("propose_category", [t["name"] for t in resp[1]["result"]["tools"]])
        self.assertFalse(resp[2]["result"]["isError"])
        self.assertTrue(resp[3]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
