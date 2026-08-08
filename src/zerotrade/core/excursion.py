"""MFE / MAE（建玉中の最大含み益・最大含み損）の追跡。

確定損益だけを見ていると「エントリーが悪いのか、出口が悪いのか」が
区別できない。同じ ``-1,000`` の負けでも、一度も含み益にならなかった
トレードと、含み益 ``+5,000`` を取り逃してから負けたトレードでは
直すべき場所がまったく違う。

* **MFE**（maximum favorable excursion）— 建玉中に到達した最大の含み益。
  実現損益がこれに遠く及ばないなら、問題は出口にある。
* **MAE**（maximum adverse excursion）— 建玉中に到達した最大の含み損。
  勝ちトレードの MAE が大きいなら、ストップが広すぎるか、
  エントリーの位置が早い。

金額の単位は :attr:`~zerotrade.models.ClosedTrade.realized_pnl` と同じ
口座通貨建てにしてある。確定損益とそのまま比較できないと意味がないため。

追跡の粒度は呼び出し側に依存する。バックテストとペーパーは
ローソク足の高値・安値で1本ずつ観測するので実質的な上限・下限を捉えるが、
ライブはループごとの気配値を標本にするだけなので、
ループ間の突発的な行き過ぎは取りこぼす。**過小評価する方向にしか
外れない**ので、判断材料としては安全側に倒れる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from zerotrade.models import Position, Side

__all__ = ["Excursion", "ExcursionTracker"]


@dataclass(frozen=True, slots=True)
class Excursion:
    """1つの建玉についての含み損益の振れ幅。"""

    favorable: Decimal = Decimal(0)
    """最大含み益（MFE）。0以上。"""

    adverse: Decimal = Decimal(0)
    """最大含み損（MAE）。0以下。"""

    def extended(self, favorable: Decimal, adverse: Decimal) -> Excursion:
        """観測値を取り込んだ新しい値を返す。"""
        return Excursion(
            favorable=max(self.favorable, favorable),
            adverse=min(self.adverse, adverse),
        )

    def scaled(self, ratio: Decimal) -> Excursion:
        """建玉の一部だけを決済したときの取り分。

        振れ幅は建玉全体の数量で計算しているので、部分決済では
        決済した割合を掛けて按分する。
        """
        return Excursion(favorable=self.favorable * ratio, adverse=self.adverse * ratio)


class ExcursionTracker:
    """銘柄ごとに建玉中の振れ幅を保持する。

    建玉の同一性は ``opened_at`` で判定する。時刻が変わっていれば
    別の建玉とみなして観測をやり直す。決済して建て直した直後に
    前の建玉の振れ幅が混ざるのを防ぐため。
    """

    def __init__(self, contract_size: Decimal = Decimal(1)) -> None:
        self._contract_size = contract_size
        self._values: dict[str, Excursion] = {}
        self._opened_at: dict[str, datetime] = {}

    # ------------------------------------------------------------ 観測

    def observe_range(self, position: Position, high: Decimal, low: Decimal) -> None:
        """足の高値・安値から振れ幅を更新する。

        バックテストとペーパー向け。1本の足の中で実際にどこまで動いたかを
        使えるので、気配値の標本より正確になる。
        """
        self._ensure(position)
        if position.side is Side.BUY:
            favorable = self._amount(position, high)
            adverse = self._amount(position, low)
        else:
            favorable = self._amount(position, low)
            adverse = self._amount(position, high)
        current = self._values[position.symbol]
        self._values[position.symbol] = current.extended(favorable, adverse)

    def observe_price(self, position: Position, price: Decimal) -> None:
        """現在値1点から振れ幅を更新する。ライブ実行向け。"""
        self.observe_range(position, price, price)

    # ------------------------------------------------------------ 取り出し

    def snapshot(self, symbol: str, *, ratio: Decimal = Decimal(1)) -> Excursion | None:
        """現時点の振れ幅を返す（保持したまま）。

        Args:
            symbol: 対象銘柄。
            ratio: 決済した数量の割合。部分決済のときに按分する。
        """
        value = self._values.get(symbol)
        if value is None:
            return None
        return value if ratio == 1 else value.scaled(ratio)

    def forget(self, symbol: str) -> None:
        """建玉が完全に消えたので観測を捨てる。"""
        self._values.pop(symbol, None)
        self._opened_at.pop(symbol, None)

    # ------------------------------------------------------------ 内部

    def _ensure(self, position: Position) -> None:
        previous = self._opened_at.get(position.symbol)
        if previous is None or previous != position.opened_at:
            self._values[position.symbol] = Excursion()
            self._opened_at[position.symbol] = position.opened_at

    def _amount(self, position: Position, price: Decimal) -> Decimal:
        """指定価格での含み損益（口座通貨建て）。"""
        return position.pnl_at(price) * self._contract_size
