from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.rules import AlertEvent, AlertRule, evaluate

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_below_price_triggers_on_downward_crossing() -> None:
    # Kabul: given a below_price rule with threshold 2400 and price crossing from 2499.90 to 2350.00,
    # expect evaluate returns an AlertEvent with event_type=='below_price' and old/new values
    rule = AlertRule(
        rule_type="below_price",
        threshold_value=Decimal("2400.00"),
        cooldown_min=60,
        last_sent_at=None,
    )
    event = evaluate(
        rule,
        old_price=Decimal("2499.90"),
        new_price=Decimal("2350.00"),
        old_in_stock=True,
        new_in_stock=True,
        now=NOW,
    )
    assert isinstance(event, AlertEvent)
    assert event.event_type == "below_price"
    assert event.old_val == "2499.90"
    assert event.new_val == "2350.00"
    assert event.message


def test_below_price_triggers_when_old_equals_threshold() -> None:
    # Kabul: given old_price equal to threshold (2400.00) and new_price below threshold (2399.99),
    # expect evaluate triggers below_price because old >= threshold and diff >= 0.01
    rule = AlertRule(rule_type="below_price", threshold_value=Decimal("2400.00"))
    event = evaluate(
        rule,
        old_price=Decimal("2400.00"),
        new_price=Decimal("2399.99"),
        old_in_stock=True,
        new_in_stock=True,
        now=NOW,
    )
    assert event is not None
    assert event.event_type == "below_price"


def test_below_price_does_not_trigger_when_already_below_threshold() -> None:
    # Kabul: given old_price (2350.00) already below threshold (2400.00) and new_price drops further to 2200.00,
    # expect evaluate returns None (no repeated alert while already below)
    rule = AlertRule(rule_type="below_price", threshold_value=Decimal("2400.00"))
    event = evaluate(
        rule,
        old_price=Decimal("2350.00"),
        new_price=Decimal("2200.00"),
        old_in_stock=True,
        new_in_stock=True,
        now=NOW,
    )
    assert event is None


def test_below_price_does_not_trigger_when_new_equals_threshold() -> None:
    # Kabul: given old_price 2500.00 and new_price equal to threshold 2400.00,
    # expect evaluate returns None because new_price is not strictly less than threshold
    rule = AlertRule(rule_type="below_price", threshold_value=Decimal("2400.00"))
    event = evaluate(
        rule,
        old_price=Decimal("2500.00"),
        new_price=Decimal("2400.00"),
        old_in_stock=True,
        new_in_stock=True,
        now=NOW,
    )
    assert event is None


def test_pct_drop_triggers_at_exact_threshold_and_above() -> None:
    # Kabul: given pct_drop threshold 5.00% and price dropping exactly 5.00% (1000.00 -> 950.00),
    # expect evaluate returns a pct_drop AlertEvent
    rule = AlertRule(rule_type="pct_drop", threshold_value=Decimal("5.00"))
    event = evaluate(
        rule,
        old_price=Decimal("1000.00"),
        new_price=Decimal("950.00"),
        old_in_stock=True,
        new_in_stock=True,
        now=NOW,
    )
    assert event is not None
    assert event.event_type == "pct_drop"
    assert event.old_val == "1000.00"
    assert event.new_val == "950.00"


def test_pct_drop_does_not_trigger_below_threshold() -> None:
    # Kabul: given pct_drop threshold 5.00% and price dropping 4.99% (1000.00 -> 950.10),
    # expect evaluate returns None
    rule = AlertRule(rule_type="pct_drop", threshold_value=Decimal("5.00"))
    event = evaluate(
        rule,
        old_price=Decimal("1000.00"),
        new_price=Decimal("950.10"),
        old_in_stock=True,
        new_in_stock=True,
        now=NOW,
    )
    assert event is None


def test_pct_drop_does_not_trigger_on_zero_old_price_or_increase() -> None:
    # Kabul: given old_price == 0 or price increase, expect pct_drop returns None
    rule = AlertRule(rule_type="pct_drop", threshold_value=Decimal("5.00"))
    assert (
        evaluate(
            rule,
            old_price=Decimal("0.00"),
            new_price=Decimal("0.00"),
            old_in_stock=True,
            new_in_stock=True,
            now=NOW,
        )
        is None
    )
    assert (
        evaluate(
            rule,
            old_price=Decimal("100.00"),
            new_price=Decimal("120.00"),
            old_in_stock=True,
            new_in_stock=True,
            now=NOW,
        )
        is None
    )


def test_stock_change_triggers_both_directions() -> None:
    # Kabul: given a stock_change rule with non-None old_price and stock flipping True->False or False->True,
    # expect evaluate returns stock_change AlertEvent in both directions
    rule = AlertRule(rule_type="stock_change", threshold_value=None)

    ev_out = evaluate(
        rule,
        old_price=Decimal("649.50"),
        new_price=Decimal("649.50"),
        old_in_stock=True,
        new_in_stock=False,
        now=NOW,
    )
    assert ev_out is not None
    assert ev_out.event_type == "stock_change"
    assert ev_out.old_val != ev_out.new_val

    ev_in = evaluate(
        rule,
        old_price=Decimal("649.50"),
        new_price=Decimal("649.50"),
        old_in_stock=False,
        new_in_stock=True,
        now=NOW,
    )
    assert ev_in is not None
    assert ev_in.event_type == "stock_change"
    assert ev_in.old_val != ev_in.new_val


@pytest.mark.parametrize(
    ("old_stock", "new_stock"),
    [(None, True), (True, None), (None, False), (False, None), (None, None), (True, True), (False, False)],
)
def test_stock_change_does_not_trigger_on_none_or_unchanged(
    old_stock: bool | None, new_stock: bool | None
) -> None:
    # Kabul: given either stock value is None or both stock values are equal,
    # expect stock_change returns None
    rule = AlertRule(rule_type="stock_change", threshold_value=None)
    event = evaluate(
        rule,
        old_price=Decimal("500.00"),
        new_price=Decimal("500.00"),
        old_in_stock=old_stock,
        new_in_stock=new_stock,
        now=NOW,
    )
    assert event is None


def test_sub_cent_price_diff_is_not_treated_as_change() -> None:
    # Kabul: given a price difference of 0.005 (below 0.01 minimum),
    # expect both below_price and pct_drop return None
    below_rule = AlertRule(rule_type="below_price", threshold_value=Decimal("100.00"))
    pct_rule = AlertRule(rule_type="pct_drop", threshold_value=Decimal("0.001"))

    assert (
        evaluate(
            below_rule,
            old_price=Decimal("100.000"),
            new_price=Decimal("99.995"),
            old_in_stock=True,
            new_in_stock=True,
            now=NOW,
        )
        is None
    )
    assert (
        evaluate(
            pct_rule,
            old_price=Decimal("100.000"),
            new_price=Decimal("99.995"),
            old_in_stock=True,
            new_in_stock=True,
            now=NOW,
        )
        is None
    )


def test_cooldown_blocks_within_window_and_allows_outside_window() -> None:
    # Kabul: given a rule with cooldown_min=60 and last_sent_at 59m59s ago, expect None;
    # given last_sent_at exactly 60m00s ago (or earlier), expect AlertEvent
    rule_within = AlertRule(
        rule_type="pct_drop",
        threshold_value=Decimal("5.00"),
        cooldown_min=60,
        last_sent_at=NOW - timedelta(minutes=59, seconds=59),
    )
    assert (
        evaluate(
            rule_within,
            old_price=Decimal("100.00"),
            new_price=Decimal("80.00"),
            old_in_stock=True,
            new_in_stock=True,
            now=NOW,
        )
        is None
    )

    rule_at_boundary = AlertRule(
        rule_type="pct_drop",
        threshold_value=Decimal("5.00"),
        cooldown_min=60,
        last_sent_at=NOW - timedelta(minutes=60),
    )
    event = evaluate(
        rule_at_boundary,
        old_price=Decimal("100.00"),
        new_price=Decimal("80.00"),
        old_in_stock=True,
        new_in_stock=True,
        now=NOW,
    )
    assert event is not None
    assert event.event_type == "pct_drop"


def test_first_measurement_never_triggers_any_rule() -> None:
    # Kabul: given old_price is None (first measurement), expect below_price, pct_drop, and stock_change all return None
    rules = [
        AlertRule(rule_type="below_price", threshold_value=Decimal("5000.00")),
        AlertRule(rule_type="pct_drop", threshold_value=Decimal("1.00")),
        AlertRule(rule_type="stock_change", threshold_value=None),
    ]
    for rule in rules:
        event = evaluate(
            rule,
            old_price=None,
            new_price=Decimal("100.00"),
            old_in_stock=False,
            new_in_stock=True,
            now=NOW,
        )
        assert event is None


def test_inactive_rule_does_not_trigger() -> None:
    # Kabul: given an inactive rule (is_active=False) whose price condition is otherwise met,
    # expect evaluate returns None
    rule = AlertRule(
        rule_type="below_price",
        threshold_value=Decimal("2400.00"),
        is_active=False,
    )
    assert (
        evaluate(
            rule,
            old_price=Decimal("2500.00"),
            new_price=Decimal("2000.00"),
            old_in_stock=True,
            new_in_stock=True,
            now=NOW,
        )
        is None
    )
