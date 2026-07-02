from dataclasses import dataclass

EBAY_FEE_PCT = 0.1325
PAYMENT_FEE_PCT = 0.0299
PAYMENT_FIXED_FEE = 0.49
SHIPPING_COST = 4.50
TARGET_MARGIN_PCT = 0.20
MAX_MARKUP = 3.0


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
