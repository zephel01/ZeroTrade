"""ShadowBroker のテスト。

検証したいのは3点。
1) 上流の**実勢気配値**で約定すること（設定スプレッドではない）
2) 上流へ**発注が漏れない**こと
3) 確定足だけを取り込み、増えたぶんだけ時間を進めること
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from zerotrade.brokers.base import BaseBroker
from zerotrade.brokers.shadow import ShadowBroker
from zerotrade.errors import BrokerError, ConfigError
from zerotrade.models import (
    Balance,
    Candle,
    Order,
    OrderRequest,
    Position,
    Side,
    Ticker,
    utcnow,
)

SYMBOL = "1000PEPE_USDT"


def _bars(count: int, *, end: datetime | None = None, price: str = "100") -> list[Candle]:
    """count 本の確定足。最後の足は end（既定は1時間前）に終わる。"""
    # 末尾に確定足を足せるよう、既定では3時間ぶん余裕を持たせる。
    last = end or (utcnow() - timedelta(hours=3))
    base = Decimal(price)
    return [
        Candle(
            symbol=SYMBOL,
            timestamp=last - timedelta(hours=count - 1 - i),
            open=base,
            high=base + 2,
            low=base - 2,
            close=base,
            volume=Decimal(10),
        )
        for i in range(count)
    ]


class FakeUpstream(BaseBroker):
    """実勢価格だけを返す上流。発注系が呼ばれたら即座に失敗させる。"""

    name = "fake-upstream"

    def __init__(self, candles: list[Candle], bid: str = "99", ask: str = "101") -> None:
        self.candles = candles
        self.bid = Decimal(bid)
        self.ask = Decimal(ask)
        self.connected = False
        self.order_attempts = 0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def get_balance(self) -> Balance:
        # 上流の実残高を読んだら、それは設計違反。
        raise AssertionError("上流の残高を参照してはいけない")

    async def get_positions(self) -> list[Position]:
        raise AssertionError("上流の建玉を参照してはいけない")

    async def place_order(self, request: OrderRequest) -> Order:
        self.order_attempts += 1
        raise AssertionError("上流へ発注が漏れている")

    async def cancel_order(self, order_id: str) -> Order:
        self.order_attempts += 1
        raise AssertionError("上流へ取消が漏れている")

    async def get_order(self, order_id: str) -> Order:
        raise AssertionError("上流の注文を参照してはいけない")

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        raise AssertionError("上流の注文を参照してはいけない")

    async def get_ticker(self, symbol: str) -> Ticker:
        return Ticker(symbol=symbol, bid=self.bid, ask=self.ask, timestamp=utcnow())

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        granularity: str = "M5",
        count: int = 200,
        end: datetime | None = None,
    ) -> list[Candle]:
        return list(self.candles[-count:])


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream(_bars(300))


@pytest.fixture
async def broker(upstream: FakeUpstream) -> ShadowBroker:
    b = ShadowBroker(
        upstream,
        [SYMBOL],
        granularity="H1",
        initial_balance=Decimal(1_000_000),
        currency="USDT",
    )
    await b.connect()
    return b


# ------------------------------------------------------------ 接続


async def test_接続で上流の足を取り込む(broker: ShadowBroker) -> None:
    candles = await broker.get_ohlcv(SYMBOL, granularity="H1", count=500)
    assert len(candles) == 300


async def test_起動時に過去の足を舐め直さない(broker: ShadowBroker) -> None:
    """カーソルを0から始めると、存在しない取引を積み上げてしまう。"""
    assert await broker.get_positions() == []
    assert await broker.get_closed_trades() == []


async def test_足が取れなければ接続に失敗する(upstream: FakeUpstream) -> None:
    upstream.candles = []
    b = ShadowBroker(upstream, [SYMBOL], granularity="H1")
    with pytest.raises(BrokerError, match="取得できませんでした"):
        await b.connect()


# ------------------------------------------------------------ 実勢価格


async def test_気配値は上流の実勢を使う(broker: ShadowBroker, upstream: FakeUpstream) -> None:
    """設定スプレッドで代用すると、設定値を測っているだけになる。"""
    ticker = await broker.get_ticker(SYMBOL)
    assert ticker.bid == upstream.bid
    assert ticker.ask == upstream.ask


async def test_成行は実勢気配値で約定する(broker: ShadowBroker, upstream: FakeUpstream) -> None:
    """VSTのような歪んだ板ではなく、上流の板で約定させる。"""
    order = await broker.place_order(
        OrderRequest(
            symbol=SYMBOL,
            side=Side.BUY,
            quantity=Decimal(10),
            stop_loss=Decimal(90),
        )
    )
    assert order.average_price == upstream.ask, "実勢のアスクで約定していない"


async def test_スプレッドが広いほど不利に約定する(upstream: FakeUpstream) -> None:
    """器がスプレッドを正しく反映していることの確認。"""
    fills = []
    for bid, ask in (("99.9", "100.1"), ("95", "105")):
        up = FakeUpstream(_bars(300), bid=bid, ask=ask)
        b = ShadowBroker(up, [SYMBOL], granularity="H1", initial_balance=Decimal(1_000_000))
        await b.connect()
        order = await b.place_order(
            OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=Decimal(10), stop_loss=Decimal(90))
        )
        fills.append(order.average_price)
    assert fills[1] is not None and fills[0] is not None
    assert fills[1] > fills[0], "スプレッドが広いのに約定価格が悪化していない"


# ------------------------------------------------------------ 発注が漏れない


async def test_発注は上流へ出ない(broker: ShadowBroker, upstream: FakeUpstream) -> None:
    """**これが ShadowBroker の存在意義。**"""
    await broker.place_order(
        OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=Decimal(10), stop_loss=Decimal(90))
    )
    assert upstream.order_attempts == 0, "上流へ発注が漏れている"


async def test_残高は手元の模擬値(broker: ShadowBroker) -> None:
    """上流の実残高を読むと AssertionError になる仕掛けにしてある。"""
    balance = await broker.get_balance()
    assert balance.equity == Decimal(1_000_000)
    assert balance.currency == "USDT"


# ------------------------------------------------------------ 足の取り込み


async def test_未確定足は取り込まない(upstream: FakeUpstream) -> None:
    """形成中の足を混ぜると、まだ付いていない高値安値でストップが動く。"""
    settled = _bars(300)
    forming = Candle(
        symbol=SYMBOL,
        timestamp=utcnow(),  # 今まさに形成中
        open=Decimal(100),
        high=Decimal(999),  # あり得ない高値。取り込まれたら分かる
        low=Decimal(1),
        close=Decimal(100),
        volume=Decimal(1),
        complete=False,
    )
    upstream.candles = [*settled, forming]

    b = ShadowBroker(upstream, [SYMBOL], granularity="H1")
    await b.connect()
    candles = await b.get_ohlcv(SYMBOL, granularity="H1", count=500)

    assert all(c.high < 999 for c in candles), "未確定足が混ざっている"


async def test_足が増えなければ時間は進まない(broker: ShadowBroker) -> None:
    """1時間足を1分ごとに叩いても取引が増えてはいけない。"""
    first = await broker.get_ohlcv(SYMBOL, granularity="H1", count=500)
    second = await broker.get_ohlcv(SYMBOL, granularity="H1", count=500)
    third = await broker.get_ohlcv(SYMBOL, granularity="H1", count=500)
    assert len(first) == len(second) == len(third)


async def test_新しい足が来たら取り込む(broker: ShadowBroker, upstream: FakeUpstream) -> None:
    before = len(await broker.get_ohlcv(SYMBOL, granularity="H1", count=500))
    last = upstream.candles[-1]
    upstream.candles = [
        *upstream.candles,
        Candle(
            symbol=SYMBOL,
            timestamp=last.timestamp + timedelta(hours=1),
            open=Decimal(100),
            high=Decimal(102),
            low=Decimal(98),
            close=Decimal(101),
            volume=Decimal(5),
        ),
    ]
    after = len(await broker.get_ohlcv(SYMBOL, granularity="H1", count=500))
    assert after == before + 1


async def test_新しい足でストップ判定が動く(broker: ShadowBroker, upstream: FakeUpstream) -> None:
    """建玉を持ったまま逆行したら、取り込んだ足で決済されること。"""
    await broker.place_order(
        OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=Decimal(10), stop_loss=Decimal(95))
    )
    assert len(await broker.get_positions()) == 1

    last = upstream.candles[-1]
    upstream.candles = [
        *upstream.candles,
        Candle(
            symbol=SYMBOL,
            timestamp=last.timestamp + timedelta(hours=1),
            open=Decimal(99),
            high=Decimal(99),
            low=Decimal(90),  # ストップ 95 を割り込む
            close=Decimal(91),
            volume=Decimal(5),
        ),
    ]
    await broker.get_ohlcv(SYMBOL, granularity="H1", count=500)

    assert await broker.get_positions() == [], "ストップが機能していない"
    trades = await broker.get_closed_trades()
    assert trades and trades[-1].exit_price == Decimal(95)


# ------------------------------------------------------------ 設定


def test_上流未指定は拒否される() -> None:
    from zerotrade.brokers.shadow import build_from_settings
    from zerotrade.settings import Settings

    settings = Settings.model_validate({"symbols": [SYMBOL], "broker": {"name": "shadow"}})
    with pytest.raises(ConfigError, match=r"broker\.upstream"):
        build_from_settings(settings)


@pytest.mark.parametrize("bad", ["paper", "shadow"])
def test_上流にpaperやshadowは指定できない(bad: str) -> None:
    from zerotrade.brokers.shadow import build_from_settings
    from zerotrade.settings import Settings

    settings = Settings.model_validate(
        {"symbols": [SYMBOL], "broker": {"name": "shadow", "upstream": bad}}
    )
    with pytest.raises(ConfigError, match="指定できません"):
        build_from_settings(settings)


async def test_時計は実時間を使う(broker: ShadowBroker) -> None:
    """1時間足の最新足の時刻を使うと、日次リセットが最大1時間遅れる。"""
    assert (utcnow() - broker.simulated_time).total_seconds() < 5


# ------------------------------------------------------------ 再起動をまたぐ


async def test_建玉と残高が再起動で残る(upstream: FakeUpstream, tmp_path: Path) -> None:
    """**90日回すあいだにマシンは必ず再起動する。**

    保存しないと再起動のたびに建玉が消えて残高が初期値へ戻る。
    しかも消えるのは「そのとき保有中だった建玉」なので、長く持つ取引ほど
    失われる。トレンドフォローは大きく勝つ取引ほど長く持つため、
    成績を系統的に過小評価してしまう。
    """
    state = tmp_path / "shadow_state.json"

    first = ShadowBroker(
        upstream,
        [SYMBOL],
        granularity="H1",
        initial_balance=Decimal(1_000_000),
        state_path=state,
    )
    await first.connect()
    await first.place_order(
        OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=Decimal(10), stop_loss=Decimal(90))
    )
    before = await first.get_positions()
    cash_before = (await first.get_balance()).equity
    await first.disconnect()

    assert state.is_file(), "状態が保存されていない"

    # 別インスタンス＝再起動に相当
    second = ShadowBroker(
        upstream,
        [SYMBOL],
        granularity="H1",
        initial_balance=Decimal(1_000_000),
        state_path=state,
    )
    await second.connect()
    after = await second.get_positions()

    assert len(after) == 1, "再起動で建玉が消えている"
    assert after[0].quantity == before[0].quantity
    assert after[0].entry_price == before[0].entry_price
    assert after[0].stop_loss == before[0].stop_loss, "ストップまで引き継げていない"
    assert (await second.get_balance()).equity == cash_before, "残高が初期値へ戻っている"


async def test_決済履歴も再起動で残る(upstream: FakeUpstream, tmp_path: Path) -> None:
    state = tmp_path / "shadow_state.json"

    first = ShadowBroker(
        upstream, [SYMBOL], granularity="H1", initial_balance=Decimal(1_000_000), state_path=state
    )
    await first.connect()
    await first.place_order(
        OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=Decimal(10), stop_loss=Decimal(90))
    )
    await first.place_order(
        OrderRequest(symbol=SYMBOL, side=Side.SELL, quantity=Decimal(10), reduce_only=True)
    )
    assert len(await first.get_closed_trades()) == 1

    second = ShadowBroker(
        upstream, [SYMBOL], granularity="H1", initial_balance=Decimal(1_000_000), state_path=state
    )
    await second.connect()
    assert len(await second.get_closed_trades()) == 1, "確定損益の履歴が失われている"


async def test_状態ファイルが壊れていても起動する(upstream: FakeUpstream, tmp_path: Path) -> None:
    """壊れたファイルで起動不能になるほうが困る。"""
    state = tmp_path / "shadow_state.json"
    state.write_text("{壊れたJSON", encoding="utf-8")

    b = ShadowBroker(
        upstream, [SYMBOL], granularity="H1", initial_balance=Decimal(1_000_000), state_path=state
    )
    await b.connect()
    assert (await b.get_balance()).equity == Decimal(1_000_000)


async def test_状態パス未指定でも動く(upstream: FakeUpstream) -> None:
    b = ShadowBroker(upstream, [SYMBOL], granularity="H1", initial_balance=Decimal(1_000_000))
    await b.connect()
    await b.place_order(
        OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=Decimal(10), stop_loss=Decimal(90))
    )
    assert len(await b.get_positions()) == 1
