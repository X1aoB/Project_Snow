"""A dedicated whitelist-only logger; never enable root/application debug logs."""
from __future__ import annotations

import json
import logging
import re


class PublicLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            if re.fullmatch(r"public_[a-z_]{1,80}", text):
                return record.levelno >= logging.WARNING
            data = json.loads(text)
            if data.get("event") != "public_generation_complete":
                return False
            for key in ("request_id", "character_id"):
                if not isinstance(data.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", data[key]):
                    return False
            elapsed = data.get("elapsed_ms")
            if type(elapsed) is not int or not 0 <= elapsed <= 86400000:
                return False
            # Record only whether a failure occurred. Provider errors can contain
            # input fragments or private identifiers; their text never reaches IO.
            safe = dict(event="public_generation_complete", request_id=data["request_id"],
                        character_id=data["character_id"], elapsed_ms=elapsed,
                        stage="complete" if data.get("stage") == "complete" else "failed",
                        terminal_error=bool(data.get("terminal_error")),
                        exception_type=bool(data.get("exception_type")))
            record.msg = json.dumps(safe, separators=(",", ":"), ensure_ascii=True)
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return True
        except (ValueError, TypeError, AttributeError):
            return False


def configure_public_logger() -> logging.Logger:
    logger = logging.getLogger("snow.public")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, "snow_public_whitelist", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.snow_public_whitelist = True
        handler.setLevel(logging.INFO)
        handler.addFilter(PublicLogFilter())
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger
