import asyncio
import base64
import os
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

_OAUTH_URLS = {
    "sandbox": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
    "production": "https://api.ebay.com/identity/v1/oauth2/token",
}

_API_BASE = {
    "sandbox": "https://api.sandbox.ebay.com",
    "production": "https://api.ebay.com",
}


def _basic_auth(client_id: str, client_secret: str) -> str:
    return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


async def fetch_token(client_id: str, client_secret: str) -> dict:
    env = os.getenv("EBAY_ENV", "sandbox")
    url = _OAUTH_URLS[env]
    headers = {
        "Authorization": f"Basic {_basic_auth(client_id, client_secret)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, data=data)
        resp.raise_for_status()
        return resp.json()


async def _call(
    method: str,
    path: str,
    token: str,
    *,
    params: dict | None = None,
    json_body: Any = None,
    db: Session | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    _retry: bool = True,
) -> dict:
    env = os.getenv("EBAY_ENV", "sandbox")
    marketplace = os.getenv("EBAY_MARKETPLACE", "EBAY_GB")
    base = _API_BASE[env]

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method,
            f"{base}{path}",
            headers=headers,
            params=params,
            json=json_body,
        )

    if resp.status_code == 429 and _retry:
        await asyncio.sleep(2)
        return await _call(
            method, path, token,
            params=params, json_body=json_body,
            db=db, client_id=client_id, client_secret=client_secret,
            _retry=False,
        )

    if resp.status_code == 401 and _retry and db and client_id and client_secret:
        token_data = await fetch_token(client_id, client_secret)
        new_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 7200)

        from models import Token
        db_token = db.query(Token).filter(Token.client_id == client_id).first()
        if db_token:
            db_token.access_token = new_token
            db_token.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
            db.commit()

        return await _call(
            method, path, new_token,
            params=params, json_body=json_body,
            db=db, client_id=client_id, client_secret=client_secret,
            _retry=False,
        )

    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"eBay API {resp.status_code}: {resp.text}",
            request=resp.request,
            response=resp,
        )

    if not resp.content:
        return {}
    return resp.json()


async def ebay_get(
    path: str, token: str, params: dict | None = None, **kwargs
) -> dict:
    return await _call("GET", path, token, params=params, **kwargs)


async def ebay_post(
    path: str, token: str, json_body: Any = None, **kwargs
) -> dict:
    return await _call("POST", path, token, json_body=json_body, **kwargs)


async def ebay_delete(path: str, token: str, **kwargs) -> dict:
    return await _call("DELETE", path, token, **kwargs)


async def get_account_policies(token: str, marketplace: str = "EBAY_GB") -> dict:
    """Fetch the first fulfillment, payment, and return policy IDs from the seller account."""
    env = os.getenv("EBAY_ENV", "sandbox")
    base = _API_BASE[env]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    policies: dict = {}
    for policy_type, key in [
        ("fulfillment_policy", "fulfillmentPolicies"),
        ("payment_policy", "paymentPolicies"),
        ("return_policy", "returnPolicies"),
    ]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base}/sell/account/v1/{policy_type}",
                headers=headers,
                params={"marketplace_id": marketplace},
            )
        if resp.status_code == 200:
            items = resp.json().get(key, [])
            if items:
                id_field = policy_type.replace("_policy", "") + "PolicyId"
                # camelCase: fulfillmentPolicyId, paymentPolicyId, returnPolicyId
                id_field = (
                    "fulfillmentPolicyId" if "fulfillment" in policy_type
                    else "paymentPolicyId" if "payment" in policy_type
                    else "returnPolicyId"
                )
                policies[id_field] = items[0][id_field]
    return policies


async def create_inventory_item(
    token: str,
    sku: str,
    title: str,
    quantity: int,
    condition: str = "NEW",
    image_url: str = "",
    description: str = "",
) -> None:
    """PUT /sell/inventory/v1/inventory_item/{sku} — 204 No Content on success."""
    env = os.getenv("EBAY_ENV", "sandbox")
    base = _API_BASE[env]
    marketplace = os.getenv("EBAY_MARKETPLACE", "EBAY_GB")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
        "Content-Language": "en-GB",
    }
    payload = {
        "availability": {
            "shipToLocationAvailability": {"quantity": quantity}
        },
        "condition": condition,
        "product": {
            "title": title,
            "description": description or title,
            "imageUrls": [image_url] if image_url else [],
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{base}/sell/inventory/v1/inventory_item/{sku}",
            headers=headers,
            json=payload,
        )
    if resp.status_code not in (200, 201, 204):
        raise httpx.HTTPStatusError(
            f"create_inventory_item {resp.status_code}: {resp.text}",
            request=resp.request,
            response=resp,
        )


async def create_offer(
    token: str,
    sku: str,
    price: float,
    category_id: str,
    marketplace: str,
    fulfillment_policy_id: str,
    payment_policy_id: str,
    return_policy_id: str,
    quantity: int = 1,
    description: str = "",
) -> str:
    """POST /sell/inventory/v1/offer — returns offerId."""
    env = os.getenv("EBAY_ENV", "sandbox")
    base = _API_BASE[env]
    currency = "GBP" if "GB" in marketplace else "USD"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
        "Content-Language": "en-GB",
    }
    payload: dict = {
        "sku": sku,
        "marketplaceId": marketplace,
        "format": "FIXED_PRICE",
        "availableQuantity": quantity,
        "categoryId": category_id,
        "listingDescription": description or f"Quality item — {sku}",
        "pricingSummary": {
            "price": {"currency": currency, "value": f"{price:.2f}"}
        },
    }
    if fulfillment_policy_id:
        payload["fulfillmentPolicyId"] = fulfillment_policy_id
    if payment_policy_id:
        payload["paymentPolicyId"] = payment_policy_id
    if return_policy_id:
        payload["returnPolicyId"] = return_policy_id

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/sell/inventory/v1/offer",
            headers=headers,
            json=payload,
        )
    if resp.status_code not in (200, 201):
        raise httpx.HTTPStatusError(
            f"create_offer {resp.status_code}: {resp.text}",
            request=resp.request,
            response=resp,
        )
    return resp.json()["offerId"]


async def publish_offer(token: str, offer_id: str) -> str:
    """POST /sell/inventory/v1/offer/{offerId}/publish — returns listingId."""
    env = os.getenv("EBAY_ENV", "sandbox")
    base = _API_BASE[env]
    marketplace = os.getenv("EBAY_MARKETPLACE", "EBAY_GB")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/sell/inventory/v1/offer/{offer_id}/publish",
            headers=headers,
        )
    if resp.status_code not in (200, 201):
        raise httpx.HTTPStatusError(
            f"publish_offer {resp.status_code}: {resp.text}",
            request=resp.request,
            response=resp,
        )
    return resp.json().get("listingId", "")


async def withdraw_offer(token: str, offer_id: str) -> None:
    """POST /sell/inventory/v1/offer/{offerId}/withdraw — ends the listing."""
    env = os.getenv("EBAY_ENV", "sandbox")
    base = _API_BASE[env]
    marketplace = os.getenv("EBAY_MARKETPLACE", "EBAY_GB")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/sell/inventory/v1/offer/{offer_id}/withdraw",
            headers=headers,
        )
    if resp.status_code not in (200, 204):
        raise httpx.HTTPStatusError(
            f"withdraw_offer {resp.status_code}: {resp.text}",
            request=resp.request,
            response=resp,
        )


async def get_pending_orders(token: str, marketplace: str = "EBAY_GB") -> list[dict]:
    """GET /sell/fulfillment/v1/order with filter for NOT_STARTED|IN_PROGRESS orders."""
    env = os.getenv("EBAY_ENV", "sandbox")
    base = _API_BASE[env]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{base}/sell/fulfillment/v1/order",
            headers=headers,
            params={"filter": "orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}", "limit": "50"},
        )
    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Fulfillment API {resp.status_code}: {resp.text}",
            request=resp.request,
            response=resp,
        )
    orders = []
    for order in resp.json().get("orders", []):
        instrs = order.get("fulfillmentStartInstructions", [{}])
        ship_to = (instrs[0].get("shippingStep", {}) if instrs else {}).get("shipTo", {})
        contact = ship_to.get("contactAddress", {})
        line_items = order.get("lineItems", [{}])
        item = line_items[0] if line_items else {}
        orders.append({
            "order_id": order.get("orderId", ""),
            "buyer_username": order.get("buyer", {}).get("username", ""),
            "buyer_name": ship_to.get("fullName", ""),
            "shipping_address": {
                "name": ship_to.get("fullName", ""),
                "line1": contact.get("addressLine1", ""),
                "city": contact.get("city", ""),
                "postcode": contact.get("postalCode", ""),
                "country": contact.get("countryCode", ""),
            },
            "item_title": item.get("title", ""),
            "sku": item.get("sku", ""),
            "line_item_id": item.get("lineItemId", ""),
            "quantity": item.get("quantity", 1),
            "sale_price": float((item.get("lineItemCost") or {}).get("value", 0)),
        })
    return orders


async def add_shipping_fulfillment(
    token: str,
    order_id: str,
    line_item_id: str,
    quantity: int,
    tracking_number: str,
    carrier_code: str = "ROYALMAIL",
) -> dict:
    """POST /sell/fulfillment/v1/order/{orderId}/shipping_fulfillment — add tracking."""
    env = os.getenv("EBAY_ENV", "sandbox")
    base = _API_BASE[env]
    marketplace = os.getenv("EBAY_MARKETPLACE", "EBAY_GB")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    payload = {
        "lineItems": [{"lineItemId": line_item_id, "quantity": quantity}],
        "shippingCarrierCode": carrier_code,
        "trackingNumber": tracking_number,
        "shippedDate": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment",
            headers=headers,
            json=payload,
        )
    if resp.status_code not in (200, 201, 204):
        raise httpx.HTTPStatusError(
            f"add_shipping_fulfillment {resp.status_code}: {resp.text}",
            request=resp.request,
            response=resp,
        )
    return resp.json() if resp.content else {}


async def search_seller_listings(
    token: str,
    username: str,
    marketplace: str = "EBAY_GB",
) -> list[dict]:
    env = os.getenv("EBAY_ENV", "sandbox")
    base = _API_BASE[env]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": marketplace,
    }
    params = {
        "q": "a",
        "filter": f"sellers:{{{username}}}",
        "limit": "20",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{base}/buy/browse/v1/item_summary/search",
            headers=headers,
            params=params,
        )

    if resp.status_code == 429:
        await asyncio.sleep(2)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base}/buy/browse/v1/item_summary/search",
                headers=headers,
                params=params,
            )

    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"eBay Browse API {resp.status_code}: {resp.text}",
            request=resp.request,
            response=resp,
        )

    data = resp.json()
    items = []
    for item in data.get("itemSummaries", []):
        price_info = item.get("price", {})
        items.append({
            "item_id": item.get("itemId", ""),
            "title": item.get("title", ""),
            "price": float(price_info.get("value", 0)),
            "currency": price_info.get("currency", "GBP"),
            "condition": item.get("condition", ""),
            "image_url": (item.get("image") or {}).get("imageUrl", ""),
            "seller": (item.get("seller") or {}).get("username", username),
        })
    return items
