"""Tests for the SQLite cache."""

from pathlib import Path

from jellyfiler.cache import Cache
from jellyfiler.models import MediaType, TmdbMatch


def _make_cache(tmp_path: Path) -> Cache:
    return Cache(db_path=tmp_path / "test.db")


def test_tmdb_cache_miss(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    result = cache.get_tmdb("futurama", 1999, MediaType.EPISODE)
    assert result is None


def test_tmdb_cache_hit(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    matches = [TmdbMatch(tmdb_id=615, title="Futurama", year=1999, media_type=MediaType.EPISODE)]
    cache.set_tmdb("futurama", 1999, MediaType.EPISODE, matches)

    result = cache.get_tmdb("futurama", 1999, MediaType.EPISODE)
    assert result is not None
    assert len(result) == 1
    assert result[0].tmdb_id == 615
    assert result[0].title == "Futurama"


def test_tmdb_cache_case_insensitive(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    matches = [TmdbMatch(tmdb_id=1, title="Coco", year=2017, media_type=MediaType.MOVIE)]
    cache.set_tmdb("Coco", 2017, MediaType.MOVIE, matches)

    result = cache.get_tmdb("coco", 2017, MediaType.MOVIE)
    assert result is not None


def test_move_log(tmp_path: Path) -> None:
    cache = _make_cache(tmp_path)
    src = Path("/media/movies/Coco.mkv")
    dst = Path("/output/Coco (2017)/Coco (2017).mkv")

    assert not cache.already_moved(src)
    cache.record_move(src, dst)
    assert cache.already_moved(src)


def test_cache_dir_created(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "test.db"
    cache = Cache(db_path=deep)
    assert deep.exists()
    cache.close()
