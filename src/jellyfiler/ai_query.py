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


def suggest_search(
    parent_dir: str,
    filename: str,
    api_key: str,
    is_tv: bool = False,
) -> dict[str, object] | None:
    """Ask Claude Haiku to parse a release name into a clean TMDB search query.

    For movies returns {title, year}. For TV returns {title} only — year is
    never passed to search_tv so there is no point asking for it.
    Returns None on any error so the caller can silently continue.

    Uses ANTHROPIC_API_KEY — not Bedrock — since this is a personal project.
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
        block = message.content[0]
        if not isinstance(block, _TextBlock):
            return None
        raw = block.text.strip()
        # Strip markdown code fences if the model wraps the JSON
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        data: dict[str, object] = json.loads(raw)
        if not isinstance(data.get("title"), str) or not data["title"]:
            return None
        return data
    except Exception:
        return None
