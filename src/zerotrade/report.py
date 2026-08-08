"""静的HTMLレポートの生成。

サーバーもJavaScriptもCDNも使わない、**単体で開ける1枚のHTML**を吐く。
理由は三つある。ブラウザで開くだけで見られること、Discordへ添付したり
別マシンへコピーしても壊れないこと、そしてポートを開けずに済むこと。

グラフはインラインSVGを手で組み立てている。チャートライブラリを入れると
CDN依存かビルド工程のどちらかが増えるが、equityカーブ1本のために
払うコストとしては高い。
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from zerotrade.models import utcnow
from zerotrade.store import Store
from zerotrade.store.models import (
    EquityPoint,
    EventRow,
    ExecutionQuality,
    PerformanceSummary,
    RejectionRow,
    TradeRow,
)

__all__ = ["build_report", "format_price", "render_report"]

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
  --card: #f9fafb; --win: #16794e; --loss: #b91c1c; --accent: #2563eb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a; --fg: #e8e8e8; --muted: #9aa1ac; --line: #2a2e35;
    --card: #1c1f25; --win: #4ade80; --loss: #f87171; --accent: #60a5fa;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif;
  font-size: 14px; line-height: 1.7; max-width: 1000px; margin-inline: auto;
}
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: .01em; }
h2 { font-size: 15px; margin: 36px 0 12px; font-weight: 600;
     padding-bottom: 6px; border-bottom: 1px solid var(--line); }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 8px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         gap: 10px; margin-top: 20px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
        padding: 12px 14px; }
.tile .label { color: var(--muted); font-size: 11px; letter-spacing: .04em; }
.tile .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums;
               margin-top: 2px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums;
        font-size: 13px; }
th { text-align: left; color: var(--muted); font-weight: 500; font-size: 11px;
     letter-spacing: .04em; padding: 6px 10px; border-bottom: 1px solid var(--line); }
td { padding: 7px 10px; border-bottom: 1px solid var(--line); }
tbody tr:last-child td { border-bottom: none; }
.num { text-align: right; }
.win { color: var(--win); }
.loss { color: var(--loss); }
.muted { color: var(--muted); }
.empty { color: var(--muted); padding: 16px 0; font-style: normal; }
svg { display: block; width: 100%; height: auto; }
footer { margin-top: 40px; color: var(--muted); font-size: 12px;
         border-top: 1px solid var(--line); padding-top: 12px; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value))


def _money(value: Decimal) -> str:
    """符号付きの金額表記。"""
    return f"{value:+,.0f}"


def format_price(value: Decimal) -> str:
    """価格の表示用整形。

    ATR から逆算したストップ価格は割り切れずに小数28桁まで伸びる。
    表示に必要なのは実際の呼値の刻みまでなので、5桁で丸めて末尾のゼロを落とす。
    """
    quantized = value.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
    return f"{quantized.normalize():f}"


def _pnl_cell(value: Decimal) -> str:
    cls = "win" if value > 0 else "loss" if value < 0 else "muted"
    return f'<td class="num {cls}">{_esc(_money(value))}</td>'


def _tile(label: str, value: str, *, tone: str = "") -> str:
    cls = f' class="value {tone}"' if tone else ' class="value"'
    return (
        f'<div class="tile"><div class="label">{_esc(label)}</div>'
        f"<div{cls}>{_esc(value)}</div></div>"
    )


def _equity_svg(points: Sequence[EquityPoint], *, width: int = 940, height: int = 220) -> str:
    """equity 推移のインラインSVG。

    2点未満では線を引けないので、その旨を表示する。
    """
    if len(points) < 2:
        return '<p class="empty">equity の記録が足りません（2点以上必要です）。</p>'

    values = [p.equity for p in points]
    low, high = min(values), max(values)
    span = high - low
    if span == 0:
        # 完全な横ばい。上下に余白を作って中央に線を引く。
        span = Decimal(1)
        low -= Decimal("0.5")

    pad = 8
    inner_h = height - pad * 2
    step = Decimal(width) / Decimal(len(points) - 1)

    coords = []
    for i, value in enumerate(values):
        x = float(Decimal(i) * step)
        ratio = (value - low) / span
        y = pad + inner_h * (1 - float(ratio))
        coords.append(f"{x:.1f},{y:.1f}")

    line = " ".join(coords)
    area = f"0,{height} {line} {width},{height}"
    gained = values[-1] >= values[0]
    stroke = "var(--win)" if gained else "var(--loss)"

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="equity 推移（{_esc(_money(values[-1] - values[0]))}）">'
        f'<polygon points="{area}" fill="{stroke}" opacity="0.10"/>'
        f'<polyline points="{line}" fill="none" stroke="{stroke}" '
        f'stroke-width="1.8" stroke-linejoin="round"/>'
        f"</svg>"
        f'<p class="sub">{_esc(points[0].created_at.strftime("%Y-%m-%d %H:%M"))} 〜 '
        f"{_esc(points[-1].created_at.strftime('%Y-%m-%d %H:%M'))} / "
        f"最小 {_esc(f'{low:,.0f}')} 〜 最大 {_esc(f'{high:,.0f}')}</p>"
    )


def _summary_tiles(summary: PerformanceSummary, currency: str) -> str:
    factor = summary.profit_factor
    tone = "win" if summary.net_pnl > 0 else "loss" if summary.net_pnl < 0 else ""
    tiles = [
        _tile("確定損益", f"{_money(summary.net_pnl)} {currency}", tone=tone),
        _tile("トレード数", str(summary.trades)),
        _tile("勝率", f"{summary.win_rate:.1%}"),
        _tile("平均利益", _money(summary.average_win)),
        _tile("平均損失", _money(-summary.average_loss)),
        _tile("プロフィットファクタ", "—" if factor is None else f"{factor:.2f}"),
        _tile("期待値/トレード", _money(summary.expectancy)),
        _tile("最大ドローダウン", f"-{summary.max_drawdown:,.0f}"),
    ]
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _quality_tiles(quality: ExecutionQuality, currency: str) -> str:
    """約定品質。**優位性と同じ単位（bp）で並べる。**

    バックテストはコストを仮定で置いている。その仮定が実測に負けていないかは、
    成績そのものより先に見るべき数字になる。
    """
    if quality.fills == 0 and quality.trades_with_excursion == 0:
        return (
            '<p class="empty">約定品質の記録がまだありません'
            "（想定価格を残した注文が約定すると貯まります）。</p>"
        )

    capture = quality.average_capture_ratio
    tiles = [
        _tile("滑り 平均", f"{quality.average_slippage_bp:+.2f} bp"),
        _tile(
            "滑り 最悪",
            f"{quality.worst_slippage_bp:+.2f} bp",
            tone="loss" if quality.worst_slippage_bp > 0 else "",
        ),
        _tile("約定件数", str(quality.fills)),
        _tile("MFE 平均", f"{_money(quality.average_mfe)} {currency}"),
        _tile("MAE 平均", f"{_money(quality.average_mae)} {currency}"),
        _tile("取り切り率", "—" if capture is None else f"{capture:.0%}"),
        _tile("勝ちの平均MAE", f"{_money(quality.winners_average_mae)} {currency}"),
        _tile("MFE/MAE 記録数", str(quality.trades_with_excursion)),
    ]
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _trades_table(trades: Sequence[TradeRow]) -> str:
    if not trades:
        return '<p class="empty">まだ決済されたトレードがありません。</p>'

    rows = []
    for t in trades:
        rows.append(
            "<tr>"
            f'<td class="muted">{_esc(t.closed_at.strftime("%m/%d %H:%M"))}</td>'
            f"<td>{_esc(t.symbol)}</td>"
            f"<td>{_esc(t.side)}</td>"
            f'<td class="num">{_esc(f"{t.quantity:,.0f}")}</td>'
            f'<td class="num">{_esc(format_price(t.entry_price))}</td>'
            f'<td class="num">{_esc(format_price(t.exit_price))}</td>'
            f"{_pnl_cell(t.realized_pnl)}"
            f'<td class="muted">{_esc(t.reason)}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>決済時刻</th><th>銘柄</th><th>方向</th>"
        '<th class="num">数量</th><th class="num">建値</th><th class="num">決済値</th>'
        '<th class="num">損益</th><th>要因</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _rejection_table(counts: dict[str, int], recent: Sequence[RejectionRow]) -> str:
    """却下されたルールの内訳。

    「どのルールが実際に効いたか」は、設定を緩めるべきか締めるべきかを
    判断する唯一の一次情報になる。
    """
    if not counts:
        return '<p class="empty">リスク検査で却下された注文はありません。</p>'

    total = sum(counts.values())
    rows = [
        "<tr>"
        f"<td>{_esc(rule)}</td>"
        f'<td class="num">{count}</td>'
        f'<td class="num muted">{count / total:.0%}</td>'
        "</tr>"
        for rule, count in counts.items()
    ]
    table = (
        "<table><thead><tr><th>ルール</th>"
        '<th class="num">件数</th><th class="num">割合</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )

    if recent:
        latest = recent[0]
        table += (
            f'<p class="sub">直近: {_esc(latest.created_at.strftime("%m/%d %H:%M"))} '
            f"{_esc(latest.symbol)} — {_esc(latest.detail)}</p>"
        )
    return table


def _events_table(events: Sequence[EventRow]) -> str:
    if not events:
        return '<p class="empty">記録された節目はありません。</p>'
    rows = [
        "<tr>"
        f'<td class="muted">{_esc(e.created_at.strftime("%m/%d %H:%M"))}</td>'
        f"<td>{_esc(e.kind)}</td>"
        f'<td class="muted">{_esc(e.detail)}</td>'
        "</tr>"
        for e in events
    ]
    return (
        "<table><thead><tr><th>時刻</th><th>種別</th><th>内容</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_report(
    *,
    summary: PerformanceSummary,
    trades: Sequence[TradeRow],
    equity: Sequence[EquityPoint],
    rejection_counts: dict[str, int],
    rejections: Sequence[RejectionRow],
    events: Sequence[EventRow],
    quality: ExecutionQuality | None = None,
    currency: str = "JPY",
    period_label: str = "全期間",
    generated_at: datetime | None = None,
) -> str:
    """レポートのHTML文字列を組み立てる。

    Store に触れないので、任意のデータからテスト用のレポートも作れる。
    """
    stamp = (generated_at or utcnow()).strftime("%Y-%m-%d %H:%M:%S %Z").strip()

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ZeroTrade レポート</title>
<style>{_CSS}</style>
</head>
<body>
<h1>ZeroTrade レポート</h1>
<p class="sub">{_esc(period_label)} / 生成 {_esc(stamp)}</p>

{_summary_tiles(summary, currency)}

<h2>equity 推移</h2>
{_equity_svg(equity)}

<h2>約定品質</h2>
{_quality_tiles(quality or ExecutionQuality(), currency)}

<h2>トレード履歴</h2>
{_trades_table(trades)}

<h2>リスク検査で却下された注文</h2>
{_rejection_table(rejection_counts, rejections)}

<h2>運用の節目</h2>
{_events_table(events)}

<footer>
損益はすべて確定損益です。含み損益は集計に含めていません
（リスク管理の判定基準と揃えるため）。
</footer>
</body>
</html>
"""


def build_report(
    store: Store,
    output: Path,
    *,
    days: int | None = None,
    currency: str = "JPY",
    trade_limit: int = 200,
) -> Path:
    """記録層を読んでHTMLレポートを書き出す。

    Args:
        store: 読み取り対象の記録層。
        output: 出力先のHTMLファイル。
        days: 直近何日ぶんに絞るか。``None`` なら全期間。
        currency: 表示に使う通貨コード。
        trade_limit: 一覧に載せるトレードの最大件数。

    Returns:
        書き出したファイルのパス。
    """
    since = utcnow() - timedelta(days=days) if days else None
    label = f"直近 {days} 日" if days else "全期間"

    content = render_report(
        summary=store.performance(since=since),
        trades=store.trades(limit=trade_limit, since=since),
        equity=store.equity_curve(since=since),
        rejection_counts=store.rejection_counts(since=since),
        rejections=store.rejections(limit=1),
        events=store.events(limit=30),
        quality=store.execution_quality(since=since),
        currency=currency,
        period_label=label,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    return output
