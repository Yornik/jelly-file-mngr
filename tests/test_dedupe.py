"""Tests for duplicate detection and resolution."""

from pathlib import Path

import pytest

from jellyfiler.dedupe import (
    DuplicateChoice,
    _resolution_score,
    describe,
    find_duplicate_groups,
    quality_score,
    quarantine_path,
    resolve_duplicates,
)
from jellyfiler.models import MediaType, Plan, PlannedMove


def _move(source: Path, destination: Path) -> PlannedMove:
    return PlannedMove(
        source=source,
        destination=destination,
        media_type=MediaType.EPISODE,
        tmdb_id=1,
        matched_title="Show",
        confidence="high",
    )


# ---------------------------------------------------------------------------
# _resolution_score — parsed from filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Show.S01E01.2160p.mkv", 2160),
        ("Show.S01E01.4K.mkv", 2160),
        ("Show.S01E01.4k.mkv", 2160),
        ("Show.S01E01.1080p.mkv", 1080),
        ("Show.S01E01.720p.mkv", 720),
        ("Show.S01E01.480p.mkv", 480),
        ("Show.S01E01.360p.mkv", 360),
        ("Show.S01E01.mkv", 0),  # no resolution tag
        ("Show.S01E01.UHD.BluRay.mkv", 0),  # tag without "p" suffix
    ],
)
def test_resolution_score(name, expected):
    assert _resolution_score(Path(name)) == expected


def test_quality_score_higher_resolution_wins(tmp_path):
    f1080 = tmp_path / "show.1080p.mkv"
    f720 = tmp_path / "show.720p.mkv"
    f1080.write_bytes(b"x" * 100)  # smaller file, but higher res
    f720.write_bytes(b"x" * 1000)
    assert quality_score(f1080) > quality_score(f720)


def test_quality_score_size_breaks_tie(tmp_path):
    """Same resolution → larger file wins on tiebreak."""
    big = tmp_path / "big.1080p.mkv"
    small = tmp_path / "small.1080p.mkv"
    big.write_bytes(b"x" * 1000)
    small.write_bytes(b"x" * 100)
    assert quality_score(big) > quality_score(small)


def test_quality_score_missing_file_returns_zero(tmp_path):
    """File doesn't exist on disk → size=0 (no exception)."""
    score = quality_score(tmp_path / "nonexistent.1080p.mkv")
    assert score == (1080, 0)


# ---------------------------------------------------------------------------
# find_duplicate_groups
# ---------------------------------------------------------------------------


def test_find_duplicate_groups_empty():
    assert find_duplicate_groups([]) == []


def test_find_duplicate_groups_no_duplicates():
    moves = [
        _move(Path("a.mkv"), Path("/dest/A/S01E01.mkv")),
        _move(Path("b.mkv"), Path("/dest/B/S01E01.mkv")),
    ]
    assert find_duplicate_groups(moves) == []


def test_find_duplicate_groups_one_pair():
    dest = Path("/dest/Show/Season 01/S01E01.mkv")
    moves = [
        _move(Path("a.mkv"), dest),
        _move(Path("b.mkv"), dest),
        _move(Path("c.mkv"), Path("/dest/Show/Season 01/S01E02.mkv")),
    ]
    groups = find_duplicate_groups(moves)
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_find_duplicate_groups_three_way_collision():
    dest = Path("/dest/Show/Season 01/S01E01.mkv")
    moves = [
        _move(Path("a.mkv"), dest),
        _move(Path("b.mkv"), dest),
        _move(Path("c.mkv"), dest),
    ]
    groups = find_duplicate_groups(moves)
    assert len(groups) == 1
    assert len(groups[0]) == 3


# ---------------------------------------------------------------------------
# resolve_duplicates
# ---------------------------------------------------------------------------


def test_resolve_duplicates_no_groups_returns_unchanged(tmp_path):
    plan = Plan(moves=[_move(tmp_path / "a.mkv", tmp_path / "out" / "a.mkv")])
    cleaned, to_delete, to_quarantine, _dirs = resolve_duplicates(
        plan, interactive=False, auto_remove=False
    )
    assert cleaned.moves == plan.moves
    assert to_delete == []
    assert to_quarantine == []


def test_resolve_duplicates_non_interactive_skips_both(tmp_path):
    """--no-interactive without --remove-duplicates: skip both files in each duplicate."""
    f1 = tmp_path / "a.1080p.mkv"
    f2 = tmp_path / "b.720p.mkv"
    f1.touch()
    f2.touch()
    dest = tmp_path / "out/Show/S01E01.mkv"
    plan = Plan(moves=[_move(f1, dest), _move(f2, dest)])

    cleaned, to_delete, to_quarantine, _dirs = resolve_duplicates(
        plan, interactive=False, auto_remove=False
    )
    assert cleaned.moves == []
    assert to_delete == []
    assert to_quarantine == []
    assert len(cleaned.skipped) == 2
    assert all("both skipped" in m.skip_reason for m in cleaned.skipped)


def test_resolve_duplicates_auto_remove_keeps_higher_quality(tmp_path):
    """--remove-duplicates: keep highest quality, losers go to to_delete."""
    f1080 = tmp_path / "show.1080p.mkv"
    f720 = tmp_path / "show.720p.mkv"
    f1080.write_bytes(b"x" * 1000)
    f720.write_bytes(b"x" * 500)
    dest = tmp_path / "out/Show/S01E01.mkv"
    plan = Plan(moves=[_move(f720, dest), _move(f1080, dest)])

    cleaned, to_delete, to_quarantine, _dirs = resolve_duplicates(
        plan, interactive=False, auto_remove=True
    )
    assert len(cleaned.moves) == 1
    assert cleaned.moves[0].source == f1080
    assert len(to_delete) == 1
    assert to_delete[0].source == f720
    assert to_quarantine == []


def test_resolve_duplicates_interactive_keep_index_0(tmp_path):
    """User picks file 1 (index 0) — others stay in source."""
    f1 = tmp_path / "a.1080p.mkv"
    f2 = tmp_path / "b.720p.mkv"
    f1.touch()
    f2.touch()
    dest = tmp_path / "out/S01E01.mkv"
    plan = Plan(moves=[_move(f1, dest), _move(f2, dest)])

    def prompt(group):
        return DuplicateChoice(DuplicateChoice.KEEP_INDEX, index=0)

    cleaned, to_delete, to_quarantine, _dirs = resolve_duplicates(
        plan, interactive=True, auto_remove=False, prompt=prompt
    )
    assert len(cleaned.moves) == 1
    assert cleaned.moves[0].source == f1
    assert to_delete == []
    assert to_quarantine == []
    # Loser was put in skipped, not deleted/quarantined
    assert len(cleaned.skipped) == 1
    assert cleaned.skipped[0].source == f2


def test_resolve_duplicates_interactive_skip_all(tmp_path):
    """User picks 's' — neither file moves."""
    f1 = tmp_path / "a.1080p.mkv"
    f2 = tmp_path / "b.720p.mkv"
    f1.touch()
    f2.touch()
    dest = tmp_path / "out/S01E01.mkv"
    plan = Plan(moves=[_move(f1, dest), _move(f2, dest)])

    cleaned, to_delete, to_quarantine, _dirs = resolve_duplicates(
        plan,
        interactive=True,
        auto_remove=False,
        prompt=lambda g: DuplicateChoice(DuplicateChoice.SKIP_ALL),
    )
    assert cleaned.moves == []
    assert len(cleaned.skipped) == 2
    assert to_delete == []
    assert to_quarantine == []


def test_resolve_duplicates_interactive_always_higher_is_sticky(tmp_path):
    """Once user picks 'a', subsequent groups don't re-prompt — auto-pick highest, leave losers in source."""
    f1080a = tmp_path / "a.1080p.mkv"
    f720a = tmp_path / "a.720p.mkv"
    f1080b = tmp_path / "b.1080p.mkv"
    f720b = tmp_path / "b.720p.mkv"
    for f in (f1080a, f720a, f1080b, f720b):
        f.touch()
    dest_a = tmp_path / "out/Show/S01E01.mkv"
    dest_b = tmp_path / "out/Show/S01E02.mkv"
    plan = Plan(
        moves=[
            _move(f1080a, dest_a),
            _move(f720a, dest_a),
            _move(f1080b, dest_b),
            _move(f720b, dest_b),
        ]
    )

    call_count = {"n": 0}

    def prompt(group):
        call_count["n"] += 1
        return DuplicateChoice(DuplicateChoice.ALWAYS_HIGHER)

    cleaned, to_delete, to_quarantine, _dirs = resolve_duplicates(
        plan, interactive=True, auto_remove=False, prompt=prompt
    )
    # Sticky → only the FIRST group prompts, the second is auto-handled.
    assert call_count["n"] == 1
    assert len(cleaned.moves) == 2  # both winners
    winner_sources = {m.source for m in cleaned.moves}
    assert winner_sources == {f1080a, f1080b}
    assert to_delete == []
    assert to_quarantine == []
    assert len(cleaned.skipped) == 2  # losers stay in source


def test_resolve_duplicates_interactive_always_quarantine_is_sticky(tmp_path):
    """ALWAYS_QUARANTINE: losers go into to_quarantine, sticky for rest of run."""
    f1080a = tmp_path / "a.1080p.mkv"
    f720a = tmp_path / "a.720p.mkv"
    f1080b = tmp_path / "b.1080p.mkv"
    f720b = tmp_path / "b.720p.mkv"
    for f in (f1080a, f720a, f1080b, f720b):
        f.touch()
    dest_a = tmp_path / "out/Show/S01E01.mkv"
    dest_b = tmp_path / "out/Show/S01E02.mkv"
    plan = Plan(
        moves=[
            _move(f1080a, dest_a),
            _move(f720a, dest_a),
            _move(f1080b, dest_b),
            _move(f720b, dest_b),
        ]
    )

    call_count = {"n": 0}

    def prompt(group):
        call_count["n"] += 1
        return DuplicateChoice(DuplicateChoice.ALWAYS_QUARANTINE)

    cleaned, to_delete, to_quarantine, _dirs = resolve_duplicates(
        plan, interactive=True, auto_remove=False, prompt=prompt
    )
    assert call_count["n"] == 1  # sticky → only first group prompts
    assert len(cleaned.moves) == 2  # winners
    assert to_delete == []
    quar_sources = {m.source for m in to_quarantine}
    assert quar_sources == {f720a, f720b}


def test_resolve_duplicates_delete_losers_one_shot(tmp_path):
    """DELETE_LOSERS: losers go to to_delete + their parent dirs to dirs_to_remove (this group only)."""
    rel_a_dir = tmp_path / "Show.S01E01.1080p-RARBG"
    rel_b_dir = tmp_path / "Show.S01E01.720p-YIFY"
    rel_a_dir.mkdir()
    rel_b_dir.mkdir()
    f1080 = rel_a_dir / "Show.S01E01.1080p.mkv"
    f720 = rel_b_dir / "Show.S01E01.720p.mkv"
    f1080.write_bytes(b"x" * 1000)
    f720.write_bytes(b"x" * 500)
    dest = tmp_path / "out/Show/S01E01.mkv"
    plan = Plan(moves=[_move(f1080, dest), _move(f720, dest)])

    def prompt(group):
        # Index 0 in the sorted group is the highest quality (1080p)
        return DuplicateChoice(DuplicateChoice.DELETE_LOSERS, index=0)

    cleaned, to_delete, to_quarantine, dirs = resolve_duplicates(
        plan, interactive=True, auto_remove=False, prompt=prompt
    )
    assert len(cleaned.moves) == 1
    assert cleaned.moves[0].source == f1080
    assert len(to_delete) == 1
    assert to_delete[0].source == f720
    assert dirs == {rel_b_dir}  # parent dir of the loser
    assert to_quarantine == []


def test_resolve_duplicates_delete_losers_is_NOT_sticky(tmp_path):
    """DELETE_LOSERS only applies to its own group — second group must re-prompt."""
    f1080a = tmp_path / "a.1080p.mkv"
    f720a = tmp_path / "a.720p.mkv"
    f1080b = tmp_path / "b.1080p.mkv"
    f720b = tmp_path / "b.720p.mkv"
    for f in (f1080a, f720a, f1080b, f720b):
        f.write_bytes(b"x")
    dest_a = tmp_path / "out/Show/S01E01.mkv"
    dest_b = tmp_path / "out/Show/S01E02.mkv"
    plan = Plan(
        moves=[
            _move(f1080a, dest_a),
            _move(f720a, dest_a),
            _move(f1080b, dest_b),
            _move(f720b, dest_b),
        ]
    )

    call_count = {"n": 0}
    responses = [
        DuplicateChoice(DuplicateChoice.DELETE_LOSERS, index=0),
        DuplicateChoice(DuplicateChoice.KEEP_INDEX, index=0),  # second group: just skip
    ]

    def prompt(group):
        idx = call_count["n"]
        call_count["n"] += 1
        return responses[idx]

    cleaned, to_delete, _, dirs = resolve_duplicates(
        plan, interactive=True, auto_remove=False, prompt=prompt
    )
    assert call_count["n"] == 2  # NOT sticky — both groups prompted
    # Only the first group's loser was deleted
    assert len(to_delete) == 1
    assert len(dirs) == 1


def test_resolve_duplicates_passes_through_non_conflicting_moves(tmp_path):
    """Moves with unique destinations are passed through unchanged."""
    f1 = tmp_path / "alone.1080p.mkv"
    f2a = tmp_path / "dup1.mkv"
    f2b = tmp_path / "dup2.mkv"
    f1.touch()
    f2a.touch()
    f2b.touch()
    plan = Plan(
        moves=[
            _move(f1, tmp_path / "out/A/S01E01.mkv"),
            _move(f2a, tmp_path / "out/B/S01E01.mkv"),
            _move(f2b, tmp_path / "out/B/S01E01.mkv"),
        ]
    )

    cleaned, to_delete, _quar, _dirs = resolve_duplicates(plan, interactive=False, auto_remove=True)
    # Non-conflicting move is preserved; the duplicate group has one winner.
    sources = {m.source for m in cleaned.moves}
    assert f1 in sources
    assert len(cleaned.moves) == 2
    assert len(to_delete) == 1


# ---------------------------------------------------------------------------
# quarantine_path
# ---------------------------------------------------------------------------


def test_quarantine_path_preserves_relative(tmp_path):
    src_root = tmp_path / "src"
    dst_root = tmp_path / "dst"
    loser_src = src_root / "Show" / "ep.720p.mkv"
    loser = _move(loser_src, dst_root / "Show/S01E01.mkv")
    qpath = quarantine_path(loser, src_root, dst_root)
    assert qpath == dst_root / ".aside" / "duplicates" / "Show" / "ep.720p.mkv"


def test_quarantine_path_falls_back_when_outside_source(tmp_path):
    src_root = tmp_path / "src"
    dst_root = tmp_path / "dst"
    loser_src = tmp_path / "elsewhere" / "ep.mkv"
    loser = _move(loser_src, dst_root / "Show/S01E01.mkv")
    qpath = quarantine_path(loser, src_root, dst_root)
    # Falls back to bare filename when the loser isn't under source_root.
    assert qpath == dst_root / ".aside" / "duplicates" / "ep.mkv"


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def test_describe_with_resolution_and_size(tmp_path):
    f = tmp_path / "show.1080p.mkv"
    f.write_bytes(b"x" * 2048)  # 2 KB
    move = _move(f, tmp_path / "out/x.mkv")
    s = describe(move)
    assert "1080p" in s
    assert "KB" in s
    assert str(f) in s


def test_describe_no_resolution_uses_question_mark(tmp_path):
    f = tmp_path / "show.mkv"
    f.touch()
    move = _move(f, tmp_path / "out/x.mkv")
    s = describe(move)
    assert "?" in s
