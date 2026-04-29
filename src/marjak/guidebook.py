# guidebook.py — Mārjak: macOS Filesystem Knowledge Retrieval
#
# Tag-based retrieval from macos_guidebook.yaml.  Zero dependencies beyond
# PyYAML (already a project dep) and the stdlib.
#
# Usage:
#   from guidebook import retrieve_guidebook
#   text = retrieve_guidebook("brave browser cache cleanup",
#                             list(session_book.nodes.keys()))

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

_GUIDEBOOK_PATH = Path(__file__).parent / "data" / "macos_guidebook.yaml"

# ─── loader ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_guidebook() -> list[dict]:
    """Load and cache the YAML guidebook (called once per process)."""
    with open(_GUIDEBOOK_PATH, "r", encoding="utf-8") as f:
        entries = yaml.safe_load(f)
    # Pre-expand ~ in paths for matching
    home = os.path.expanduser("~")
    for entry in entries:
        entry["_expanded_paths"] = []
        for p in entry.get("paths", []):
            expanded = p.replace("~", home).rstrip("/")
            entry["_expanded_paths"].append(expanded)
    return entries

# ─── tokeniser ──────────────────────────────────────────────────────────────

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "can", "could", "i", "my", "me", "you",
    "your", "it", "its", "we", "our", "they", "them", "their", "this",
    "that", "these", "those", "and", "or", "but", "if", "then", "so",
    "for", "of", "in", "on", "at", "to", "from", "by", "with", "about",
    "into", "through", "up", "out", "all", "some", "any", "no", "not",
    "how", "much", "many", "where", "what", "which", "who", "when",
    "there", "here", "very", "just", "also", "go", "find", "get",
    "take", "make", "want", "need", "please", "help", "show", "tell",
    "look", "see", "check", "okay", "ok",
})

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Extract meaningful lowercase tokens from a query string."""
    words = set(_WORD_RE.findall(text.lower()))
    return words - _STOP_WORDS


# ─── scoring ────────────────────────────────────────────────────────────────

_TAG_WEIGHT = 2
_PATH_WEIGHT = 3
_MIN_SCORE = 3


def _score_entry(entry: dict, query_tokens: set[str],
                 vfs_paths: list[str]) -> int:
    """Score a guidebook entry against the user query and explored VFS paths.

    Scoring rules:
    - Tag match:  +2 per query token found in entry tags (primary signal).
    - Path match: +3 if a VFS path overlaps AND there is at least one tag hit.
                  +1 if a VFS path overlaps but zero tag hits (weak context-only).
    This prevents stale VFS paths from flooding results when the user changes topic.
    """
    score = 0

    # Tag matching: +2 per query token that appears in entry tags
    entry_tags = set(entry.get("tags", []))
    tag_hits = len(query_tokens & entry_tags)
    score += tag_hits * _TAG_WEIGHT

    # Path matching: strong bonus only when tags also match
    path_matched = False
    for ep in entry.get("_expanded_paths", []):
        # Strip glob characters for prefix matching
        prefix = ep.split("*")[0].rstrip("/")
        if not prefix:
            continue
        for vp in vfs_paths:
            if vp.startswith(prefix):
                path_matched = True
                break
        if path_matched:
            break

    if path_matched:
        score += _PATH_WEIGHT if tag_hits > 0 else 1

    return score


# ─── formatter ──────────────────────────────────────────────────────────────

_SAFETY_ICONS = {"safe": "✅", "caution": "⚠️", "dangerous": "🚫"}
_CONFIDENCE_TAG = {"high": "✓docs", "medium": "~observed", "low": "?inferred"}


def _format_entry(entry: dict) -> str:
    """Format a single guidebook entry for LLM injection.

    NOTE: Paths are deliberately OMITTED. The model must discover real paths
    via search_system(). Injecting hardcoded paths causes Gemma4 to blindly
    navigate nonexistent directories for apps that aren't installed.
    """
    icon = _SAFETY_ICONS.get(entry.get("safety", ""), "")
    importance = entry.get("importance", 0)
    stars = "★" * importance + "☆" * (5 - importance)
    conf = _CONFIDENCE_TAG.get(entry.get("confidence", ""), "")
    etype = entry.get("entry_type", "")

    lines = [
        f"**{entry['id']}** [{entry['category']}/{etype}] "
        f"— {icon} {entry['safety'].upper()} — {stars} ({conf})",
        f"  Size: {entry.get('typical_size', 'unknown')}",
        f"  What: {entry.get('what', '').strip()}",
        f"  Delete OK? {entry.get('delete_ok', '').strip()}",
    ]
    if entry.get("requires_app_closed"):
        lines.append("  ⚡ Close the app before deleting.")
    reclaimable = entry.get("reclaimable")
    if reclaimable:
        lines.append(f"  Reclaimable subdirs: {', '.join(reclaimable)}")
    preserve = entry.get("preserve")
    if preserve:
        lines.append(f"  DO NOT DELETE: {', '.join(preserve)}")
    return "\n".join(lines)


# ─── public API ─────────────────────────────────────────────────────────────

def retrieve_guidebook(
    user_query: str,
    vfs_paths: Optional[list[str]] = None,
    max_entries: int = 5,
    max_chars: int = 3200,
) -> str:
    """Retrieve relevant macOS filesystem knowledge for the current context.

    Args:
        user_query: The user's latest message text.
        vfs_paths:  List of absolute paths currently in the SessionBook VFS.
        max_entries: Maximum entries to return (default 5).
        max_chars:  Hard character cap (~800 tokens at 4 chars/token).

    Returns:
        Formatted guidebook text ready for <macos_knowledge> injection,
        or empty string if nothing relevant found.
    """
    entries = _load_guidebook()
    tokens = _tokenize(user_query)
    paths = vfs_paths or []

    # Score all entries
    scored = []
    for entry in entries:
        s = _score_entry(entry, tokens, paths)
        if s >= _MIN_SCORE:
            scored.append((s, entry))

    if not scored:
        return ""

    # Sort by score descending, then by importance descending
    scored.sort(key=lambda x: (x[0], x[1].get("importance", 0)), reverse=True)

    # Build output within budget
    parts = []
    total_chars = 0
    for _, entry in scored[:max_entries]:
        block = _format_entry(entry)
        if total_chars + len(block) > max_chars:
            break
        parts.append(block)
        total_chars += len(block)

    return "\n\n".join(parts)
