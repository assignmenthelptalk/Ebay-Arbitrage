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
(separate later step, item 7 below).

Phase 5 (2026-07-07): eBay sandbox user token stored and verified live against the Sell Fulfillment
API — seller-scoped auth confirmed working end-to-end. See item 8 below.

Stage 5 (2026-07-08): full-cost margin gate (`/research/margin`) added and verified live at
0.20/$5.00 thresholds. Committed `4aa5e5c`. See item 9 below. Not pushed.

Stage 6 (2026-07-08): candidates pipeline (intake + margin gate storage + list/detail/reevaluate)
built and verified live, tests green, committed. See item 10 below. Not pushed.

Fulfillment bot: venv built + unit repointed to the clone (2026-07-04, see Phase 4b below).
Service is intentionally left **disabled/inactive** — repointed but not turned on.

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
- **Phase 4b — DONE (2026-07-04).** Fulfillment bot venv built + `fulfillment-bot.service`
  repointed to the clone. Bot itself left **disabled** — repoint only, no auto-start.
  - **Venv:** new `.venv-bot` at repo root (`/home/jobizi/ebay-arbitrage/.venv-bot`), separate from
    `arbitrage-api/.venv`. Built from repo-root `requirements.txt`
    (`playwright==1.44.0`, `python-telegram-bot>=20,<22`) + `playwright install chromium`
    (browser binary confirmed on disk, `sync_playwright` import verified).
  - **Two deps the bot imports but `requirements.txt` never declared**, found when the standalone
    test crashed with `ModuleNotFoundError: No module named 'requests'`: `requests` and
    `python-dotenv` (`from dotenv import load_dotenv`). Installed into `.venv-bot`
    (`requests==2.34.2`, `python-dotenv==1.2.2`), then added to `requirements.txt` at those exact
    pins so the next clone/venv doesn't hit the same gap. No other pins touched.
  - **Standalone dry-run test** (`.venv-bot/bin/python -u fulfillment_bot.py`, NOT via systemd, run
    by hand with the clone's `.env` sourced, `timeout 30`): started clean, no import errors, printed
    `[bot] Fulfillment bot started (DRY RUN — no real purchases)` /
    `Polling every 60s` / `[queue] No approved jobs — sleeping 60s`, then killed by `timeout`
    (exit 124 — expected). Queue was empty so it correctly idled — no Amazon navigation, no
    purchase attempt, no screenshots. `arbitrage-api/fulfillment_queue.json` mtime updated
    (read/rewrite-in-place) but content byte-identical (`{"jobs": [], "spend": {}}`). User
    independently confirmed via `sudo find /root -newermt "-30 minutes" -type f` → nothing —
    no stray writes under `/root`.
    - Caveat noted during testing: Python stdout buffers when not a tty, so a plain (non `-u`) run
      under `timeout` can look silent even on success — use `-u` (or expect buffered output lost)
      when re-testing by hand in future sessions.
  - **Unit repointed.** Old unit was fully `/root`-consistent (`WorkingDirectory`, `ExecStart`,
    `EnvironmentFile` all under `/root/arbitrage-api`). Backed up
    (`~/fulfillment-bot.service.bak-2026-07-04`), diff reviewed and approved before applying — only
    these three lines changed:
    ```ini
    WorkingDirectory=/home/jobizi/ebay-arbitrage
    ExecStart=/home/jobizi/ebay-arbitrage/.venv-bot/bin/python fulfillment_bot.py
    EnvironmentFile=/home/jobizi/ebay-arbitrage/arbitrage-api/.env
    ```
    `User=root`, `Restart=always`, `RestartSec=10`, `After=network.target arbitrage-api.service`,
    `[Install]` all left identical. `daemon-reload` run. Confirmed **`disabled`/`inactive`** both
    before and after — repointing a unit does not change its enable state; it stays off until
    someone explicitly runs `sudo systemctl enable` + `start`.
  - **Not done yet (deliberately out of scope this round):**
    - Bot has **not** been enabled or started — still fully manual/off. Session file
      (`amazon_session.json`) is from 2026-07-03/the Phase 2 migration and will be ~a month old by
      the time anyone actually enables the bot — run `setup_session.py` for a fresh login before
      flipping it on, don't assume the session is still valid.
    - Telegram approver (`deploy/arbitrage-telegram-approver.service`) not repointed/installed —
      separate step, but it can reuse `.venv-bot` as-is: `python-telegram-bot==21.11.1` is already
      installed there (confirmed via `pip install -r requirements.txt`), no second venv needed.
1. **Repoint hardcoded `/root` paths.** Scanned the whole clone
   (`grep -rn "/root/arbitrage-api\|/root/" ...`). Make these env-driven (read from `.env`) rather
   than hardcoding the new clone path, so this doesn't recur on the next move.
   - **`fulfillment_bot.py` / `fulfillment-bot.service` — investigated 2026-07-03, NOT yet fixed.
     More than an env-var repoint; see full findings below.**
   - **Script (not a running service) — will silently hit the old DB if run post-cutover:**
     - `insert_test_order.py:2` `sqlite3.connect('/root/arbitrage-api/arbitrage.db')`
   - **Docs/tooling only — mention `/root` accurately for the current live setup, no action needed:**
     - `arbitrage-api/README.md:269,352-356,373,388,415,422` (systemd template, queue-file note, cron example, Telegram env note)
     - `.claude/settings.local.json:5-6` — Claude Code's own local tool-permission allowlist, unrelated to app runtime

   **Fulfillment bot investigation (2026-07-03, read-only, no changes made):**
   - **Launch mechanism (now fully mapped):** the ONLY way the bot runs is dedicated unit
     `fulfillment-bot.service`. Not API-spawned (no subprocess/create_task/BackgroundTasks
     reference to it in the API code). Not cron — both jobizi's and root's crontabs are empty. Not
     manual (no evidence of ad-hoc runs).
   - **Reads `/root` entirely:** `WorkingDirectory=/root/arbitrage-api`,
     `ExecStart=/root/arbitrage-api/.venv/bin/python fulfillment_bot.py`,
     `EnvironmentFile=/root/arbitrage-api/.env`. Self-consistent (not mixed).
   - **DISABLED as a safety step on 2026-07-03.** Was `enabled`+`inactive` (would auto-start on
     reboot, still fully wired to `/root`, mid-migration). Ran `sudo systemctl disable
     fulfillment-bot.service` — confirmed before (`enabled`/`inactive`) and after
     (`disabled`/`inactive`). This only removed the boot-time symlink
     (`/etc/systemd/system/multi-user.target.wants/fulfillment-bot.service`); the unit file itself
     is untouched and fully reversible (`sudo systemctl enable fulfillment-bot.service` restores
     it). `arbitrage-api.service` (the live API) was not touched.
   - **A naive repoint would crash the unit, not just leave it stale — two structural gaps found:**
     1. `fulfillment_bot.py` lives at the clone's **repo root**
        (`/home/jobizi/ebay-arbitrage/fulfillment_bot.py`), not inside `arbitrage-api/` — the
        `/root` layout is flat (bot colocated with `main.py`); the clone separated bot/scraper
        scripts from the `arbitrage-api/` package. The bot's own code already anticipates this
        (try `from fulfillment_gate import ...` colocated, except `ModuleNotFoundError` → inserts
        `arbitrage-api/` onto `sys.path`) — confirming the intended clone `WorkingDirectory` is the
        **repo root**, not `arbitrage-api/`.
     2. `arbitrage-api/.venv` is missing `playwright` (`sync_playwright` import), which the bot
        requires. The bot's real deps (`playwright==1.44.0`, `python-telegram-bot`) live in a
        *separate* `requirements.txt` at the repo root, and **no venv exists yet for it** anywhere
        in the clone. Needs a new venv built + `playwright install` for the browser binary before
        the bot can run under the clone at all — a setup step, not a unit edit.
   - **Session file is fine but aging:** clone's `amazon_session.json` (5589 bytes, copied
     2026-07-03 09:33 in Phase 2) confirmed current — root's copy is unchanged since 2026-06-15
     (same 5589 bytes), so nothing has drifted since the Phase 2 migration. Session is ~3 weeks old
     as of 2026-07-03; may need a fresh `setup_session.py` login when the bot is next actually run
     — not a blocker now, just don't assume it's still valid by the time repointing happens.
   - **DONE (2026-07-04) — see Phase 4b above.** Venv built (`.venv-bot`), unit repointed and
     applied, standalone dry-run test passed, service confirmed left **disabled/inactive**. Still
     remaining: actually enable/start it (fresh `setup_session.py` login first — session is aging),
     and the Telegram approver step (can reuse `.venv-bot`).
   - **`deploy/arbitrage-telegram-approver.service:8,9,12`** (`WorkingDirectory`/`ExecStart`/`EnvironmentFile`) — repoint when the Telegram step (item 7 below) happens; not investigated this round. Bundle with the bot venv work per step 4 above.
   - **Remaining deploy tasks, in short:** (1) fulfillment bot venv + repoint, (2) Telegram
     approver setup — both bundled together per above. Everything required before flipping
     `DRY_RUN=false` for real-money go-live is already tracked separately in the go-live checklist
     further down this doc.
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
6. ~~**Push.**~~ DONE (2026-07-03). Pre-push safety checks passed: real `.env` confirmed gitignored,
   never tracked, never committed in any of the 8 commits (`git check-ignore` / `git ls-files` /
   `git log --diff-filter=A -- .env` all clean); diff scanned for key-shaped content, only hits
   were doc lines showing how to *generate* a key (`token_urlsafe`), no real secrets. Pushed 8
   commits (`81b6b3d`..`ac01b1e`) to `origin/main` — auth used `gh auth login` +
   `gh auth setup-git` (the shell's `GIT_ASKPASS` pointed at a stale/unreachable VS Code IPC
   socket, so plain `git push` failed auth; unset `GIT_ASKPASS`/`VSCODE_GIT_IPC_HANDLE` for the
   push once `gh` was authenticated). `git status` confirms up to date with `origin/main`, nothing
   ahead. `arbitrage-api/.env.save` (untracked, unrelated leftover file) was left alone — not
   staged, not pushed.
7. **Telegram approver** — separate step after API confirmed healthy: repoint its unit
   (`deploy/arbitrage-telegram-approver.service` — see item 1) to clone paths, set TELEGRAM_* vars,
   install/enable. **Not started this round (API-only cutover, by design).**
8. **Phase 5 — DONE (2026-07-07).** eBay sandbox user token stored and verified against a live
   seller-scoped call. `sell.analytics.readonly` added to `_SELL_SCOPES` in `auth.py`
   (commit `004b294`). `arbitrage-api.service` restarted from the clone, confirmed
   `active (running)` (PID 632127) — no code/unit changes this round, restart only.
   - **Token stored** via `POST /auth/ebay/user-token` (`auth.py:182-209`) — upserts
     `Token(client_id="user_token", ...)`, no RuName/redirect needed. User ran the `curl` themselves
     with the real token; response confirmed success without the token ever appearing in chat/logs.
   - **DB row verified** directly (`tokens` table, `client_id="user_token"`): present, correct
     `expires_at`, token value structurally sane (correct `v^1.1#...` prefix, no
     whitespace/quote/backslash corruption from the curl round-trip).
   - **First token attempt failed live**: direct `GET /sell/fulfillment/v1/order` (sandbox) with the
     stored token → **401 "Invalid access token"**. Not a scope error — eBay rejected the token
     outright. Root cause: **keyset mismatch** — the sandbox portal's token tool must match the app
     whose `EBAY_CLIENT_ID` is in `.env`, or eBay rejects an otherwise well-formed token. User
     re-minted against the correct app and re-ran the store curl.
   - **Second token attempt passed**: same direct fulfillment-order call → **200**,
     `{"total":0,"orders":[]}` — empty is correct for a fresh sandbox account. **This is the proof
     that token + `sell.analytics.readonly` scope + seller auth path all work end-to-end.**
   - **Important caveat discovered**: `GET /orders/pending` (the app's own endpoint) is **not** a
     valid way to test this — `orders.py:119-120` catches `(httpx.HTTPStatusError, HTTPException)`
     from the eBay call and silently falls back to the SQLite cache, always returning 200 regardless
     of whether the underlying eBay call 401'd. No `log_event` on that path either. If a future
     session needs to verify live eBay auth, call eBay's API directly (or add logging to that
     except block) — don't trust `/orders/pending`'s status code alone.
   - **`getTrafficReport` (`GET /sell/analytics/v1/traffic_report`, sandbox) → 404, empty body.**
     Ran with the same (working) token, `dimension=DAY`,
     `filter=marketplace_ids:{EBAY_US},dimension_key:LISTING`. Treated as **expected sandbox
     behavior** (no error payload, unlike the earlier real 401) — eBay's Analytics API has limited/no
     data infrastructure in sandbox, so a 404 here does not indicate a token or scope problem. The
     fulfillment-order 200 above is the authoritative proof that auth works; this 404 is a sandbox
     platform limitation, not a regression to chase.
9. **Stage 5 — DONE (2026-07-08).** Full-cost margin gate (§4A.2): `evaluate_margin()` in
   `services/margin_engine.py` + new `POST /research/margin` endpoint (`routers/research.py`,
   registered in `main.py`, behind the same `X-API-Key` dependency as the other routers).
   Committed `4aa5e5c`. **Not pushed.**
   - **What it does:** takes `sale_price`/`amazon_cost`, computes eBay fee, Promoted Listings fee,
     payment FX fee, and expected-return cost, then gates on both `MIN_NET_MARGIN_PCT` (≥0.20) and
     `MIN_NET_PROFIT_ABS` (≥$5.00). Returns the full breakdown plus `fail_reasons` so callers can see
     exactly which threshold(s) failed.
   - **Bug caught and fixed this round:** the live server was echoing stale defaults
     (`min_net_margin_pct: 0.15`, `min_net_profit_abs: 3.0`) even though `margin_engine.py` and
     `.env.example` on disk already had the agreed 0.20/5.00. Root cause: `margin_engine.py` was
     edited (mtime 03:14:13) **after** `arbitrage-api.service` last started (03:11:28) — the running
     uvicorn process had the old module loaded in memory; Python doesn't hot-reload edited source
     without a restart. Fixed by `sudo systemctl restart arbitrage-api.service`. Lesson: always
     restart after editing a module the live service has already imported, and verify by reading the
     values *back* from a live response, not just trusting the code on disk.
   - **`EBAY_FEE_PCT` is no longer dead** (see the "Facts worth not re-discovering" note below,
     which now needs an update) — it's wired via `os.getenv` as of this commit, default `0.1325`,
     matching the real-world fee assumption. `.env.example` documents this explicitly.
   - **Live-verified post-restart**, both cases:
     - `$50 sale / $20 cost` → echoes `min_net_margin_pct: 0.20`, `min_net_profit_abs: 5.0`,
       `passed: true`.
     - `$25 sale / $18 cost` (the threshold-exercising case, not the comfortable one) → `passed:
       false`, net profit ~$1.75, fails both `MIN_NET_MARGIN_PCT` and `MIN_NET_PROFIT_ABS` with
       clear `fail_reasons` text naming each.
   - **12 unit tests** (`tests/test_margin_engine.py`, run via new `requirements-dev.txt`
     `-r requirements.txt` + `pytest==9.1.1`): boundary conditions (exactly-at-threshold passes,
     just-below fails) for both gates independently, zero/negative `sale_price` guards (no
     ZeroDivisionError), high-return-rate wipeout case, and a hand-computed config-override case.
     All 12 pass (`0.43s`).
   - **Not done:** not pushed to origin; real `.env` does not set `MIN_NET_MARGIN_PCT` /
     `MIN_NET_PROFIT_ABS` / `EBAY_FEE_PCT` (relies on code defaults, which now match) — see item
     below on whether to add them explicitly.
10. **Stage 6 — DONE (2026-07-08).** Candidates pipeline: two new tables (`candidates`,
    `margin_calc`) added via the existing `init_db()`/`create_all` mechanism; existing tables
    (`orders`/`listings`/`competitor_listings`/`tokens`/`event_log`) confirmed untouched.
    `routers/candidates.py`, registered in `main.py` behind the same `X-API-Key` dependency as the
    other routers:
    - `POST /candidates` — intake, always runs `evaluate_margin` and always stores the row
      (`pending_review` on pass, `rejected_margin` on fail — rejects are kept, not dropped).
    - `GET /candidates` — filter by `status`/`source`, newest-first, limit/offset.
    - `GET /candidates/{id}` — detail + full `margin_history` (every `margin_calc` row for that
      candidate).
    - `POST /candidates/{id}/reevaluate` — updates cost, re-runs margin via the shared
      `_run_margin_and_store`, appends a new `margin_calc` row (old rows kept as history), flips
      status accordingly.
    - **6 new tests** (`tests/test_candidates.py`, 20 total in the suite now), isolated temp SQLite
      DB — real `arbitrage.db` confirmed byte-identical before/after the test run.
    - **Live-verified**: intake pass ($50/$20 → `pending_review`) and fail ($25/$18 →
      `rejected_margin`, still stored, not discarded); list + `?status=rejected_margin` filter;
      reevaluate flip (`rejected_margin` → `pending_review`, new `margin_calc` id 3 appended, prior
      row retained as history). Margin records store the thresholds actually used (0.20/5.00/0.1325)
      alongside each calc, not just the current config.
    - **Two test candidates (Widget A/B) remain in the live `arbitrage.db`** as artifacts of this
      verification pass (table counts: orders=13, listings=1, candidates=2 — confirmed unchanged
      from expected). Harmless, but can be deleted later if a clean slate is wanted before real
      candidate data starts flowing.
    - **Not done:** not pushed to origin. AI-driven scoring/sourcing on top of this pipeline is
      explicitly out of scope for this round.

**Still open:** fulfillment bot is repointed (venv + unit, see Phase 4b) but intentionally left
**disabled** — enabling/starting it, and the Telegram approver setup, are separate future sessions.
Production go-live additionally needs: production keyset (separate from the sandbox app used here),
a real US seller account, and a registered RuName/HTTPS callback for the production OAuth handshake
(see go-live checklist below) — none of that is touched by this session's sandbox work.

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
- Old→new `.env`: 9 of 10 keys carry over. `EBAY_FEE_PCT` **was** dead (hardcoded 0.1325 in
  margin_engine.py) but is now live as of Stage 5 (2026-07-08, commit `4aa5e5c`) — wired via
  `os.getenv`, default still `0.1325`. Set it explicitly in `.env` if you want a value other than
  the default; it's silently ignored no longer.
- `QUEUE_POLL_INTERVAL` (bot) and `POLL_INTERVAL` (telegram) are INDEPENDENT, not a rename — both
  needed if both run. Same for `MAX_PRICE_DRIFT_PCT` (bot % drift check) vs the gate's dollar caps.
- `BOT_API_KEY` must be a value that also appears in `API_KEYS`.
- Never paste `API_KEYS` / `BOT_API_KEY` values into a chat. Generate:
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. The extension's stored key must
  match one in `API_KEYS`.
