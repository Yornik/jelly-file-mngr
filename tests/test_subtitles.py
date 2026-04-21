"""Tests for subtitle sidecar companion moves."""

from jellyfiler.executor import _move_subtitle, _subtitle_companions
from jellyfiler.scanner import SUBTITLE_EXTENSIONS


def test_subtitle_extensions_set():
    assert ".srt" in SUBTITLE_EXTENSIONS
    assert ".ass" in SUBTITLE_EXTENSIONS
    assert ".vtt" in SUBTITLE_EXTENSIONS
    assert ".mkv" not in SUBTITLE_EXTENSIONS


def test_subtitle_companions_exact_stem(tmp_path):
    video = tmp_path / "episode.mkv"
    video.touch()
    sub = tmp_path / "episode.srt"
    sub.touch()
    result = _subtitle_companions(video)
    assert sub in result


def test_subtitle_companions_with_lang_code(tmp_path):
    video = tmp_path / "episode.mkv"
    video.touch()
    sub_en = tmp_path / "episode.en.srt"
    sub_nl = tmp_path / "episode.nl.srt"
    sub_en.touch()
    sub_nl.touch()
    result = _subtitle_companions(video)
    assert sub_en in result
    assert sub_nl in result


def test_subtitle_companions_ignores_other_stems(tmp_path):
    video = tmp_path / "episode.mkv"
    video.touch()
    other_sub = tmp_path / "other_episode.srt"
    other_sub.touch()
    result = _subtitle_companions(video)
    assert other_sub not in result


def test_subtitle_companions_ignores_non_subtitle_extensions(tmp_path):
    video = tmp_path / "episode.mkv"
    video.touch()
    nfo = tmp_path / "episode.nfo"
    nfo.touch()
    result = _subtitle_companions(video)
    assert nfo not in result


def test_subtitle_companions_empty_when_none(tmp_path):
    video = tmp_path / "episode.mkv"
    video.touch()
    result = _subtitle_companions(video)
    assert result == []


def test_move_subtitle_basic(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    sub = src_dir / "episode.srt"
    sub.touch()
    dest_video = dst_dir / "S01E05.mkv"
    _move_subtitle(sub, dest_video)
    assert (dst_dir / "S01E05.srt").exists()
    assert not sub.exists()


def test_move_subtitle_preserves_lang_code(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    sub = src_dir / "episode.en.srt"
    sub.touch()
    dest_video = dst_dir / "S01E05.mkv"
    _move_subtitle(sub, dest_video)
    assert (dst_dir / "S01E05.en.srt").exists()
    assert not sub.exists()


def test_move_subtitle_skips_existing_dest(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    sub = src_dir / "episode.srt"
    sub.touch()
    existing = dst_dir / "S01E05.srt"
    existing.write_text("existing")
    dest_video = dst_dir / "S01E05.mkv"
    _move_subtitle(sub, dest_video)
    assert sub.exists()  # not moved
    assert existing.read_text() == "existing"  # not overwritten


def test_executor_moves_subtitle_alongside_video(tmp_path):
    from jellyfiler.cache import Cache
    from jellyfiler.executor import execute
    from jellyfiler.models import MediaType, Plan, PlannedMove

    src = tmp_path / "Futurama.S12E03.mkv"
    sub = tmp_path / "Futurama.S12E03.srt"
    dst = tmp_path / "output" / "Futurama" / "Season 12" / "S12E03.mkv"
    src.touch()
    sub.touch()

    plan = Plan(
        moves=[
            PlannedMove(
                source=src,
                destination=dst,
                media_type=MediaType.EPISODE,
                tmdb_id=615,
                matched_title="Futurama",
                confidence="high",
            )
        ]
    )
    cache = Cache(tmp_path / "cache.db")
    execute(plan, dry_run=False, cache=cache)

    assert dst.exists()
    assert (tmp_path / "output" / "Futurama" / "Season 12" / "S12E03.srt").exists()
    assert not sub.exists()
