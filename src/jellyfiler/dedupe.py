"""Detect and resolve duplicate destination paths in a move plan.

Two source files that match the same TMDB show/season/episode produce two
PlannedMoves with the same destination. The executor's preflight check used
to abort the entire run on duplicate destinations; this module lets us
choose a winner instead.

Resolution modes:
  * **interactive (default)** — prompt per duplicate pair. Options include:
      - keep file 1/2 (others stay in source untouched)
      - skip both (move neither)
      - "always keep highest quality" sticky for run, losers stay in source
      - "always keep highest quality + quarantine losers" sticky, losers
        moved to ``dest/.aside/duplicates/`` (recoverable)
  * **--remove-duplicates --i-mean-it** — auto-pick the higher-quality file
    and **PERMANENTLY DELETE** the losers (the only path in the codebase
    that deletes files; protected by the double-flag safety in cli.py).
  * **--no-interactive** without --remove-duplicates — skip both files in
    every duplicate pair, log a warning. Safer than aborting the run.

Quality scoring:
  Resolution from the filename (2160p > 1080p > 720p > 480p) is the primary
  key. File size on disk is the tiebreaker. Both come straight from the
  filesystem — no re-parsing or TMDB calls.

Outputs three buckets so the CLI can apply the right action to each:
  - cleaned plan (winners + non-conflicting moves)
  - losers_to_delete (only populated when --remove-duplicates is set)
  - losers_to_quarantine (only populated when the interactive "always +
    quarantine" option was picked)
Skipped losers are added to plan.skipped and stay in the source.
"""

import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jellyfiler.models import Plan, PlannedMove

# Capture the resolution token: "2160p" / "1080p" / "720p" / "480p" / "4K".
_RESOLUTION_PATTERN = re.compile(r"\b(2160p|4k|1080p|720p|480p|360p)\b", re.IGNORECASE)

_RES_RANK = {
    "2160p": 2160,
    "4k": 2160,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}


def _resolution_score(path: Path) -> int:
    """Numeric rank of the resolution tag in the filename. 0 if absent."""
    m = _RESOLUTION_PATTERN.search(path.name)
    if not m:
        return 0
    return _RES_RANK.get(m.group(1).lower(), 0)


def _file_size(path: Path) -> int:
    """File size in bytes, 0 if the file is missing or unreadable."""
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def quality_score(path: Path) -> tuple[int, int]:
    """Sortable quality key — (resolution_rank, file_size). Higher is better."""
    return (_resolution_score(path), _file_size(path))


def _fmt_size(n: int) -> str:
    """Compact bytes → human string."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n //= 1024
    return f"{n:.0f} TB"


def find_duplicate_groups(moves: list[PlannedMove]) -> list[list[PlannedMove]]:
    """Group moves that represent the same canonical content.

    Uses ``move.dedupe_key`` when set — that's a quality-independent identity
    tuple populated by ``planner._dedupe_key`` so a 1080p file and a 720p file
    of the same episode collide on the same key. Falls back to ``destination``
    for moves without a key (e.g. legacy callers, skipped moves).

    Single-element groups are omitted.
    """
    groups: dict[Any, list[PlannedMove]] = defaultdict(list)
    for m in moves:
        key = m.dedupe_key if m.dedupe_key is not None else m.destination
        groups[key].append(m)
    return [g for g in groups.values() if len(g) > 1]


def _to_skipped(m: PlannedMove, reason: str) -> PlannedMove:
    """Return a copy of *m* marked as skipped with the given reason."""
    return PlannedMove(
        source=m.source,
        destination=m.destination,
        media_type=m.media_type,
        tmdb_id=m.tmdb_id,
        matched_title=m.matched_title,
        confidence=m.confidence,
        skipped=True,
        skip_reason=reason,
    )


# Type alias for the interactive prompt callback so the CLI can wire
# in `interactive.prompt_duplicate_choice` without a circular import.
PromptFn = Callable[[list[PlannedMove]], "DuplicateChoice"]


class DuplicateChoice:
    """Outcome of a per-group duplicate prompt."""

    KEEP_INDEX = "keep_index"  # keep group[index], skip others (others stay in source)
    SKIP_ALL = "skip_all"  # don't move any of them
    ALWAYS_HIGHER = "always_higher"  # sticky: highest quality, losers stay in source
    ALWAYS_QUARANTINE = "always_quarantine"  # sticky: highest quality + losers → .aside/duplicates/
    DELETE_LOSERS = (
        "delete_losers"  # ONE-SHOT: keep group[index], delete losers + their parent dirs
    )

    def __init__(self, kind: str, index: int | None = None) -> None:
        self.kind = kind
        self.index = index


def resolve_duplicates(
    plan: Plan,
    *,
    interactive: bool,
    auto_remove: bool,
    prompt: PromptFn | None = None,
) -> tuple[Plan, list[PlannedMove], list[PlannedMove], set[Path]]:
    """Return (resolved_plan, losers_to_delete, losers_to_quarantine, dirs_to_remove).

    Buckets:
      - `losers_to_delete` — files to ``unlink()``. Populated by either
        ``auto_remove=True`` (CLI: --remove-duplicates --i-mean-it) or the
        per-group ``DELETE_LOSERS`` interactive choice.
      - `losers_to_quarantine` — files to move to ``.aside/duplicates/``.
        Only populated by the interactive ``ALWAYS_QUARANTINE`` sticky.
      - `dirs_to_remove` — set of parent directories to recursively remove
        AFTER the losers in them have been unlinked. Populated only by the
        per-group ``DELETE_LOSERS`` choice; never by sticky options.
      - `Plan.skipped` — losers we leave alone in the source.
    """
    groups = find_duplicate_groups(plan.moves)
    if not groups:
        return plan, [], [], set()

    duplicate_destinations = {g[0].destination for g in groups}

    kept_moves: list[PlannedMove] = []
    skipped: list[PlannedMove] = list(plan.skipped)
    to_delete: list[PlannedMove] = []
    to_quarantine: list[PlannedMove] = []
    dirs_to_remove: set[Path] = set()
    sticky: str | None = None  # None | ALWAYS_HIGHER | ALWAYS_QUARANTINE

    # Pass through non-conflicting moves unchanged.
    for m in plan.moves:
        if m.destination not in duplicate_destinations:
            kept_moves.append(m)

    for group in groups:
        # Sort highest quality first so group[0] is always the candidate winner.
        group_sorted = sorted(group, key=lambda m: quality_score(m.source), reverse=True)

        choice: DuplicateChoice
        delete_losers_this_group = False  # one-shot DELETE_LOSERS flag

        if auto_remove:
            # --remove-duplicates: highest quality wins, losers DELETED (file only).
            choice = DuplicateChoice(DuplicateChoice.KEEP_INDEX, index=0)
        elif sticky == DuplicateChoice.ALWAYS_HIGHER or sticky == DuplicateChoice.ALWAYS_QUARANTINE:
            choice = DuplicateChoice(DuplicateChoice.KEEP_INDEX, index=0)
        elif interactive and prompt is not None:
            choice = prompt(group_sorted)
            if choice.kind in (DuplicateChoice.ALWAYS_HIGHER, DuplicateChoice.ALWAYS_QUARANTINE):
                sticky = choice.kind
                choice = DuplicateChoice(DuplicateChoice.KEEP_INDEX, index=0)
            elif choice.kind == DuplicateChoice.DELETE_LOSERS:
                # One-shot: delete file + parent dir for losers in THIS group only.
                delete_losers_this_group = True
                choice = DuplicateChoice(
                    DuplicateChoice.KEEP_INDEX,
                    index=choice.index if choice.index is not None else 0,
                )
        else:
            # Non-interactive without --remove-duplicates: skip all.
            choice = DuplicateChoice(DuplicateChoice.SKIP_ALL)

        if choice.kind == DuplicateChoice.SKIP_ALL:
            for m in group_sorted:
                skipped.append(_to_skipped(m, "duplicate — both skipped"))
            continue

        # KEEP_INDEX: keep the chosen one, handle the losers.
        winner_idx = choice.index if choice.index is not None else 0
        winner = group_sorted[winner_idx]
        losers = [m for i, m in enumerate(group_sorted) if i != winner_idx]
        kept_moves.append(winner)

        for m in losers:
            if auto_remove or delete_losers_this_group:
                to_delete.append(m)
                if delete_losers_this_group:
                    dirs_to_remove.add(m.source.parent)
            elif sticky == DuplicateChoice.ALWAYS_QUARANTINE:
                to_quarantine.append(m)
            else:
                skipped.append(
                    _to_skipped(m, f"duplicate — kept {winner.source.name} (higher quality)")
                )

    return Plan(moves=kept_moves, skipped=skipped), to_delete, to_quarantine, dirs_to_remove


def quarantine_path(loser: PlannedMove, source_root: Path, dest_root: Path) -> Path:
    """Where a loser file lands when quarantined (not deleted).

    Format: dest_root/.aside/duplicates/<relative-from-source>/filename
    Keeps duplicate losers visually separate from regular junk inside .aside/.
    """
    try:
        rel = loser.source.relative_to(source_root)
    except ValueError:
        rel = Path(loser.source.name)
    return dest_root / ".aside" / "duplicates" / rel


# Public helper for nice display strings used by both the prompt and tests.
def describe(move: PlannedMove) -> str:
    """One-line summary of a candidate file: '/path/file.mkv  (1080p, 2.4 GB)'."""
    res = _resolution_score(move.source)
    res_str = f"{res}p" if res else "?"
    size = _file_size(move.source)
    return f"{move.source}  ({res_str}, {_fmt_size(size)})"
