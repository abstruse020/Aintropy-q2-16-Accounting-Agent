"""Tests for the deterministic parts (no LLM, no key needed)."""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from accounting_agent import db, ledger, mcp_server, tools     # noqa: E402
from accounting_agent.ledger import FakeLedger         # noqa: E402

CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "statement.csv")


class Base(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k) for k in ledger.AKAUNTING_ENV if k in os.environ}   # hermetic: FakeLedger only
        self.dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.dir.name, "t.db"))
        db.ingest_csv(self.conn, CSV)

    def tearDown(self):
        os.environ.update(self._env)
        self.conn.close()
        self.dir.cleanup()

    def tid(self, like):
        return self.conn.execute("SELECT id FROM transactions WHERE description LIKE ?", (f"%{like}%",)).fetchone()[0]


class TestIngest(Base):
    def test_reload_adds_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        added, _ = db.ingest_csv(self.conn, CSV)
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
