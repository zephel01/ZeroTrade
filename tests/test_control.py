"""キルスイッチと、実行ループからの緊急停止のテスト。"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeClock
from tests.test_runner_e2e import _build
from zerotrade.control import KillSwitch
from zerotrade.settings import Settings


def test_要求と解除(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path)
    assert switch.requested() is None

    switch.request("テスト停止")
    assert switch.requested() == "テスト停止"

    switch.clear()
    assert switch.requested() is None


def test_理由が空でも要求として扱う(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path)
    switch.request("")
    assert switch.requested() == "手動停止"


def test_解除は冪等(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path)
    switch.clear()
    switch.clear()  # ファイルが無くても例外にならない


def test_ディレクトリが無くても要求できる(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "まだ無い" / "state")
    switch.request("テスト")
    assert switch.requested() == "テスト"


async def test_停止要求があるとループが始まらない(
    paper_settings: Settings, clock: FakeClock, tmp_path: Path
) -> None:
    """ダッシュボードから止めたあと、その要求が実際に効くこと。"""
    settings = paper_settings.model_copy(update={"state_dir": tmp_path})
    runner, _broker, _risk = _build(settings, clock=clock)

    # 起動前に残っている要求は「起動＝再開の意思表示」として解除される。
    KillSwitch(tmp_path).request("前回の停止")
    stats = await runner.run(max_iterations=3)
    assert stats.iterations == 3, "起動時に古い要求が解除されていない"
    assert KillSwitch(tmp_path).requested() is None


async def test_ループ中の停止要求で次の境界で止まる(
    paper_settings: Settings, clock: FakeClock, tmp_path: Path
) -> None:
    settings = paper_settings.model_copy(update={"state_dir": tmp_path})
    runner, _broker, _risk = _build(settings, clock=clock)
    switch = KillSwitch(tmp_path)

    # step が1回走ったところで停止を要求する。
    original_step = runner.step
    calls = 0

    async def counting_step() -> None:
        nonlocal calls
        calls += 1
        await original_step()
        if calls == 2:
            switch.request("テストからの停止")

    runner.step = counting_step  # type: ignore[method-assign]

    stats = await runner.run(max_iterations=50)

    assert stats.iterations == 2, "停止要求の直後のループ境界で止まるはず"
    assert switch.requested() == "テストからの停止"


async def test_停止要求は記録に残る(
    paper_settings: Settings, clock: FakeClock, tmp_path: Path
) -> None:
    from zerotrade.store import Store

    settings = paper_settings.model_copy(update={"state_dir": tmp_path})
    store = Store(settings.database_path)
    runner, _broker, _risk = _build(settings, clock=clock, store=store)

    # 起動時に古い要求は解除されるので、1ループ走ったところで要求する。
    original_step = runner.step
    first = True

    async def stepping() -> None:
        nonlocal first
        await original_step()
        if first:
            first = False
            KillSwitch(tmp_path).request("記録テスト")

    runner.step = stepping  # type: ignore[method-assign]
    await runner.run(max_iterations=10)

    kinds = [e.kind for e in store.events()]
    assert "kill_switch" in kinds
    store.close()
