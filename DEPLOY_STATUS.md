# Deployment status — eBay Arbitrage (auth + human-in-the-loop cutover)

Living handoff doc. If a Claude Code session loses context, read this first. Last known state
below; update it as phases complete.

## The big picture
Two copies of the app on the VPS (13.140.171.246):
- **`/root/arbitrage-api`** — the OLD pre-auth code. NOT a git clone. As of Phase 4, no longer live —
  `arbitrage-api.service` now runs from the clone. **Retained as the rollback target** (see Rollback
  section) until explicitly retired.
- **`/home/jobizi/ebay-arbitrage`** — the git clone with the NEW code. **NOW LIVE as of Phase 4**
  (port 8000, systemd `arbitrage-api.service`, `User=root`,
  `EnvironmentFile=/home/jobizi/ebay-arbitrage/arbitrage-api/.env`). Committed locally, still **not
  pushed** to origin.

Phase 4 (cutover) is complete and verified. API-only — Telegram approver not yet repointed/started
(separate later step, item 8 below).

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
- Bot fix: `fulfillment_bot.py` now reads `BOT_API_KEY` from env, warns on startup if unset, sends
  it as `X-API-Key` on the `/orders/{id}/status` PATCH (the bot's only API call), and logs a clear
  `REJECTED (401/403)` message if the key is missing/wrong. `.env.example` and both affected
  `README.md` curl examples (status PATCH + cron) updated to match.

## In progress / next (resume here)
- **Phase 2 — DONE.** Migrated `arbitrage.db` (61440 bytes; 13 orders, 5 listings, 36 event_log rows,
  1 token) and `amazon_session.json` (5589 bytes) from `/root` via sudo cp, chowned to jobizi.
  Initialized fresh `fulfillment_queue.json` = `{"jobs": [], "spend": {}}` (did NOT copy old
  `/root` queue). No debug PNGs copied. All three confirmed jobizi-owned, non-zero size.
- **Phase 3 — VERIFIED.** Test-booted clone on `127.0.0.1:8130` (not `:8000`, not `0.0.0.0`), prod
  untouched throughout.
  - Boots clean, no import errors.
  - Auth triangle: no-key→401, key→200, bad-key→403 (all pass).
  - `/docs` → 404 (closed).
  - **DB_PATH resolves correctly to the migrated DB** — confirmed exactly one `arbitrage.db` on
    disk, at the `DB_PATH` location, with the migrated row counts intact; no stray/empty DB created
    in cwd or elsewhere.
  - `/fulfillment/pending` with key → `[]` (fresh queue read correctly).
  - Fulfillment router present and routed (`/fulfillment/pending`, `/fulfillment/approve` respond,
    not framework 404s).
  - Test server killed; confirmed nothing listening on 8130 afterward.
  - Note: first pass of this test failed because `API_KEYS`/`BOT_API_KEY` were blank in the clone's
    `.env` (Phase 1 gap, not a code bug — `auth.py` fail-closed as designed). User filled in real
    keys directly in the clone's `.env` (do NOT re-seed this file from prod's `.env` — that's what
    blanked them originally). Re-ran; all checks passed.
- **Phase 4 — DONE (2026-07-03).** Cut `arbitrage-api.service` over to the clone, API-only (no
  Telegram approver this round). Backups taken first: `~/arbitrage-api.service.bak-2026-07-03` and
  `/root/arbitrage-api/.env.bak-2026-07-03` (root-owned, verified present before any change).
  Unit diff reviewed and approved before applying — only `WorkingDirectory`, `ExecStart`,
  `EnvironmentFile` changed to clone paths; `User=root`, `Restart=always`, `RestartSec=5`,
  `[Install]` all left identical. Pre-flight re-confirmed `API_KEYS`/`BOT_API_KEY` non-empty and
  `DRY_RUN=true`/`EBAY_ENV=sandbox` unchanged immediately before restart. After
  `daemon-reload` + `restart`:
  - `systemctl status` → `active (running)`, not restart-looping, single `uvicorn` process on
    `:8000`, cwd/exec path confirmed under the clone.
  - Live auth triangle on `:8000`: no-key→401, wrong-key→403, real-key→200 (confirmed, key length
    43 — value not printed).
  - `/docs` → 404 (still closed).
  - No 500-storm, no "authentication is not configured" in status/behavior.
  - `/root/arbitrage-api` left in place, untouched, as the rollback target (rollback command is in
    the Rollback section below, using `~/arbitrage-api.service.bak-2026-07-03`).
  - Not pushed to origin. Telegram approver not started. `DRY_RUN`/`EBAY_ENV` not flipped.
1. **Repoint hardcoded `/root` paths — do this as part of Phase 4, not before.** Scanned the whole
   clone (`grep -rn "/root/arbitrage-api\|/root/" ...`). Make these env-driven (read from `.env`)
   rather than hardcoding the new clone path, so this doesn't recur on the next move.
   - **Runtime-critical (must repoint before/at cutover):**
     - `fulfillment_bot.py:32` `SESSION_FILE = "/root/arbitrage-api/amazon_session.json"` — bot's Amazon cookie jar
     - `fulfillment_bot.py:33` `DEBUG_DIR = "/root/arbitrage-api"` — debug screenshot dir on failure
     - `deploy/arbitrage-telegram-approver.service:8,9,12` (`WorkingDirectory`/`ExecStart`/`EnvironmentFile`) — repoint when the Telegram step (item 8 below) happens
   - **Script (not a running service) — will silently hit the old DB if run post-cutover:**
     - `insert_test_order.py:2` `sqlite3.connect('/root/arbitrage-api/arbitrage.db')`
   - **Docs/tooling only — mention `/root` accurately for the current live setup, no action needed:**
     - `arbitrage-api/README.md:269,352-356,373,388,415,422` (systemd template, queue-file note, cron example, Telegram env note)
     - `.claude/settings.local.json:5-6` — Claude Code's own local tool-permission allowlist, unrelated to app runtime
2. **Phase 1 — build clone `.env`.** Seed from old prod `.env` (carries eBay creds without printing).
   Decisions: `DRY_RUN=true` (explicit), drop `EBAY_FEE_PCT` (dead), `FULFILLMENT_QUEUE`=absolute
   clone path, `ENABLE_DOCS=false`, **no Telegram vars this round** (API-first). Fill `API_KEYS` and
   `BOT_API_KEY` yourself (BOT_API_KEY must be one of the values in API_KEYS). `chmod 600`.
3. **Phase 2 — migrate data.** `sudo cp` `arbitrage.db` + `amazon_session.json` from `/root` into
   clone. Do NOT migrate old `fulfillment_queue.json` (old format, dry-run test data) — init fresh
   `{"jobs": [], "spend": {}}`. Leave debug PNGs behind.
4. ~~**Phase 3 — test boot.**~~ DONE, see above.
5. ~~**Phase 4 — cutover.**~~ DONE, see above. Live verification (status active, auth checks, one
   process on 8000, no 500-storm) was folded into the Phase 4 cutover verification itself, so the
   separate "Phase 5" verify pass below is effectively already covered — nothing further needed
   there unless something regresses.
6. **Push** `81b6b3d` (+ bot fix commit, + Phase 2/3/4 doc commits) to origin when ready.
7. **Telegram approver** — separate step after API confirmed healthy: repoint its unit
   (`deploy/arbitrage-telegram-approver.service` — see item 1) to clone paths, set TELEGRAM_* vars,
   install/enable. **Not started this round (API-only cutover, by design).**

**Still open and now more time-sensitive since the API is live from the clone:** item 1's
runtime-critical repoints (`fulfillment_bot.py` `SESSION_FILE`/`DEBUG_DIR` hardcoded to
`/root/arbitrage-api`) were **not** touched by the Phase 4 cutover (that was unit-file only, as
scoped). If/when the fulfillment bot is run, it will still read/write `/root` paths, not the
clone — worth resolving before relying on the bot against the now-live clone-backed API.

## Rollback (keep ready)
Restore `~/arbitrage-api.service.bak-2026-07-03` → `sudo systemctl daemon-reload` → `sudo systemctl
restart arbitrage-api.service`. Puts the service back on `/root/arbitrage-api` with its old `.env`
(`/root/arbitrage-api/.env.bak-2026-07-03` also preserved separately). `/root/arbitrage-api` stays
intact and untouched until you explicitly decide otherwise.

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
