"""Where bank transactions come from.

A provider returns canonical rows. Everything after this point (fingerprints, the
agent, the ledger) only ever sees BankRow, never the source format.

What this covers: an exported statement file (CsvStatementProvider).
What it does not cover: a live bank connection through a paid aggregator such as
Plaid or GoCardless. Those would implement fetch() too, but they also need things
this interface has no place for: token storage and renewal, per-account sync
cursors, pending-to-posted matching, and the bank's own transaction ids.
Adding one means extending this interface, not just writing another class.
"""
import csv
from typing import NamedTuple

from .db import to_minor


class BankRow(NamedTuple):
    date: str            # YYYY-MM-DD
    description: str     # as the bank wrote it
    amount_minor: int    # signed cents: negative = money out
    account: str         # e.g. "Checking"


class BankProvider:
    name = "base"

    @property
    def source(self):
        """Human-readable origin, recorded in the audit log."""
        return self.name

    def fetch(self):
        """Return a list of BankRow. Must be repeatable: the same data twice gives the same rows."""
        raise NotImplementedError


class CsvStatementProvider(BankProvider):
    """An exported statement with the columns: date, description, amount, account."""
    name = "csv"

    def __init__(self, path):
        self.path = str(path)

    @property
    def source(self):
        return f"csv:{self.path}"

    def fetch(self):
        with open(self.path, newline="") as f:
            return [BankRow(r["date"].strip(), r["description"].strip(), to_minor(r["amount"]), r["account"].strip())
                    for r in csv.DictReader(f)]
