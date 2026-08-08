"""シャドーブローカー: 実勢価格で読み、約定だけ手元で模擬する。

前向き検証のための器である。**発注は一切外へ出ない。**

## なぜ必要になったか

前向き検証をデモ環境（BingX VST）でやろうとして、成立しないことが分かった。
VST の板は本番と別物で、1000PEPE のスプレッドは本番 4.5bp に対し
**VST は 351bp（78倍）** だった。往復 701bp のコストは、検証したい優位性
（1件あたり 5.63bp）の 125倍にあたる。同じデータで背景のスプレッドだけ
差し替えると、成績は +30.63% から **-20.20%** に転落する。

**測りたいものより測定誤差が2桁大きい器では、何を測っても意味がない。**

かといって実弾を入れるのは順序が違う。過去データの分割検証では
判定基準を満たしていない（多重比較と少数トレード依存）。だから
「本番の実勢価格を読み、約定は手元で模擬する」器を用意する。

## 何が検証できて、何ができないか

**できること.** 戦略の優位性が、まだ誰も見ていない期間で再現するか。
価格は本番のものなので、スプレッドもボラティリティも実勢である。

**できないこと.** 実際の約定。板の厚みを超える数量を出したときの滑り、
急変時に指値が届かない事象、取引所の障害。これらは模擬されない。
**約定経路の確認は VST で別途行うこと**（そちらは価格の質を問わない）。

この分離は意図的である。1つの環境で両方を確かめようとすると、
どちらも中途半端になる。

## 安全性

:class:`ShadowBroker` は :class:`~zerotrade.brokers.paper.PaperBroker` を継承し、
上流の実ブローカーへは**読み取り系メソッドだけ**を委譲する。
``place_order`` / ``cancel_order`` は親クラス（手元の模擬）のままである。
上流の発注メソッドを呼ぶ経路がコード上に存在しないため、
設定を間違えても実弾は動かない。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from zerotrade.brokers.base import BaseBroker
from zerotrade.brokers.paper import PaperBroker
from zerotrade.errors import BrokerError, ConfigError
from zerotrade.log import get_logger
from zerotrade.models import (
    Balance,
    Candle,
    ClosedTrade,
    Position,
    Side,
    Ticker,
    to_decimal,
    utcnow,
)
from zerotrade.settings import Settings

__all__ = ["ShadowBroker", "build_from_settings"]

logger = get_logger(__name__)

#: 起動時に上流から読み込む足の本数。ウォームアップに足りる量を取る。
INITIAL_BARS = 400


class ShadowBroker(PaperBroker):
    """上流ブローカーの実勢価格で動く、発注しないブローカー。"""

    name = "shadow"
    supports_closed_trades = True

    # 価格は実勢を読むが、約定は手元で模擬する。注文は外へ出ない。
    is_simulated = True

    def __init__(
        self,
        upstream: BaseBroker,
        symbols: list[str],
        *,
        granularity: str = "H1",
        initial_balance: object = None,
        state_path: Path | None = None,
        **kwargs: object,
    ) -> None:
        if not symbols:
            raise ConfigError("symbols を1つ以上指定してください")

        # 親には仮の系列を持たせておく。connect() で実勢に差し替える。
        placeholder = {
            symbol: [
                Candle(
                    symbol=symbol,
                    timestamp=utcnow(),
                    open=Decimal(1),
                    high=Decimal(1),
                    low=Decimal(1),
                    close=Decimal(1),
                    volume=Decimal(0),
                )
            ]
            for symbol in symbols
        }
        params: dict[str, object] = {"candles": placeholder, "warmup_bars": 1, **kwargs}
        if initial_balance is not None:
            params["initial_balance"] = initial_balance
        super().__init__(symbols, **params)  # type: ignore[arg-type]

        self._upstream = upstream
        self._granularity = granularity
        self._bar_seconds = _granularity_seconds(granularity)
        self._state_path = state_path
        self.name = f"shadow:{upstream.name}"

    # ------------------------------------------------------------ 接続

    async def connect(self) -> None:
        await self._upstream.connect()
        await super().connect()

        for symbol in self._symbols:
            candles = await self._closed_bars(symbol, INITIAL_BARS)
            if not candles:
                raise BrokerError(
                    f"{symbol} の足を上流（{self._upstream.name}）から取得できませんでした"
                )
            self.inject_candles(symbol, candles)
            # 起動時点の足はすべて「見えている」状態にする。
            # ここを 0 にすると、過去の足を1本ずつ舐め直して
            # 存在しない取引を積んでしまう。
            self._cursor[symbol] = len(candles)

        self._restore()

        logger.info(
            "ShadowBroker を開始しました（実勢価格=%s / 足種=%s / 銘柄 %s / 残高 %s）"
            "。**発注は外へ出ません**",
            self._upstream.name,
            self._granularity,
            ", ".join(self._symbols),
            self._cash,
        )

    async def disconnect(self) -> None:
        await super().disconnect()
        await self._upstream.disconnect()

    # ------------------------------------------------------------ 時計

    @property
    def simulated_time(self) -> datetime:
        """実時間。ライブなので相場時間ではなく現在時刻を使う。

        親クラスは「見えている足の最新時刻」を返すが、1時間足だと
        最大1時間ずれる。日次・週次のリセット判定が遅れるため上書きする。
        """
        return utcnow()

    # ------------------------------------------------------------ 相場

    async def get_ticker(self, symbol: str) -> Ticker:
        """上流の実勢気配値をそのまま返す。

        親クラスは「最後の足の終値 ± 設定スプレッド」を返すが、
        それでは設定値を測っているだけになる。**実勢の板を使う**。
        """
        self._require_connected()
        return await self._upstream.get_ticker(symbol)

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        granularity: str = "M5",
        count: int = 200,
        end: datetime | None = None,
    ) -> list[Candle]:
        """上流から新しい確定足を取り込み、その値幅で執行判定を行う。

        親クラスは呼ばれるたびに1本進めるが、こちらは**実際に増えた本数だけ**
        進める。ポーリング間隔と足種は独立なので、1時間足を1分ごとに
        叩いても取引が増えたりはしない。
        """
        self._require_connected()
        fresh = await self._closed_bars(symbol, max(count, 200))
        self._absorb(symbol, fresh)

        series = self._series(symbol)
        # 取り込んだぶんだけ時間を進める。各足でストップ・指値の判定が走る。
        while self._cursor[symbol] < len(series):
            self._advance(symbol)

        cutoff = self._cursor[symbol]
        if end is not None:
            cutoff = min(cutoff, sum(1 for c in series if c.timestamp < end))
        return list(series[max(0, cutoff - count) : cutoff])

    # ------------------------------------------------------------ 口座

    async def get_balance(self) -> Balance:
        """手元の模擬残高を返す。上流の実残高は参照しない。"""
        return await super().get_balance()

    # ------------------------------------------------------------ 内部

    async def _closed_bars(self, symbol: str, count: int) -> list[Candle]:
        """上流から足を取り、**確定済みのものだけ**返す。

        形成中の足を混ぜると、まだ付いていない高値・安値でストップ判定が
        動く。バックテストは確定足しか見ないので、ここを揃えないと
        検証結果と挙動が変わる。
        """
        candles = await self._upstream.get_ohlcv(symbol, granularity=self._granularity, count=count)
        limit = utcnow() - timedelta(seconds=self._bar_seconds)
        return [c for c in candles if c.complete and c.timestamp <= limit]

    def _absorb(self, symbol: str, fresh: list[Candle]) -> None:
        """既存の系列に、まだ持っていない足だけを継ぎ足す。"""
        if not fresh:
            return
        series = self._series(symbol)
        if not series:
            self.inject_candles(symbol, fresh)
            return

        newest = series[-1].timestamp
        added = [replace(c, symbol=symbol) for c in fresh if c.timestamp > newest]
        if not added:
            return
        self._candles[symbol] = [*series, *added]
        logger.debug("%s: 新しい確定足を %d 本取り込みました", symbol, len(added))

    # ------------------------------------------------------ 状態の永続化

    def _fill(self, order: object, price: object, *, reason: str) -> None:
        """約定のたびに状態を保存する。

        90日回すあいだにマシンは必ず再起動する。保存しないと、
        **再起動のたびに建玉が消え、残高が初期値に戻る**。
        しかも消えるのは「そのとき building 中だった建玉」なので、
        長く持っている取引ほど失われる。トレンドフォローでは
        大きく勝つ取引ほど長く持つため、**成績を系統的に過小評価する**。
        """
        super()._fill(order, price, reason=reason)  # type: ignore[arg-type]
        self._persist()

    def _close_position(self, symbol: str, price: object, *, reason: str) -> None:
        super()._close_position(symbol, price, reason=reason)  # type: ignore[arg-type]
        self._persist()

    def _persist(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "cash": str(self._cash),
            "positions": [
                {
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": str(p.quantity),
                    "entry_price": str(p.entry_price),
                    "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
                    "take_profit": str(p.take_profit) if p.take_profit is not None else None,
                    "opened_at": p.opened_at.isoformat(),
                }
                for p in self._positions.values()
            ],
            "closed_trades": [
                {
                    "symbol": t.symbol,
                    "side": t.side.value,
                    "quantity": str(t.quantity),
                    "entry_price": str(t.entry_price),
                    "exit_price": str(t.exit_price),
                    "realized_pnl": str(t.realized_pnl),
                    "opened_at": t.opened_at.isoformat(),
                    "closed_at": t.closed_at.isoformat(),
                    "reason": t.reason,
                }
                for t in self._closed_trades
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            # 書き込み途中で落ちても壊れたファイルを残さない。
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.warning("シャドー状態を保存できませんでした: %s", exc)

    def _restore(self) -> None:
        """保存済みの建玉と残高を読み戻す。壊れていたら初期状態から続ける。"""
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error(
                "シャドー状態が読めませんでした。初期状態から続けます"
                "（前半の記録は記録DBに残っています）: %s",
                exc,
            )
            return

        self._cash = to_decimal(str(payload.get("cash", self._cash)))
        self._positions = {
            entry["symbol"]: Position(
                symbol=entry["symbol"],
                side=Side(entry["side"]),
                quantity=to_decimal(entry["quantity"]),
                entry_price=to_decimal(entry["entry_price"]),
                stop_loss=(
                    to_decimal(entry["stop_loss"]) if entry.get("stop_loss") is not None else None
                ),
                take_profit=(
                    to_decimal(entry["take_profit"])
                    if entry.get("take_profit") is not None
                    else None
                ),
                opened_at=datetime.fromisoformat(entry["opened_at"]),
            )
            for entry in payload.get("positions", [])
        }
        self._closed_trades = [
            ClosedTrade(
                symbol=entry["symbol"],
                side=Side(entry["side"]),
                quantity=to_decimal(entry["quantity"]),
                entry_price=to_decimal(entry["entry_price"]),
                exit_price=to_decimal(entry["exit_price"]),
                realized_pnl=to_decimal(entry["realized_pnl"]),
                opened_at=datetime.fromisoformat(entry["opened_at"]),
                closed_at=datetime.fromisoformat(entry["closed_at"]),
                reason=entry.get("reason", ""),
            )
            for entry in payload.get("closed_trades", [])
        ]
        if self._positions or self._closed_trades:
            logger.info(
                "前回の状態を引き継ぎました（建玉 %d / 決済済み %d / 残高 %s）",
                len(self._positions),
                len(self._closed_trades),
                self._cash,
            )


def _granularity_seconds(name: str) -> int:
    from zerotrade.data.importer import parse_granularity

    return int(parse_granularity(name).total_seconds())


def build_from_settings(settings: Settings) -> ShadowBroker:
    """設定から ShadowBroker を組み立てる。

    ``broker.upstream`` に実勢価格の取得元（``bingx`` など）を書く。
    上流は**読み取りにしか使わない**ため、``environment: live`` を
    指定しても実弾は動かない。
    """
    from zerotrade.brokers import create_broker

    upstream_name = settings.broker.upstream
    if not upstream_name:
        raise ConfigError(
            "shadow ブローカーには broker.upstream が必要です（例: bingx）。"
            "実勢価格の取得元を指定してください"
        )
    if upstream_name in {"shadow", "paper"}:
        raise ConfigError(
            f"broker.upstream に {upstream_name} は指定できません。"
            "実勢価格を返すブローカーを指定してください"
        )

    upstream_settings = settings.model_copy(
        update={
            "broker": settings.broker.model_copy(
                update={"name": upstream_name, "fallback_to_paper": False}
            )
        }
    )
    upstream = create_broker(upstream_settings)

    return ShadowBroker(
        upstream,
        list(settings.symbols),
        granularity=settings.strategy.granularity,
        initial_balance=settings.broker.initial_balance,
        # 90日回すあいだにマシンは必ず再起動する。建玉と残高を残さないと、
        # そのたびに検証がリセットされる。
        state_path=settings.state_dir / "shadow_state.json",
        currency=settings.broker.account_currency,
        leverage=settings.risk.assumed_leverage,
        contract_size=settings.sizing.contract_size,
    )
