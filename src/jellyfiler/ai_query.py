"""Claude Haiku fallback for generating a clean TMDB search query from a messy release name."""

from __future__ import annotations

import json
import re

try:
    import anthropic as _anthropic
    from anthropic.types import TextBlock as _TextBlock

    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _anthropic = None  # type: ignore[assignment]
    _TextBlock = None  # type: ignore[assignment,misc]
    _ANTHROPIC_AVAILABLE = False


_SYSTEM_MOVIE = 'Extract the movie title and release year from a release name. Reply with ONLY JSON: {"title":"...","year":null}'
_SYSTEM_TV = 'Extract the TV show title from a release name. Reply with ONLY JSON: {"title":"..."}'

# Classification labels Haiku can return. Keep in sync with AsideKind in aside.py
# plus MAIN_MEDIA (= "this is a real episode/movie, not bonus content").
_VALID_ASIDE_KINDS = {
    "MAIN_MEDIA",
    "DISCARD",
    "EXTRAS",
    "FEATURETTES",
    "BEHIND_THE_SCENES",
    "DELETED_SCENES",
    "INTERVIEWS",
    "TRAILERS",
    "SHORTS",
    "BLOOPERS",
    "ANIME_OP_ED",
}

_SYSTEM_ASIDE_CLASSIFY = (
    "Classify a media file by its parent directory name and filename. "
    "Reply with ONLY JSON: "
    '{"kind":"<LABEL>"}\n\n'
    "Labels:\n"
    "  MAIN_MEDIA        — a real episode or movie itself.\n"
    "  DISCARD           — sample/promo/junk with no real value (delete-candidate).\n"
    "  EXTRAS            — generic bonus content (DVD extras, bonus features, …).\n"
    "  FEATURETTES       — making-of, production featurettes.\n"
    "  BEHIND_THE_SCENES — on-set, behind-the-camera footage.\n"
    "  DELETED_SCENES    — cut scenes.\n"
    "  INTERVIEWS        — cast or crew interviews.\n"
    "  TRAILERS          — trailers, teasers.\n"
    "  SHORTS            — short films packaged as bonus.\n"
    "  BLOOPERS          — gag reels.\n"
    "  ANIME_OP_ED       — non-credit anime opening/ending tracks (NCOP/NCED).\n\n"
    "Foreign-language directory names are common (Bonusy, Doplnki, etc.) — "
    "use the filename and dir together to decide. When in doubt, prefer MAIN_MEDIA."
)


class AiQueryError(Exception):
    """Raised when the Anthropic API call itself fails (auth, network, quota)."""


def preflight_check(api_key: str) -> bool:
    """Send a minimal ping to Haiku and verify it responds with "true".

    Returns True when the key works, False on any error.
    """
    if not _ANTHROPIC_AVAILABLE or _anthropic is None:
        return False
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "respond with the single word: true"}],
        )
        block = message.content[0]
        if not isinstance(block, _TextBlock):
            return False
        return block.text.strip().lower() == "true"
    except Exception:
        return False


def suggest_search(
    parent_dir: str,
    filename: str,
    api_key: str,
    is_tv: bool = False,
) -> dict[str, object] | None:
    """Ask Claude Haiku to parse a release name into a clean TMDB search query.

    For movies returns {title, year}. For TV returns {title} only.
    Returns None when the model gives an unusable response (bad JSON, no title).
    Raises AiQueryError when the API call itself fails (auth, network, quota).
    """
    if not _ANTHROPIC_AVAILABLE or _anthropic is None:
        return None

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=48,
            system=_SYSTEM_TV if is_tv else _SYSTEM_MOVIE,
            messages=[
                {
                    "role": "user",
                    "content": f"{parent_dir} / {filename}",
                }
            ],
        )
    except Exception as exc:
        raise AiQueryError(str(exc)) from exc

    block = message.content[0]
    if not isinstance(block, _TextBlock):
        return None
    raw = block.text.strip()
    # Strip markdown code fences if the model wraps the JSON
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    try:
        data: dict[str, object] = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data.get("title"), str) or not data["title"]:
        return None
    return data


def suggest_aside_kind(
    parent_dir: str,
    filename: str,
    api_key: str,
) -> str | None:
    """Ask Claude Haiku to classify a file as MAIN_MEDIA / DISCARD / extras-kind.

    Returns the label string when the model answers cleanly, ``None`` on any
    parse error or unrecognised label. Raises :class:`AiQueryError` when the
    API call itself fails so the caller can decide whether to abort the run.

    Pattern-based ``classify_aside`` in aside.py uses parent-dir-name regexes
    which miss foreign-language names (``Bonusy/``, ``Doplnki/``, …) and
    irregular layouts. This function fills that gap by reading the filename
    + parent-dir together — much more reliable than name regexes.
    """
    if not _ANTHROPIC_AVAILABLE or _anthropic is None:
        return None

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=24,
            system=_SYSTEM_ASIDE_CLASSIFY,
            messages=[
                {
                    "role": "user",
                    "content": f"Parent directory: {parent_dir}\nFilename: {filename}",
                }
            ],
        )
    except Exception as exc:
        raise AiQueryError(str(exc)) from exc

    block = message.content[0]
    if not isinstance(block, _TextBlock):
        return None
    raw = block.text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    try:
        data: dict[str, object] = json.loads(raw)
    except Exception:
        return None
    kind = data.get("kind")
    if not isinstance(kind, str):
        return None
    kind = kind.strip().upper()
    if kind not in _VALID_ASIDE_KINDS:
        return None
    return kind
