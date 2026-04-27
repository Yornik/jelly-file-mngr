"""Structured JSON-lines event logger for ``--log``.

One event per line, written to a file in append mode. Format::

    {"ts": "2026-04-28T15:42:01+00:00", "level": "info", "event": "match_found",
     "file": "/src/Show.S01E01.mkv", "tmdb_id": 1234, "title": "Show", ...}

Use :class:`NullLogger` (or pass ``None`` to :func:`open_logger`) when no log
path is requested — every ``log()`` call is then a no-op.

Thread-safety
-------------
The parallel TMDB lookup pool means many threads call ``log()`` concurrently.
A ``threading.Lock`` serialises writes so lines never interleave.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

# Levels mirror Python's logging conventions; consumers can grep/jq by them.
LEVEL_DEBUG = "debug"
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"


def _serialise(value: Any) -> Any:
    """Convert non-JSON-native values to strings so json.dumps doesn't choke."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return [_serialise(v) for v in sorted(value, key=str)]
    if isinstance(value, tuple):
        return [_serialise(v) for v in value]
    return value


class NullLogger:
    """No-op logger — used when ``--log`` was not passed.

    Has the same API as :class:`JsonLogger` but every method is a cheap no-op,
    so callers don't have to ``if logger:`` everywhere.
    """

    def log(self, event: str, level: str = LEVEL_INFO, **fields: Any) -> None:
        return None

    def debug(self, event: str, **fields: Any) -> None:
        return None

    def info(self, event: str, **fields: Any) -> None:
        return None

    def warning(self, event: str, **fields: Any) -> None:
        return None

    def error(self, event: str, **fields: Any) -> None:
        return None

    def close(self) -> None:
        return None


class JsonLogger(NullLogger):
    """Thread-safe append-only JSONL logger."""

    def __init__(self, log_path: Path) -> None:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = log_path
        self._lock = threading.Lock()
        # Append + line-buffered so a Ctrl-C still leaves a usable log file.
        self._fp: IO[str] | None = log_path.open("a", encoding="utf-8", buffering=1)

    def log(self, event: str, level: str = LEVEL_INFO, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": level,
            "event": event,
        }
        for k, v in fields.items():
            record[k] = _serialise(v)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            if self._fp is not None:
                self._fp.write(line + "\n")

    def debug(self, event: str, **fields: Any) -> None:
        self.log(event, level=LEVEL_DEBUG, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self.log(event, level=LEVEL_INFO, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.log(event, level=LEVEL_WARNING, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.log(event, level=LEVEL_ERROR, **fields)

    def close(self) -> None:
        with self._lock:
            if self._fp is not None:
                self._fp.close()
                self._fp = None


def open_logger(path: Path | None) -> NullLogger:
    """Construct the right logger flavour. ``None`` → no-op; path → real JSONL."""
    if path is None:
        return NullLogger()
    return JsonLogger(path)
