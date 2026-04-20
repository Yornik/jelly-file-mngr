"""Tests for junk file detection and quarantine."""

from pathlib import Path

import pytest

from jellyfiler.junk import find_junk, is_junk, junk_destination, move_junk, report_junk

# ---------------------------------------------------------------------------
# is_junk — non-video sidecar extensions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "movie.nfo",
        "release.txt",
        "archive.sfv",
        "checksum.md5",
        "cover.jpg",
        "cover.jpeg",
        "fanart.png",
        "thumb.bmp",
        "banner.gif",
        "subtitle.sub",
        "subtitle.idx",
        "release.srr",
        "link.url",
        "readme.htm",
        "readme.html",
    ],
)
def test_is_junk_sidecar_extensions(filename):
    assert is_junk(Path(filename))


# ---------------------------------------------------------------------------
# is_junk — video files with junk stem patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "Sample.mkv",
        "sample.mp4",
        "SAMPLE.avi",
        "Trailer.mkv",
        "trailer.mp4",
        "RARBG.com.mp4",
        "RARBG.com.mkv",
        "rarbg.info.mp4",
        "RARBG.mkv",
        "etrg.mp4",
        "www.YTS.AM.mp4",
        "Featurette.mkv",
        "Deleted.Scenes.mkv",
        "deleted_scenes.mkv",
        "Behind.The.Scenes.mkv",
        "behind-the-scenes.mkv",
        "Interview.mkv",
        "Short.Film.mkv",
        "Scene.mkv",
    ],
)
def test_is_junk_video_stem_patterns(filename):
    assert is_junk(Path(filename))


# ---------------------------------------------------------------------------
# is_junk — hex hash filenames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "8fa41b40995c44c9a883b1e0fe62f16a.mkv",  # 32-char MD5 hash
        "deadbeefcafebabe0123456789abcdef.mp4",  # 32-char hex
        "abcdef0123456789.mkv",  # exactly 16 chars (boundary)
        "ABCDEF0123456789.mkv",  # uppercase hex
    ],
)
def test_is_junk_hex_hash(filename):
    assert is_junk(Path(filename))


# ---------------------------------------------------------------------------
# is_junk — real media files should NOT be junk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "Blade.Runner.2049.2017.2160p.UHD.BluRay.REMUX.mkv",
        "Futurama.S12E03.1080p.x265-ELiTE.mkv",
        "The.Dark.Knight.2008.IMAX.4K.mkv",
        "Karate.Kid.Legends.2025.mkv",
        "How.to.Train.Your.Dragon.The.Hidden.World.mkv",
        # hex-looking but too short (15 chars)
        "abcdef012345678.mkv",
        # stem with non-hex chars
        "abcdefg0123456789.mkv",
    ],
)
def test_is_not_junk_real_files(filename):
    assert not is_junk(Path(filename))


# ---------------------------------------------------------------------------
# is_junk — unknown/non-media extensions are not flagged as junk video
# ---------------------------------------------------------------------------


def test_is_not_junk_unknown_extension():
    # .xyz is not a video extension and not a known sidecar — should not be junk
    assert not is_junk(Path("sample.xyz"))


# ---------------------------------------------------------------------------
# find_junk
# ---------------------------------------------------------------------------


def test_find_junk_returns_junk_files(tmp_path):
    (tmp_path / "Movie.mkv").touch()
    (tmp_path / "Sample.mkv").touch()
    (tmp_path / "cover.jpg").touch()
    (tmp_path / "release.nfo").touch()

    result = find_junk(tmp_path)
    names = {p.name for p in result}
    assert names == {"Sample.mkv", "cover.jpg", "release.nfo"}


def test_find_junk_recurses_subdirectories(tmp_path):
    subdir = tmp_path / "disc1"
    subdir.mkdir()
    (subdir / "8fa41b40995c44c9a883b1e0fe62f16a.mkv").touch()
    (subdir / "Movie.mkv").touch()

    result = find_junk(tmp_path)
    assert len(result) == 1
    assert result[0].name == "8fa41b40995c44c9a883b1e0fe62f16a.mkv"


def test_find_junk_empty_directory(tmp_path):
    assert find_junk(tmp_path) == []


def test_find_junk_no_junk_in_clean_directory(tmp_path):
    (tmp_path / "Blade.Runner.2049.mkv").touch()
    (tmp_path / "Futurama.S01E01.mkv").touch()
    assert find_junk(tmp_path) == []


def test_find_junk_returns_sorted(tmp_path):
    (tmp_path / "zzz.nfo").touch()
    (tmp_path / "aaa.nfo").touch()
    (tmp_path / "mmm.nfo").touch()

    result = find_junk(tmp_path)
    assert result == sorted(result)


# ---------------------------------------------------------------------------
# junk_destination
# ---------------------------------------------------------------------------


def test_junk_destination_flat(tmp_path):
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    file = source_root / "Sample.mkv"

    result = junk_destination(file, source_root, dest_root)
    assert result == dest_root / ".junk" / "Sample.mkv"


def test_junk_destination_preserves_subdir(tmp_path):
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    file = source_root / "SomeMovie" / "Sample.mkv"

    result = junk_destination(file, source_root, dest_root)
    assert result == dest_root / ".junk" / "SomeMovie" / "Sample.mkv"


def test_junk_destination_file_outside_source(tmp_path):
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    file = tmp_path / "elsewhere" / "RARBG.com.mp4"

    # Falls back to filename only when file is outside source_root
    result = junk_destination(file, source_root, dest_root)
    assert result == dest_root / ".junk" / "RARBG.com.mp4"


# ---------------------------------------------------------------------------
# move_junk
# ---------------------------------------------------------------------------


def test_move_junk_moves_files(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source.mkdir()
    dest.mkdir()

    f1 = source / "Sample.mkv"
    f2 = source / "cover.jpg"
    f1.touch()
    f2.touch()

    moved, failed = move_junk([f1, f2], source, dest)

    assert moved == 2
    assert failed == 0
    assert not f1.exists()
    assert not f2.exists()
    assert (dest / ".junk" / "Sample.mkv").exists()
    assert (dest / ".junk" / "cover.jpg").exists()


def test_move_junk_preserves_subdirectory_structure(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    subdir = source / "SomeMovie"
    subdir.mkdir(parents=True)
    dest.mkdir()

    f = subdir / "Trailer.mkv"
    f.touch()

    move_junk([f], source, dest)

    assert (dest / ".junk" / "SomeMovie" / "Trailer.mkv").exists()
    assert not f.exists()


def test_move_junk_counts_failures(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    missing = source / "nonexistent.mkv"

    moved, failed = move_junk([missing], source, dest)

    assert moved == 0
    assert failed == 1


def test_move_junk_partial_success(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source.mkdir()
    dest.mkdir()

    real = source / "Sample.mkv"
    real.touch()
    missing = source / "ghost.nfo"

    moved, failed = move_junk([real, missing], source, dest)

    assert moved == 1
    assert failed == 1
    assert not real.exists()
    assert (dest / ".junk" / "Sample.mkv").exists()


def test_move_junk_empty_list(tmp_path):
    moved, failed = move_junk([], tmp_path / "source", tmp_path / "dest")
    assert moved == 0
    assert failed == 0


# ---------------------------------------------------------------------------
# report_junk
# ---------------------------------------------------------------------------


def test_report_junk_empty_list(tmp_path, capsys):
    report_junk([], tmp_path / "source", tmp_path / "dest", dry_run=True)
    # Should not raise — Rich output goes to its own console, no assertion needed


def test_report_junk_dry_run(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    files = [source / "Sample.mkv", source / "cover.jpg"]
    # Should not raise
    report_junk(files, source, dest, dry_run=True)


def test_report_junk_live(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    files = [source / "Sample.mkv"]
    report_junk(files, source, dest, dry_run=False)
