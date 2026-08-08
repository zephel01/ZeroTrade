"""前向き検証6本の現況を1画面で見る。

6銘柄を別プロセスで回しているため、`dashboard` や `report` は銘柄ごとにしか
開けない。6枚並べるのは現実的でないので、まとめて見るためのもの。

    python3 scripts/forward_watch.py                    # config/forward/
    python3 scripts/forward_watch.py --watch            # 10秒ごとに更新
    python3 scripts/forward_watch.py --group forward2   # 別の検証グループ

**表示するだけで、何も変更しない。** 判定は
``python3 scripts/forward_judge.py`` が行う。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from zerotrade.store import Store


def _width(text: str) -> int:
    """端末上の表示幅。日本語は2桁ぶん取る。

    Python の書式指定は文字数で数えるため、日本語が混じると桁がずれる。
    毎日眺めるものなので揃えておく。
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    text = _clip(text, width)
    space = " " * max(0, width - _width(text))
    return space + text if right else text + space


def _clip(text: str, width: int) -> str:
    """表示幅で切り詰める。1つ長い値が表全体の桁を崩すのを防ぐ。"""
    if _width(text) <= width:
        return text
    out = ""
    for char in text:
        if _width(out) + _width(char) > width - 1:
            return out + "…"
        out += char
    return out


def _price(value: object) -> str:
    """価格を読める桁に落とす。

    ATR から計算したストップは ``64692.01652383044993090602514`` のように
    端数が続く。取引所へ送る値は刻みに丸めているが、シャドー実行は手元で
    完結するため生の値が残る。表示だけ整える。
    """
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return str(value)
    exponent = -8 if abs(number) < 1 else (-4 if abs(number) < 1000 else -2)
    return f"{number.quantize(Decimal(1).scaleb(exponent)).normalize():f}"


#: 判定に必要なトレード数。forward_judge.py と揃えること。
TARGET_TRADES = 60


def _is_running(name: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"config/forward/{name}.yaml run"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _shadow_state(state_dir: Path) -> dict[str, object]:
    path = state_dir / "shadow_state.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _db_summary(state_dir: Path) -> dict[str, object]:
    path = state_dir / "zerotrade.db"
    if not path.is_file():
        return {"trades": 0, "realized": Decimal(0), "equity": None, "updated": None}
    try:
        with Store.open_for_read(path) as store:
            trades = store.trades(limit=100_000)
            curve = store.equity_curve(limit=1)
    except Exception:
        return {"trades": 0, "realized": Decimal(0), "equity": None, "updated": None}

    return {
        "trades": len(trades),
        "realized": sum((t.realized_pnl for t in trades), Decimal(0)),
        "equity": curve[-1].equity if curve else None,
        "updated": curve[-1].created_at if curve else None,
    }


def _age(stamp: datetime | None) -> str:
    if stamp is None:
        return "—"
    seconds = (datetime.now(UTC) - stamp).total_seconds()
    if seconds < 120:
        return f"{seconds:.0f}秒前"
    if seconds < 7200:
        return f"{seconds / 60:.0f}分前"
    return f"{seconds / 3600:.1f}時間前"


def render(group: str) -> str:
    config_root = Path("config") / group
    state_root = Path("state") / group
    names = sorted(p.stem for p in config_root.glob("*.yaml"))
    lines = [
        f"前向き検証の現況 [{group}]  {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC",
        "",
        "  "
        + _pad("銘柄", 8)
        + _pad("稼働", 6)
        + _pad("建玉", 34)
        + _pad("確定損益", 14, right=True)
        + _pad("件数", 6, right=True)
        + _pad("更新", 10, right=True),
        f"  {'-' * 76}",
    ]

    total_trades = 0
    total_realized = Decimal(0)
    running = 0

    for name in names:
        state_dir = state_root / name
        alive = _is_running(name)
        running += int(alive)
        shadow = _shadow_state(state_dir)
        summary = _db_summary(state_dir)

        positions = shadow.get("positions") or []
        if positions:
            p = positions[0]
            side = "買" if str(p.get("side", "")).lower() == "buy" else "売"
            stop = p.get("stop_loss")
            held = f"{side} {_price(p.get('quantity'))} @ {_price(p.get('entry_price'))}"
            held += f" 逆{_price(stop)}" if stop else " **ストップ無し**"
        else:
            held = "なし"

        trades = int(summary["trades"])  # type: ignore[call-overload]
        realized = summary["realized"]
        total_trades += trades
        total_realized += realized  # type: ignore[operator]

        lines.append(
            "  "
            + _pad(name, 8)
            + _pad("○" if alive else "×", 6)
            + _pad(held, 34)
            + _pad(f"{float(realized):+,.1f}", 14, right=True)
            + _pad(str(trades), 6, right=True)
            + _pad(_age(summary["updated"]), 10, right=True)  # type: ignore[arg-type]
        )

    lines += [
        f"  {'-' * 76}",
        f"  稼働 {running}/{len(names)}    "
        f"確定損益 合計 {float(total_realized):+,.1f} USDT    "
        f"決済済み {total_trades} / {TARGET_TRADES} 件",
        "",
    ]

    if not names:
        lines.append(f"  設定がありません: config/{group}/*.yaml")
    elif running < len(names):
        lines.append(
            f"  停止しているものがあります。scripts/forward_start.sh {group} で再開できます。"
        )
    if total_trades >= TARGET_TRADES:
        lines.append(
            f"  **{TARGET_TRADES}件に到達しました。** "
            f"python3 scripts/forward_judge.py --group {group} で判定できます。"
        )
    elif names:
        lines.append(
            f"  判定まであと {TARGET_TRADES - total_trades} 件"
            "（この時点の成績を見て設定を変えないこと）"
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="前向き検証6本の現況を表示する")
    parser.add_argument("--watch", action="store_true", help="10秒ごとに更新する")
    parser.add_argument(
        "--group", default="forward", help="検証グループ名（config/<group>/ を見る）"
    )
    args = parser.parse_args()

    if not args.watch:
        print(render(args.group))
        return 0

    try:
        while True:
            print("\033[2J\033[H" + render(args.group), flush=True)
            time.sleep(10)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
