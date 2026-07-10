from services import competitor_signals as sig


# --- compute_saturation ---


def test_saturation_green_for_few_competing_sellers():
    result = sig.compute_saturation(2, 8.0, 10.0, 4.0)
    assert result["level"] == "green"
    assert result["competing_sellers"] == 2
    assert "green" in result["reason"]


def test_saturation_yellow_for_moderate_competing_sellers():
    result = sig.compute_saturation(5, 8.0, 10.0, 4.0)
    assert result["level"] == "yellow"


def test_saturation_red_for_many_competing_sellers():
    result = sig.compute_saturation(20, 8.0, 10.0, 4.0)
    assert result["level"] == "red"


def test_saturation_defaults_to_cautious_yellow_when_data_missing():
    result = sig.compute_saturation(None, None, None, None)
    assert result["level"] == "yellow"
    assert result["competing_sellers"] is None
    assert "NOT a verified reading" in result["reason"]


# --- compute_demand ---


def test_demand_confidence_high_when_both_signals_available():
    result = sig.compute_demand(watch_count=30, competing_sellers=6)
    assert result["confidence"] == "high"
    assert result["level"] == "high"


def test_demand_confidence_med_when_only_competing_sellers_available():
    result = sig.compute_demand(watch_count=None, competing_sellers=6)
    assert result["confidence"] == "med"
    assert result["level"] == "high"
    assert result["components"] == {"watch_count": None, "competing_sellers": 6}


def test_demand_confidence_low_when_no_signals_available():
    result = sig.compute_demand(watch_count=None, competing_sellers=None)
    assert result["confidence"] == "low"
    assert result["level"] == "low"
    assert "Not measured demand" in result["reason"]


def test_demand_never_crashes_on_missing_watch_count():
    # watch_count is always None in layer 1 (Browse search doesn't return it)
    # — this must degrade to the competing_sellers proxy, not raise.
    result = sig.compute_demand(watch_count=None, competing_sellers=0)
    assert result["level"] == "low"
    assert result["confidence"] == "med"


def test_demand_levels_from_competing_sellers_proxy():
    assert sig.compute_demand(None, 0)["level"] == "low"
    assert sig.compute_demand(None, 1)["level"] == "med"
    assert sig.compute_demand(None, 5)["level"] == "high"


def test_demand_never_claims_measured():
    for watch, sellers in [(None, None), (None, 3), (50, 3)]:
        result = sig.compute_demand(watch, sellers)
        assert "ESTIMATE" in result["reason"]
        assert "Not measured demand" in result["reason"]


# --- velocity_stub ---


def test_velocity_stub_is_clearly_marked_dormant():
    result = sig.velocity_stub()
    assert result["level"] is None
    assert result["signal"] == "dormant_pending_scan_history"
    assert "not computed" in result["note"].lower() or "dormant" in result["note"].lower()
