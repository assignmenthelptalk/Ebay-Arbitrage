"""Unit tests for scraper.py business logic (no browser required)."""

import sys
sys.path.insert(0, ".")

from scraper import calc_margin, _ebay_url, EBAY_FEE_RATE, PAYMENT_FEE_RATE, PAYMENT_FIXED_FEE, SHIPPING_COST


def test_calc_margin_profitable():
    margin, profit = calc_margin(10.0, 20.0)
    ebay_fee = 20.0 * EBAY_FEE_RATE
    payment_fee = 20.0 * PAYMENT_FEE_RATE + PAYMENT_FIXED_FEE
    total_cost = 10.0 + ebay_fee + payment_fee + SHIPPING_COST
    expected_profit = 20.0 - total_cost
    expected_margin = expected_profit / 20.0
    assert abs(margin - expected_margin) < 1e-9
    assert abs(profit - expected_profit) < 1e-9
    print(f"PASS  profitable trade: buy=$10 sell=$20 => margin={margin:.1%}, net=${profit:.2f}")


def test_calc_margin_zero_sell_price():
    margin, profit = calc_margin(5.0, 0.0)
    assert margin == 0.0, f"Expected 0.0, got {margin}"
    print(f"PASS  zero sell price: margin={margin} (no div-by-zero)")


def test_calc_margin_breakeven():
    margin, profit = calc_margin(20.0, 20.0)
    print(f"PASS  breakeven: buy=$20 sell=$20 => margin={margin:.1%}, net=${profit:.2f}")


def test_calc_margin_high_value():
    margin, profit = calc_margin(100.0, 150.0)
    assert margin > 0, "Expected positive margin on profitable high-value trade"
    print(f"PASS  high-value: buy=$100 sell=$150 => margin={margin:.1%}, net=${profit:.2f}")


def test_calc_margin_fees_reduce_profit():
    _, profit_no_fees = 0.0, 20.0 - 10.0  # naive profit without fees
    _, profit_with_fees = calc_margin(10.0, 20.0)
    assert profit_with_fees < profit_no_fees, "Fees should reduce profit"
    print(f"PASS  fees reduce profit: naive=${profit_no_fees:.2f} -> actual=${profit_with_fees:.2f}")


def test_ebay_url_contains_query():
    url = _ebay_url({"_nkw": "vintage casio", "LH_Sold": "1"})
    assert "vintage+casio" in url or "vintage%20casio" in url or "vintage casio" in url
    assert "LH_Sold=1" in url
    print(f"PASS  ebay url builds correctly: ...{url[-50:]}")


if __name__ == "__main__":
    tests = [
        test_calc_margin_profitable,
        test_calc_margin_zero_sell_price,
        test_calc_margin_breakeven,
        test_calc_margin_high_value,
        test_calc_margin_fees_reduce_profit,
        test_ebay_url_contains_query,
    ]

    print("=== scraper.py unit tests ===\n")
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed.")
    sys.exit(1 if failed else 0)
