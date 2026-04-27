"""Tests for the JSONL event logger."""

import json
import threading
from pathlib import Path

from jellyfiler.jsonlog import JsonLogger, NullLogger, _serialise, open_logger


def test_null_logger_is_a_noop():
    log = NullLogger()
    log.info("anything", file="/tmp/x")
    log.warning("warn", reason="x")
    log.error("err")
    log.debug("dbg")
    log.close()  # must not raise


def test_open_logger_returns_null_when_path_is_none():
    log = open_logger(None)
    assert isinstance(log, NullLogger)
    assert not isinstance(log, JsonLogger)


def test_open_logger_returns_real_logger_for_path(tmp_path: Path):
    log = open_logger(tmp_path / "log.jsonl")
    assert isinstance(log, JsonLogger)
    log.close()


def test_json_logger_writes_one_line_per_event(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    log = JsonLogger(path)
    log.info("hello", file="/tmp/x")
    log.warning("warn", count=3)
    log.error("boom")
    log.debug("noisy", n=42)
    log.close()

    lines = path.read_text().splitlines()
    assert len(lines) == 4
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["event"] == "hello"
    assert parsed[0]["level"] == "info"
    assert parsed[0]["file"] == "/tmp/x"
    assert parsed[1]["level"] == "warning"
    assert parsed[2]["level"] == "error"
    assert parsed[3]["level"] == "debug"
    # Every record carries a UTC timestamp
    assert all("ts" in r for r in parsed)


def test_json_logger_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "log.jsonl"
    log = JsonLogger(path)
    log.info("ping")
    log.close()
    assert path.exists()


def test_json_logger_appends_to_existing_file(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    log1 = JsonLogger(path)
    log1.info("first")
    log1.close()

    log2 = JsonLogger(path)
    log2.info("second")
    log2.close()

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "first"
    assert json.loads(lines[1])["event"] == "second"


def test_json_logger_serialises_paths_and_sets(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    log = JsonLogger(path)
    log.info("x", path=Path("/tmp/foo"), tags={Path("/a"), Path("/b")})
    log.close()
    rec = json.loads(path.read_text().splitlines()[0])
    assert rec["path"] == "/tmp/foo"
    # set is converted to a sorted list
    assert isinstance(rec["tags"], list)
    assert sorted(rec["tags"]) == ["/a", "/b"]


def test_json_logger_handles_tuples():
    """Tuples become lists (recursively)."""
    assert _serialise(("a", Path("/b"), 3)) == ["a", "/b", 3]


def test_json_logger_thread_safe(tmp_path: Path):
    """100 threads × 50 writes must produce 5000 valid JSON lines, no interleaving."""
    path = tmp_path / "log.jsonl"
    log = JsonLogger(path)

    def burst(thread_id: int):
        for i in range(50):
            log.info("burst", thread=thread_id, i=i)

    threads = [threading.Thread(target=burst, args=(t,)) for t in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()

    lines = path.read_text().splitlines()
    assert len(lines) == 100 * 50
    # Every line must be valid JSON — no torn writes
    for line in lines:
        rec = json.loads(line)
        assert rec["event"] == "burst"


def test_json_logger_close_is_idempotent(tmp_path: Path):
    log = JsonLogger(tmp_path / "x.jsonl")
    log.close()
    log.close()  # must not raise


def test_json_logger_log_after_close_is_silent(tmp_path: Path):
    """Logging after close() doesn't raise — useful for cleanup paths."""
    path = tmp_path / "x.jsonl"
    log = JsonLogger(path)
    log.info("before")
    log.close()
    # Should not raise — write becomes a silent no-op
    log.info("after")
    lines = path.read_text().splitlines()
    assert len(lines) == 1


def test_serialise_passes_through_native_types():
    assert _serialise(42) == 42
    assert _serialise("hello") == "hello"
    assert _serialise(None) is None
    assert _serialise([1, 2, 3]) == [1, 2, 3]
    assert _serialise({"a": 1}) == {"a": 1}
