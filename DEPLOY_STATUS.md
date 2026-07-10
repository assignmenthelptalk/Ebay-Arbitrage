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

AI Product Scorer (§4A.3, 2026-07-08): 4-provider model layer (Anthropic/Kimi/OpenAI/Mock) +
human-curated priors + cost-guarded scoring endpoints built, 46/46 tests green. **Live-verified
end-to-end at zero spend via `SCORER_PROVIDER=mock`** — real intake → margin gate → score → stored
`scores` row, over the actual running service, no external call. The 3 real providers (Anthropic,
Kimi, OpenAI) remain mock-tested-only — no live call made yet, deliberately deferred until spend is
explicitly requested. See item 11 below.

Review/Approval Dashboard (§4A.5, 2026-07-09): `approve`/`reject` endpoints + a candidate-review web
UI — the human gate the whole plan hinges on. No AI spend, no eBay dependency. 52/52 tests green.
See item 12 below.

AI Listing Generator (§4A.4, 2026-07-09): draft eBay listings for `scored` candidates via the same
provider-agnostic model layer (Anthropic/Kimi/OpenAI/Mock), editable in the dashboard, approved
together with the candidate. Cassini/Taxonomy enrichment deliberately deferred (clearly-marked
stub). 64/64 tests green, **live-verified end-to-end at zero spend** (score → generate → edit →
approve, mock provider). **The acquisition half of the v2 system is now functionally complete**:
a candidate can flow all the way from intake to an approved product + finished listing, gated by
margin, scored by AI, reviewed/edited by a human, entirely free until a real model is deliberately
switched on. See item 13 below.

Cassini/Taxonomy item-specifics enrichment (§4A.4 socket, 2026-07-10): the deferred stub from item
13 is now built and sandbox-live-verified — real category resolution + cached real per-category
item aspects, via an app (client-credentials) token, feeding the listing generator's prompt with
graceful fallback on any failure. 78/78 tests green. See item 15 below (item 14 is the recon that
preceded it).

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
11. **AI Product Scorer (§4A.3) — BUILT, MOCK-VERIFIED, NOT YET LIVE-VERIFIED (2026-07-08).**
    OpenClaw was deliberately **not** used as a model gateway — recon (separate session) found it's
    an agent gateway (every call runs the full agent/tool loop under full operator scope), not a
    plain completion endpoint, so this scorer calls providers directly instead.
    - **`services/model_providers.py`** — provider-agnostic layer. `ModelProvider` interface
      (`async complete(system_prompt, user_content) -> dict`, parsed JSON only — the scorer never
      touches a provider SDK). `_AnthropicMessagesProvider` shared base implements the Anthropic
      Messages wire format; `AnthropicProvider` and `KimiProvider` both subclass it (same format,
      different base_url/key/model). `OpenAIProvider` implements Chat Completions separately.
      `get_provider()` factory reads `SCORER_PROVIDER`/`SCORER_MODEL` from `.env`. Defensive JSON
      extraction (strips ``` fences / preamble before `json.loads`) raises a clear `ProviderError`
      on unparseable output rather than returning garbage.
    - **Kimi's exact shape confirmed from OpenClaw's own (separately running) config, not guessed:**
      base URL `https://api.kimi.com/coding`, model id `kimi-for-coding`, speaks the Anthropic
      Messages format. Cross-checked against OpenClaw's transport source, which appends `/v1/messages`
      to the base — so the real wire endpoint is `https://api.kimi.com/coding/v1/messages`. This app
      calls that endpoint directly; it does **not** route through OpenClaw's gateway process at all.
    - **Two new tables** (`scores`, `scoring_priors`) added via the existing `init_db()`/`create_all`
      mechanism. Confirmed additive: all 7 existing tables' row counts unchanged before/after
      (orders=13, listings=1, competitor_listings=5, tokens=2, event_log=38, candidates=2,
      margin_calc=3). Both new tables created empty.
    - **`routers/scoring.py`** — `POST /candidates/{id}/score` (score one), `POST /scoring/run`
      (cost-guarded batch: only `pending_review` candidates, skips any candidate that already has a
      `Score` row unless `?force=true` — checked by row existence, not just status, so a
      reevaluate-flip back to `pending_review` doesn't silently re-spend — caps at
      `SCORING_BATCH_MAX`/default 25, oldest-first, overflow reported as `skipped` not dropped so
      re-running drains the backlog), `GET/POST /scoring/priors`, `POST /scoring/priors/{id}/toggle`.
      A dormant, clearly-commented `_suggest_priors_from_performance()` stub documents the future
      `listing_performance` → human-approved-prior feedback loop; no logic, not wired anywhere.
      Registered in `main.py` behind `dependencies=protected` (confirmed: app boots, full route
      table includes all new endpoints).
    - **43/43 tests pass** (20 pre-existing + 23 new across `tests/test_model_providers.py` and
      `tests/test_scoring.py`), **all with the provider mocked** — a fake `httpx.AsyncClient`
      standing in for the real one, zero real network calls, no key required. Covers: each adapter
      parsing clean/fenced/preamble-wrapped JSON and raising `ProviderError` cleanly on malformed
      output, missing key (verified no network attempt is even made), or HTTP error status; the
      factory's provider selection and its error on an unknown provider name; the scorer storing a
      `Score` row and flipping a candidate to `scored` on success vs. `scoring_failed` (candidate
      not lost, no crash) on a provider error; active priors appearing in the prompt and inactive
      ones being excluded (verified via distinct marker text); and all three `/scoring/run` cost
      guards (rejected_margin skip, already-scored skip, batch cap with overflow reporting).
      Real `arbitrage.db` confirmed byte-unchanged (mtime check) after the full suite.
    - **Deliberately NOT done this round — no live provider call made.** All three adapters
      (Anthropic, Kimi, OpenAI) are mock-tested only; none has round-tripped to a real API yet.
      This was a deliberate choice to avoid spend before the operator explicitly signs off on a
      live test. `.env` still has no real `KIMI_API_KEY`/`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` set.
      **Do not treat Kimi (or any provider) as verified working end-to-end until a real key is
      added, the service is restarted, and `POST /candidates/1/score` (or similar) actually returns
      a real score — that step is still pending, by choice, not by failure.**
    - **MockProvider added (2026-07-08, same day) — pipeline now exercisable end-to-end for free.**
      A 4th adapter, `MockProvider`, returns a fixed plausible score
      (`should_list=true, risk_level="low", confidence="med", competition_score=null`, reason
      self-identifies as `"MOCK score — no model was called"`) with **no HTTP call and no API key**.
      Selectable via `SCORER_PROVIDER=mock`. Registered in `get_provider()` alongside the 3 real
      adapters; real adapters untouched. 3 new tests (46/46 total): factory selects `MockProvider`
      for `SCORER_PROVIDER=mock`; `complete()` returns the exact schema with `httpx.AsyncClient`
      patched to explode if touched (proves zero network); and a scorer test that goes through the
      **real** `get_provider()` factory (not the usual per-test fake) to prove the actual
      `mock` wiring works, not just that the scorer tolerates an arbitrary stand-in.
      - **Live-verified on the real service (2026-07-08):** `.env` set to `SCORER_PROVIDER=mock`
        (no real keys), service restarted, then exercised live: `POST /candidates/1/score` returned
        the mock score and flipped candidate 1 to `scored`; `POST /scoring/run` scored candidate 2
        via mock and correctly skipped candidate 1 (already had a `scores` row) —
        `scored_count=1, skipped_count=1, failed_count=0`. Confirms the full live path (endpoint →
        scorer → provider factory → parse → store → status flip → cost-guard skip logic) end-to-end
        with zero spend.
      - **To go live with a real provider later:** it's a `.env` change only —
        `SCORER_PROVIDER=kimi` (or `anthropic`/`openai`) + the matching API key + restart. No code
        change needed. Switch back to `mock` any time to keep demoing/testing for free.
    - **Not done:** not pushed to origin.
12. **Review/Approval Dashboard (§4A.5) — DONE (2026-07-09).** Makes the candidates pipeline usable
    by a human. Additive only — no existing tables altered, `EBAY_ENV`/`DRY_RUN` untouched, fulfillment
    bot untouched, behind the same `X-API-Key` dependency as every other router.
    - **`POST /candidates/{id}/approve`** — sets `status="approved"`. Blocked (409, clear
      `detail.message`) from `rejected`/`rejected_margin`/`scoring_failed`; allowed from
      `pending_review`/`scored`; re-approving an already-`approved` candidate is a no-op (still 200).
    - **`POST /candidates/{id}/reject`** — sets `status="rejected"` from any state (universal
      kill-switch); re-rejecting is a no-op. Optional `reason` in the body is accepted but **not
      persisted** — no existing column/table to hold it without a schema change, so it's intentionally
      dropped rather than silently faked.
    - **Score enrichment (required, not originally scoped for Stage 1):** `GET /candidates`,
      `GET /candidates/{id}`, and every mutating candidates endpoint now also return the candidate's
      latest `Score` row (`should_list`/`risk_level`/`confidence`/`reason`) alongside `margin` — that
      data existed in the `scores` table but no endpoint surfaced it before this. Purely additive to
      the response shape; no schema change.
    - **7 new tests** (`tests/test_candidates.py`, 52 total in the suite): approve from `scored` and
      from `pending_review`, approve idempotency, approve blocked from `rejected_margin` (409 +
      unchanged status/history), reject from a non-pending state, reject idempotency including from
      `rejected_margin`. All confirm margin/score history is untouched by status transitions. Real
      `arbitrage.db` confirmed byte-unchanged after the suite.
    - **`arbitrage-api/candidates.html`** — new page, not an extension of `dashboard.html` (that
      page's "Pending Approvals" panel is the *fulfillment*-purchase gate, a different domain).
      Vanilla HTML/JS, same dark theme/CSS vars/helpers as `dashboard.html`, same auth pattern
      (`X-API-Key` from `localStorage['apiKey']`, prompted client-side — no new auth scheme, no keys
      added). Lists candidates with title/source/ASIN, sale price, amazon cost, margin
      (net profit/%, pass/fail, reason), score (should_list/risk/confidence/reason), and a
      color-coded status badge; status filter (`?status=`) with pagination; per-row Approve/Reject/
      Re-evaluate (posts new `amazon_cost` to the existing reevaluate endpoint) actions; Amazon
      click-through link (`amazon.com/dp/{asin}`) when an ASIN is present; reloads the row/list after
      any action so status changes are visible immediately.
    - **Served via `GET /candidates-dashboard`** in `main.py`, mirroring `/dashboard`'s mechanism
      exactly (`FileResponse`, not behind `require_api_key` — same as the existing dashboard; auth
      happens per-fetch via the JS, not on page load).
    - **Live-verified** by the operator after a restart: candidates list with margin+score+status,
      Approve/Reject/filter/Amazon-link all confirmed working in the browser.
    - **Not done:** listing generator, publish flow, and ZIK integration are explicitly out of scope
      (blocked on §4A.4/§4A.6 not existing yet) — this dashboard only shows/actions candidates, it
      can't show or edit listings that don't exist yet. Not pushed to origin.
13. **AI Listing Generator (§4A.4) — DONE (2026-07-09).** Draft eBay listings for `scored`
    candidates, editable and approved together with the candidate. Additive only — no existing
    tables altered, `EBAY_ENV`/`DRY_RUN`/fulfillment bot untouched, behind the same `X-API-Key`
    dependency as every other router.
    - **New `generated_listings` table** (`models.py`): `title`/`description`/`item_specifics`
      (JSON)/`keywords` (JSON), `provider`/`model`/`raw_response` (audit), `edited` (false=AI draft,
      true=human-edited), `status` (`draft`/`approved`, mirrors the candidate), multiple rows per
      candidate allowed (regen history), latest = current. Created via the existing
      `init_db()`/`create_all` mechanism — confirmed live: table now exists (0 rows pre-test-run),
      all 9 prior tables' row counts unchanged after restart.
    - **Provider layer generalized, not duplicated:** `services/model_providers.py`'s
      `get_provider()` now takes `(provider_env, model_env, default_provider)` instead of hardcoding
      `SCORER_PROVIDER`/`SCORER_MODEL`/`"kimi"` — the scorer's call site is unchanged (same
      defaults), the generator calls it with `LISTING_PROVIDER`/`LISTING_MODEL`/**`"mock"`** as the
      default, so an unset env var never accidentally spends (unlike the scorer, whose unset default
      is a paid provider — real `.env` overrides that to `mock` explicitly). `MockProvider` now
      serves both callers: it returns a listing-shaped payload when the prompt contains the literal
      marker `"item_specifics"`, else the original score payload — confirmed backward compatible,
      the 46 pre-existing scorer tests are untouched.
    - **`services/listing_generator.py`** (new) — pure computation, no DB access, mirrors
      `margin_engine.py`'s role rather than `scoring.py`'s router-embedded one. `_build_prompt`
      (honest about thin inputs, general item specifics only), `generate_listing()` (same
      `{"ok": bool, ...}` contract as the scorer's `_score_candidate`). **Cassini/Taxonomy socket**:
      `_category_aspects(category) -> dict` stub, always returns `{}`, docstring marks it as the
      future eBay Taxonomy/Metadata rewarded-aspects injection point — deliberately not built this
      round.
    - **`routers/listings_gen.py`** (new, registered in `main.py`): `POST
      /candidates/{id}/generate-listing` (allowed from `scored`/`approved`, 409 from
      `pending_review`/`rejected`/`rejected_margin`/`scoring_failed`), `POST
      /listings/generate-pending` (cost-guarded batch mirroring `/scoring/run`: skips
      already-drafted candidates unless `force=true`, capped at `LISTING_BATCH_MAX`/default 25),
      `GET /candidates/{id}/listing` (returns `{"listing": null}` rather than 404 when none exists —
      a normal dashboard state), `PUT /listings/{id}` (partial edit, sets `edited=true`). Note:
      `/listings/generate-pending` and `PUT /listings/{id}` share the `/listings` prefix with the
      pre-existing live-eBay-listings router (`routers/listings.py`) — confirmed no path/method
      collision, but it's the same URL namespace for two different resources (draft listings vs.
      live eBay listings); worth knowing if that router grows.
    - **`_candidate_to_dict` enriched again** (`routers/candidates.py`): now also carries `"listing"`
      — the latest `GeneratedListing` — alongside `margin`/`score`, threaded through
      list/detail/reevaluate/approve/reject, same pattern as the score enrichment in item 12.
    - **Approve product + listing together**: `POST /candidates/{id}/approve` now also locks the
      latest draft to `status="approved"` (idempotent) if one exists. No draft yet doesn't block
      approval — response carries `"listing": null`, no crash, no extra warning field.
    - **12 new tests** (`tests/test_listings_gen.py`, 64 total in the suite): generate for `scored`
      (draft stored, candidate status untouched), same flow through the **real** `get_provider()`
      factory with `LISTING_PROVIDER=mock` (not just a fake, proves actual wiring), blocked for
      `rejected_margin` (409) and missing candidate (404), `generate-pending` cost guards (skip
      already-drafted, batch cap with overflow reported not dropped), `PUT` edit (partial update,
      `edited=true`, untouched fields preserved), 404 on missing listing id, approve-with-draft locks
      both (re-fetched to prove persistence, not just echoed), approve-without-draft is fine
      (`listing: null`, no crash). Real `arbitrage.db` confirmed byte-unchanged after the suite.
    - **Dashboard (`candidates.html`) extended**, not a new page: new "Listing Draft" table column
      showing title/description/item-specifics/keywords with an `AI DRAFT`/`EDITED`/`LOCKED` state
      tag; Generate/Regenerate button (`POST .../generate-listing`); Edit button opening a small
      modal (title/description/item-specifics-as-JSON/keywords, client-side JSON validation) that
      calls `PUT /listings/{id}`; existing Approve button unchanged (server-side lock is automatic).
      Reused all existing CSS vars/badge classes/`esc`/`money` helpers, no new auth path, no external
      libraries. Generalized the old `apiPost` helper into method-aware `apiSend` for the new PUT
      call.
    - **Live-verified end-to-end at zero spend (2026-07-09), operator-run curls**: `POST
      /candidates/1/score` (mock) → `scored`; `POST /candidates/1/generate-listing` (mock) → draft
      stored; `PUT /listings/{id}` → `edited=true` with the hand-edited title preserved; `POST
      /candidates/1/approve` → both candidate and listing flipped to `approved`, edited title still
      intact. Confirmed in the dashboard too: state tag progressed `AI DRAFT` → `EDITED` → `LOCKED`
      as expected; Regenerate on a `rejected` candidate correctly surfaced the 409 guard in the UI.
    - **Not done (deliberately out of scope):** Cassini/Taxonomy enrichment (stubbed only), Publish
      (§4A.6 — pushing an approved listing to eBay, a production/go-live step needing the real
      seller account), ZIK integration. Not pushed to origin.

**Acquisition-side milestone:** as of item 13, a candidate can go from intake all the way to an
approved product + finished, human-edited listing — gated by margin (§4A.2), scored by AI (§4A.3),
reviewed/edited by a human (§4A.5/§4A.4) — entirely in a browser, entirely free until a real model
provider is deliberately switched on. What remains for the *outbound* loop is Publish (§4A.6) alone,
which is intentionally a production/go-live step. Everything before publish is done.

14. **Cassini/Taxonomy sandbox recon (2026-07-10, read-only, no code changed).** Answered whether
    eBay's Taxonomy/Metadata APIs are usable in sandbox before building the `_category_aspects()`
    stub in `listing_generator.py` (item 13). **Verdict: sandbox serves full real data — build +
    fully test Cassini enrichment now, no need to defer to production.**
    - **Auth gotcha found first:** the stored `client_id="user_token"` (3-legged User token, used
      for Sell Fulfillment) got a flat **403 "Access denied" (errorId 1100)** on
      `get_default_category_tree_id`, even though `_SELL_SCOPES` in `auth.py` already includes the
      base `https://api.ebay.com/oauth/api_scope` that Taxonomy's docs say is sufficient. eBay's
      error body never names the missing scope (no `WWW-Authenticate` header either) — indistinguishable
      from a real sandbox-data gap without a second probe.
    - **Second probe (client-credentials app token, same `EBAY_CLIENT_ID`/`SECRET`, scope
      `oauth/api_scope`) → 200 on every call.** Confirms Taxonomy/Metadata are Application-token
      endpoints in practice; the existing User token (however it was scoped/minted via the sandbox
      "Get a Token" tool) just isn't the right credential for them. **This is a token-type/config
      fix, not a sandbox limitation** — `ebay_client.fetch_token()` already knows how to mint this
      app token, it's just never been wired to a caller other than the OAuth bootstrap.
    - **Taxonomy — works, real data:** `get_default_category_tree_id?marketplace_id=EBAY_US` →
      `categoryTreeId="0"`, version 134. `get_category_suggestions?q=wireless+earbuds` → sensible
      real categories (`112529` Headphones, `80077` Headsets, `48705` Headsets & Earpieces, with full
      ancestor paths) — auto title→category mapping works in sandbox today.
    - **Metadata (item aspects) — works, real data, THE key question:**
      `get_item_aspects_for_category?category_id=112529` → 200, **27 aspects**, each with
      `localizedAspectName`, `aspectConstraint.aspectRequired`, `aspectMode`, and real allowed-value
      lists. Required: `Brand`, `Color`, `Connectivity`, `Model`, `Type`. Optional: `Features`,
      `Form Factor`, `Microphone Type`, etc. — exactly the per-category rewarded specifics Cassini
      enrichment needs to fill.
    - **Not like `getTrafficReport`'s 404** (item 8 above) — that was a genuine sandbox
      data-infrastructure gap on the Analytics API. Taxonomy/Metadata sandbox data is populated and
      real, once called with the right token type.
    - **Follow-up for whoever builds Cassini:** mint/cache an Application (client-credentials) token
      for these calls rather than reusing `user_token` — either a small dedicated helper in
      `ebay_client.py`, or extend `_call()`'s token selection. Read-only investigation only; no code
      changed this round.
15. **Cassini item-specifics enrichment (§4A.4 socket) — BUILT + SANDBOX-LIVE-VERIFIED
    (2026-07-10).** Wires real eBay Taxonomy/Metadata data into the listing generator's
    `_category_aspects()` stub (item 13), fulfilling item 14's recon verdict. Additive only — no
    existing tables altered, `EBAY_ENV`/`DRY_RUN`/fulfillment bot/`user_token` flow untouched,
    behind the same auth as every other router (no new endpoints added).
    - **New `category_aspects` table** (`models.py`): one row per eBay `category_id` —
      `category_name`, `tree_id`, `aspects` (JSON: `[{name, required, allowed_values}]`),
      `fetched_at`. Reused/updated in place until `CASSINI_ASPECTS_TTL_DAYS` (default 30) elapses,
      then refetched. Confirmed additive on a throwaway DB copy before ever touching the live one.
    - **App-token path, not `user_token`:** extracted `ebay_client.get_cached_app_token(db,
      client_id, client_secret)` — the caching logic that `routers/auth.py`'s `POST /auth/ebay/token`
      endpoint already had inline, now shared. Caches the client-credentials token in the existing
      `tokens` table keyed by the real `EBAY_CLIENT_ID` (not a new table, not colliding with
      `"user_token"`) — confirmed a stale row from an earlier session already existed under that
      exact key, validating the approach before writing a line of new code. The `/auth/ebay/token`
      endpoint itself is now a thin wrapper around the same helper — identical response shape,
      verified by import + AST check, not just eyeballed.
    - **`services/cassini.py`** (new): `resolve_category(db, title)` (Taxonomy —
      `get_default_category_tree_id`, in-process-cached since it's ~constant per marketplace, then
      `get_category_suggestions`, top match) and `get_aspects(db, category_id, tree_id,
      category_name)` (Metadata — `get_item_aspects_for_category`, normalized and cached via
      `category_aspects`). Both wrapped so **any** failure (missing creds, 403, timeout, malformed
      response) degrades to `None`/`{}` rather than raising — Cassini enriches, never blocks.
      `CASSINI_ENABLED` (default true) short-circuits both with zero eBay calls when off.
    - **Wired into the generator:** `_category_aspects()` in `listing_generator.py` (async now)
      resolves title → category → aspects and returns `{required, recommended, allowed_values}` or
      `{}`. Prompt dynamically instructs the AI to fill every required aspect (using allowed values
      where given) when Cassini resolves something, falls back to the old honest general-judgement
      wording otherwise. **Defense-in-depth added beyond the original plan:** wrapped the
      `cassini.*` calls in `_category_aspects()` in their own try/except too, so even a hypothetical
      future bug in `cassini.py` that bypasses its own internal catch still can't break generation —
      proved with a test that makes `resolve_category` raise directly and confirms generation still
      returns 200.
    - **14 new tests** (`tests/test_cassini.py` — parsing, cache-hit-makes-zero-further-calls,
      stale-cache-refetches, 403/no-creds/disabled all degrade to `None` with zero network calls,
      app-token reused across both `resolve_category`+`get_aspects` (exactly one `oauth2/token` POST
      proven by call-count assertion); `tests/test_listings_gen.py` +3 — prompt includes the exact
      required-aspects/allowed-values text when Cassini resolves something, honest fallback wording
      when it doesn't, and the defense-in-depth raise-survival case). **78/78 total pass**, real
      `arbitrage.db` confirmed byte-unchanged. No test file imports `main`/calls `load_dotenv()`, so
      `EBAY_CLIENT_ID` stays unset during the suite — the pre-existing generate-listing tests stay
      fully hermetic (zero live calls) even with Cassini now wired in for real.
    - **Sandbox-live-verified (2026-07-10, operator-run curls, `LISTING_PROVIDER=mock` so listing
      *text* was mock but the Cassini fetch was 100% real sandbox eBay):** created a real candidate
      ("Wireless Bluetooth Earbuds"), scored it (mock), generated twice.
      - **Category resolved for real:** `112529`/`80077` suggestions from item 14's recon — this
        run landed on `80077` "Headsets".
      - **Real aspects fetched and cached:** `category_aspects` got exactly 1 row for `80077` with
        **21 real aspects** (huge realistic `Brand` allowed-value list, etc.) — not empty, not a
        stub.
      - **App token confirmed working, not `user_token`:** the app-token row's `expires_at` jumped
        to a fresh ~2h-out value seconds before the aspects fetch — a real mint succeeded.
      - **Cache genuinely hit on the second generate:** `category_aspects.fetched_at` for `80077`
        stayed at the *first* call's timestamp (02:27:39) even though the second generate happened
        24s later (02:28:03) — proves zero repeat `get_item_aspects_for_category` calls. Same for
        the app token (`expires_at` unchanged across both calls — reused from cache, not re-minted).
      - **One real finding along the way, diagnosed and explained, not a bug:** the stored listing's
        `item_specifics` still showed the generic `{"Brand": "Unbranded", "Condition": "New"}`
        after all this — looked like a silent Cassini failure at first glance. Root cause:
        `MockProvider.complete()` (`services/model_providers.py:200-233`) is a **hardcoded stub, not
        an LLM** — it only checks whether the literal string `"item_specifics"` appears anywhere in
        the system prompt (always true, it's baked into the fixed schema instruction) and then
        always returns the same fixed dict, regardless of what Cassini actually injected. Confirmed
        via `event_log` (zero `cassini_error` rows in the whole test window) and the cache evidence
        above that Cassini itself ran and succeeded end-to-end — the generic output is purely a
        `LISTING_PROVIDER=mock` test-harness artifact. **To see item_specifics actually reflect real
        Cassini aspects, generation needs to run against a real provider** (Anthropic/Kimi/OpenAI) —
        not required for Cassini's own correctness, which this round fully proved.
    - **Not done (deliberately out of scope this round):** no resolved-category column added to
      `generated_listings` (task called it optional; the `category_aspects` cache already records
      what was resolved, used in-prompt only). Publish (§4A.6), ZIK integration untouched. Not
      pushed to origin (per instruction — commit only).

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
