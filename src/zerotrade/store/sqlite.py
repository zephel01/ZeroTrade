"""SQLite による記録層。

設計の要点:

* **WAL モードで開く。** ダッシュボードとレポートは取引プロセスとは
  別プロセスから同じファイルを読む。WAL なら書き込み中でも読み手が
  ブロックされないため、これがこの設計の前提になっている。
* **金額は文字列で保存する。** SQLite の REAL は倍精度浮動小数なので、
  Decimal で通してきた値をここで float に落とすと台無しになる。
* **時刻は ISO 8601 の文字列で保存する。** sqlite3 組み込みの
  datetime アダプタは Python 3.12 で非推奨になったため使わない。
* **書き込みは同期呼び出し。** ローカルSQLiteへの1行 INSERT は
  サブミリ秒で終わるため、取引ループから直接呼んでも実害がない。
  非同期化するとトランザクション境界の管理が複雑になる割に得るものが無い。

記録は取引の成否に影響してはならない。そのため書き込み失敗は
すべてログに落として握りつぶす（:meth:`Store._execute` を参照）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from zerotrade.log import get_logger
from zerotrade.models import (
    Balance,
    ClosedTrade,
    Order,
    OrderRequest,
    Signal,
    utcnow,
)
from zerotrade.store.models import (
    EquityPoint,
    EventRow,
    PerformanceSummary,
    RejectionRow,
    SignalRow,
    TradeRow,
)

__all__ = ["Store"]

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY,
    dedup_key     TEXT    NOT NULL UNIQUE,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    quantity      TEXT    NOT NULL,
    entry_price   TEXT    NOT NULL,
    exit_price    TEXT    NOT NULL,
    realized_pnl  TEXT    NOT NULL,
    opened_at     TEXT    NOT NULL,
    closed_at     TEXT    NOT NULL,
    reason        TEXT    NOT NULL DEFAULT '',
    strategy      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    status          TEXT NOT NULL,
    filled_quantity TEXT NOT NULL DEFAULT '0',
    average_price   TEXT,
    stop_loss       TEXT,
    take_profit     TEXT,
    strategy        TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);

CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY,
    symbol     TEXT NOT NULL,
    action     TEXT NOT NULL,
    strategy   TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at);

CREATE TABLE IF NOT EXISTS rejections (
    id         INTEGER PRIMARY KEY,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    quantity   TEXT NOT NULL,
    rule       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejections_created_at ON rejections(created_at);
CREATE INDEX IF NOT EXISTS idx_rejections_rule ON rejections(rule);

CREATE TABLE IF NOT EXISTS equity (
    id             INTEGER PRIMARY KEY,
    equity         TEXT NOT NULL,
    available      TEXT NOT NULL,
    used_margin    TEXT NOT NULL,
    currency       TEXT NOT NULL DEFAULT '',
    open_positions INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_created_at ON equity(created_at);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
"""


def _dec(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _opt_dec(value: Any) -> Decimal | None:
    return None if value is None else _dec(value)


def _time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return utcnow()


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class Store:
    """取引の記録を SQLite に残し、別プロセスから読めるようにする。

    Example:
        >>> with Store(Path("state/zerotrade.db")) as store:
        ...     summary = store.performance()
        ...     print(summary.win_rate)
    """

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self._read_only = read_only

        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            path,
            # 取引ループ以外のスレッド（TUIのワーカーなど）からも触れるようにする。
            check_same_thread=False,
            timeout=5.0,
            isolation_level=None,  # autocommit。記録は1行ずつ確定させたい。
        )
        self._conn.row_factory = sqlite3.Row
        # WAL: 書き手（取引プロセス）と読み手（TUI/レポート）を同時に動かすため。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

        if not read_only:
            self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------ ライフサイクル

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @classmethod
    def open_for_read(cls, path: Path) -> Store:
        """既存DBを読み取り用に開く。

        Raises:
            FileNotFoundError: DB がまだ作られていない場合。
        """
        if not path.is_file():
            raise FileNotFoundError(path)
        return cls(path, read_only=True)

    # ------------------------------------------------------------ 書き込み

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """記録の失敗で取引を止めないための共通ラッパ。"""
        if self._read_only:
            return
        try:
            self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            logger.warning("記録に失敗しました（取引は継続します）: %s", exc)

    def record_signal(self, signal: Signal) -> None:
        self._execute(
            "INSERT INTO signals (symbol, action, strategy, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                signal.symbol,
                str(signal.action),
                signal.strategy,
                signal.reason,
                signal.timestamp.isoformat(),
            ),
        )

    def record_order(self, order: Order) -> None:
        """発注を記録する。同じ client_order_id は最新状態で上書きする。"""
        self._execute(
            "INSERT INTO orders (client_order_id, broker_order_id, symbol, side, quantity,"
            " order_type, status, filled_quantity, average_price, stop_loss, take_profit,"
            " strategy, reason, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(client_order_id) DO UPDATE SET"
            " broker_order_id=excluded.broker_order_id, status=excluded.status,"
            " filled_quantity=excluded.filled_quantity, average_price=excluded.average_price,"
            " updated_at=excluded.updated_at",
            (
                order.client_order_id,
                order.broker_order_id,
                order.symbol,
                str(order.side),
                str(order.quantity),
                str(order.order_type),
                str(order.status),
                str(order.filled_quantity),
                _str(order.average_price),
                _str(order.stop_loss),
                _str(order.take_profit),
                str(order.metadata.get("strategy", "")),
                str(order.metadata.get("reason", "")),
                order.created_at.isoformat(),
                order.updated_at.isoformat(),
            ),
        )

    def record_rejection(self, request: OrderRequest, rule: str, detail: str) -> None:
        self._execute(
            "INSERT INTO rejections (symbol, side, quantity, rule, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                request.symbol,
                str(request.side),
                str(request.quantity),
                rule,
                detail,
                utcnow().isoformat(),
            ),
        )

    def record_trade(self, trade: ClosedTrade) -> None:
        """確定トレードを記録する。

        ``dedup_key`` に UNIQUE 制約を張ってあるので、
        ブローカーが同じトレードを何度返しても二重計上されない。
        """
        dedup_key = "|".join(
            (
                trade.trade_id or trade.symbol,
                trade.closed_at.isoformat(),
                str(trade.realized_pnl),
            )
        )
        self._execute(
            "INSERT OR IGNORE INTO trades (dedup_key, symbol, side, quantity, entry_price,"
            " exit_price, realized_pnl, opened_at, closed_at, reason, strategy)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dedup_key,
                trade.symbol,
                str(trade.side),
                str(trade.quantity),
                str(trade.entry_price),
                str(trade.exit_price),
                str(trade.realized_pnl),
                trade.opened_at.isoformat(),
                trade.closed_at.isoformat(),
                trade.reason,
                "",
            ),
        )

    def record_equity(self, balance: Balance, *, open_positions: int = 0) -> None:
        self._execute(
            "INSERT INTO equity (equity, available, used_margin, currency, open_positions,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(balance.equity),
                str(balance.available),
                str(balance.used_margin),
                balance.currency,
                open_positions,
                balance.timestamp.isoformat(),
            ),
        )

    def record_event(self, kind: str, detail: str = "") -> None:
        """起動・停止・取引停止などの節目を残す。"""
        self._execute(
            "INSERT INTO events (kind, detail, created_at) VALUES (?, ?, ?)",
            (kind, detail, utcnow().isoformat()),
        )

    # ------------------------------------------------------------ 読み取り

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            return list(self._conn.execute(sql, params))
        except sqlite3.Error as exc:
            logger.warning("読み出しに失敗しました: %s", exc)
            return []

    def trades(self, *, limit: int = 100, since: datetime | None = None) -> list[TradeRow]:
        """決済済みトレードを新しい順に返す。"""
        sql = "SELECT * FROM trades"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE closed_at >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY closed_at DESC LIMIT ?"
        params.append(limit)

        return [
            TradeRow(
                symbol=row["symbol"],
                side=row["side"],
                quantity=_dec(row["quantity"]),
                entry_price=_dec(row["entry_price"]),
                exit_price=_dec(row["exit_price"]),
                realized_pnl=_dec(row["realized_pnl"]),
                opened_at=_time(row["opened_at"]),
                closed_at=_time(row["closed_at"]),
                reason=row["reason"],
                strategy=row["strategy"],
            )
            for row in self._query(sql, params)
        ]

    def signals(self, *, limit: int = 50) -> list[SignalRow]:
        return [
            SignalRow(
                symbol=row["symbol"],
                action=row["action"],
                strategy=row["strategy"],
                reason=row["reason"],
                created_at=_time(row["created_at"]),
            )
            for row in self._query("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
        ]

    def rejections(self, *, limit: int = 50) -> list[RejectionRow]:
        return [
            RejectionRow(
                symbol=row["symbol"],
                side=row["side"],
                quantity=_dec(row["quantity"]),
                rule=row["rule"],
                detail=row["detail"],
                created_at=_time(row["created_at"]),
            )
            for row in self._query("SELECT * FROM rejections ORDER BY id DESC LIMIT ?", (limit,))
        ]

    def rejection_counts(self, *, since: datetime | None = None) -> dict[str, int]:
        """却下ルールごとの件数。多い順に並んだ辞書を返す。

        「どのルールが実際に効いているか」は設定を見直すときの一次情報になる。
        """
        sql = "SELECT rule, COUNT(*) AS n FROM rejections"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            params.append(since.isoformat())
        sql += " GROUP BY rule ORDER BY n DESC"
        return {row["rule"]: int(row["n"]) for row in self._query(sql, params)}

    def equity_curve(
        self, *, limit: int = 2000, since: datetime | None = None
    ) -> list[EquityPoint]:
        """equity 推移を古い順に返す（グラフ描画用）。"""
        sql = "SELECT * FROM equity"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        points = [
            EquityPoint(
                created_at=_time(row["created_at"]),
                equity=_dec(row["equity"]),
                used_margin=_dec(row["used_margin"]),
                open_positions=int(row["open_positions"]),
            )
            for row in self._query(sql, params)
        ]
        points.reverse()
        return points

    def latest_equity(self) -> EquityPoint | None:
        rows = self._query("SELECT * FROM equity ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        row = rows[0]
        return EquityPoint(
            created_at=_time(row["created_at"]),
            equity=_dec(row["equity"]),
            used_margin=_dec(row["used_margin"]),
            open_positions=int(row["open_positions"]),
        )

    def events(self, *, limit: int = 50) -> list[EventRow]:
        return [
            EventRow(
                kind=row["kind"],
                detail=row["detail"],
                created_at=_time(row["created_at"]),
            )
            for row in self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        ]

    # ------------------------------------------------------------ 集計

    def performance(self, *, since: datetime | None = None) -> PerformanceSummary:
        """トレード履歴から成績を算出する。

        最大ドローダウンは確定損益の累積曲線に対して計算する。
        含み損を混ぜないのは、リスク管理の判定基準と揃えるため。
        """
        trades = self.trades(limit=1_000_000, since=since)
        # 古い順に並べ直してから累積を取る。
        return summarize(reversed(trades))


def summarize(trades: Iterable[TradeRow]) -> PerformanceSummary:
    """古い順のトレード列から成績を計算する。

    Store を介さずに使えるよう、あえて関数として切り出してある。
    """
    count = wins = losses = 0
    gross_profit = gross_loss = Decimal(0)
    cumulative = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)

    for trade in trades:
        count += 1
        pnl = trade.realized_pnl
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += -pnl

        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    return PerformanceSummary(
        trades=count,
        wins=wins,
        losses=losses,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_pnl=cumulative,
        max_drawdown=max_drawdown,
    )
