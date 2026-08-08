"""キルスイッチ（緊急停止）。

ダッシュボードは取引プロセスとは別プロセスなので、直接 :meth:`StrategyRunner.stop`
を呼べない。そこで ``state/STOP`` というファイルの有無だけを合図に使う。

この方式を選んだ理由は、**停止方向にしか作用しないから**である。
ファイルを作れば止まるが、消しても勝手には始まらない。
表示層に渡す制御としてはこれが上限で、再開や手動決済は CLI に残してある。
再開に摩擦を残すことが、損失上限で止めた意味を保つ。
"""

from __future__ import annotations

from pathlib import Path

from zerotrade.log import get_logger

__all__ = ["KillSwitch"]

logger = get_logger(__name__)


class KillSwitch:
    """ファイルの有無で取引ループの停止を伝える。"""

    #: state_dir の下に置くファイル名。
    FILENAME = "STOP"

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / self.FILENAME

    def requested(self) -> str | None:
        """停止が要求されていれば理由を返す。されていなければ None。"""
        if not self.path.is_file():
            return None
        try:
            return self.path.read_text(encoding="utf-8").strip() or "理由なし"
        except OSError:
            return "理由なし"

    def request(self, reason: str = "") -> None:
        """停止を要求する。

        Args:
            reason: 誰が・なぜ止めたかの記録。レポートの節目一覧に出る。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(reason or "手動停止", encoding="utf-8")
        logger.warning("緊急停止を要求しました: %s", reason or "手動停止")

    def clear(self) -> None:
        """要求を取り消す。取引ループの起動時に呼ぶ。

        起動時にクリアしないと、前回の停止要求が残っていて
        起動した瞬間に止まる。「起動する」という操作自体が
        明示的な再開の意思表示なので、ここで消してよい。
        """
        self.path.unlink(missing_ok=True)
