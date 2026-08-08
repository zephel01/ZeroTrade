"""ブートストラップによるロバストネス検定。

**「この成績は運か」を、いちばん安く判定するための道具。**

バックテストが返す数字は1本の経路でしかない。同じ戦略・同じ相場でも、
トレードの並び順が少し違えば最終損益も最大ドローダウンも変わる。
1本の経路だけを見て「勝てる」と判断するのは、コインを10回投げて
7回表が出たのを見て「このコインは表が出やすい」と言うのに近い。

やっていることは単純で、**確定したトレードの損益列から復元抽出で
同じ件数を引き直し、それを何千回も繰り返す**。得られた分布の中で、
実際に観測した成績がどのあたりに位置するかを見る。

分かること:

* 資金を減らして終わる確率（``loss_probability``）
* 最大ドローダウンの現実的な上限（95パーセンタイル）。
  観測した最大DDは、たいてい「運が良かった経路」の値でしかない
* この優位性を偶然と区別するのに必要なトレード件数（``required_trades``）

分からないこと（重要）:

* **相場の変化には答えない。** 過去のトレードの分布を使い回すだけなので、
  相場つきが変わって優位性が消える可能性は評価できない。
* **トレードの独立性を仮定している。** 連敗が連鎖する戦略や、
  同時に複数銘柄を持つ運用では、実際のドローダウンはここで出る値より
  深くなりうる。
* 元のトレード件数が少なければ、分布そのものが当てにならない。
  ``trades`` が2桁前半なら、出てくる数字は目安以上のものではない。
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from zerotrade.log import get_logger

__all__ = ["RobustnessReport", "bootstrap", "required_trades"]

logger = get_logger(__name__)

#: 復元抽出の総手数がこれを超えると打ち切る。件数×回数で効いてくる。
_MAX_DRAWS = 4_000_000

#: 95% 両側の正規近似。必要件数の目安に使う。
_Z95 = 1.959964


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    """ブートストラップの結果。"""

    trades: int
    iterations: int
    observed_net_pnl: Decimal
    observed_max_drawdown: Decimal

    mean_per_trade: Decimal
    """1トレードあたりの平均損益。優位性の推定値。"""

    stdev_per_trade: Decimal

    net_pnl_p05: Decimal
    net_pnl_p50: Decimal
    net_pnl_p95: Decimal
    max_drawdown_p50: Decimal
    max_drawdown_p95: Decimal
    """最大ドローダウンの95パーセンタイル。**資金計画はここを基準にする。**"""

    loss_probability: float
    """引き直した経路のうち、最終損益がマイナスで終わった割合。"""

    required_trades: int | None
    """観測した優位性を偶然と区別するのに必要なトレード件数（95%・正規近似）。

    平均が0以下なら区別する対象が無いので ``None``。
    """

    @property
    def is_significant(self) -> bool:
        """5パーセンタイルがプラス側にあるか。

        これを満たさない成績を「勝てる戦略を見つけた」と呼ばない。
        """
        return self.net_pnl_p05 > 0

    def describe(self) -> str:
        """人が読む用の複数行サマリ。"""
        lines = [
            f"検定: {self.trades}件のトレードを {self.iterations:,}回 引き直し",
            f"  1トレードあたり: {self.mean_per_trade:+,.2f}"
            f"（標準偏差 {self.stdev_per_trade:,.2f}）",
            f"  最終損益: 観測 {self.observed_net_pnl:+,.0f} / "
            f"5% {self.net_pnl_p05:+,.0f} / 中央 {self.net_pnl_p50:+,.0f} / "
            f"95% {self.net_pnl_p95:+,.0f}",
            f"  最大DD: 観測 {self.observed_max_drawdown:,.0f} / "
            f"中央 {self.max_drawdown_p50:,.0f} / 95% {self.max_drawdown_p95:,.0f}",
            f"  資金を減らして終わる確率: {self.loss_probability:.1%}",
        ]
        if self.required_trades is not None:
            lines.append(
                f"  この優位性を偶然と区別するのに必要な件数: 約 {self.required_trades:,}件"
                f"（現在 {self.trades}件）"
            )
        else:
            lines.append("  平均がプラスではないので、必要件数は算出しない")
        lines.append(
            "  判定: "
            + (
                "5パーセンタイルがプラス側にある"
                if self.is_significant
                else "5パーセンタイルがマイナス側。運と区別がついていない"
            )
        )
        return "\n".join(lines)


def bootstrap(
    pnls: Iterable[Decimal],
    *,
    iterations: int = 2_000,
    seed: int = 20260808,
) -> RobustnessReport:
    """トレードの損益列を復元抽出で引き直し、成績の分布を出す。

    Args:
        pnls: 確定損益の列（**古い順である必要はない**。並びは引き直しで壊れる）。
        iterations: 引き直しの回数。多いほど分布が滑らかになる。
        seed: 乱数の種。既定を固定してあるので、同じ入力なら同じ結果が出る。

    Returns:
        観測値とブートストラップ分布をまとめた結果。

    Raises:
        ValueError: トレードが2件未満の場合。分布を作りようがない。
    """
    values = list(pnls)
    if len(values) < 2:
        raise ValueError(f"トレードが少なすぎます（{len(values)}件）。2件以上必要です")
    if iterations < 1:
        raise ValueError("iterations は1以上で指定してください")

    count = len(values)
    if count * iterations > _MAX_DRAWS:
        reduced = max(200, _MAX_DRAWS // count)
        logger.warning(
            "トレードが多いため引き直しを %d 回から %d 回へ減らしました", iterations, reduced
        )
        iterations = reduced

    observed_net, observed_dd = _walk(values)
    mean = sum(values, Decimal(0)) / Decimal(count)
    stdev = _stdev(values, mean)

    rng = random.Random(seed)
    nets: list[Decimal] = []
    drawdowns: list[Decimal] = []
    losses = 0
    for _ in range(iterations):
        sample = rng.choices(values, k=count)
        net, drawdown = _walk(sample)
        nets.append(net)
        drawdowns.append(drawdown)
        if net <= 0:
            losses += 1

    nets.sort()
    drawdowns.sort()

    return RobustnessReport(
        trades=count,
        iterations=iterations,
        observed_net_pnl=observed_net,
        observed_max_drawdown=observed_dd,
        mean_per_trade=mean,
        stdev_per_trade=stdev,
        net_pnl_p05=_percentile(nets, 0.05),
        net_pnl_p50=_percentile(nets, 0.50),
        net_pnl_p95=_percentile(nets, 0.95),
        max_drawdown_p50=_percentile(drawdowns, 0.50),
        max_drawdown_p95=_percentile(drawdowns, 0.95),
        loss_probability=losses / iterations,
        required_trades=required_trades(mean, stdev),
    )


def required_trades(mean: Decimal, stdev: Decimal, *, z: float = _Z95) -> int | None:
    """観測した優位性を「0ではない」と言うのに必要なトレード件数。

    正規近似で ``n ≈ (z * σ / μ)²``。ばらつきが平均の10倍あれば
    約400件必要、という当たり前の関係を数字にしただけのもの。
    **前向き検証を何件で打ち切るかを、勘ではなくこれで決める。**

    Returns:
        必要件数。平均が0以下、またはばらつきが0なら ``None``。
    """
    if mean <= 0 or stdev <= 0:
        return None
    ratio = float(stdev) / float(mean)
    return max(1, math.ceil((z * ratio) ** 2))


# ---------------------------------------------------------------- 内部


def _walk(pnls: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    """損益列を順に足して、最終損益と最大ドローダウンを返す。"""
    cumulative = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return cumulative, max_drawdown


def _stdev(values: Sequence[Decimal], mean: Decimal) -> Decimal:
    """標本標準偏差。平方根だけ float を経由する。"""
    if len(values) < 2:
        return Decimal(0)
    variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / Decimal(len(values) - 1)
    return Decimal(str(math.sqrt(float(variance))))


def _percentile(sorted_values: Sequence[Decimal], fraction: float) -> Decimal:
    """昇順に並んだ列のパーセンタイル（最近傍）。"""
    if not sorted_values:
        return Decimal(0)
    index = round(fraction * (len(sorted_values) - 1))
    return sorted_values[min(max(index, 0), len(sorted_values) - 1)]
