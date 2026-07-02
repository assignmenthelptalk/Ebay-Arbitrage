# Deployment status — eBay Arbitrage (auth + human-in-the-loop cutover)

Living handoff doc. If a Claude Code session loses context, read this first. Last known state
below; update it as phases complete.

## The big picture
Two copies of the app on the VPS (13.140.171.246):
- **`/root/arbitrage-api`** — the CURRENTLY LIVE service (port 8000, systemd `arbitrage-api.service`,
  `User=root`, `EnvironmentFile=/root/arbitrage-api/.env`). Runs the OLD, pre-auth code. NOT a git
  clone. **Leave intact as the rollback target** until the cutover is confirmed healthy.
- **`/home/jobizi/ebay-arbitrage`** — the git clone with the NEW code (commit `81b6b3d`). Verified,
  committed locally, **not yet pushed** to origin, **not yet live**.

The plan: repoint the systemd service from `/root/...` to the clone (Option A), migrating live
data. Currently mid-way, still **pre-production** — nothing has touched the live service.

## Done
- Auth: `auth.py` — API-key via `X-API-Key`, fails closed on empty `API_KEYS`, `compare_digest`.
- Human-in-the-loop gate: `fulfillment_gate.py` — pending_review → approve(ceiling) → bot claims →
  live-total-vs-ceiling check before Place Order. Hard caps `ABSOLUTE_MAX_ORDER` / `DAILY_SPEND_CAP`.
  Has unit tests.
- Approval surfaces: Telegram approver + dashboard panel (code in repo;
  `deploy/arbitrage-telegram-approver.service` exists, points at `/root` paths — needs repointing).
- Docs lockdown: `ENABLE_DOCS` gates /docs,/redoc,/openapi.
- Merge of upstream + local work resolved and verified (all router/auth/docs checks passed).
  Committed locally as `81b6b3d` (1 ahead of origin).
- Dead cron poller (`*/15 curl /orders/pending`) removed from root crontab (backup:
  `/root/root-crontab.bak-<date>`).
- venv created in the clone. `.env` key diff completed.

## In progress / next (resume here)
1. **[CRITICAL — do before cutover] Fix bot's missing API key.** `fulfillment_bot.py`'s
   `update_order_status()` PATCHes `/orders/{id}/status` with no `X-API-Key` → will 401 once
   `API_KEYS` is set, so purchases never get marked fulfilled and eBay tracking never uploads.
   Fix: add `BOT_API_KEY` env var, attach header to every bot→API call, warn if unset.
   (Prompt: FIX_BOT_API_KEY.md.) Then commit.
2. **Phase 1 — build clone `.env`.** Seed from old prod `.env` (carries eBay creds without printing).
   Decisions: `DRY_RUN=true` (explicit), drop `EBAY_FEE_PCT` (dead), `FULFILLMENT_QUEUE`=absolute
   clone path, `ENABLE_DOCS=false`, **no Telegram vars this round** (API-first). Fill `API_KEYS` and
   `BOT_API_KEY` yourself (BOT_API_KEY must be one of the values in API_KEYS). `chmod 600`.
3. **Phase 2 — migrate data.** `sudo cp` `arbitrage.db` + `amazon_session.json` from `/root` into
   clone. Do NOT migrate old `fulfillment_queue.json` (old format, dry-run test data) — init fresh
   `{"jobs": [], "spend": {}}`. Leave debug PNGs behind.
4. **Phase 3 — test boot.** Run uvicorn from clone on a spare port (e.g. 8130). Verify
   /fulfillment/pending → 401 no key / 200 with key / 403 wrong key; /docs → 404; DB reads work.
   Kill test server. Prod still untouched.
5. **Phase 4 — cutover ("cut over now").** Back up unit file + old `.env`. Edit unit:
   WorkingDirectory / ExecStart (clone's `.venv/bin/uvicorn`) / EnvironmentFile → clone. Keep
   `User=root` (lower-risk; ensure migrated files readable). `daemon-reload` + `restart`.
6. **Phase 5 — verify live + rollback ready.** status active (not restart-looping); live :8000 auth
   checks; one process on 8000; tail journal for 500-storm (= empty API_KEYS → roll back).
7. **Push** `81b6b3d` (+ bot fix commit) to origin when ready.
8. **Telegram approver** — separate step after API confirmed healthy: repoint its unit to clone
   paths, set TELEGRAM_* vars, install/enable.

## Rollback (keep ready during Phase 4/5)
Restore `~/arbitrage-api.service.bak` → `sudo systemctl daemon-reload` → `sudo systemctl restart
arbitrage-api.service`. Puts prod back on `/root/arbitrage-api` with its old `.env`. `/root` stays
intact until you explicitly decide otherwise.

## BEFORE flipping DRY_RUN=false (go-live checklist — real money)
None of these block the sandbox cutover; all matter before autonomous real purchasing.
- [ ] **Queue atomicity.** `_locked_queue()` resets `{"jobs":[], "spend":{}}` on any JSON parse
      failure and writes non-atomically. A torn write wipes `spend` → `DAILY_SPEND_CAP` forgets
      today's total, defeating the hard cap. Fix: write-temp-then-rename; don't zero `spend` on
      parse error.
- [ ] **TLS.** Extension talks to `http://13.140.171.246:8000` in cleartext — the `X-API-Key` that
      authorizes real purchases travels in the clear. Put Caddy/HTTPS in front; bind uvicorn to
      127.0.0.1 behind it.
- [ ] **Real cap values.** Set `ABSOLUTE_MAX_ORDER` / `DAILY_SPEND_CAP` to true order sizes (not the
      150/500 placeholders).
- [ ] **Live account readiness.** Real payment card on the Amazon account; known last blocker.
- [ ] **`EBAY_ENV` flip** sandbox → production (deliberate, separate).
- [ ] **`EBAY_RU_NAME`** set if the eBay OAuth token needs re-authorization.
- [ ] Lower-priority from review: `/dashboard` shell served without auth (shape leak);
      `ebay_client.py` token-refresh retry is dead code; `database.py` migration loop swallows all
      exceptions.

## Facts worth not re-discovering
- Old→new `.env`: 9 of 10 keys carry over; `EBAY_FEE_PCT` is dead (hardcoded 0.1325 in
  margin_engine.py — note fee assumption changed from .14 to .1325).
- `QUEUE_POLL_INTERVAL` (bot) and `POLL_INTERVAL` (telegram) are INDEPENDENT, not a rename — both
  needed if both run. Same for `MAX_PRICE_DRIFT_PCT` (bot % drift check) vs the gate's dollar caps.
- `BOT_API_KEY` must be a value that also appears in `API_KEYS`.
- Never paste `API_KEYS` / `BOT_API_KEY` values into a chat. Generate:
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. The extension's stored key must
  match one in `API_KEYS`.
