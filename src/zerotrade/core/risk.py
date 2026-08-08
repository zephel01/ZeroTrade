"""RiskManager — ZeroTrade の中核。

このシステムの存在理由は「監視できない時間にルールを破らないこと」なので、
リスク判定は *発注経路の唯一の関門* として実装してある。

* 戦略は :class:`~zerotrade.models.Signal` を出すだけで発注できない。
* :class:`~zerotrade.core.orders.OrderManager` は必ず
  :meth:`RiskManager.evaluate` を通してからブローカーへ渡す。
* 日次・週次の損失上限に達したら :attr:`RiskManager.is_halted` が立ち、
  以降の **新規** 建ては一切通らない。決済（``reduce_only``）だけは通す。

判定はすべて純粋な同期処理にしてある。I/O が無いのでテストしやすく、
「なぜ弾かれたか」を :class:`RiskDecision` から機械的に取り出せる。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from zerotrade.errors import RiskViolation, TradingHalted
from zerotrade.log import get_logger
from zerotrade.models import Balance, OrderRequest, Position, Side, Ticker, to_decimal, utcnow
from zerotrade.settings import RiskSettings

__all__ = ["MarketContext", "RiskDecision", "RiskManager", "RiskState"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """リスク判定の結果。

    ``approved`` が False のとき ``rule`` に違反したルール名が入る。
    ログ・通知・テストのすべてがこのルール名をキーに動く。
    """

    approved: bool
    rule: str | None = None
    detail: str = ""

    @classmethod
    def approve(cls, detail: str = "") -> RiskDecision:
        return cls(approved=True, detail=detail)

    @classmethod
    def reject(cls, rule: str, detail: str) -> RiskDecision:
        return cls(approved=False, rule=rule, detail=detail)

    def __bool__(self) -> bool:
        return self.approved

    def raise_if_rejected(self) -> None:
        """却下されていれば例外を送出する。

        戻り値を無視した発注事故を防ぎたい呼び出し側が使う。
        """
        if self.approved:
            return
        assert self.rule is not None
        if self.rule in ("daily_loss_limit", "weekly_loss_limit", "halted"):
            raise TradingHalted(self.rule, self.detail)
        raise RiskViolation(self.rule, self.detail)


@dataclass(frozen=True, slots=True)
class MarketContext:
    """リスク判定に必要な相場情報。

    すべて任意。渡されなかった項目に対応するチェックは
    「判定できない」として **スキップではなく素通し** になる。
    厳格にしたい場合は StrategyRunner 側で必ず埋めること。
    """

    ticker: Ticker | None = None
    atr: Decimal | None = None
    """直近のATR。"""

    atr_baseline: Decimal | None = None
    """平常時のATR（長期平均）。急拡大の判定基準。"""


@dataclass
class RiskState:
    """日次・週次で持ち越すリスク状態。

    プロセスを再起動しても損失上限がリセットされないよう、
    JSON へ永続化できるようにしてある。
    """

    day_key: str = ""
    week_key: str = ""
    daily_pnl: Decimal = Decimal(0)
    weekly_pnl: Decimal = Decimal(0)
    daily_trades: int = 0
    halt_reason: str | None = None
    last_loss_at: dict[str, datetime] = field(default_factory=dict)

    # ------------------------------------------------------------ 期間管理

    def roll(self, now: datetime, tz: ZoneInfo) -> bool:
        """日付・週が変わっていればカウンタをリセットする。

        Returns:
            リセットが発生したか。
        """
        local = now.astimezone(tz)
        day_key = local.strftime("%Y-%m-%d")
        iso = local.isocalendar()
        week_key = f"{iso.year}-W{iso.week:02d}"

        rolled = False
        if day_key != self.day_key:
            self.day_key = day_key
            self.daily_pnl = Decimal(0)
            self.daily_trades = 0
            # 日次停止のみ解除する。週次停止は週が変わるまで維持。
            if self.halt_reason == "daily_loss_limit":
                self.halt_reason = None
            rolled = True
        if week_key != self.week_key:
            self.week_key = week_key
            self.weekly_pnl = Decimal(0)
            if self.halt_reason == "weekly_loss_limit":
                self.halt_reason = None
            rolled = True
        return rolled

    # ------------------------------------------------------------ 永続化

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_key": self.day_key,
            "week_key": self.week_key,
            "daily_pnl": str(self.daily_pnl),
            "weekly_pnl": str(self.weekly_pnl),
            "daily_trades": self.daily_trades,
            "halt_reason": self.halt_reason,
            "last_loss_at": {k: v.isoformat() for k, v in self.last_loss_at.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RiskState:
        return cls(
            day_key=str(data.get("day_key", "")),
            week_key=str(data.get("week_key", "")),
            daily_pnl=to_decimal(data.get("daily_pnl", 0)),
            weekly_pnl=to_decimal(data.get("weekly_pnl", 0)),
            daily_trades=int(data.get("daily_trades", 0)),
            halt_reason=data.get("halt_reason"),
            last_loss_at={
                k: datetime.fromisoformat(v) for k, v in dict(data.get("last_loss_at", {})).items()
            },
        )


class RiskManager:
    """発注前のすべてのリスクチェックを担う。

    Example:
        >>> manager = RiskManager(RiskSettings())
        >>> decision = manager.evaluate(request, balance=balance, positions=[])
        >>> if decision:
        ...     await broker.place_order(request)
    """

    def __init__(
        self,
        settings: RiskSettings,
        *,
        contract_size: Decimal = Decimal(1),
        state: RiskState | None = None,
        state_path: Path | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._contract_size = contract_size
        self._state_path = state_path
        self._clock = clock
        self._tz = ZoneInfo(settings.reset_timezone)
        self._state = state or RiskState()
        self._reference_equity: Decimal | None = None
        self._state.roll(self._clock(), self._tz)

    # ------------------------------------------------------------- 参照系

    @property
    def settings(self) -> RiskSettings:
        return self._settings

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def is_halted(self) -> bool:
        """損失上限に達して取引が停止しているか。"""
        self._state.roll(self._clock(), self._tz)
        return self._state.halt_reason is not None

    # ------------------------------------------------------------- 判定本体

    def evaluate(
        self,
        request: OrderRequest,
        *,
        balance: Balance,
        positions: Iterable[Position],
        market: MarketContext | None = None,
    ) -> RiskDecision:
        """発注要求を検査する。

        Args:
            request: 検査対象の注文。
            balance: 現在の口座残高。
            positions: 現在の保有ポジション。
            market: 気配値・ATR などの相場情報（任意）。

        Returns:
            承認なら ``approved=True``、却下ならルール名と理由を含む
            :class:`RiskDecision`。
        """
        now = self._clock()
        self._state.roll(now, self._tz)
        market = market or MarketContext()
        position_list = list(positions)

        # 基準資金が未設定なら最初に見た equity を採用する。
        # これを忘れると損失上限の判定が一切働かないため、既定値を用意しておく。
        if self._reference_equity is None and balance.equity > 0:
            self._reference_equity = balance.equity

        # --- 決済注文は常に通す ------------------------------------------
        # 停止中でも建玉を閉じられなければリスク管理として本末転倒。
        if request.reduce_only:
            return RiskDecision.approve("決済注文のためリスク検査を免除")

        # --- 取引停止 -----------------------------------------------------
        if self._state.halt_reason is not None:
            return RiskDecision.reject(
                self._state.halt_reason,
                f"損失上限に達したため取引を停止中です"
                f"（日次 {self._state.daily_pnl:+,.2f} / 週次 {self._state.weekly_pnl:+,.2f}）",
            )

        # --- 口座の健全性 --------------------------------------------------
        if balance.equity <= 0:
            return RiskDecision.reject("equity_depleted", f"有効証拠金が {balance.equity} です")

        # --- 取引回数 ------------------------------------------------------
        if self._state.daily_trades >= self._settings.max_daily_trades:
            return RiskDecision.reject(
                "max_daily_trades",
                f"本日の取引回数が上限 {self._settings.max_daily_trades} に達しています",
            )

        # --- 損失後クールダウン --------------------------------------------
        cooldown = self._settings.cooldown_seconds_after_loss
        last_loss = self._state.last_loss_at.get(request.symbol)
        if cooldown > 0 and last_loss is not None:
            elapsed = (now - last_loss).total_seconds()
            if elapsed < cooldown:
                return RiskDecision.reject(
                    "cooldown_after_loss",
                    f"{request.symbol} は損失後クールダウン中です"
                    f"（残り {cooldown - elapsed:.0f} 秒）",
                )

        # --- ポジション数 --------------------------------------------------
        if len(position_list) >= self._settings.max_open_positions:
            return RiskDecision.reject(
                "max_open_positions",
                f"保有ポジション数 {len(position_list)} が上限 "
                f"{self._settings.max_open_positions} に達しています",
            )

        same_symbol = sum(1 for p in position_list if p.symbol == request.symbol)
        if same_symbol >= self._settings.max_positions_per_symbol:
            return RiskDecision.reject(
                "max_positions_per_symbol",
                f"{request.symbol} の保有数 {same_symbol} が上限 "
                f"{self._settings.max_positions_per_symbol} に達しています",
            )

        # --- ストップ必須 --------------------------------------------------
        if self._settings.require_stop_loss and request.stop_loss is None:
            return RiskDecision.reject(
                "require_stop_loss", "ストップロス未設定の新規建ては禁止されています"
            )

        # --- 参照価格 ------------------------------------------------------
        entry_price = self._reference_price(request, market.ticker)
        if entry_price is None:
            return RiskDecision.reject(
                "no_reference_price",
                "参照価格が無いためリスク額を計算できません（ticker か指値価格が必要）",
            )

        # --- ストップの向き -------------------------------------------------
        if request.stop_loss is not None:
            wrong_side = (request.side is Side.BUY and request.stop_loss >= entry_price) or (
                request.side is Side.SELL and request.stop_loss <= entry_price
            )
            if wrong_side:
                return RiskDecision.reject(
                    "invalid_stop_loss",
                    f"{request.side} に対してストップ {request.stop_loss} が "
                    f"参照価格 {entry_price} の反対側にありません",
                )

        # --- スプレッド ------------------------------------------------------
        if (
            self._settings.max_spread is not None
            and market.ticker is not None
            and market.ticker.spread > self._settings.max_spread
        ):
            return RiskDecision.reject(
                "max_spread",
                f"スプレッド {market.ticker.spread} が上限 "
                f"{self._settings.max_spread} を超えています",
            )

        # --- 1トレードあたりリスク -------------------------------------------
        if request.stop_loss is not None:
            risk_amount = (
                abs(entry_price - request.stop_loss) * request.quantity * self._contract_size
            )
            allowed = balance.equity * self._settings.max_risk_per_trade
            if risk_amount > allowed:
                return RiskDecision.reject(
                    "max_risk_per_trade",
                    f"想定損失 {risk_amount:.2f} が上限 {allowed:.2f}"
                    f"（equity の {self._settings.max_risk_per_trade:%}）を超えています",
                )

        # --- 証拠金使用率 -----------------------------------------------------
        notional = entry_price * request.quantity * self._contract_size
        required_margin = notional / self._settings.assumed_leverage
        projected = (balance.used_margin + required_margin) / balance.equity
        if projected > self._settings.max_margin_usage:
            return RiskDecision.reject(
                "max_margin_usage",
                f"発注後の証拠金使用率 {projected:.1%} が上限 "
                f"{self._settings.max_margin_usage:.1%} を超えます",
            )
        if required_margin > balance.available:
            return RiskDecision.reject(
                "insufficient_margin",
                f"必要証拠金 {required_margin:.2f} が余力 {balance.available:.2f} を超えています",
            )

        # --- 大相場（ATR急拡大） -----------------------------------------------
        if market.atr is not None and (market.atr_baseline or 0) > 0:
            assert market.atr_baseline is not None
            ratio = market.atr / market.atr_baseline
            if ratio > self._settings.atr_spike_multiplier:
                return RiskDecision.reject(
                    "atr_spike",
                    f"ATRが平常時の {ratio:.1f} 倍に拡大しています"
                    f"（上限 {self._settings.atr_spike_multiplier} 倍）",
                )

        return RiskDecision.approve()

    # ------------------------------------------------------- 状態の更新

    def record_order_submitted(self, request: OrderRequest) -> None:
        """発注が実際に送信されたことを記録する（取引回数のカウント）。"""
        if request.reduce_only:
            return
        self._state.roll(self._clock(), self._tz)
        self._state.daily_trades += 1

    def record_trade_closed(self, symbol: str, realized_pnl: Decimal) -> None:
        """決済損益を反映し、必要なら取引を停止する。

        Args:
            symbol: 決済した銘柄。
            realized_pnl: 確定損益（口座通貨建て、損失は負値）。
        """
        now = self._clock()
        self._state.roll(now, self._tz)
        self._state.daily_pnl += realized_pnl
        self._state.weekly_pnl += realized_pnl

        if realized_pnl < 0:
            self._state.last_loss_at[symbol] = now

        self._check_halt()
        self.save()

    def _check_halt(self) -> None:
        """損失が上限を超えていれば停止フラグを立てる。

        equity ではなく「その期間の開始時点の資金」で判定したいところだが、
        ブローカーによっては期初残高を取れないため、
        ここでは損失額そのものと設定比率 × 現在 equity を比較せず、
        呼び出し側が渡した基準額（:meth:`set_reference_equity`）を使う。
        """
        reference = self._reference_equity
        if reference is None or reference <= 0:
            return

        daily_limit = reference * self._settings.max_daily_loss
        weekly_limit = reference * self._settings.max_weekly_loss

        if self._state.weekly_pnl <= -weekly_limit:
            self._halt("weekly_loss_limit", self._state.weekly_pnl, weekly_limit)
        elif self._state.daily_pnl <= -daily_limit:
            self._halt("daily_loss_limit", self._state.daily_pnl, daily_limit)

    def _halt(self, rule: str, pnl: Decimal, limit: Decimal) -> None:
        if self._state.halt_reason == rule:
            return
        self._state.halt_reason = rule
        logger.error(
            "取引を停止しました: %s（損益 %s / 上限 -%s）",
            rule,
            pnl,
            limit,
            extra={"rule": rule, "pnl": str(pnl), "limit": str(limit)},
        )

    def set_reference_equity(self, equity: Decimal) -> None:
        """損失上限の基準となる資金額を設定する。

        通常は「その日の取引開始時点の equity」を渡す。
        StrategyRunner が起動時と日付更新時に呼ぶ。
        """
        self._reference_equity = equity

    def resume(self) -> None:
        """手動で取引停止を解除する。運用者の明示的な操作でのみ呼ぶ。"""
        if self._state.halt_reason is not None:
            logger.warning("取引停止を手動解除しました（理由: %s）", self._state.halt_reason)
        self._state.halt_reason = None
        self.save()

    # ------------------------------------------------------------- 永続化

    def save(self) -> None:
        """状態をJSONへ書き出す。``state_path`` 未設定なら何もしない。"""
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # アトミックに差し替え、書き込み途中のファイルを読ませない。
        tmp.replace(self._state_path)

    @classmethod
    def load(
        cls,
        settings: RiskSettings,
        state_path: Path,
        *,
        contract_size: Decimal = Decimal(1),
        clock: Callable[[], datetime] = utcnow,
    ) -> RiskManager:
        """保存済み状態があれば復元して RiskManager を構築する。"""
        state: RiskState | None = None
        if state_path.is_file():
            try:
                state = RiskState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                # 壊れた状態ファイルで「損失上限に達していない」と誤認するのは危険。
                # 読めないことを明示して初期状態から始める。
                logger.error("リスク状態の読み込みに失敗しました（初期化します）: %s", exc)
        return cls(
            settings,
            contract_size=contract_size,
            state=state,
            state_path=state_path,
            clock=clock,
        )

    # ------------------------------------------------------------- ユーティリティ

    def summary(self) -> str:
        """日次サマリ通知用の文字列。"""
        self._state.roll(self._clock(), self._tz)
        status = f"停止中({self._state.halt_reason})" if self._state.halt_reason else "稼働中"
        return (
            f"[{self._state.day_key}] {status} / "
            f"日次損益 {self._state.daily_pnl:+.2f} / "
            f"週次損益 {self._state.weekly_pnl:+.2f} / "
            f"取引 {self._state.daily_trades} 回"
        )

    @staticmethod
    def _reference_price(request: OrderRequest, ticker: Ticker | None) -> Decimal | None:
        """リスク計算に使う価格。指値 > 逆指値 > 気配値の順で採用する。"""
        if request.limit_price is not None:
            return request.limit_price
        if request.stop_price is not None:
            return request.stop_price
        if ticker is not None:
            return ticker.price_for(request.side)
        return None
