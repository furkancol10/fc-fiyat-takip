from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

MIN_PRICE_DIFF = Decimal("0.01")


@dataclass(frozen=True)
class AlertRule:
    rule_type: str
    threshold_value: Decimal | None = None
    cooldown_min: int = 60
    last_sent_at: datetime | None = None
    is_active: bool = True
    id: int | None = None


@dataclass(frozen=True)
class AlertEvent:
    event_type: str
    old_val: str | None
    new_val: str | None
    message: str


def _get_field(rule: Any, name: str, default: Any = None) -> Any:
    if isinstance(rule, Mapping):
        return rule.get(name, default)
    return getattr(rule, name, default)


def _to_decimal(value: Decimal | int | float | str | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _in_cooldown(
    last_sent_at: datetime | None,
    cooldown_min: int,
    now: datetime,
) -> bool:
    if last_sent_at is None:
        return False
    elapsed = _normalize_dt(now) - _normalize_dt(last_sent_at)
    return elapsed < timedelta(minutes=cooldown_min)


def evaluate(
    rule: AlertRule | Mapping[str, Any] | Any,
    *,
    old_price: Decimal | int | float | str | None,
    new_price: Decimal | int | float | str | None,
    old_in_stock: bool | None,
    new_in_stock: bool | None,
    now: datetime,
) -> AlertEvent | None:
    """Evaluate a single alert rule against old/new measurements with cooldown and noise guards."""
    if not _get_field(rule, "is_active", True):
        return None

    # First measurement establishes baseline and never triggers any rule.
    if old_price is None:
        return None

    last_sent_at = _get_field(rule, "last_sent_at", None)
    cooldown_min = int(_get_field(rule, "cooldown_min", 60))
    if _in_cooldown(last_sent_at, cooldown_min, now):
        return None

    rule_type = _get_field(rule, "rule_type")
    threshold = _to_decimal(_get_field(rule, "threshold_value", None))
    old_dec = _to_decimal(old_price)
    new_dec = _to_decimal(new_price)

    if rule_type == "below_price":
        if old_dec is None or new_dec is None or threshold is None:
            return None
        if abs(new_dec - old_dec) < MIN_PRICE_DIFF:
            return None
        if old_dec >= threshold and new_dec < threshold:
            return AlertEvent(
                event_type="below_price",
                old_val=f"{old_dec:.2f}",
                new_val=f"{new_dec:.2f}",
                message=(
                    f"Fiyat eşiğin ({threshold:.2f}) altına düştü: "
                    f"{old_dec:.2f} -> {new_dec:.2f}"
                ),
            )
        return None

    if rule_type == "pct_drop":
        if old_dec is None or new_dec is None or threshold is None:
            return None
        if old_dec <= 0:
            return None
        drop = old_dec - new_dec
        if drop < MIN_PRICE_DIFF:
            return None
        if drop * Decimal("100") >= threshold * old_dec:
            drop_pct = (drop / old_dec) * Decimal("100")
            return AlertEvent(
                event_type="pct_drop",
                old_val=f"{old_dec:.2f}",
                new_val=f"{new_dec:.2f}",
                message=(
                    f"Fiyat %{drop_pct:.2f} düştü: "
                    f"{old_dec:.2f} -> {new_dec:.2f}"
                ),
            )
        return None

    if rule_type == "stock_change":
        if old_in_stock is None or new_in_stock is None:
            return None
        if old_in_stock == new_in_stock:
            return None
        old_label = "Stokta" if old_in_stock else "Tükendi"
        new_label = "Stokta" if new_in_stock else "Tükendi"
        return AlertEvent(
            event_type="stock_change",
            old_val=old_label,
            new_val=new_label,
            message=f"Stok durumu değişti: {old_label} -> {new_label}",
        )

    return None
