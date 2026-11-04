# Running the agent against a real Akaunting (optional)

The default run uses FakeLedger and needs none of this. Use this only to see the agent post into a real Akaunting.
The credentials in `env/` are dev-only values for a local container; do not reuse them anywhere else.

```bash
cd akaunting-dev
AKAUNTING_SETUP=true docker compose up -d      # first start only; installs the company and admin user
# wait ~30 s, then (fresh installs only, otherwise payment methods are rejected as invalid):
docker exec akaunting php artisan cache:clear

cd ..
export AKAUNTING_URL=http://localhost:8080
export AKAUNTING_USER=admin@example.com
export AKAUNTING_PASSWORD=dev-password-123

python src/main.py seed-categories             # Akaunting ships with only Deposit/Sales/Other
python src/main.py init --reset && python src/main.py sync
python src/main.py review --accept-all
python src/main.py chat "How much did I spend in October 2026?"
python src/main.py status                      # confirms every posted transaction exists in Akaunting
```

UI: http://localhost:8080 (same login). Stop with `docker compose down`; add `-v` to wipe all data.
Later starts: `docker compose up -d` (never pass `AKAUNTING_SETUP` again).
