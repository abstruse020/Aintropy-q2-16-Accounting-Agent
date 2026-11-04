"""AkauntingLedger tests against a tiny in-process fake of the Akaunting REST API.

The fake mimics behavior observed on a real Akaunting 3.x instance (see
INTERNAL_NOTES.md): Basic auth, {data, meta} pagination, HTTP 422 on a duplicate
`number`, categories/accounts created through the API. Runs offline.
"""
import base64
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from accounting_agent import db, tools                                               # noqa: E402
from accounting_agent.providers import CsvStatementProvider                         # noqa: E402
from accounting_agent.ledger import AkauntingLedger, DuplicateNumber, LedgerError    # noqa: E402

AUTH = "Basic " + base64.b64encode(b"u@example.com:pw").decode()


class FakeAkaunting(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method):
        s = self.state
        if self.headers.get("Authorization") != AUTH:
            return self._send(401, {"message": "Unauthenticated."})
        u = urlparse(self.path)
        res = u.path.split("/api/")[1]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if method == "GET":
            rows = s[res]
            if res == "transactions" and q.get("search", "").startswith("number:"):
                rows = [t for t in rows if t["number"] == q["search"][7:]]
            if res == "settings":
                rows = [{"key": "offline-payments.methods",
                         "value": json.dumps([{"code": "offline-payments.cash.1"}, {"code": "offline-payments.bank_transfer.2"}])}]
            limit, page = int(q.get("limit", 15)), int(q.get("page", 1))
            last = max(1, -(-len(rows) // limit))
            return self._send(200, {"data": rows[(page - 1) * limit:page * limit], "meta": {"last_page": last}})
        # POST
        if res == "transactions":
            if any(t["number"] == body["number"] for t in s["transactions"]):
                return self._send(422, {"message": "taken", "errors": {"number": ["taken"]}})
            if body["payment_method"] not in ("offline-payments.cash.1", "offline-payments.bank_transfer.2"):
                return self._send(422, {"message": "bad", "errors": {"payment_method": ["invalid"]}})
            s["posts"].append(body)
        row = dict(body, id=len(s[res]) + 1, paid_at=body.get("paid_at", "")[:10] + "T10:00:00+00:00", enabled=True)
        s[res].append(row)
        self._send(201, {"data": row})

    do_GET = lambda self: self._route("GET")      # noqa: E731
    do_POST = lambda self: self._route("POST")    # noqa: E731


class TestAkauntingLedger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeAkaunting)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeAkaunting.state = {
            "categories": [{"id": 1, "name": "Transfer", "type": "other", "enabled": True},
                           {"id": 2, "name": "Other", "type": "expense", "enabled": True},
                           {"id": 3, "name": "Deposit", "type": "income", "enabled": True}],
            "accounts": [{"id": 1, "name": "Cash", "currency_code": "USD"}],
            "transactions": [], "settings": [], "posts": []}
        self.ak = AkauntingLedger(self.url, "u@example.com", "pw")

    def test_categories_only_income_expense(self):
        self.assertEqual(self.ak.categories(), {"Other": "expense", "Deposit": "income"})

    def test_seed_creates_only_missing(self):
        self.assertEqual(self.ak.ensure_categories({"Other": "expense", "Dining": "expense"}), ["Dining"])
        self.assertIn("Dining", self.ak.categories())

    def test_post_maps_fields_and_creates_account(self):
        self.ak.ensure_categories({"Dining": "expense"})
        ref = self.ak.post("n-1", "2026-10-03", "Checking", -575, "expense", "Dining", "BLUE BOTTLE")
        sent = FakeAkaunting.state["posts"][0]
        self.assertEqual((sent["type"], sent["amount"], sent["number"], sent["paid_at"]),
                         ("expense", "5.75", "n-1", "2026-10-03 00:00:00"))     # amount positive, exact
        self.assertEqual(sent["payment_method"], "offline-payments.bank_transfer.2")
        self.assertEqual(ref, self.ak.find_by_number("n-1"))
        self.assertIn("checking", [a["name"].lower() for a in FakeAkaunting.state["accounts"]])

    def test_duplicate_number_and_unknown_category(self):
        self.ak.post("n-1", "2026-10-03", "Cash", -100, "expense", "Other", "x")
        with self.assertRaises(DuplicateNumber):
            self.ak.post("n-1", "2026-10-03", "Cash", -100, "expense", "Other", "x")
        with self.assertRaises(ValueError):
            self.ak.post("n-2", "2026-10-03", "Cash", -100, "expense", "Nope", "x")
        with self.assertRaises(ValueError):                      # right name, wrong type
            self.ak.post("n-3", "2026-10-03", "Cash", 100, "income", "Other", "x")

    def test_search_and_report_use_signed_exact_amounts(self):
        self.ak.post("a", "2026-10-02", "Cash", -8642, "expense", "Other", "WHOLEFDS")
        self.ak.post("b", "2026-10-20", "Cash", 320010, "income", "Deposit", "PAYROLL")
        self.assertEqual(self.ak.search(query="whole")[0]["amount"], "-86.42")
        rep = self.ak.spend_report()
        self.assertEqual((rep["total_spent"], rep["total_income"], rep["net"]), ("86.42", "3200.10", "3113.68"))
        self.assertEqual(self.ak.spend_report(start="2026-10-10")["total_spent"], "0.00")

    def test_pagination_reads_every_page(self):
        for i in range(120):
            self.ak.post(f"n-{i}", "2026-10-01", "Cash", -100, "expense", "Other", "x")
        self.assertEqual(self.ak.spend_report()["expenses_by_category"]["Other"]["count"], 120)

    def test_bad_credentials_and_unreachable(self):
        with self.assertRaises(LedgerError):
            AkauntingLedger(self.url, "u@example.com", "wrong").categories()
        with self.assertRaises(LedgerError):
            AkauntingLedger("http://127.0.0.1:9", "u", "p", timeout=2).categories()


class TestPipelineWithAkaunting(unittest.TestCase):
    """The gate and the retry path, running on top of the Akaunting adapter."""

    @classmethod
    def setUpClass(cls):
        TestAkauntingLedger.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        TestAkauntingLedger.setUp(self)
        self._env = dict(os.environ)
        os.environ.update(AKAUNTING_URL=self.url, AKAUNTING_USER="u@example.com", AKAUNTING_PASSWORD="pw")
        from tempfile import mkdtemp
        self.conn = db.connect(os.path.join(mkdtemp(), "t.db"))
        csv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "statement.csv")
        db.ingest(self.conn, CsvStatementProvider(csv_path))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_categories_come_from_akaunting_not_defaults(self):
        t = self.conn.execute("SELECT id FROM transactions WHERE description LIKE '%NETFLIX%'").fetchone()[0]
        with self.assertRaises(tools.ToolError):                 # 'Subscriptions' is not in this Akaunting
            tools.propose_category(self.conn, t, "expense", "Subscriptions", 0.99)
        out = tools.propose_category(self.conn, t, "expense", "Other", 0.99)
        self.assertEqual(out["status"], "posted")
        self.assertEqual(len(FakeAkaunting.state["transactions"]), 1)

    def test_ledger_down_leaves_approved_then_retry_posts_once(self):
        t = self.conn.execute("SELECT id FROM transactions WHERE description LIKE '%NETFLIX%'").fetchone()[0]
        tools.list_categories(self.conn)                          # warm the cache while the server is up
        os.environ["AKAUNTING_URL"] = "http://127.0.0.1:9"       # new key -> new adapter instance, unreachable
        with self.assertRaises(tools.ToolError):                  # the real path: MCP server -> call_tool
            tools.call_tool(self.conn, "sync", "propose_category",
                            {"txn_id": t, "kind": "expense", "category": "Other", "confidence": 0.99})
        self.assertEqual(self.conn.execute("SELECT state FROM transactions WHERE id=?", (t,)).fetchone()[0], "new")
        os.environ["AKAUNTING_URL"] = self.url
        tools.propose_category(self.conn, t, "expense", "Other", 0.99)
        self.conn.execute("UPDATE transactions SET state='approved', ledger_ref=NULL WHERE id=?", (t,))
        self.conn.commit()
        tools.post_transaction(self.conn, t)                      # duplicate number -> recovered, not re-posted
        self.assertEqual(len(FakeAkaunting.state["transactions"]), 1)


if __name__ == "__main__":
    unittest.main()
