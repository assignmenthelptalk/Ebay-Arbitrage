# eBay Arbitrage API

Zero-inventory eBay-to-Amazon arbitrage backend. FastAPI + SQLite REST API connecting a Chrome extension, dashboard, and Playwright automation script.

## Architecture

```
Chrome Extension / Dashboard
        │
        ▼
  FastAPI REST API  (port 8000)
        │
   ┌────┴────────────────────────────────┐
   │  Module 1: Auth & eBay OAuth        │
   │  Module 2: Competitor Research      │
   │  Module 3: Margin Calculator        │
   │  Module 4: Listings Manager         │
   │  Module 5: Orders & Fulfillment     │
   │  Module 6: Learning Loop Logger     │
   └────┬────────────────────────────────┘
        │
   SQLite (arbitrage.db)    fulfillment_queue.json
        │                           │
        ▼                           ▼
  eBay Sell/Browse API     Playwright script
                           (polls every 60s,
                            places Amazon orders)
```

## Setup

### Prerequisites

- Python 3.12+
- eBay Developer account with a sandbox app
- VPS or local machine

### 1. Install dependencies

```bash
cd arbitrage-api
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your eBay credentials
```

See [Environment Variables](#environment-variables) below.

### 3. Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

API docs: `http://localhost:8000/docs`

---

## Environment Variables

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `EBAY_CLIENT_ID` | Yes | `MyApp-SBX-abc123` | eBay app client ID |
| `EBAY_CLIENT_SECRET` | Yes | `SBX-abc123...` | eBay app client secret |
| `EBAY_ENV` | No | `sandbox` | `sandbox` or `production` |
| `EBAY_MARKETPLACE` | No | `EBAY_GB` | eBay marketplace ID |
| `EBAY_RU_NAME` | For OAuth | `MyApp-MyApp-MyRu-...` | Registered redirect URI name |
| `TARGET_MARGIN_PCT` | No | `0.20` | Target profit margin (20%) |
| `EBAY_FEE_PCT` | No | `0.14` | eBay final value fee % |
| `EBAY_DEFAULT_CATEGORY_ID` | No | `9355` | Default eBay category for new listings |
| `API_KEYS` | Yes | `key1,key2` | Comma-separated keys accepted via `X-API-Key` — API fails closed (500) if unset |
| `ABSOLUTE_MAX_ORDER` | No | `150` | Hard per-order USD ceiling — `approve-fulfillment` refuses above this regardless of what's confirmed |
| `DAILY_SPEND_CAP` | No | `500` | Hard total-per-day USD cap across all approved fulfillments |
| `FULFILLMENT_QUEUE` | No | `fulfillment_queue.json` | Path to the gate's queue file — **must be set to the same value (an absolute path is safest) for both the API service and `fulfillment_bot.py`**, since they share this file directly rather than talking over HTTP |

---

## API Reference

### Health

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

---

### Module 1 — Auth & eBay OAuth

#### Fetch app token (client credentials)
```bash
curl -X POST http://localhost:8000/auth/ebay/token \
  -H "Content-Type: application/json" \
  -d '{"client_id": "YOUR_CLIENT_ID", "client_secret": "YOUR_SECRET"}'
```

#### Start user OAuth flow (required for Sell API)
```bash
curl http://localhost:8000/auth/ebay/authorize
# Returns authorize_url — open in browser, log in as sandbox seller
# eBay redirects to /auth/ebay/callback which auto-stores the user token
# Pre-req: set EBAY_RU_NAME in .env and register the callback URL in developer.ebay.com
```

#### Store user token manually (sandbox shortcut)
```bash
curl -X POST http://localhost:8000/auth/ebay/user-token \
  -H "Content-Type: application/json" \
  -d '{"user_token": "v^1.1#i^1#...", "expires_in_seconds": 7200}'
```

---

### Module 2 — Competitor Research

#### Scan sellers (6-hour cache per seller)
```bash
curl -X POST http://localhost:8000/competitors/scan \
  -H "Content-Type: application/json" \
  -d '{"seller_usernames": ["testuser_1"], "marketplace": "EBAY_GB"}'
```

#### Query cached listings
```bash
curl "http://localhost:8000/competitors/listings?min_price=10&max_price=50&limit=20"
```

---

### Module 3 — Margin Calculator

#### Calculate single product margin
```bash
curl -X POST http://localhost:8000/margin/calculate \
  -H "Content-Type: application/json" \
  -d '{"amazon_price": 18.99}'
# Returns: minimum_list_price, target_profit, margin_rate, viable
```

#### Batch validate product list
```bash
curl -X POST http://localhost:8000/margin/validate-batch \
  -H "Content-Type: application/json" \
  -d '{
    "products": [
      {"title": "Casio Watch", "item_id": "abc123", "amazon_price": 18.99}
    ],
    "target_margin_pct": 0.20
  }'
# Returns only viable products, sorted by target_profit desc
```

#### Browse opportunities from competitor scan
```bash
curl "http://localhost:8000/margin/opportunities?min_margin=0.20"
# Joins competitor_listings with margin calc, returns viable sorted by profit
```

---

### Module 4 — Listings Manager

Requires a user token with sell scopes. See Module 1 OAuth flow.

#### Create listing (3-step eBay Sell Inventory API)
```bash
curl -X POST http://localhost:8000/listings/create \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Vintage Casio Watch A158W",
    "amazon_price": 18.99,
    "amazon_asin": "B000CASIO01",
    "ebay_list_price": 33.83,
    "quantity": 1,
    "condition": "NEW",
    "description": "Classic retro digital watch"
  }'
```

#### Get listings (filter by status)
```bash
curl "http://localhost:8000/listings?status=active"
# status: active | paused | deleted | banned
```

#### Status summary
```bash
curl http://localhost:8000/listings/summary
# {"active": 5, "paused": 1, "banned": 0, "deleted": 2, "total": 8}
```

#### Pause a listing
```bash
curl -X PATCH http://localhost:8000/listings/LISTING-ID/pause
# Calls eBay withdraw_offer; gracefully handles 403 (app token) and updates SQLite
```

#### Delete a listing
```bash
curl -X DELETE http://localhost:8000/listings/LISTING-ID \
  -H "Content-Type: application/json" \
  -d '{"reason": "price_break"}'
# reason: price_break | banned | manual
```

---

### Module 5 — Orders & Fulfillment

#### Fetch pending orders from eBay + upsert to SQLite
```bash
curl http://localhost:8000/orders/pending
# Calls eBay Sell Fulfillment API; falls back to SQLite cache if API unavailable
```

#### Trigger fulfillment (queues a REVIEW request — does NOT purchase)
```bash
curl -X POST http://localhost:8000/orders/ORDER-ID/fulfill -H "X-API-Key: $API_KEY"
# Looks up the order's Amazon price from its Listing, queues a pending_review
# job via fulfillment_gate.request_fulfillment(). No purchase happens here.
```

#### See what's awaiting human review
```bash
curl http://localhost:8000/fulfillment/pending -H "X-API-Key: $API_KEY"
```

#### Approve a fulfillment (sets the hard price ceiling)
```bash
curl -X POST http://localhost:8000/orders/ORDER-ID/approve-fulfillment \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"confirmed_max_price": 24.99, "approver": "you@example.com"}'
# Refused (422) if confirmed_max_price exceeds ABSOLUTE_MAX_ORDER or would
# exceed DAILY_SPEND_CAP. The bot will never pay above confirmed_max_price,
# even if the live Amazon price moved since the request.
```

#### Reject a fulfillment
```bash
curl -X POST http://localhost:8000/orders/ORDER-ID/reject-fulfillment \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"reason": "price no longer viable", "approver": "you@example.com"}'
```

#### Update order status
```bash
curl -X PATCH http://localhost:8000/orders/ORDER-ID/status \
  -H "Content-Type: application/json" \
  -d '{"status": "fulfilled", "tracking_number": "TRK123456", "note": "Royal Mail"}'
# status: fulfilled | failed | refunded
# If fulfilled + tracking_number: calls eBay shipping_fulfillment API
```

#### Get all orders
```bash
curl "http://localhost:8000/orders?status=pending"
# status: pending | fulfillment_triggered | fulfilled | failed | refunded
```

**Queue file:** `/root/arbitrage-api/fulfillment_queue.json` — read/written directly
by both the API and `fulfillment_bot.py` via `fulfillment_gate.py` (file-locked,
not an HTTP call), so both services must agree on `FULFILLMENT_QUEUE`.

**Fulfillment automation flow (human approval required — nothing auto-buys):**
```
eBay sale occurs
  → cron: GET /orders/pending (every 15 min)
  → POST /orders/{id}/fulfill              → fulfillment_gate: pending_review
  → a human reviews GET /fulfillment/pending
  → POST /orders/{id}/approve-fulfillment  → fulfillment_gate: approved (with a $ ceiling)
      (or reject-fulfillment → rejected, nothing further happens)
  → fulfillment_bot.py calls claim_next_approved() every POLL_INTERVAL
  → Logs into Amazon, adds to cart, reaches checkout review
  → Right before "Place Order": reads the LIVE order total and refuses to
    buy if it exceeds the approved ceiling (in addition to DRY_RUN and the
    existing price-drift check — neither is bypassed by this)
  → On success: PATCH /orders/{id}/status (ships tracking to eBay) AND
    fulfillment_gate.mark_result(success=True, actual_price=...)
  → On any failure: PATCH /orders/{id}/status {"status":"failed",...} AND
    mark_result(success=False, error=...)
```

---

### Module 6 — Learning Loop Logger

#### Log a custom event
```bash
curl -X POST http://localhost:8000/log/event \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "sale",
    "listing_id": "LISTING-001",
    "order_id": "ORDER-001",
    "detail": "Sold Casio watch",
    "metadata": {"sale_price": 33.83, "margin": 0.22}
  }'
# Valid event_type: sale | impression | ban | price_break | fulfillment_error |
#   listing_created | listing_paused | listing_deleted | fulfillment_triggered |
#   margin_scan | api_error
```

#### 24-hour summary
```bash
curl http://localhost:8000/log/summary
# {"period": "last_24_hours", "events": {...}, "totals": {...}}
```

#### Feedback / intelligence report (7-day window)
```bash
curl http://localhost:8000/log/feedback
# Returns ban_patterns, margin_performance, fulfillment_health, recommendations[]
```

#### Raw event log with pagination
```bash
curl "http://localhost:8000/log/events?event_type=sale&limit=20&offset=0"
```

#### Cleanup old events (keep SQLite small on VPS)
```bash
curl -X DELETE http://localhost:8000/log/events \
  -H "Content-Type: application/json" \
  -d '{"older_than_days": 30}'
# {"deleted_count": 45, "oldest_remaining": "2026-05-16T00:00:00"}
```

---

## Running as a systemd Service

The service auto-restarts on crash and starts on VPS reboot.

`/etc/systemd/system/arbitrage-api.service`:
```ini
[Unit]
Description=eBay Arbitrage API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/arbitrage-api
ExecStart=/root/arbitrage-api/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
EnvironmentFile=/root/arbitrage-api/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable arbitrage-api
systemctl start arbitrage-api
systemctl status arbitrage-api

# Live logs
journalctl -u arbitrage-api -f
```

**Important:** whatever service runs `fulfillment_bot.py` must load the *same*
`.env` (`EnvironmentFile=/root/arbitrage-api/.env`) as the API service above.
`ABSOLUTE_MAX_ORDER`, `DAILY_SPEND_CAP`, and especially `FULFILLMENT_QUEUE`
have to match on both sides — they share `fulfillment_queue.json` directly via
a file lock, not over HTTP, so a mismatched path means the bot and the API are
silently looking at two different queues.

## Cron: Poll pending orders every 15 minutes

```bash
(crontab -l 2>/dev/null; echo "*/15 * * * * curl -s http://localhost:8000/orders/pending >> /root/arbitrage-api/cron.log 2>&1") | crontab -
```

---

## Database

SQLite file: `/root/arbitrage-api/arbitrage.db`

Tables: `tokens`, `competitor_listings`, `listings`, `orders`, `event_log`

Schema migrations run automatically on startup. To keep the file small, run the cleanup endpoint monthly.
