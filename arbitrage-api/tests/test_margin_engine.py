import pytest

from services import margin_engine


def test_clean_profitable_product_passes_with_exact_numbers():
    # sale=50, cost=20, all default rates (ebay=0.1325, ads=0.02, fx=0.02, return_rate=0.05, return_loss=8)
    # ebay_fee=6.625, ads_fee=1.0, fx_fee=1.0, returns_cost=0.05*(20+8)=1.4
    # net_profit=50-20-6.625-1.0-1.0-1.4=19.975, margin_pct=19.975/50=0.3995
    result = margin_engine.evaluate_margin(50.0, 20.0)

    assert result.sale_price == 50.0
    assert result.amazon_cost == 20.0
    assert result.ebay_fee == pytest.approx(6.625)
    assert result.ads_fee == pytest.approx(1.0)
    assert result.fx_fee == pytest.approx(1.0)
    assert result.returns_cost == pytest.approx(1.4)
    assert result.net_profit == pytest.approx(19.975)
    assert result.margin_pct == pytest.approx(0.3995)
    assert result.passed is True
    assert result.fail_reasons == []


def test_fails_min_net_profit_abs_but_passes_margin_pct():
    # sale=10, cost=5: net_profit=2.625, margin_pct=0.2625 (>=0.15) but profit < 3.00 default floor
    result = margin_engine.evaluate_margin(10.0, 5.0)

    assert result.net_profit == pytest.approx(2.625)
    assert result.margin_pct == pytest.approx(0.2625)
    assert result.passed is False
    assert len(result.fail_reasons) == 1
    assert "MIN_NET_PROFIT_ABS" in result.fail_reasons[0]
    assert not any("MIN_NET_MARGIN_PCT" in r for r in result.fail_reasons)


def test_fails_min_net_margin_pct_but_passes_abs_profit():
    # sale=70, cost=50: net_profit=5.025 (>=3.00) but margin_pct=0.0718 (<0.15 default floor)
    result = margin_engine.evaluate_margin(70.0, 50.0)

    assert result.net_profit == pytest.approx(5.025)
    assert result.margin_pct == pytest.approx(5.025 / 70)
    assert result.passed is False
    assert len(result.fail_reasons) == 1
    assert "MIN_NET_MARGIN_PCT" in result.fail_reasons[0]
    assert not any("MIN_NET_PROFIT_ABS" in r for r in result.fail_reasons)


def test_boundary_margin_pct_at_threshold_passes():
    # Zero out every other fee/cost so net_profit = sale_price - amazon_cost exactly.
    result = margin_engine.evaluate_margin(
        100.0, 80.0,
        ebay_fee_pct=0.0, promoted_listings_pct=0.0, payment_fx_pct=0.0,
        expected_return_rate=0.0, return_shipping_loss=0.0,
        min_net_margin_pct=0.20, min_net_profit_abs=0.0,
    )
    assert result.net_profit == pytest.approx(20.0)
    assert result.margin_pct == pytest.approx(0.20)
    assert result.passed is True  # exactly at threshold must pass (>=, not >)


def test_boundary_margin_pct_just_below_threshold_fails():
    result = margin_engine.evaluate_margin(
        100.0, 80.01,
        ebay_fee_pct=0.0, promoted_listings_pct=0.0, payment_fx_pct=0.0,
        expected_return_rate=0.0, return_shipping_loss=0.0,
        min_net_margin_pct=0.20, min_net_profit_abs=0.0,
    )
    assert result.margin_pct < 0.20
    assert result.passed is False
    assert "MIN_NET_MARGIN_PCT" in result.fail_reasons[0]


def test_boundary_net_profit_abs_at_threshold_passes():
    result = margin_engine.evaluate_margin(
        100.0, 80.0,
        ebay_fee_pct=0.0, promoted_listings_pct=0.0, payment_fx_pct=0.0,
        expected_return_rate=0.0, return_shipping_loss=0.0,
        min_net_margin_pct=0.0, min_net_profit_abs=20.0,
    )
    assert result.net_profit == pytest.approx(20.0)
    assert result.passed is True  # exactly at threshold must pass (>=, not >)


def test_boundary_net_profit_abs_just_below_threshold_fails():
    result = margin_engine.evaluate_margin(
        100.0, 80.01,
        ebay_fee_pct=0.0, promoted_listings_pct=0.0, payment_fx_pct=0.0,
        expected_return_rate=0.0, return_shipping_loss=0.0,
        min_net_margin_pct=0.0, min_net_profit_abs=20.0,
    )
    assert result.net_profit < 20.0
    assert result.passed is False
    assert "MIN_NET_PROFIT_ABS" in result.fail_reasons[0]


def test_high_expected_return_rate_wipes_margin():
    # Same 50/20 base as the clean-product case, but a 90% return rate blows out returns_cost.
    result = margin_engine.evaluate_margin(50.0, 20.0, expected_return_rate=0.9)

    assert result.returns_cost == pytest.approx(0.9 * (20.0 + 8.0))
    assert result.net_profit < 0
    assert result.passed is False
    assert len(result.fail_reasons) == 2  # fails both margin% and abs profit


def test_sale_price_below_amazon_cost_is_negative_no_crash():
    result = margin_engine.evaluate_margin(20.0, 50.0)

    assert result.net_profit < 0
    assert result.margin_pct < 0
    assert result.passed is False
    assert len(result.fail_reasons) == 2


def test_sale_price_zero_is_guarded_no_zero_division():
    result = margin_engine.evaluate_margin(0.0, 20.0)

    assert result.margin_pct == 0.0
    assert result.net_profit == 0.0
    assert result.passed is False
    assert result.fail_reasons == ["sale_price must be greater than 0"]
    assert result.reason == "sale_price must be greater than 0"


def test_sale_price_negative_is_also_guarded():
    result = margin_engine.evaluate_margin(-5.0, 20.0)

    assert result.passed is False
    assert result.fail_reasons == ["sale_price must be greater than 0"]


def test_config_override_zero_ads_and_fx_matches_hand_computed_math():
    # sale=50, cost=20, ads/fx zeroed out, ebay_fee and returns_cost at defaults.
    # net_profit=50-20-6.625-0-0-1.4=21.975, margin_pct=21.975/50=0.4395
    result = margin_engine.evaluate_margin(
        50.0, 20.0, promoted_listings_pct=0.0, payment_fx_pct=0.0,
    )

    assert result.ads_fee == 0.0
    assert result.fx_fee == 0.0
    assert result.ebay_fee == pytest.approx(6.625)
    assert result.returns_cost == pytest.approx(1.4)
    assert result.net_profit == pytest.approx(21.975)
    assert result.margin_pct == pytest.approx(0.4395)
    assert result.passed is True
