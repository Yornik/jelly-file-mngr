"""Claude Haiku fallback for generating a clean TMDB search query from a messy release name."""

from __future__ import annotations

import json
import re

try:
    import anthropic as _anthropic

    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _anthropic = None  # type: ignore[assignment]
    _ANTHROPIC_AVAILABLE = False


def suggest_search(
    parent_dir: str,
    filename: str,
    api_key: str,
) -> dict[str, object] | None:
    """Ask Claude Haiku to parse a release name into a clean TMDB search query.

    Returns a dict with keys: title (str), year (int|None), media_type ("movie"|"episode"),
    season (int|None).  Returns None on any error so the caller can silently continue.

    Uses ANTHROPIC_API_KEY — not Bedrock — since this is a personal project.
    """
    if not _ANTHROPIC_AVAILABLE or _anthropic is None:
        return None

    prompt = (
        "You are a media file metadata extractor. Given a parent directory name and filename "
        "from a torrent/release, return the most likely TMDB search query as JSON.\n\n"
        f"Parent directory: {parent_dir}\n"
        f"Filename: {filename}\n\n"
        "Respond with ONLY valid JSON, no explanation:\n"
        '{"title": "...", "year": null_or_int, "media_type": "movie" or "episode", "season": null_or_int}'
    )

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if the model wraps the JSON
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        data: dict[str, object] = json.loads(raw)
        if not isinstance(data.get("title"), str) or not data["title"]:
            return None
        return data
    except Exception:
        return None
