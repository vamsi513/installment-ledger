"""Chunking a reply for the socket. No LLM, no network, database required."""

from __future__ import annotations

import pytest

from assistant.api.routes import _chunk


@pytest.mark.parametrize(
    ("text", "size", "expected"),
    [
        ("one two three four", 2, ["one two", "three four"]),
        ("one two three", 2, ["one two", "three"]),
        ("single", 4, ["single"]),
        ("", 4, [""]),
    ],
)
def test_chunking_splits_on_word_boundaries(text: str, size: int, expected: list[str]) -> None:
    assert _chunk(text, size) == expected


def test_chunks_rejoin_into_the_original_reply() -> None:
    """Whatever the socket streams has to reassemble into exactly the reply."""
    text = "Paid 4500 cents. The outstanding balance is now 75000 cents."
    assert " ".join(_chunk(text, 3)) == text
