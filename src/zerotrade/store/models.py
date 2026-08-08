"""記録層が返す読み取り用の行モデル。

ドメインモデル（:mod:`zerotrade.models`）とは意図的に分けてある。
こちらは「保存されたものを読み出した結果」であり、
戦略や発注に使うことは想定していない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

__all__ = [
    "EquityPoint",
    "EventRow",
    "ExecutionQuality",
    "PerformanceSummary",
    "RejectionRow",
    "SignalRow",
    "SlippageRow",
    "TradeRow",
]


@dataclass(frozen=True, slots=True)
class TradeRow:
    """決済済みトレード1件。"""

    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    realized_pnl: Decimal
    opened_at: datetime
    closed_at: datetime
    reason: str
    strategy: str

    mfe: Decimal | None = None
    """建玉中の最大含み益。古い記録には入っていないので ``None`` がありうる。"""

    mae: Decimal | None = None
    """建玉中の最大含み損（0以下）。"""

    @property
    def is_win(self) -> bool:
        return self.realized_pnl > 0

    @property
    def capture_ratio(self) -> Decimal | None:
        """含み益のピークのうち、実際に取れた割合。"""
        if self.mfe is None or self.mfe <= 0:
            return None
        return self.realized_pnl / self.mfe


@dataclass(frozen=True, slots=True)
class SignalRow:
    symbol: str
    action: str
    strategy: str
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RejectionRow:
    """リスク検査で却下された発注。

    「なぜ発注されなかったか」を後から追える唯一の記録なので、
    トレード履歴と同じ重みで残す。
    """

    symbol: str
    side: str
    quantity: Decimal
    rule: str
    detail: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SlippageRow:
    """1件の約定における、想定価格と実約定価格の差。"""

    symbol: str
    side: str
    reference_price: Decimal
    """発注を決めた時点で観測していた価格。"""

    average_price: Decimal
    slippage: Decimal
    """価格の単位での差。**正が不利側**（買いは高く、売りは安く約定した）。"""

    slippage_bp: Decimal
    """同じ差をベーシスポイントで表したもの。銘柄をまたいで比べるため。"""

    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionQuality:
    """約定品質のサマリ。

    **優位性と同じ単位（bp）で並べるための集計。** 1トレードあたりの
    優位性が 5bp しかないなら、往復の滑りが 3bp あるだけで
    半分以上が消える。バックテストの成績はこの部分を仮定で置いているので、
    実測が仮定を超えていないかを見続ける必要がある。
    """

    fills: int = 0
    average_slippage_bp: Decimal = Decimal(0)
    worst_slippage_bp: Decimal = Decimal(0)
    """最も不利だった1件。平均だけ見ていると外れ値を見落とす。"""

    trades_with_excursion: int = 0
    average_mfe: Decimal = Decimal(0)
    average_mae: Decimal = Decimal(0)
    """負の値。"""

    average_capture_ratio: Decimal | None = None
    """含み益のピーク合計のうち、実際に取れた割合。低いほど出口が悪い。

    1件ずつの比を平均するのではなく、**合計どうしの比**で出している。
    含み益がほぼ0のトレードで分母が潰れ、外れ値ひとつで指標が壊れるため。
    マイナスになることもある（伸びた利益を全部返した上で負けている状態）。
    """

    winners_average_mae: Decimal = Decimal(0)
    """勝ちトレードの平均 MAE。深いほど「耐えて勝っている」ことになる。"""

    def describe(self) -> str:
        """1行サマリ。"""
        if self.fills == 0 and self.trades_with_excursion == 0:
            return "約定品質: まだ記録がありません"
        parts = []
        if self.fills:
            parts.append(
                f"滑り 平均 {self.average_slippage_bp:+.2f}bp / "
                f"最悪 {self.worst_slippage_bp:+.2f}bp（{self.fills}件）"
            )
        if self.trades_with_excursion:
            capture = (
                "—" if self.average_capture_ratio is None else f"{self.average_capture_ratio:.0%}"
            )
            parts.append(
                f"MFE 平均 {self.average_mfe:+,.0f} / MAE 平均 {self.average_mae:+,.0f} / "
                f"取り切り率 {capture}（{self.trades_with_excursion}件）"
            )
        return " / ".join(parts)


@dataclass(frozen=True, slots=True)
class EquityPoint:
    created_at: datetime
    equity: Decimal
    used_margin: Decimal
    open_positions: int


@dataclass(frozen=True, slots=True)
class EventRow:
    """起動・停止・取引停止など、運用上の節目。"""

    kind: str
    detail: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    """トレード履歴から算出した成績。

    ``trades`` が 0 のときは全項目が 0 になる。
    「まだ判断材料が無い」ことを NaN ではなく 0 で表す。
    """

    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: Decimal = Decimal(0)
    gross_loss: Decimal = Decimal(0)
    """負けトレードの損失合計（正の値で保持）。"""

    net_pnl: Decimal = Decimal(0)
    max_drawdown: Decimal = Decimal(0)
    """確定損益の累積曲線における最大の落ち込み（正の値）。"""

    @property
    def win_rate(self) -> Decimal:
        if self.trades == 0:
            return Decimal(0)
        return Decimal(self.wins) / Decimal(self.trades)

    @property
    def average_win(self) -> Decimal:
        if self.wins == 0:
            return Decimal(0)
        return self.gross_profit / Decimal(self.wins)

    @property
    def average_loss(self) -> Decimal:
        if self.losses == 0:
            return Decimal(0)
        return self.gross_loss / Decimal(self.losses)

    @property
    def profit_factor(self) -> Decimal | None:
        """総利益 ÷ 総損失。負けが1件も無ければ None（値が定義できない）。"""
        if self.gross_loss == 0:
            return None
        return self.gross_profit / self.gross_loss

    @property
    def expectancy(self) -> Decimal:
        """1トレードあたりの期待損益。"""
        if self.trades == 0:
            return Decimal(0)
        return self.net_pnl / Decimal(self.trades)
