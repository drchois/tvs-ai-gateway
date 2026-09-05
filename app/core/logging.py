import logging
import logging.handlers
import os
import json
import sys
from pathlib import Path
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

REQUEST_ID_CTX: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(request_id: str):
    return REQUEST_ID_CTX.set(request_id)


def reset_request_id(token) -> None:
    REQUEST_ID_CTX.reset(token)


def get_request_id() -> str:
    return REQUEST_ID_CTX.get()


def _resolve_log_dir(raw_dir: str) -> Path:
    """Resolve relative log dir against project root so cwd does not change log path."""
    candidate = Path(raw_dir)
    if candidate.is_absolute():
        return candidate

    # app/core/logging.py -> project root
    project_root = Path(__file__).resolve().parents[2]
    return project_root / candidate


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    _RESERVED = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
    }

    def __init__(self, mask_keys: set[str] | None = None):
        super().__init__()
        self._mask_keys = {k.strip().lower() for k in (mask_keys or set()) if k and k.strip()}

    def _mask(self, value: Any, key: str | None = None) -> Any:
        if key and key.lower() in self._mask_keys:
            return "***"
        if isinstance(value, dict):
            return {k: self._mask(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self._mask(item) for item in value]
        return value

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "env": os.getenv("LOG_APP_ENV", "dev"),
            "request_id": getattr(record, "request_id", "-"),
        }

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in self._RESERVED and not key.startswith("_")
        }
        if extras:
            payload["fields"] = self._mask(extras)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    json_enabled = os.getenv("LOG_JSON", "true").lower() == "true"
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    raw_mask_keys = os.getenv("LOG_PII_MASK_KEYS", "")
    mask_keys = {item.strip() for item in raw_mask_keys.split(",") if item.strip()}
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)

    handler = logging.StreamHandler()
    handler.addFilter(RequestContextFilter())
    if json_enabled:
        handler.setFormatter(JsonFormatter(mask_keys=mask_keys))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(request_id)s | %(message)s")
        )

    root_logger.addHandler(handler)
    # Add file handler per environment (local/dev/prod) when LOG_FILE_DIR set or by default
    file_dir = os.getenv("LOG_FILE_DIR", "logs")
    resolved_log_dir = _resolve_log_dir(file_dir)
    try:
        os.makedirs(resolved_log_dir, exist_ok=True)
    except Exception:
        # best-effort: if directory cannot be created, continue with stdout only
        resolved_log_dir = None

    if resolved_log_dir:
        env_name = os.getenv("LOG_APP_ENV", "dev")
        log_filename = str(resolved_log_dir / f"app-{env_name}.log")
        max_bytes = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
        backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5"))

        try:
            fh = logging.handlers.RotatingFileHandler(
                log_filename, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            fh.addFilter(RequestContextFilter())
            # reuse same formatter as stream handler
            fh.setFormatter(handler.formatter)
            fh.setLevel(level)
            root_logger.addHandler(fh)
        except Exception:
            # If file handler cannot be created, fallback silently to stream-only
            pass

    # Optionally capture prints and native stdout/stderr into logging so external
    # libraries that use print() or write to sys.stderr are persisted to file as well.
    capture_std = os.getenv("LOG_CAPTURE_STDOUT", "true").lower() == "true"
    if capture_std:
        class _StreamToLogger:
            def __init__(self, logger: logging.Logger, level: int) -> None:
                self.logger = logger
                self.level = level

            def write(self, buf: str) -> None:
                for line in buf.rstrip().splitlines():
                    if line:
                        self.logger.log(self.level, line)

            def flush(self) -> None:
                return

            # Some third-party libraries (including LLM SDK stacks) check stream TTY capability.
            # Returning False keeps behavior explicit for redirected logging streams.
            def isatty(self) -> bool:
                return False

        try:
            sys.stdout = _StreamToLogger(root_logger, logging.INFO)  # type: ignore
            sys.stderr = _StreamToLogger(root_logger, logging.ERROR)  # type: ignore
        except Exception:
            # If we cannot replace stdout/stderr, continue without capture
            pass
