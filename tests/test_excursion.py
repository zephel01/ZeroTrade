"""MFE / MAE 追跡のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from zerotrade.core.excursion import Excursion, ExcursionTracker
from zerotrade.models import Position, Side

OPENED = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _position(side: Side = Side.BUY, *, opened_at: datetime = OPENED) -> Position:
    return Position(
        symbol="BTC_USDT",
        side=side,
        quantity=Decimal(2),
        entry_price=Decimal(100),
        opened_at=opened_at,
    )


def test_買い建玉は高値がMFE_安値がMAEになる() -> None:
    tracker = ExcursionTracker()
    tracker.observe_range(_position(), high=Decimal(110), low=Decimal(95))

    excursion = tracker.snapshot("BTC_USDT")
    assert excursion is not None
    assert excursion.favorable == Decimal(20)  # (110-100) * 2
    assert excursion.adverse == Decimal(-10)  # (95-100) * 2


def test_売り建玉は符号が反転する() -> None:
    tracker = ExcursionTracker()
    tracker.observe_range(_position(Side.SELL), high=Decimal(110), low=Decimal(95))

    excursion = tracker.snapshot("BTC_USDT")
    assert excursion is not None
    assert excursion.favorable == Decimal(10)  # 下がった分が利益
    assert excursion.adverse == Decimal(-20)


def test_振れ幅は観測を重ねても最大値だけを保つ() -> None:
    tracker = ExcursionTracker()
    position = _position()
    tracker.observe_range(position, high=Decimal(110), low=Decimal(95))
    # あとから内側に収まる足が来ても、記録は緩まない。
    tracker.observe_range(position, high=Decimal(102), low=Decimal(99))

    excursion = tracker.snapshot("BTC_USDT")
    assert excursion is not None
    assert excursion.favorable == Decimal(20)
    assert excursion.adverse == Decimal(-10)


def test_MFEとMAEは符号の向きを外れない() -> None:
    """一度も利益にならなかった建玉の MFE は0であって負にはならない。"""
    tracker = ExcursionTracker()
    tracker.observe_range(_position(), high=Decimal(98), low=Decimal(90))

    excursion = tracker.snapshot("BTC_USDT")
    assert excursion is not None
    assert excursion.favorable == Decimal(0)
    assert excursion.adverse == Decimal(-20)


def test_建玉が入れ替わると観測をやり直す() -> None:
    """決済して建て直した直後に、前の建玉の振れ幅が混ざってはいけない。"""
    tracker = ExcursionTracker()
    tracker.observe_range(_position(), high=Decimal(200), low=Decimal(100))

    later = _position(opened_at=datetime(2026, 8, 8, 15, 0, tzinfo=UTC))
    tracker.observe_range(later, high=Decimal(101), low=Decimal(100))

    excursion = tracker.snapshot("BTC_USDT")
    assert excursion is not None
    assert excursion.favorable == Decimal(2)


def test_部分決済では割合で按分する() -> None:
    tracker = ExcursionTracker()
    tracker.observe_range(_position(), high=Decimal(110), low=Decimal(95))

    half = tracker.snapshot("BTC_USDT", ratio=Decimal("0.5"))
    assert half is not None
    assert half.favorable == Decimal(10)
    assert half.adverse == Decimal(-5)


def test_契約サイズが金額に効く() -> None:
    tracker = ExcursionTracker(contract_size=Decimal(1000))
    tracker.observe_range(_position(), high=Decimal(110), low=Decimal(100))

    excursion = tracker.snapshot("BTC_USDT")
    assert excursion is not None
    assert excursion.favorable == Decimal(20_000)


def test_忘れた銘柄はNoneを返す() -> None:
    tracker = ExcursionTracker()
    tracker.observe_range(_position(), high=Decimal(110), low=Decimal(95))
    tracker.forget("BTC_USDT")

    assert tracker.snapshot("BTC_USDT") is None


def test_未観測の銘柄はNoneを返す() -> None:
    assert ExcursionTracker().snapshot("ETH_USDT") is None


def test_extended_は最大値と最小値だけを更新する() -> None:
    base = Excursion(favorable=Decimal(10), adverse=Decimal(-5))
    assert base.extended(Decimal(3), Decimal(-1)) == base
    assert base.extended(Decimal(20), Decimal(-9)) == Excursion(Decimal(20), Decimal(-9))
