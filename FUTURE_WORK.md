# Future Work

- **Real bank providers.** Implement `BankProvider` for Plaid (US/CA) and GoCardless (UK/EU): token storage and renewal, per-account sync cursors, rate-limit backoff, and "needs re-auth" states. Also handle pending-to-posted matching, which a CSV statement doesn't exercise.
- **Reconciliation.** Compare ledger and bank balances, surface "unexplained difference" items, handle opening balances and closed periods. Akaunting has a reconciliation feature (`/reconciliations` API resource); decide whether to drive it or check balances independently.
- **Akaunting Bank Feeds interop.** Decide how the agent coexists with the paid Bank Feeds app and its "Unmatched Transactions" list.
- **Multi-currency and FX.** Rate source, rate date, decimal-safe rounding.
- **Split transactions** across categories, and linking payments to invoices and bills.
- **Anomaly and budget reporting.** Price changes, duplicate charges, category spikes.
- **Privacy.** Redact or tokenize data before any hosted LLM call, or use a local model.
- **Scheduling and ops.** Run locking, metrics, alerts when a sync silently returns zero rows.
- **Secrets management** for bank tokens and API credentials.
- **Rule promotion tuning.** Pick N and the evidence shown to the user when a rule is proposed.
- **Refunds.** Link a refund to its original purchase if Akaunting does not handle this itself.
- **Record and replay** of agent runs, so the demo works without a key.
- **Transfer detection.** Recognize the two legs of a transfer between own accounts, link them and keep them out of spend reports. Deliberately out of scope for v0.
- **Akaunting adapter at scale.** Chat reads every transaction and filters client-side. Use server-side `search` filters (`type:`, `category_id:`; date filtering needs verifying) and cache.
- **Akaunting multi-company and multi-currency.** The adapter assumes the default company and the account's currency with rate 1.
- **Akaunting extras.** Contacts (vendors), attaching the bank row as a document, and tagging agent-posted entries via `reference`.
