"""Structured JSONL logging for all agents.

Usage:
    log = get_logger("research_agent")
    log.info("recommendation_generated", rec_id=rec_id, ticker=ticker)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

LOGS_DIR = Path(__file__).parents[2] / "logs"

# Fields that must never appear in logs (redact if present)
_REDACTED_KEYS = {"private_key", "password", "api_secret", "token", "authorization"}


def _redact(obj: Any, depth: int = 0) -> Any:
    if depth > 10:
        return obj
    if isinstance(obj, dict):
        return {
            k: "***REDACTED***" if k.lower() in _REDACTED_KEYS else _redact(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(i, depth + 1) for i in obj]
    return obj


class JsonlHandler(logging.Handler):
    def __init__(self, agent_name: str) -> None:
        super().__init__()
        log_dir = LOGS_DIR / agent_name
        log_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        self._path = log_dir / f"{date_str}.jsonl"

    def emit(self, record: logging.LogRecord) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "agent": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra"):
            entry.update(_redact(record.extra))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        try:
            with open(self._path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass  # never let logging break the application


class _AgentLogger:
    """Thin wrapper that adds structured keyword args to every log call."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)
        if not self._logger.handlers:
            self._logger.addHandler(JsonlHandler(name))
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(logging.Formatter("[%(name)s] %(levelname)s %(message)s"))
            self._logger.addHandler(stream)

    def _log(self, level: int, msg: str, **kwargs: Any) -> None:
        # Build a readable message that includes key=value pairs for the console
        if kwargs:
            kv = "  " + "  ".join(f"{k}={v}" for k, v in kwargs.items())
        else:
            kv = ""
        record = self._logger.makeRecord(
            self._logger.name, level, "(unknown)", 0, msg + kv, (), None
        )
        record.extra = kwargs
        self._logger.handle(record)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, **kwargs)

    def critical(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, msg, **kwargs)


_loggers: dict[str, _AgentLogger] = {}


def get_logger(agent_name: str) -> _AgentLogger:
    if agent_name not in _loggers:
        _loggers[agent_name] = _AgentLogger(agent_name)
    return _loggers[agent_name]
