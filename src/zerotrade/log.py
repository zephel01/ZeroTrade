"""ロギング設定。

運用中の「なぜこの注文が通った/弾かれたのか」を後から再現できることを
最優先にしている。JSON 形式を選べるのは、ログ集約基盤へ流す前提のため。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zerotrade.settings import LoggingSettings

__all__ = ["JsonFormatter", "get_logger", "setup_logging"]

_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"

# LogRecord の標準属性。extra で渡された追加フィールドだけを抽出するために使う。
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | frozenset(
    {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """1行1JSON のフォーマッタ。``extra=`` で渡した値も出力する。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(settings: LoggingSettings | None = None) -> None:
    """ルートロガーを設定する。多重呼び出しでもハンドラは重複しない。"""
    from zerotrade.settings import LoggingSettings as _LoggingSettings

    cfg = settings or _LoggingSettings()
    root = logging.getLogger()
    root.setLevel(cfg.level)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter: logging.Formatter = (
        JsonFormatter() if cfg.json_output else logging.Formatter(_TEXT_FORMAT)
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if cfg.file is not None:
        cfg.file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(cfg.file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # httpx のリクエストログは INFO でも冗長なため一段落とす。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """``zerotrade.`` 名前空間配下のロガーを返す。"""
    return logging.getLogger(name if name.startswith("zerotrade") else f"zerotrade.{name}")
