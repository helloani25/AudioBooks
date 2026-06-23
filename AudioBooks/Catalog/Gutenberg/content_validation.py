from __future__ import annotations

import re


# Project Gutenberg headers are not fully uniform across legacy texts, so keep
# a small ordered list of canonical marker patterns.
_GUTENBERG_ID_PATTERNS = (
    re.compile(r"\[\s*ebook\s*#\s*(\d+)\s*\]", re.IGNORECASE),
    re.compile(r"\bebook\s*#\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\betext\s*#\s*(\d+)\b", re.IGNORECASE),
)


def extract_ebook_gutenberg_id(text: str | None, *, scan_chars: int = 30000) -> int | None:
    """Extract the canonical Project Gutenberg eBook id from payload text."""
    if not text:
        return None
    snippet = text[: max(1, scan_chars)]
    for pattern in _GUTENBERG_ID_PATTERNS:
        match = pattern.search(snippet)
        if match:
            return int(match.group(1))
    return None


def detect_gutenberg_id_mismatch(
    text: str | None,
    expected_gutenberg_id: int | None,
    *,
    scan_chars: int = 30000,
) -> tuple[bool, int | None]:
    """
    Return (is_mismatch, detected_id).

    A missing marker is not treated as mismatch; only explicit conflicting IDs
    are rejected.
    """
    if expected_gutenberg_id is None:
        return False, None
    detected_id = extract_ebook_gutenberg_id(text, scan_chars=scan_chars)
    if detected_id is None:
        return False, None
    return detected_id != int(expected_gutenberg_id), detected_id
