# eBay Arbitrage

Automated pipeline that finds underpriced Amazon products being resold at a margin on eBay,
lists them for sale, and — after a human approves each purchase — fulfills orders by buying
from Amazon and shipping to the eBay buyer.

Human approval is required before any money is spent. Nothing purchases automatically.

## How it fits together

```
ecom-sniffer/         Chrome extension — margin-checks a product on Amazon, scans an eBay
                       seller's listings, one-click creates a listing via the API.

scraper.py             Standalone eBay sold/active listing scraper (Playwright) — cost basis
                       vs. sell price, margin after eBay/PayPal fees. Feeds manual research;
                       not wired into the live pipeline.

arbitrage-api/          FastAPI backend — the hub everything else talks to.
                       eBay OAuth, competitor research, margin calculator, listings manager,
                       order/fulfillment tracking, event logging. API-key authenticated.
                       Full endpoint reference: arbitrage-api/README.md

fulfillment_bot.py      Polls the API's fulfillment queue, drives a real Amazon checkout via
                       Playwright, reports the result back to the API. Runs only after a human
                       has approved the order and set a hard price ceiling (see "the gate" below).

telegram_approver.py    (in arbitrage-api/) Telegram bot alternative to the dashboard for
                       approving/rejecting pending fulfillments from a phone.

setup_session.py        Run once, manually, to log into Amazon in a headed browser and save
                       cookies to amazon_session.json — the fulfillment bot reuses that session.

insert_test_order.py    Dev/debug helper — inserts a synthetic order row directly into SQLite.
                       Not part of the running system.

deploy/                 systemd unit template(s) for services not yet installed on this host
                       (arbitrage-telegram-approver.service). arbitrage-api.service and
                       fulfillment-bot.service are installed directly on the VPS, not tracked
                       here.
```

## The human-in-the-loop gate

This is the core safety mechanism and worth understanding before touching anything:

1. An eBay order comes in. The API looks up the matching Amazon listing's price and creates a
   `pending_review` fulfillment job — **no purchase happens yet**.
2. A human reviews it (via the dashboard or the Telegram approver) and approves it with a
   **hard price ceiling** (`confirmed_max_price`). The API refuses the approval outright if that
   ceiling exceeds `ABSOLUTE_MAX_ORDER` or would blow the day's `DAILY_SPEND_CAP`.
3. `fulfillment_bot.py` polls for approved jobs, checks the live Amazon price against the
   ceiling at purchase time, and refuses to buy if the price moved above it — even though a
   human already approved the job.
4. The bot reports success/failure back to the API, which updates the order and (on success)
   pushes tracking back to eBay.

Nothing above runs while `DRY_RUN=true` — see `arbitrage-api/.env.example`.

## Setup

Each component has its own dependencies and only needs building once per host:

```bash
# API (FastAPI backend)
cd arbitrage-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in real values — see arbitrage-api/README.md

# Fulfillment bot / scraper / session setup (Playwright-based, separate deps)
cd ..
python3 -m venv .venv-bot        # any name — not currently present on this host
.venv-bot/bin/pip install -r requirements.txt
.venv-bot/bin/playwright install chromium
```

The bot reads the same `arbitrage-api/.env` as the API (via `EnvironmentFile=` in its systemd
unit, or `load_dotenv()` when run manually from a directory containing it) — `BOT_API_KEY` must
be one of the values in the API's `API_KEYS`.

Chrome extension: `chrome://extensions` → Developer mode → Load unpacked → select `ecom-sniffer/`.

## Running

- **API reference, endpoint list, systemd unit templates, environment variables:**
  `arbitrage-api/README.md`
- **Current deployment state of this specific host** (what's live, what's disabled, what's
  pending, rollback instructions): `DEPLOY_STATUS.md` — read this first if you're picking up
  ops work on the VPS.

## Safety notes

- `DRY_RUN` and `EBAY_ENV` gate real purchases and eBay production traffic respectively — never
  flip either without deliberately deciding to.
- `.env`, `amazon_session.json`, and `*.db` are gitignored — never commit real credentials,
  Amazon session cookies, or the SQLite database. Only `.env.example` (with placeholder/empty
  values) belongs in git.
- `ABSOLUTE_MAX_ORDER` / `DAILY_SPEND_CAP` are hard caps enforced server-side at approval time —
  raising them is a deliberate, real-money decision, not a routine config tweak.
