"""Tests for cache management methods (stats, unpin, clear)."""

from pathlib import Path

import pytest

from jellyfiler.cache import Cache
from jellyfiler.models import MediaType, TmdbMatch


@pytest.fixture
def cache(tmp_path: Path):
    c = Cache(db_path=tmp_path / "test.db")
    yield c
    c.close()


def _match(tmdb_id: int = 1, title: str = "Futurama") -> TmdbMatch:
    return TmdbMatch(tmdb_id=tmdb_id, title=title, year=1999, media_type=MediaType.EPISODE)


def test_stats_empty(cache: Cache) -> None:
    s = cache.stats()
    assert s == {"tmdb_cache": 0, "pinned": 0, "move_log": 0}


def test_stats_counts(cache: Cache, tmp_path: Path) -> None:
    cache.set_tmdb("futurama", None, MediaType.EPISODE, [_match()])
    cache.set_pinned("futurama", None, MediaType.EPISODE, _match())
    cache.record_move(Path("/src/a.mkv"), Path("/dst/a.mkv"))
    s = cache.stats()
    assert s["tmdb_cache"] == 1
    assert s["pinned"] == 1
    assert s["move_log"] == 1


def test_unpin_removes_entry(cache: Cache) -> None:
    cache.set_pinned("futurama", None, MediaType.EPISODE, _match())
    assert cache.get_pinned("futurama", None, MediaType.EPISODE) is not None
    removed = cache.unpin("futurama", None, MediaType.EPISODE)
    assert removed is True
    assert cache.get_pinned("futurama", None, MediaType.EPISODE) is None


def test_unpin_returns_false_when_not_found(cache: Cache) -> None:
    assert cache.unpin("nonexistent", None, MediaType.EPISODE) is False


def test_clear_pinned(cache: Cache) -> None:
    cache.set_pinned("futurama", None, MediaType.EPISODE, _match())
    cache.set_tmdb("futurama", None, MediaType.EPISODE, [_match()])
    deleted = cache.clear(pinned=True)
    assert deleted["pinned"] == 1
    assert "tmdb_cache" not in deleted
    assert cache.get_pinned("futurama", None, MediaType.EPISODE) is None
    assert cache.get_tmdb("futurama", None, MediaType.EPISODE) is not None


def test_clear_tmdb(cache: Cache) -> None:
    cache.set_tmdb("futurama", None, MediaType.EPISODE, [_match()])
    deleted = cache.clear(tmdb=True)
    assert deleted["tmdb_cache"] == 1
    assert cache.get_tmdb("futurama", None, MediaType.EPISODE) is None


def test_clear_moves(cache: Cache, tmp_path: Path) -> None:
    src = Path("/src/a.mkv")
    cache.record_move(src, Path("/dst/a.mkv"))
    assert cache.already_moved(src)
    deleted = cache.clear(moves=True)
    assert deleted["move_log"] == 1
    assert not cache.already_moved(src)


def test_clear_all(cache: Cache, tmp_path: Path) -> None:
    cache.set_tmdb("futurama", None, MediaType.EPISODE, [_match()])
    cache.set_pinned("futurama", None, MediaType.EPISODE, _match())
    cache.record_move(Path("/src/a.mkv"), Path("/dst/a.mkv"))
    deleted = cache.clear(pinned=True, tmdb=True, moves=True)
    assert deleted["pinned"] == 1
    assert deleted["tmdb_cache"] == 1
    assert deleted["move_log"] == 1
    s = cache.stats()
    assert s == {"tmdb_cache": 0, "pinned": 0, "move_log": 0}


def test_junk_report_truncates_at_10(tmp_path: Path) -> None:
    from jellyfiler.junk import report_junk

    files = [tmp_path / f"sample{i}.nfo" for i in range(15)]
    for f in files:
        f.touch()
    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    import jellyfiler.junk as junk_module

    orig = junk_module.console
    junk_module.console = Console(file=buf, highlight=False)
    try:
        report_junk(files, tmp_path, tmp_path / "dest", dry_run=True)
    finally:
        junk_module.console = orig

    output = buf.getvalue()
    assert "and 5 more" in output
    # 10 files shown (sample0..9), sample10..14 are in the "N more" line
    assert "sample9" in output
    assert "sample10" not in output
