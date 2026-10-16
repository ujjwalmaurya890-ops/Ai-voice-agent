"""Turns streamed transcription fragments into readable per-speaker lines."""
from __future__ import annotations

from typing import Optional


class TranscriptAssembler:
    """Tracks who is talking so the UI knows when to start a new line."""

    def __init__(self) -> None:
        self._speaker: Optional[str] = None

    def add_fragment(self, speaker: str, text: str) -> tuple[bool, str]:
        """Return (starts_new_line, text_to_append)."""
        new_line = speaker != self._speaker
        self._speaker = speaker
        # Strip the leading space only when a line begins.
        return new_line, text.lstrip() if new_line else text

    def end_turn(self) -> None:
        self._speaker = None
