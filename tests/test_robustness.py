"""ブートストラップ検定のテスト。"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from zerotrade.backtest.robustness import bootstrap, required_trades


def _pnls(values: list[int | str]) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def test_同じ種なら同じ結果になる() -> None:
    """検定そのものが再現できないと、判断の根拠として使えない。"""
    pnls = _pnls([100, -50, 30, -20, 80, -10, 40, -60, 90, -30])
    first = bootstrap(pnls, iterations=200, seed=1)
    second = bootstrap(pnls, iterations=200, seed=1)
    assert first == second


def test_種が違えば分布はずれる() -> None:
    pnls = _pnls([100, -50, 30, -20, 80, -10, 40, -60, 90, -30])
    assert bootstrap(pnls, iterations=200, seed=1) != bootstrap(pnls, iterations=200, seed=2)


def test_観測値は入力そのものから計算する() -> None:
    """観測した最終損益と最大DDは、引き直しの影響を受けてはいけない。"""
    report = bootstrap(_pnls([100, -300, 250]), iterations=50)
    assert report.observed_net_pnl == Decimal(50)
    assert report.observed_max_drawdown == Decimal(300)


def test_全勝なら資金を減らす確率は0になる() -> None:
    report = bootstrap(_pnls([10, 20, 30, 40]), iterations=300)
    assert report.loss_probability == 0.0
    assert report.is_significant


def test_全敗なら5パーセンタイルはマイナス側にとどまる() -> None:
    report = bootstrap(_pnls([-10, -20, -30, -40]), iterations=300)
    assert report.loss_probability == 1.0
    assert not report.is_significant
    assert report.required_trades is None


def test_プラスで終わっていても運と区別がつかないことがある() -> None:
    """勝率50%のコイン投げを80回。合計はプラスに出るが、優位性は無い。

    最終損益がプラスというだけで「勝てる戦略」と呼ばないための検定なので、
    ここが素通りすると道具として意味を失う。
    """
    rng = random.Random(11)
    pnls = [Decimal(rng.choice([100, -100])) for _ in range(80)]
    report = bootstrap(pnls, iterations=500, seed=3)

    assert report.observed_net_pnl > 0  # 見た目は勝っている
    assert not report.is_significant  # 5パーセンタイルはマイナス側
    assert report.required_trades is not None
    assert report.required_trades > report.trades  # 件数がまるで足りない


def test_パーセンタイルは順序どおりに並ぶ() -> None:
    report = bootstrap(_pnls([50, -20, 70, -40, 15, 5, -5, 30]), iterations=400)
    assert report.net_pnl_p05 <= report.net_pnl_p50 <= report.net_pnl_p95
    assert report.max_drawdown_p50 <= report.max_drawdown_p95


def test_必要件数はばらつきの二乗で効く() -> None:
    """ばらつきが2倍になれば、必要な件数は約4倍になる。"""
    few = required_trades(Decimal(1), Decimal(10))
    many = required_trades(Decimal(1), Decimal(20))
    assert few is not None and many is not None
    assert 3.5 < many / few < 4.5


def test_平均がプラスでなければ必要件数は出さない() -> None:
    assert required_trades(Decimal(0), Decimal(10)) is None
    assert required_trades(Decimal(-1), Decimal(10)) is None
    assert required_trades(Decimal(1), Decimal(0)) is None


def test_トレードが1件以下なら検定できない() -> None:
    with pytest.raises(ValueError, match="少なすぎます"):
        bootstrap(_pnls([100]))
    with pytest.raises(ValueError, match="少なすぎます"):
        bootstrap([])


def test_回数が0以下なら弾く() -> None:
    with pytest.raises(ValueError, match="iterations"):
        bootstrap(_pnls([10, -5]), iterations=0)


def test_describe_に主要な数字が出る() -> None:
    text = bootstrap(_pnls([100, -50, 30, -20, 80]), iterations=100).describe()
    assert "5件" in text
    assert "資金を減らして終わる確率" in text
    assert "判定" in text


def test_件数が多いと引き直しの回数を自動で減らす() -> None:
    """計算量が爆発して検定そのものを回さなくなるほうが害が大きい。"""
    report = bootstrap(_pnls([1, -1] * 3_000), iterations=2_000)
    assert report.iterations < 2_000
    assert report.trades == 6_000
