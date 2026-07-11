import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import CompetitorListingSnapshot, CompetitorScan
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


# --- normalize_product_key ---


def test_normalize_product_key_ignores_case_and_punctuation():
    a = sig.normalize_product_key("Apple iPhone 7 - 32GB (Unlocked)!!")
    b = sig.normalize_product_key("apple iphone 7 32gb unlocked")
    assert a == b


def test_normalize_product_key_collapses_whitespace_runs():
    assert sig.normalize_product_key("Widget   X\t\n Pro") == sig.normalize_product_key("Widget X Pro")


def test_normalize_product_key_relisted_item_still_matches():
    # Same product, relisted with a slightly different punctuation/emoji title
    # (the item_id changes on relist, but the key must not).
    original = sig.normalize_product_key("Vintage Camera - Leather Case Included")
    relisted = sig.normalize_product_key("VINTAGE CAMERA!! Leather Case Included ✨")
    assert original == relisted


def test_normalize_product_key_different_products_do_not_collide():
    a = sig.normalize_product_key("Apple iPhone 7 32GB")
    b = sig.normalize_product_key("Apple iPhone 8 32GB")
    assert a != b


def test_normalize_product_key_empty_title_is_empty_key():
    assert sig.normalize_product_key("") == ""
    assert sig.normalize_product_key(None) == ""


# --- find_prior_appearances (real temp DB, multi-scan fixtures) ---


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test_velocity_signals.db'}", connect_args={"check_same_thread": False}
    )
    TestSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


def _make_scan(db, seller):
    scan = CompetitorScan(seller_username=seller, marketplace="EBAY_US", listing_count=0)
    db.add(scan)
    db.commit()
    db.refresh(scan)
    return scan


def _make_snapshot(db, scan, seller, product_key, price, competing_sellers=None):
    row = CompetitorListingSnapshot(
        scan_id=scan.id,
        seller=seller,
        product_key=product_key,
        price=price,
        competing_sellers=competing_sellers,
    )
    db.add(row)
    db.commit()
    return row


def test_find_prior_appearances_empty_when_seller_has_no_history(db_session):
    scan1 = _make_scan(db_session, "sellerV")
    key = sig.normalize_product_key("Widget X")
    assert sig.find_prior_appearances(db_session, "sellerV", key, scan1.id) == []


def test_find_prior_appearances_returns_oldest_first(db_session):
    seller = "sellerV"
    key = sig.normalize_product_key("Widget X")

    scan1 = _make_scan(db_session, seller)
    _make_snapshot(db_session, scan1, seller, key, 40.0, competing_sellers=3)
    scan2 = _make_scan(db_session, seller)
    _make_snapshot(db_session, scan2, seller, key, 38.0, competing_sellers=4)
    scan3 = _make_scan(db_session, seller)  # current scan, not yet given a snapshot

    prior = sig.find_prior_appearances(db_session, seller, key, scan3.id)
    assert [p["scan_id"] for p in prior] == [scan1.id, scan2.id]
    assert prior[0]["price"] == 40.0
    assert prior[1]["price"] == 38.0
    assert prior[1]["competing_sellers"] == 4


def test_find_prior_appearances_survives_same_item_id_rescanned(db_session):
    # This is the bug this table exists to fix: CompetitorListing is upserted
    # in place per item_id, so a still-active listing rescanned with the SAME
    # item_id would otherwise lose its scan-1 data by the time scan 2 runs.
    # Snapshots are independent of that upsert, so history must survive here.
    seller = "sellerV"
    key = sig.normalize_product_key("Widget X")

    scan1 = _make_scan(db_session, seller)
    _make_snapshot(db_session, scan1, seller, key, 40.0, competing_sellers=3)
    scan2 = _make_scan(db_session, seller)

    prior = sig.find_prior_appearances(db_session, seller, key, scan2.id)
    assert len(prior) == 1
    assert prior[0]["scan_id"] == scan1.id
    assert prior[0]["price"] == 40.0


def test_find_prior_appearances_ignores_other_sellers_and_other_products(db_session):
    seller = "sellerV"
    key = sig.normalize_product_key("Widget X")

    scan1 = _make_scan(db_session, "otherSeller")
    _make_snapshot(db_session, scan1, "otherSeller", key, 40.0, competing_sellers=3)
    scan2 = _make_scan(db_session, seller)
    _make_snapshot(db_session, scan2, seller, sig.normalize_product_key("Widget Y"), 40.0, competing_sellers=3)
    scan3 = _make_scan(db_session, seller)

    assert sig.find_prior_appearances(db_session, seller, key, scan3.id) == []


# --- compute_velocity ---


def test_velocity_dormant_when_only_one_scan_exists():
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 10.0, "competing_sellers": 2}, [], total_seller_scans=1
    )
    assert result["confidence"] == "dormant"
    assert result["level"] is None
    assert result["presence"] is None


def test_velocity_new_first_seen_when_product_has_no_prior_match_but_history_exists():
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 10.0, "competing_sellers": 2}, [], total_seller_scans=3
    )
    assert result["confidence"] == "new"
    assert result["presence"]["seen"] == 1
    assert result["presence"]["total"] == 3
    assert result["seller_velocity"] is None
    assert result["price_velocity"] is None


def test_velocity_presence_persistent_when_seen_in_every_scan():
    prior = [
        {"scan_id": 1, "price": 40.0, "competing_sellers": 3},
        {"scan_id": 2, "price": 40.0, "competing_sellers": 3},
    ]
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 40.0, "competing_sellers": 3}, prior, total_seller_scans=3
    )
    assert result["presence"]["seen"] == 3
    assert result["presence"]["total"] == 3
    assert result["presence"]["label"] == "persistent"
    assert result["level"] == "persistent"


def test_velocity_presence_transient_when_rarely_seen():
    # Appeared once before, out of 6 total scans (2 of 6 = transient band).
    prior = [{"scan_id": 1, "price": 40.0, "competing_sellers": 3}]
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 40.0, "competing_sellers": 3}, prior, total_seller_scans=6
    )
    assert result["presence"]["label"] == "transient"
    assert result["level"] == "weak"


def test_velocity_seller_count_rising():
    prior = [{"scan_id": 1, "price": 40.0, "competing_sellers": 3}]
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 40.0, "competing_sellers": 5}, prior, total_seller_scans=2
    )
    assert result["seller_velocity"] == {"from": 3, "to": 5, "delta": 2, "trend": "rising"}


def test_velocity_seller_count_falling():
    prior = [{"scan_id": 1, "price": 40.0, "competing_sellers": 5}]
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 40.0, "competing_sellers": 3}, prior, total_seller_scans=2
    )
    assert result["seller_velocity"] == {"from": 5, "to": 3, "delta": -2, "trend": "falling"}


def test_velocity_price_falling_flags_erosion():
    prior = [{"scan_id": 1, "price": 40.0, "competing_sellers": 3}]
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 36.0, "competing_sellers": 3}, prior, total_seller_scans=2
    )
    assert result["price_velocity"]["trend"] == "falling"
    assert result["price_velocity"]["delta"] == -4.0


def test_velocity_price_flat_when_unchanged():
    prior = [{"scan_id": 1, "price": 40.0, "competing_sellers": 3}]
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 40.0, "competing_sellers": 3}, prior, total_seller_scans=2
    )
    assert result["price_velocity"]["trend"] == "flat"
    assert result["price_velocity"]["delta"] == 0.0


def test_velocity_confidence_scales_with_scan_count():
    prior_2 = [{"scan_id": 1, "price": 40.0, "competing_sellers": 3}]
    assert sig.compute_velocity({"product_key": "k", "price": 40.0}, prior_2, total_seller_scans=2)["confidence"] == "low"

    prior_4 = [{"scan_id": i, "price": 40.0, "competing_sellers": 3} for i in range(1, 4)]
    assert sig.compute_velocity({"product_key": "k", "price": 40.0}, prior_4, total_seller_scans=4)["confidence"] == "med"

    prior_5 = [{"scan_id": i, "price": 40.0, "competing_sellers": 3} for i in range(1, 5)]
    assert sig.compute_velocity({"product_key": "k", "price": 40.0}, prior_5, total_seller_scans=5)["confidence"] == "high"


def test_velocity_persistent_with_rising_sellers_is_heating():
    prior = [{"scan_id": 1, "price": 40.0, "competing_sellers": 3}]
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 40.0, "competing_sellers": 5}, prior, total_seller_scans=2
    )
    assert result["presence"]["label"] == "persistent"  # seen 2 of 2
    assert result["level"] == "heating"


def test_velocity_persistent_with_falling_price_is_eroding():
    prior = [{"scan_id": 1, "price": 40.0, "competing_sellers": 3}]
    result = sig.compute_velocity(
        {"product_key": "widget x", "price": 36.0, "competing_sellers": 3}, prior, total_seller_scans=2
    )
    assert result["presence"]["label"] == "persistent"
    assert result["level"] == "eroding"


def test_velocity_never_fabricates_a_trend_without_two_data_points():
    result = sig.compute_velocity({"product_key": "widget x", "price": 40.0}, [], total_seller_scans=1)
    assert result["level"] is None
    for scenario in (
        sig.compute_velocity({"product_key": "widget x", "price": 40.0}, [], total_seller_scans=3),
    ):
        assert scenario["seller_velocity"] is None
        assert scenario["price_velocity"] is None
