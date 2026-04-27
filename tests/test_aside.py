"""Tests for junk file detection and quarantine."""

from pathlib import Path

import pytest

from jellyfiler.aside import aside_destination, find_aside, is_aside, move_aside, report_aside

# ---------------------------------------------------------------------------
# is_aside — non-video sidecar extensions
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
def test_is_aside_sidecar_extensions(filename):
    assert is_aside(Path(filename))


# ---------------------------------------------------------------------------
# is_aside — video files with junk stem patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        # Stems that are ALWAYS aside regardless of where they live
        "Sample.mkv",
        "sample.mp4",
        "SAMPLE.avi",
        "RARBG.com.mp4",
        "RARBG.com.mkv",
        "rarbg.info.mp4",
        "RARBG.mkv",
        "etrg.mp4",
        "www.YTS.AM.mp4",
    ],
)
def test_is_aside_video_stem_patterns(filename):
    assert is_aside(Path(filename))


@pytest.mark.parametrize(
    "filename",
    [
        # These need a parent-dir hint now (Featurettes/, Behind The Scenes/, etc.)
        # rather than being detected by stem alone, because "Trailer.mkv" or
        # "Scene.mkv" can be real movie names.
        "Trailer.mkv",
        "Featurette.mkv",
        "Deleted.Scenes.mkv",
        "Behind.The.Scenes.mkv",
        "Interview.mkv",
        "Scene.mkv",
    ],
)
def test_no_longer_detected_by_stem_alone(filename):
    """These stems WERE flagged as junk by the old binary classifier; now they
    only count as aside when in a known parent dir (Featurettes/, etc.)."""
    assert not is_aside(Path(filename))


# ---------------------------------------------------------------------------
# is_aside — hex hash filenames
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
def test_is_aside_hex_hash(filename):
    assert is_aside(Path(filename))


# ---------------------------------------------------------------------------
# is_aside — real media files should NOT be junk
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
def test_is_not_aside_real_files(filename):
    assert not is_aside(Path(filename))


# ---------------------------------------------------------------------------
# is_aside — unknown/non-media extensions are not flagged as junk video
# ---------------------------------------------------------------------------


def test_is_not_aside_unknown_extension():
    # .xyz is not a video extension and not a known sidecar — should not be junk
    assert not is_aside(Path("sample.xyz"))


# ---------------------------------------------------------------------------
# is_aside — parent directory name detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parent",
    [
        "Sample",
        "Samples",
        "sample",
        "Screen",
        "Screens",
        "Screenshots",
        "Featurettes",
        "Featurette",
        "Extras",
        "Extra",
        "Bonus",
        "Trailers",
        "Trailer",
        "Behind the Scenes",
        "Deleted Scenes",
        "Deleted Scene",
        "Interviews",
        "Interview",
        "Bloopers",
        "Fake Endings",
        "Fake Ending",
        "Shorts",
        "Short",
        "Promos",
        "Specials",
    ],
)
def test_aside_by_parent_dir(tmp_path, parent):
    aside_dir = tmp_path / parent
    aside_dir.mkdir()
    f = aside_dir / "something.mkv"
    assert is_aside(f)


def test_real_file_in_normal_dir_not_aside(tmp_path):
    d = tmp_path / "Futurama Season 12"
    d.mkdir()
    f = d / "Futurama.S12E01.mkv"
    assert not is_aside(f)


def test_aside_nested_deep(tmp_path):
    """File deep inside a Featurettes folder is caught regardless of nesting."""
    nested = tmp_path / "Movie (2009)" / "Featurettes" / "The Movie" / "Fake Endings"
    nested.mkdir(parents=True)
    f = nested / "Zombie Meat.mkv"
    assert is_aside(f)


# ---------------------------------------------------------------------------
# find_aside
# ---------------------------------------------------------------------------


def test_find_aside_returns_aside_files(tmp_path):
    (tmp_path / "Movie.mkv").touch()
    (tmp_path / "Sample.mkv").touch()
    (tmp_path / "cover.jpg").touch()
    (tmp_path / "release.nfo").touch()

    result = find_aside(tmp_path)
    names = {p.name for p, _kind in result}
    assert names == {"Sample.mkv", "cover.jpg", "release.nfo"}


def test_find_aside_recurses_subdirectories(tmp_path):
    subdir = tmp_path / "disc1"
    subdir.mkdir()
    (subdir / "8fa41b40995c44c9a883b1e0fe62f16a.mkv").touch()
    (subdir / "Movie.mkv").touch()

    result = find_aside(tmp_path)
    assert len(result) == 1
    assert result[0][0].name == "8fa41b40995c44c9a883b1e0fe62f16a.mkv"


def test_find_aside_empty_directory(tmp_path):
    assert find_aside(tmp_path) == []


def test_find_aside_no_aside_in_clean_directory(tmp_path):
    (tmp_path / "Blade.Runner.2049.mkv").touch()
    (tmp_path / "Futurama.S01E01.mkv").touch()
    assert find_aside(tmp_path) == []


def test_find_aside_returns_sorted_by_path(tmp_path):
    (tmp_path / "zzz.nfo").touch()
    (tmp_path / "aaa.nfo").touch()
    (tmp_path / "mmm.nfo").touch()

    result = find_aside(tmp_path)
    paths = [p for p, _ in result]
    assert paths == sorted(paths)


# ---------------------------------------------------------------------------
# aside_destination
# ---------------------------------------------------------------------------


def test_aside_destination_flat(tmp_path):
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    file = source_root / "Sample.mkv"

    result = aside_destination(file, source_root, dest_root)
    assert result == dest_root / ".aside" / "Sample.mkv"


def test_aside_destination_preserves_subdir(tmp_path):
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    file = source_root / "SomeMovie" / "Sample.mkv"

    result = aside_destination(file, source_root, dest_root)
    assert result == dest_root / ".aside" / "SomeMovie" / "Sample.mkv"


def test_aside_destination_file_outside_source(tmp_path):
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    file = tmp_path / "elsewhere" / "RARBG.com.mp4"

    # Falls back to filename only when file is outside source_root
    result = aside_destination(file, source_root, dest_root)
    assert result == dest_root / ".aside" / "RARBG.com.mp4"


# ---------------------------------------------------------------------------
# move_aside
# ---------------------------------------------------------------------------


def test_move_aside_moves_files(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source.mkdir()
    dest.mkdir()

    f1 = source / "Sample.mkv"
    f2 = source / "cover.jpg"
    f1.touch()
    f2.touch()

    moved, failed = move_aside([f1, f2], source, dest)

    assert moved == 2
    assert failed == 0
    assert not f1.exists()
    assert not f2.exists()
    assert (dest / ".aside" / "Sample.mkv").exists()
    assert (dest / ".aside" / "cover.jpg").exists()


def test_move_aside_preserves_subdirectory_structure(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    subdir = source / "SomeMovie"
    subdir.mkdir(parents=True)
    dest.mkdir()

    f = subdir / "Trailer.mkv"
    f.touch()

    move_aside([f], source, dest)

    assert (dest / ".aside" / "SomeMovie" / "Trailer.mkv").exists()
    assert not f.exists()


def test_move_aside_counts_failures(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    missing = source / "nonexistent.mkv"

    moved, failed = move_aside([missing], source, dest)

    assert moved == 0
    assert failed == 1


def test_move_aside_partial_success(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source.mkdir()
    dest.mkdir()

    real = source / "Sample.mkv"
    real.touch()
    missing = source / "ghost.nfo"

    moved, failed = move_aside([real, missing], source, dest)

    assert moved == 1
    assert failed == 1
    assert not real.exists()
    assert (dest / ".aside" / "Sample.mkv").exists()


def test_move_aside_empty_list(tmp_path):
    moved, failed = move_aside([], tmp_path / "source", tmp_path / "dest")
    assert moved == 0
    assert failed == 0


# ---------------------------------------------------------------------------
# report_aside
# ---------------------------------------------------------------------------


def test_report_junk_empty_list(tmp_path, capsys):
    report_aside([], tmp_path / "source", tmp_path / "dest", dry_run=True)
    # Should not raise — Rich output goes to its own console, no assertion needed


def test_report_junk_dry_run(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    files = [source / "Sample.mkv", source / "cover.jpg"]
    # Should not raise
    report_aside(files, source, dest, dry_run=True)


def test_report_junk_live(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    files = [source / "Sample.mkv"]
    report_aside(files, source, dest, dry_run=False)


# ---------------------------------------------------------------------------
# Anime OP/ED and OVA/ONA detection (real-world filenames from SMB share)
# ---------------------------------------------------------------------------


def test_aside_ncop_anime_opening(tmp_path):
    """[Coalgirls]_Ao_no_Exorcist_NCOP_(...) — Non-Credit Opening."""
    f = tmp_path / "[Coalgirls]_Ao_no_Exorcist_NCOP_(1920x1080_Blu-Ray_FLAC)_[E92D4C42].mkv"
    f.touch()
    assert is_aside(f)


def test_aside_nced_anime_ending(tmp_path):
    """[Coalgirls]_Ao_no_Exorcist_NCED_(...) — Non-Credit Ending."""
    f = tmp_path / "[Coalgirls]_Ao_no_Exorcist_NCED_(1920x1080_Blu-Ray_FLAC)_[00518C15].mkv"
    f.touch()
    assert is_aside(f)


def test_aside_nced2_numbered_ending(tmp_path):
    """NCED2 — second ending track, also junk."""
    f = tmp_path / "[Coalgirls]_Ao_no_Exorcist_NCED2_(1920x1080_Blu-Ray_FLAC)_[3E5D24F0].mkv"
    f.touch()
    assert is_aside(f)


def test_aside_creditless_op_anime(tmp_path):
    """Creditless_OP1 — alternative spelling for non-credit opening."""
    f = tmp_path / "Fate_Stay_Night_Creditless_OP1_[720p,BluRay,x264]_-_THORA.mkv"
    f.touch()
    assert is_aside(f)


def test_aside_creditless_op2(tmp_path):
    """Creditless_OP2 — numbered creditless opening."""
    f = tmp_path / "Fate_Stay_Night_Creditless_OP2_[720p,BluRay,x264]_-_THORA.mkv"
    f.touch()
    assert is_aside(f)


def test_ova_files_are_NOT_junked(tmp_path):
    """OVAs are legitimate content (Jellyfin treats them as S00 specials) — must NOT be junked."""
    f = tmp_path / "[Cerberus] KonoSuba OVA - 01 [BD 1080p].mkv"
    f.touch()
    assert not is_aside(f)


def test_ova_subdirectory_is_NOT_junked(tmp_path):
    """An OVA/ folder full of bonus episodes is library content, not trash."""
    sub = tmp_path / "OVA"
    sub.mkdir()
    f = sub / "Show.S00E01.mkv"
    f.touch()
    assert not is_aside(f)


def test_aside_openings_directory(tmp_path):
    """Openings/ folder is junk."""
    sub = tmp_path / "Openings"
    sub.mkdir()
    f = sub / "Opening 1.mkv"
    f.touch()
    assert is_aside(f)


def test_normal_episode_with_operation_in_title_is_not_junk(tmp_path):
    """'Operation Ruthless' contains 'OP' but isn't an opening — must NOT match."""
    f = tmp_path / "Hey Arnold S01E07a Operation Ruthless.mkv"
    f.touch()
    assert not is_aside(f)


def test_normal_episode_with_ending_in_title_is_not_junk(tmp_path):
    """'Endings Are Always...' is an episode title, not a creditless ending."""
    f = tmp_path / "[OZC]Planetes E19 'Endings Are Always...'.mkv"
    f.touch()
    assert not is_aside(f)


def test_eden_does_not_match_ed_token(tmp_path):
    """Standalone 'ED' must require word boundary — 'Eden' shouldn't trigger."""
    f = tmp_path / "Garden of Eden S01E05.mkv"
    f.touch()
    assert not is_aside(f)
