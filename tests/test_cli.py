"""CLI のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from zerotrade.cli import main

CONFIG = """
mode: paper
symbols: [USD_JPY]
poll_interval_seconds: 0.001
state_dir: {state_dir}
broker:
  name: paper
  initial_balance: 1000000
strategy:
  name: sma_rsi
  params: {{fast_period: 5, slow_period: 12}}
notifications:
  console: false
"""


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG.format(state_dir=tmp_path / "state"), encoding="utf-8")
    return path


def test_strategiesは戦略名を出力する(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["strategies"]) == 0
    assert "sma_rsi" in capsys.readouterr().out


def test_checkは構成を表示する(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-c", str(config), "check"]) == 0
    out = capsys.readouterr().out
    assert "設定は有効です" in out
    assert "1トレードリスク" in out


def test_check_connectは残高と気配値を出す(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-c", str(config), "check", "--connect"]) == 0
    out = capsys.readouterr().out
    assert "接続できました" in out
    assert "残高" in out
    assert "気配値" in out
    assert "建玉" in out


def test_check_connectは発注しない(config: Path, tmp_path: Path) -> None:
    """--connect は読み取り専用。記録DBに注文が1件も入らないこと。"""
    import sqlite3

    assert main(["-c", str(config), "check", "--connect"]) == 0
    db = tmp_path / "state" / "zerotrade.db"
    if not db.exists():
        return
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from orders").fetchone()[0] == 0


def test_check_connectはペーパーへの降格を警告する(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """認証情報が無くて PaperBroker に落ちたまま気づかない、を防ぐ。"""
    path = tmp_path / "fallback.yaml"
    path.write_text(
        "mode: paper\n"
        "symbols: [USD_JPY]\n"
        f"state_dir: {tmp_path / 'state'}\n"
        "broker:\n"
        "  name: oanda\n"
        "  fallback_to_paper: true\n"
        "notifications:\n"
        "  console: false\n",
        encoding="utf-8",
    )

    assert main(["-c", str(path), "check", "--connect"]) == 1
    out = capsys.readouterr().out
    assert "PaperBroker で起動しています" in out


def test_checkは不正な設定で失敗する(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("mode: live\nbroker:\n  name: paper\n", encoding="utf-8")

    assert main(["-c", str(bad), "check"]) == 1
    assert "エラー" in capsys.readouterr().err


def test_設定ファイルが無ければ終了コード1(tmp_path: Path) -> None:
    assert main(["-c", str(tmp_path / "nope.yaml"), "check"]) == 1


def test_runは指定回数で終了する(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-c", str(config), "run", "--iterations", "3"]) == 0
    assert "ループ 3 回" in capsys.readouterr().out


def test_statusとresume(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-c", str(config), "status"]) == 0
    assert "稼働中" in capsys.readouterr().out

    assert main(["-c", str(config), "resume"]) == 0
    assert "停止していません" in capsys.readouterr().out


def test_modeの上書きが効く(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-c", str(config), "--mode", "backtest", "check"]) == 0
    assert "mode              : backtest" in capsys.readouterr().out


def test_サブコマンド無しはエラー() -> None:
    with pytest.raises(SystemExit):
        main([])


# ------------------------------------------------------- 記録層まわりのコマンド


def test_checkは記録DBの場所を出す(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-c", str(config), "check"]) == 0
    assert "記録DB" in capsys.readouterr().out


def test_記録が無ければreportは失敗する(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-c", str(config), "report"]) == 1
    assert "記録がまだありません" in capsys.readouterr().err


def test_runのあとreportが書き出せる(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-c", str(config), "run", "--iterations", "5"]) == 0
    capsys.readouterr()

    output = tmp_path / "report.html"
    assert main(["-c", str(config), "report", "-o", str(output)]) == 0
    assert "レポートを書き出しました" in capsys.readouterr().out

    content = output.read_text(encoding="utf-8")
    assert "ZeroTrade レポート" in content
    assert "equity 推移" in content


def test_reportは日数を絞れる(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["-c", str(config), "run", "--iterations", "3"])
    capsys.readouterr()

    output = tmp_path / "recent.html"
    assert main(["-c", str(config), "report", "-o", str(output), "--days", "1"]) == 0
    assert "直近 1 日" in output.read_text(encoding="utf-8")


def test_stopは停止要求を作る(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from zerotrade.control import KillSwitch

    assert main(["-c", str(config), "stop", "--reason", "テスト"]) == 0
    assert "緊急停止を要求しました" in capsys.readouterr().out
    assert KillSwitch(tmp_path / "state").requested() == "テスト"


def test_停止要求があるとrunは即座に終わる(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """起動は再開の意思表示なので、古い要求は解除されて通常どおり回る。"""
    main(["-c", str(config), "stop"])
    capsys.readouterr()

    assert main(["-c", str(config), "run", "--iterations", "3"]) == 0
    assert "ループ 3 回" in capsys.readouterr().out


# ------------------------------------------------------- バックテスト


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    from zerotrade.data.fetcher import save_csv
    from zerotrade.data.historical import synthetic_candles

    candles = synthetic_candles("USD_JPY", count=600, volatility=0.002, drift=0.0003, seed=7)
    return save_csv(candles, tmp_path / "USD_JPY_M5.csv")


def test_backtestが結果を表示する(
    config: Path, csv_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-c", str(config), "backtest", "--csv", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "期間:" in out
    assert "損益" in out
    assert "記録:" in out


def test_backtestはレポートも出せる(
    config: Path, csv_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "bt.html"
    assert (
        main(["-c", str(config), "backtest", "--csv", str(csv_path), "--report", str(report)]) == 0
    )
    assert report.is_file()
    assert "ZeroTrade レポート" in report.read_text(encoding="utf-8")


def test_backtestはパラメータを上書きできる(
    config: Path, csv_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["-c", str(config), "backtest", "--csv", str(csv_path), "--param", "fast_period=3"])
        == 0
    )
    assert "期間:" in capsys.readouterr().out


def test_存在しないCSVはエラー(config: Path, tmp_path: Path) -> None:
    assert main(["-c", str(config), "backtest", "--csv", str(tmp_path / "no.csv")]) == 1


def test_optimizeはinとoutを並べて出す(
    config: Path, csv_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "-c",
            str(config),
            "optimize",
            "--csv",
            str(csv_path),
            "--param",
            "fast_period=4,6",
            "--top",
            "2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "in :" in out
    assert "out:" in out
    assert "答え合わせ" in out


def test_不正なパラメータ指定はエラー(config: Path, csv_path: Path) -> None:
    assert main(["-c", str(config), "optimize", "--csv", str(csv_path), "--param", "こわれた"]) == 1


def test_dry_runは検証済みと言わない(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """発注していないのに「確認できました」と書くのは、自分で自分を騙す。"""
    assert main(["-c", str(config), "verify", "--dry-run", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "まだ検証していません" in out


def test_擬似ブローカーでは実物確認と言わない(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-c", str(config), "verify", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "実物の確認にはなりません" in out
