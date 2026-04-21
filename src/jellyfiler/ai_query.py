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
