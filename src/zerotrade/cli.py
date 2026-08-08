"""``zerotrade`` コマンド。

サブコマンド:

* ``run``        実行ループを開始する
* ``check``      設定を検証して構成を表示する（``--connect`` で疎通確認。発注は一切しない）
* ``strategies`` 利用可能な戦略を一覧する
* ``status``     保存済みリスク状態を表示する
* ``resume``     損失上限による取引停止を手動解除する
* ``dashboard``  TUI ダッシュボードを開く（別プロセス・読み取り中心）
* ``report``     静的HTMLレポートを書き出す
* ``verify``     発注経路を最小数量で検証する（**本物の注文を出す**）
* ``stop``       稼働中の取引ループへ緊急停止を要求する
* ``download``   公開ソース（Yahoo / Stooq）からヒストリカルを取得する
* ``fetch``      ブローカーAPIからヒストリカルデータを取得して CSV に保存する
* ``import``     外部で入手した OHLCV を取り込み、足種を揃える
* ``backtest``   ヒストリカルデータ上で戦略を検証する
* ``optimize``   パラメータを掃引し、in/out-of-sample を並べて表示する
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from pathlib import Path

from zerotrade.app import build_application
from zerotrade.brokers.base import BaseBroker
from zerotrade.errors import ConfigError, ZeroTradeError
from zerotrade.log import get_logger, setup_logging
from zerotrade.models import Candle
from zerotrade.settings import Settings, load_settings
from zerotrade.strategies import available_strategies

__all__ = ["main"]

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zerotrade",
        description="ルール徹底とリスク管理を強制する自動売買ベースシステム",
    )
    parser.add_argument("-c", "--config", type=Path, default=None, help="設定YAMLのパス")
    parser.add_argument(
        "--mode",
        choices=("paper", "live", "backtest"),
        default=None,
        help="設定ファイルの mode を上書きする",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=None,
        help="ログレベルを上書きする",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="実行ループを開始する")
    run_cmd.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="指定回数だけループして終了する（動作確認用）",
    )

    check_cmd = sub.add_parser("check", help="設定を検証して構成を表示する")
    check_cmd.add_argument(
        "--connect",
        action="store_true",
        help="実際にブローカーへ接続し、残高・建玉・気配値を取得する（発注はしない）",
    )
    sub.add_parser("strategies", help="利用可能な戦略を一覧する")
    sub.add_parser("status", help="保存済みリスク状態を表示する")
    sub.add_parser("resume", help="取引停止を手動解除する")
    sub.add_parser("dashboard", help="TUI ダッシュボードを開く")

    report_cmd = sub.add_parser("report", help="静的HTMLレポートを書き出す")
    report_cmd.add_argument(
        "-o", "--output", type=Path, default=None, help="出力先（既定は state_dir/report.html）"
    )
    report_cmd.add_argument(
        "--days", type=int, default=None, help="直近N日ぶんに絞る（既定は全期間）"
    )

    verify_cmd = sub.add_parser(
        "verify", help="発注経路を検証する（最小数量で1往復。**本物の注文を出す**）"
    )
    verify_cmd.add_argument("--symbol", default=None, help="検証に使う銘柄（既定は設定の先頭）")
    verify_cmd.add_argument(
        "--quantity", default=None, help="発注数量（既定は取引所の最小数量に近い値）"
    )
    verify_cmd.add_argument(
        "--dry-run", action="store_true", help="発注せず、読み取り系の確認だけ行う"
    )
    verify_cmd.add_argument(
        "--yes", action="store_true", help="確認プロンプトを省略する（自動実行用）"
    )

    stop_cmd = sub.add_parser("stop", help="稼働中の取引ループへ緊急停止を要求する")
    stop_cmd.add_argument("--reason", default="CLI からの緊急停止", help="停止理由の記録")

    fetch_cmd = sub.add_parser("fetch", help="ヒストリカルデータを取得して CSV に保存する")
    fetch_cmd.add_argument("--symbol", default=None, help="銘柄（既定は設定の先頭）")
    fetch_cmd.add_argument("--granularity", default="M5", help="足種（M5 / H1 など）")
    fetch_cmd.add_argument("--days", type=int, default=365, help="何日ぶん遡るか")
    fetch_cmd.add_argument("-o", "--output", type=Path, default=None, help="出力先CSV")

    download_cmd = sub.add_parser(
        "download", help="公開ソースからヒストリカルデータを取得する（口座不要）"
    )
    download_cmd.add_argument(
        "--provider", default="yahoo", choices=("yahoo", "stooq"), help="取得元"
    )
    download_cmd.add_argument("--symbol", default=None, help="銘柄（既定は設定の先頭）")
    download_cmd.add_argument(
        "--granularity", default="H1", help="足種（yahoo: M1/M5/M15/M30/H1/D1、stooq: D1）"
    )
    download_cmd.add_argument("--days", type=int, default=365, help="何日ぶん遡るか")
    download_cmd.add_argument("-o", "--output", type=Path, default=None, help="出力先CSV")

    import_cmd = sub.add_parser("import", help="外部で入手した OHLCV を取り込み、足種を揃える")
    import_cmd.add_argument("--csv", type=Path, required=True, help="入力ファイル")
    import_cmd.add_argument("--symbol", default=None, help="銘柄（既定は設定の先頭）")
    import_cmd.add_argument(
        "--format",
        dest="fmt",
        default="auto",
        choices=("auto", "histdata", "dukascopy", "generic", "mt4"),
        help="入力形式（既定は自動判定）",
    )
    import_cmd.add_argument("--resample", default=None, help="まとめ直す足種（M5 / H1 など）")
    import_cmd.add_argument(
        "--tz",
        default=None,
        help="タイムゾーンを持たない時刻の解釈（既定: HistData は米国東部、他は UTC）",
    )
    import_cmd.add_argument("-o", "--output", type=Path, default=None, help="出力先CSV")

    backtest_cmd = sub.add_parser("backtest", help="ヒストリカルデータ上で戦略を検証する")
    backtest_cmd.add_argument(
        "--csv",
        type=Path,
        action="append",
        required=True,
        help="OHLCV の CSV。複数指定すると多通貨ペアで同時に検証する",
    )
    backtest_cmd.add_argument(
        "--symbol",
        action="append",
        default=None,
        help="銘柄名。--csv と同じ数だけ並べる（省略時はファイル名から推測）",
    )
    backtest_cmd.add_argument("--report", type=Path, default=None, help="HTMLレポートの出力先")
    backtest_cmd.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="戦略パラメータを上書きする（複数指定可）",
    )

    optimize_cmd = sub.add_parser("optimize", help="パラメータを掃引する")
    optimize_cmd.add_argument(
        "--csv", type=Path, action="append", required=True, help="OHLCV の CSV（複数可）"
    )
    optimize_cmd.add_argument(
        "--symbol", action="append", default=None, help="銘柄名（--csv と同数）"
    )
    optimize_cmd.add_argument(
        "--param",
        action="append",
        default=[],
        required=True,
        metavar="NAME=V1,V2,V3",
        help="掃引するパラメータ（例: fast_period=5,10,20）",
    )
    optimize_cmd.add_argument(
        "--split", type=float, default=0.7, help="in-sample に使う割合（既定 0.7）"
    )
    optimize_cmd.add_argument("--top", type=int, default=5, help="検証する上位件数")

    return parser


def _load(args: argparse.Namespace) -> Settings:
    overrides: dict[str, object] = {}
    if args.mode is not None:
        overrides["mode"] = args.mode

    settings = load_settings(args.config, overrides=overrides)
    if args.log_level is not None:
        settings = settings.model_copy(
            update={"logging": settings.logging.model_copy(update={"level": args.log_level})}
        )
    setup_logging(settings.logging)
    return settings


async def _run(settings: Settings, iterations: int | None) -> int:
    app = build_application(settings)
    loop = asyncio.get_running_loop()

    # Ctrl-C / SIGTERM で「次のループ境界まで待ってから」安全に止める。
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows ではシグナルハンドラを登録できない。
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.runner.stop)

    try:
        stats = await app.runner.run(max_iterations=iterations)
    finally:
        await app.aclose()

    print(
        f"ループ {stats.iterations} 回 / シグナル {stats.signals} / "
        f"新規 {stats.entries} / 決済 {stats.exits} / エラー {stats.errors}"
    )
    if stats.rejections:
        print("却下の内訳:")
        for rule, count in sorted(stats.rejections.items(), key=lambda kv: -kv[1]):
            print(f"  {rule}: {count}")
    return 0


async def _probe_broker(settings: Settings, broker: BaseBroker) -> int:
    """接続して読み取り系APIだけを叩く。**発注は一切しない。**

    APIキーが本当に使えるかを、実弾を動かさずに確かめるための経路。
    ``check`` 単体は設定を読むだけなので、キーの正否は分からない。
    """
    if broker.name == "paper" and settings.broker.name != "paper":
        print(
            f"\n[警告] 設定は broker={settings.broker.name} ですが、"
            "認証情報が無いため PaperBroker で起動しています。\n"
            "        APIキーの環境変数が読めているか確認してください"
            "（fallback_to_paper: false にすると起動時に落ちて気づけます）。"
        )
        return 1

    print("\n接続しています…")
    try:
        await broker.connect()
    except ZeroTradeError as exc:
        print(f"[失敗] 接続できませんでした: {exc}")
        # 接続に失敗しても HTTP セッションは開いていることがある。
        # 閉じずに抜けると aiohttp が「Unclosed client session」を吐く。
        with contextlib.suppress(Exception):
            await broker.disconnect()
        return 1

    failures = 0
    try:
        try:
            balance = await broker.get_balance()
            print(f"  残高    : {balance.equity} {balance.currency}（余力 {balance.available}）")
        except ZeroTradeError as exc:
            print(f"  残高    : [失敗] {exc}")
            failures += 1

        try:
            positions = await broker.get_positions()
            if positions:
                for pos in positions:
                    print(
                        f"  建玉    : {pos.symbol} {pos.side.value} "
                        f"{pos.quantity} @ {pos.entry_price}"
                    )
            else:
                print("  建玉    : なし")
        except ZeroTradeError as exc:
            print(f"  建玉    : [失敗] {exc}")
            failures += 1

        for symbol in settings.symbols:
            try:
                ticker = await broker.get_ticker(symbol)
                spread = ticker.ask - ticker.bid
                print(
                    f"  気配値  : {symbol} bid={ticker.bid} ask={ticker.ask} "
                    f"（スプレッド {spread}）"
                )
            except ZeroTradeError as exc:
                print(f"  気配値  : {symbol} [失敗] {exc}")
                failures += 1
    finally:
        with contextlib.suppress(Exception):
            await broker.disconnect()

    if failures:
        print(f"\n[失敗] {failures}件の読み取りに失敗しました。APIキーの権限を確認してください。")
        return 1

    env = settings.broker.environment
    print(f"\n接続できました（environment: {env}）。発注は一切していません。")
    if env == "practice":
        print("次は `run --iterations 50` でデモ環境の発注経路を通してください。")
    return 0


def _check(settings: Settings, *, connect: bool = False) -> int:
    app = build_application(settings)
    risk = settings.risk
    print(
        "\n".join(
            [
                "設定は有効です。",
                f"  mode              : {settings.mode}",
                f"  broker            : {app.broker.name}",
                f"  strategy          : {settings.strategy.name}",
                f"  symbols           : {', '.join(settings.symbols)}",
                f"  1トレードリスク   : {risk.max_risk_per_trade:.2%}",
                f"  日次最大損失      : {risk.max_daily_loss:.2%}",
                f"  週次最大損失      : {risk.max_weekly_loss:.2%}",
                f"  証拠金使用率上限  : {risk.max_margin_usage:.2%}",
                f"  同時保有上限      : {risk.max_open_positions}",
                f"  ストップ必須      : {'はい' if risk.require_stop_loss else 'いいえ'}",
                f"  リセット基準TZ    : {risk.reset_timezone}",
                f"  状態ファイル      : {settings.state_dir / 'risk_state.json'}",
                f"  記録DB            : "
                f"{settings.database_path if settings.store.enabled else '無効'}",
            ]
        )
    )
    if not connect:
        return 0
    return asyncio.run(_probe_broker(settings, app.broker))


def _status(settings: Settings) -> int:
    from zerotrade.core.risk import RiskManager

    risk = RiskManager.load(
        settings.risk,
        settings.state_dir / "risk_state.json",
        contract_size=settings.sizing.contract_size,
    )
    print(risk.summary())
    if risk.is_halted:
        print("取引は停止中です。`zerotrade resume` で解除できます。")
    return 0


def _resume(settings: Settings) -> int:
    from zerotrade.core.risk import RiskManager

    risk = RiskManager.load(
        settings.risk,
        settings.state_dir / "risk_state.json",
        contract_size=settings.sizing.contract_size,
    )
    if not risk.is_halted:
        print("取引は停止していません。")
        return 0
    risk.resume()
    print("取引停止を解除しました。")
    return 0


def _dashboard(settings: Settings) -> int:
    from zerotrade.tui import run_dashboard

    return run_dashboard(settings)


def _report(settings: Settings, output: Path | None, days: int | None) -> int:
    from zerotrade.report import build_report
    from zerotrade.store import Store

    db_path = settings.database_path
    if not db_path.is_file():
        print(
            f"記録がまだありません（{db_path}）。`zerotrade run` を実行すると作られます。",
            file=sys.stderr,
        )
        return 1

    destination = output or (settings.state_dir / "report.html")
    with Store.open_for_read(db_path) as store:
        written = build_report(
            store,
            destination,
            days=days,
            currency=settings.broker.account_currency,
        )
    print(f"レポートを書き出しました: {written}")
    return 0


def _stop(settings: Settings, reason: str) -> int:
    from zerotrade.control import KillSwitch

    switch = KillSwitch(settings.state_dir)
    switch.request(reason)
    print(f"緊急停止を要求しました（{switch.path}）。取引ループは次のループ境界で停止します。")
    return 0


async def _download(
    settings: Settings,
    provider_name: str,
    symbol: str | None,
    granularity: str,
    days: int,
    output: Path | None,
) -> int:
    from zerotrade.data.fetcher import save_csv
    from zerotrade.data.providers import create_provider

    target = symbol or settings.symbols[0]
    provider = create_provider(provider_name)
    try:
        candles = await provider.fetch(target, granularity=granularity, days=days)
    finally:
        await provider.aclose()

    destination = output or Path("data") / f"{target}_{granularity.upper()}.csv"
    save_csv(candles, destination)
    print(
        f"{len(candles)} 本を保存しました: {destination}\n"
        f"取得元: {provider_name} / 期間: {candles[0].timestamp:%Y-%m-%d %H:%M} 〜 "
        f"{candles[-1].timestamp:%Y-%m-%d %H:%M} UTC"
    )
    return 0


async def _fetch(
    settings: Settings, symbol: str | None, granularity: str, days: int, output: Path | None
) -> int:
    from datetime import timedelta

    from zerotrade.brokers import create_broker
    from zerotrade.data.fetcher import fetch_candles, save_csv
    from zerotrade.models import utcnow

    target = symbol or settings.symbols[0]
    destination = output or Path("data") / f"{target}_{granularity}.csv"

    broker = create_broker(settings)
    await broker.connect()
    try:
        candles = await fetch_candles(
            broker,
            target,
            granularity=granularity,
            start=utcnow() - timedelta(days=days),
        )
    finally:
        await broker.disconnect()

    save_csv(candles, destination)
    print(
        f"{len(candles)} 本を保存しました: {destination}"
        f"（{candles[0].timestamp:%Y-%m-%d} 〜 {candles[-1].timestamp:%Y-%m-%d}）"
    )
    return 0


def _load_series(
    settings: Settings, paths: list[Path], symbols: list[str] | None
) -> tuple[Settings, dict[str, list[Candle]]]:
    """CSVの並びから銘柄名を決めて読み込む。

    銘柄名を省略した場合はファイル名の先頭（``USDJPY_H1.csv`` → ``USDJPY``）を使う。
    """
    from zerotrade.data.historical import load_csv

    if symbols and len(symbols) != len(paths):
        raise ConfigError(
            f"--symbol の数（{len(symbols)}）が --csv の数（{len(paths)}）と一致しません"
        )
    if not symbols:
        symbols = [p.stem.split("_")[0] for p in paths] if len(paths) > 1 else [settings.symbols[0]]

    series = {name: load_csv(path, name) for name, path in zip(symbols, paths, strict=True)}
    return settings.model_copy(update={"symbols": symbols}), series


async def _backtest(
    settings: Settings,
    csv_paths: list[Path],
    symbols: list[str] | None,
    report_path: Path | None,
    params: list[str],
) -> int:
    from zerotrade.backtest import default_database, run_backtest
    from zerotrade.backtest.optimize import parse_param_spec

    settings, series = _load_series(settings, csv_paths, symbols)

    # 単一値の上書きなので、掃引と同じ書式を1要素として解釈する。
    overrides = {k: v[0] for k, v in parse_param_spec(params).items()} if params else {}

    database = default_database(settings, "backtest")
    result = await run_backtest(settings, series, strategy_params=overrides, database=database)

    print(f"期間: {result.start:%Y-%m-%d} 〜 {result.end:%Y-%m-%d}（{result.bars} 本）")
    print(f"結果: {result.describe()}")
    print(
        f"資金: {result.initial_equity:,.0f} → {result.final_equity:,.0f} "
        f"{settings.broker.account_currency}"
    )
    if result.rejections:
        print("却下の内訳:")
        for rule, count in sorted(result.rejections.items(), key=lambda kv: -kv[1]):
            print(f"  {rule}: {count}")
    print(f"記録: {database}")

    if report_path is not None:
        from zerotrade.report import build_report
        from zerotrade.store import Store

        with Store.open_for_read(database) as store:
            build_report(store, report_path, currency=settings.broker.account_currency)
        print(f"レポート: {report_path}")
    return 0


async def _verify(
    settings: Settings,
    symbol: str | None,
    quantity: str | None,
    dry_run: bool,
    assume_yes: bool,
) -> int:
    from decimal import Decimal

    from zerotrade.verify import run_verification

    target = symbol or settings.symbols[0]

    if not dry_run and not assume_yes:
        print(
            "\n発注経路の検証を行います。\n"
            f"  ブローカー : {settings.broker.name}（environment={settings.broker.environment}）\n"
            f"  銘柄       : {target}\n"
            f"  内容       : 最小数量で新規→決済を1往復\n"
        )
        if settings.broker.environment == "live":
            print("  **本番環境です。実際の資金が動きます。**\n")
        # 確認プロンプトはイベントループを塞がないよう別スレッドで待つ。
        answer = (await asyncio.to_thread(input, "続行しますか？ [y/N]: ")).strip().lower()
        if answer not in {"y", "yes"}:
            print("中止しました。")
            return 1

    report = await run_verification(
        settings,
        target,
        quantity=Decimal(quantity) if quantity else None,
        dry_run=dry_run,
    )

    print("\n発注経路の検証結果:")
    for check in report.checks:
        print(check.line())

    if report.position_left_open:
        print("\n**建玉が残っています。** 取引所の画面から手仕舞ってください。")
        return 1
    if report.ok:
        # 擬似ブローカーやテストネットで通しても「実物で確認済み」にはならない。
        # ここを曖昧に書くと、未検証のまま検証済みだと思い込む事故になる。
        if dry_run:
            print(
                "\n読み取り系はすべて通りました。"
                "**発注経路はまだ検証していません**（--dry-run のため）。"
            )
        elif settings.broker.name in {"paper", "shadow"}:
            print("\nすべて通りました。ただし擬似ブローカーなので、実物の確認にはなりません。")
        elif settings.broker.environment != "live":
            print("\nすべて通りました（テストネット）。本番の約定エンジンでの確認は別途必要です。")
        else:
            print("\nすべて通りました。発注経路を実物のAPIで確認できました。")
        return 0

    print(f"\n{len(report.failures)}件が失敗しました。上の NG を確認してください。")
    return 1


async def _optimize(
    settings: Settings,
    csv_path: list[Path],
    symbol: list[str] | None,
    params: list[str],
    split: float,
    top: int,
) -> int:
    from zerotrade.backtest import optimize
    from zerotrade.backtest.optimize import parse_param_spec

    settings, series = _load_series(settings, csv_path, symbol)
    grid = parse_param_spec(params)

    results = await optimize(settings, series, grid, split_ratio=split, top=top)
    verified = [r for r in results if r.out_of_sample is not None]

    print(f"\n上位 {len(verified)} 件（in = 前半{split:.0%} / out = 後半で答え合わせ）\n")
    for rank, entry in enumerate(verified, start=1):
        print(f"{rank}. {entry.describe()}\n")

    robust = [r for r in verified if r.is_robust]
    if not robust:
        print(
            "後半でもプラスを保てた組み合わせはありませんでした。"
            "前半に当てはめただけの可能性が高いので、この結果でパラメータを"
            "決めるべきではありません。"
        )
    else:
        best = robust[0]
        params_text = " ".join(f"{k}={v}" for k, v in sorted(best.params.items()))
        print(f"後半でも通用した最上位: {params_text}")
    return 0


def _import(
    settings: Settings,
    source: Path,
    symbol: str | None,
    fmt: str,
    granularity: str | None,
    timezone: str | None,
    output: Path | None,
) -> int:
    from zerotrade.data.fetcher import save_csv
    from zerotrade.data.importer import read_any, resample

    target = symbol or settings.symbols[0]
    candles = read_any(source, target, fmt=fmt, timezone=timezone)
    label = "そのまま"

    if granularity:
        candles = resample(candles, granularity)
        label = granularity
        if not candles:
            print("まとめ直した結果、足が1本も残りませんでした。", file=sys.stderr)
            return 1

    destination = output or source.with_name(f"{target}_{granularity or 'imported'}.csv")
    save_csv(candles, destination)
    print(
        f"{len(candles)} 本（{label}）を保存しました: {destination}\n"
        f"期間: {candles[0].timestamp:%Y-%m-%d %H:%M} 〜 {candles[-1].timestamp:%Y-%m-%d %H:%M} UTC"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。終了コードを返す。"""
    args = build_parser().parse_args(argv)

    if args.command == "strategies":
        setup_logging()
        for name in available_strategies():
            print(name)
        return 0

    try:
        settings = _load(args)
        if args.command == "run":
            return asyncio.run(_run(settings, args.iterations))
        if args.command == "check":
            return _check(settings, connect=args.connect)
        if args.command == "status":
            return _status(settings)
        if args.command == "resume":
            return _resume(settings)
        if args.command == "dashboard":
            return _dashboard(settings)
        if args.command == "report":
            return _report(settings, args.output, args.days)
        if args.command == "stop":
            return _stop(settings, args.reason)
        if args.command == "download":
            return asyncio.run(
                _download(
                    settings,
                    args.provider,
                    args.symbol,
                    args.granularity,
                    args.days,
                    args.output,
                )
            )
        if args.command == "fetch":
            return asyncio.run(
                _fetch(settings, args.symbol, args.granularity, args.days, args.output)
            )
        if args.command == "import":
            return _import(
                settings,
                args.csv,
                args.symbol,
                args.fmt,
                args.resample,
                args.tz,
                args.output,
            )
        if args.command == "verify":
            return asyncio.run(
                _verify(settings, args.symbol, args.quantity, args.dry_run, args.yes)
            )
        if args.command == "backtest":
            return asyncio.run(_backtest(settings, args.csv, args.symbol, args.report, args.param))
        if args.command == "optimize":
            return asyncio.run(
                _optimize(settings, args.csv, args.symbol, args.param, args.split, args.top)
            )
    except ZeroTradeError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
