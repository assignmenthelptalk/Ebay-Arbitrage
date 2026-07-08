import os
from dataclasses import dataclass

from pydantic import BaseModel

EBAY_FEE_PCT = float(os.getenv("EBAY_FEE_PCT", "0.1325"))
PAYMENT_FEE_PCT = 0.0299
PAYMENT_FIXED_FEE = 0.49
SHIPPING_COST = 4.50
TARGET_MARGIN_PCT = 0.20
MAX_MARKUP = 3.0

# Full-cost calculator config (§4A.2) — see .env.example for placeholder notes.
PROMOTED_LISTINGS_PCT = float(os.getenv("PROMOTED_LISTINGS_PCT", "0.02"))
PAYMENT_FX_PCT = float(os.getenv("PAYMENT_FX_PCT", "0.02"))
EXPECTED_RETURN_RATE = float(os.getenv("EXPECTED_RETURN_RATE", "0.05"))
RETURN_SHIPPING_LOSS = float(os.getenv("RETURN_SHIPPING_LOSS", "8.00"))
MIN_NET_MARGIN_PCT = float(os.getenv("MIN_NET_MARGIN_PCT", "0.20"))
MIN_NET_PROFIT_ABS = float(os.getenv("MIN_NET_PROFIT_ABS", "5.00"))


@dataclass
class MarginResult:
    amazon_cost: float
    ebay_fees: float
    payment_fees: float
    shipping_cost: float
    total_cost: float
    minimum_list_price: float
    target_profit: float
    margin_rate: float
    viable: bool

    def to_dict(self) -> dict:
        return {
            "amazon_cost": round(self.amazon_cost, 2),
            "ebay_fees": round(self.ebay_fees, 2),
            "payment_fees": round(self.payment_fees, 2),
            "shipping_cost": round(self.shipping_cost, 2),
            "total_cost": round(self.total_cost, 2),
            "minimum_list_price": round(self.minimum_list_price, 2),
            "target_profit": round(self.target_profit, 2),
            "margin_rate": round(self.margin_rate, 4),
            "viable": self.viable,
        }


def calculate(
    amazon_price: float,
    *,
    ebay_fee_pct: float = EBAY_FEE_PCT,
    payment_fee_pct: float = PAYMENT_FEE_PCT,
    payment_fixed_fee: float = PAYMENT_FIXED_FEE,
    shipping_cost: float = SHIPPING_COST,
    target_margin_pct: float = TARGET_MARGIN_PCT,
) -> MarginResult:
    ebay_fees = amazon_price * ebay_fee_pct
    payment_fees = amazon_price * payment_fee_pct + payment_fixed_fee
    total_cost = amazon_price + ebay_fees + payment_fees + shipping_cost

    # Solve for the sell price that yields target_margin_pct net margin:
    # (sell - total_cost) / sell = target_margin_pct  →  sell = total_cost / (1 - target_margin_pct)
    minimum_list_price = total_cost / (1 - target_margin_pct)
    target_profit = minimum_list_price - total_cost
    margin_rate = target_profit / minimum_list_price if minimum_list_price > 0 else 0.0
    viable = minimum_list_price < amazon_price * MAX_MARKUP

    return MarginResult(
        amazon_cost=amazon_price,
        ebay_fees=ebay_fees,
        payment_fees=payment_fees,
        shipping_cost=shipping_cost,
        total_cost=total_cost,
        minimum_list_price=minimum_list_price,
        target_profit=target_profit,
        margin_rate=margin_rate,
        viable=viable,
    )


class MarginCalcResult(BaseModel):
    """Full-cost margin breakdown for one (sale_price, amazon_cost) pair.

    Field names mirror the planned `margin_calc` DB table (§5) so persisting
    this later is a straight column mapping — no DB writes happen here.
    """

    sale_price: float
    amazon_cost: float

    ebay_fee_pct: float
    promoted_listings_pct: float
    payment_fx_pct: float
    expected_return_rate: float
    return_shipping_loss: float
    min_net_margin_pct: float
    min_net_profit_abs: float

    ebay_fee: float
    ads_fee: float
    fx_fee: float
    returns_cost: float

    net_profit: float
    margin_pct: float

    passed: bool
    fail_reasons: list[str]
    reason: str


def evaluate_margin(
    sale_price: float,
    amazon_cost: float,
    *,
    ebay_fee_pct: float = EBAY_FEE_PCT,
    promoted_listings_pct: float = PROMOTED_LISTINGS_PCT,
    payment_fx_pct: float = PAYMENT_FX_PCT,
    expected_return_rate: float = EXPECTED_RETURN_RATE,
    return_shipping_loss: float = RETURN_SHIPPING_LOSS,
    min_net_margin_pct: float = MIN_NET_MARGIN_PCT,
    min_net_profit_abs: float = MIN_NET_PROFIT_ABS,
) -> MarginCalcResult:
    """Pure full-cost margin gate for one product. No I/O, no network, no DB.

    net_profit = sale_price - amazon_cost - ebay_fee - ads_fee - fx_fee - returns_cost
    margin_pct = net_profit / sale_price
    passed     = margin_pct >= min_net_margin_pct AND net_profit >= min_net_profit_abs
    """
    config = dict(
        ebay_fee_pct=ebay_fee_pct,
        promoted_listings_pct=promoted_listings_pct,
        payment_fx_pct=payment_fx_pct,
        expected_return_rate=expected_return_rate,
        return_shipping_loss=return_shipping_loss,
        min_net_margin_pct=min_net_margin_pct,
        min_net_profit_abs=min_net_profit_abs,
    )

    if sale_price <= 0:
        return MarginCalcResult(
            sale_price=sale_price,
            amazon_cost=amazon_cost,
            **config,
            ebay_fee=0.0,
            ads_fee=0.0,
            fx_fee=0.0,
            returns_cost=0.0,
            net_profit=0.0,
            margin_pct=0.0,
            passed=False,
            fail_reasons=["sale_price must be greater than 0"],
            reason="sale_price must be greater than 0",
        )

    ebay_fee = sale_price * ebay_fee_pct
    ads_fee = sale_price * promoted_listings_pct
    fx_fee = sale_price * payment_fx_pct
    returns_cost = expected_return_rate * (amazon_cost + return_shipping_loss)

    net_profit = sale_price - amazon_cost - ebay_fee - ads_fee - fx_fee - returns_cost
    margin_pct = net_profit / sale_price

    fail_reasons = []
    if margin_pct < min_net_margin_pct:
        fail_reasons.append(
            f"margin_pct {margin_pct:.4f} below MIN_NET_MARGIN_PCT {min_net_margin_pct:.4f}"
        )
    if net_profit < min_net_profit_abs:
        fail_reasons.append(
            f"net_profit {net_profit:.2f} below MIN_NET_PROFIT_ABS {min_net_profit_abs:.2f}"
        )

    passed = not fail_reasons
    reason = "pass" if passed else "; ".join(fail_reasons)

    return MarginCalcResult(
        sale_price=sale_price,
        amazon_cost=amazon_cost,
        **config,
        ebay_fee=ebay_fee,
        ads_fee=ads_fee,
        fx_fee=fx_fee,
        returns_cost=returns_cost,
        net_profit=net_profit,
        margin_pct=margin_pct,
        passed=passed,
        fail_reasons=fail_reasons,
        reason=reason,
    )
