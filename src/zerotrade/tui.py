"""TUI ダッシュボード（Textual）。

取引プロセスとは **別プロセス** で動き、SQLite を読むだけ。
表示側が落ちても取引は続くし、取引を止めていても履歴は見られる。

唯一の書き込み操作が緊急停止で、これも
:class:`~zerotrade.control.KillSwitch` のファイルを作るだけ。
再開・手動決済は意図的に持たせていない（CLI に摩擦を残すため）。

Textual は任意依存なので、``pip install "zerotrade[ui]"`` で入れる。
未インストールなら :func:`run_dashboard` が分かりやすい案内を出して終了する。
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from zerotrade.control import KillSwitch
from zerotrade.models import utcnow
from zerotrade.report import format_price
from zerotrade.settings import Settings
from zerotrade.store import Store
from zerotrade.store.models import PerformanceSummary

__all__ = ["run_dashboard"]

_INSTALL_HINT = (
    "TUI ダッシュボードには textual が必要です。\n"
    '  pip install "zerotrade[ui]"\n'
    "を実行してから、もう一度 `zerotrade dashboard` を起動してください。"
)


def _money(value: Decimal) -> str:
    return f"{value:+,.0f}"


def _read_risk_state(state_dir: Path) -> dict[str, Any]:
    """リスク状態のJSONを読む。

    停止しているかどうかの正は SQLite ではなくこのファイル。
    取引プロセスが動いていなくても現在の停止状態を表示できる。
    """
    path = state_dir / "risk_state.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _build_app(settings: Settings) -> Any:
    """Textual の App サブクラスを組み立てて返す。

    import を関数内に閉じ込めてあるのは、textual 未インストールでも
    :mod:`zerotrade.tui` を import できるようにするため
    （CLI が案内メッセージを出すのに必要）。
    """
    from textual.app import App, ComposeResult
    from textual.binding import Binding, BindingType
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import DataTable, Footer, Header, Static

    kill_switch = KillSwitch(settings.state_dir)
    db_path = settings.database_path

    class Dashboard(App[None]):
        """ZeroTrade の監視画面。"""

        TITLE = "ZeroTrade"
        CSS = """
        Screen { layout: vertical; }
        #tiles { height: auto; padding: 1 2 0 2; }
        .tile {
            width: 1fr; height: 5; border: round $panel;
            padding: 0 1; margin-right: 1; content-align: left top;
        }
        #status { padding: 0 2; height: auto; color: $text-muted; }
        #panes { height: 1fr; padding: 1 2; }
        DataTable { height: 1fr; }
        .pane-title { padding: 1 0 0 0; text-style: bold; }
        """

        BINDINGS: ClassVar[list[BindingType]] = [
            Binding("q", "quit", "終了"),
            Binding("r", "reload", "更新"),
            Binding("s", "emergency_stop", "緊急停止"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._stop_armed_at: Any = None

        # ------------------------------------------------------------ 構築

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="tiles"):
                yield Static(id="tile-equity", classes="tile")
                yield Static(id="tile-pnl", classes="tile")
                yield Static(id="tile-rate", classes="tile")
                yield Static(id="tile-state", classes="tile")
            yield Static("", id="status")
            with VerticalScroll(id="panes"):
                yield Static("直近のトレード", classes="pane-title")
                yield DataTable(id="trades", zebra_stripes=True)
                yield Static("却下されたルール", classes="pane-title")
                yield DataTable(id="rejections", zebra_stripes=True)
                yield Static("運用の節目", classes="pane-title")
                yield DataTable(id="events", zebra_stripes=True)
            yield Footer()

        def on_mount(self) -> None:
            trades = self.query_one("#trades", DataTable)
            trades.add_columns("決済", "銘柄", "方向", "数量", "建値", "決済値", "損益", "要因")
            rejections = self.query_one("#rejections", DataTable)
            rejections.add_columns("ルール", "件数", "直近の理由")
            events = self.query_one("#events", DataTable)
            events.add_columns("時刻", "種別", "内容")

            self.reload()
            # 取引ループのポーリング間隔に合わせる必要はない。
            # 見ている人にとって自然な更新頻度は2秒程度。
            self.set_interval(2.0, self.reload)

        # ------------------------------------------------------------ 更新

        def reload(self) -> None:
            """SQLite を読み直して画面を作り替える。"""
            risk_state = _read_risk_state(settings.state_dir)
            halt_reason = risk_state.get("halt_reason")

            if not db_path.is_file():
                self._set_status(
                    f"記録がまだありません（{db_path}）。 `zerotrade run` を実行すると作られます。"
                )
                self._render_tiles(None, None, halt_reason, "JPY")
                return

            try:
                with Store.open_for_read(db_path) as store:
                    summary = store.performance()
                    latest = store.latest_equity()
                    trades = store.trades(limit=30)
                    counts = store.rejection_counts()
                    recent_rejections = store.rejections(limit=30)
                    events = store.events(limit=20)
            except (OSError, FileNotFoundError) as exc:
                self._set_status(f"記録を読めませんでした: {exc}")
                return

            self._render_tiles(summary, latest, halt_reason, "JPY")
            self._render_trades(trades)
            self._render_rejections(counts, recent_rejections)
            self._render_events(events)

            stamp = utcnow().strftime("%H:%M:%S")
            self._set_status(
                f"{db_path} を読み込みました（{stamp} UTC） / q 終了・r 更新・s 緊急停止"
            )

        def _render_tiles(
            self,
            summary: PerformanceSummary | None,
            latest: Any,
            halt_reason: str | None,
            currency: str,
        ) -> None:
            equity_text = "—" if latest is None else f"{latest.equity:,.0f} {currency}"
            positions = "—" if latest is None else f"建玉 {latest.open_positions}"
            self.query_one("#tile-equity", Static).update(
                f"[dim]EQUITY[/dim]\n[b]{equity_text}[/b]\n[dim]{positions}[/dim]"
            )

            if summary is None:
                pnl_text, rate_text = "—", "—"
            else:
                colour = "green" if summary.net_pnl >= 0 else "red"
                pnl_text = f"[{colour}]{_money(summary.net_pnl)}[/{colour}]"
                rate_text = f"{summary.win_rate:.0%}"
            trades_note = "—" if summary is None else f"{summary.trades} トレード"
            drawdown = "—" if summary is None else f"最大DD -{summary.max_drawdown:,.0f}"

            self.query_one("#tile-pnl", Static).update(
                f"[dim]確定損益[/dim]\n[b]{pnl_text}[/b]\n[dim]{trades_note}[/dim]"
            )
            self.query_one("#tile-rate", Static).update(
                f"[dim]勝率[/dim]\n[b]{rate_text}[/b]\n[dim]{drawdown}[/dim]"
            )

            if halt_reason:
                state = f"[red b]停止中[/red b]\n[dim]{halt_reason}[/dim]"
            else:
                state = "[green b]稼働可[/green b]\n[dim]損失上限に未達[/dim]"
            self.query_one("#tile-state", Static).update(f"[dim]状態[/dim]\n{state}")

        def _render_trades(self, trades: Any) -> None:
            table = self.query_one("#trades", DataTable)
            table.clear()
            for t in trades:
                colour = "green" if t.realized_pnl > 0 else "red"
                table.add_row(
                    t.closed_at.strftime("%m/%d %H:%M"),
                    t.symbol,
                    str(t.side),
                    f"{t.quantity:,.0f}",
                    format_price(t.entry_price),
                    format_price(t.exit_price),
                    f"[{colour}]{_money(t.realized_pnl)}[/{colour}]",
                    t.reason,
                )

        def _render_rejections(self, counts: dict[str, int], recent: Any) -> None:
            table = self.query_one("#rejections", DataTable)
            table.clear()
            latest_detail: dict[str, str] = {}
            for row in recent:
                latest_detail.setdefault(row.rule, row.detail)
            for rule, count in counts.items():
                table.add_row(rule, str(count), latest_detail.get(rule, ""))

        def _render_events(self, events: Any) -> None:
            table = self.query_one("#events", DataTable)
            table.clear()
            for e in events:
                table.add_row(e.created_at.strftime("%m/%d %H:%M"), e.kind, e.detail)

        def _set_status(self, text: str) -> None:
            self.query_one("#status", Static).update(text)

        # ------------------------------------------------------------ 操作

        def action_reload(self) -> None:
            self.reload()

        def action_emergency_stop(self) -> None:
            """緊急停止。誤爆を防ぐため2回押しで確定する。"""
            now = utcnow()
            armed = self._stop_armed_at
            if armed is not None and now - armed < timedelta(seconds=5):
                kill_switch.request("ダッシュボードからの緊急停止")
                self._stop_armed_at = None
                self._set_status(
                    f"⏹ 緊急停止を要求しました（{kill_switch.path}）。"
                    " 取引ループは次のループ境界で停止します。"
                )
                return

            self._stop_armed_at = now
            self._set_status("⚠ 本当に緊急停止しますか？ 5秒以内にもう一度 s を押してください。")

    return Dashboard


def run_dashboard(settings: Settings) -> int:
    """ダッシュボードを起動する。終了コードを返す。"""
    try:
        app_class = _build_app(settings)
    except ImportError:
        print(_INSTALL_HINT)
        return 1

    app_class().run()
    return 0
