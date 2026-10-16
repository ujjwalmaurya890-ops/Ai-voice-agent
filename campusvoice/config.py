"""Central configuration. No secrets live in this file."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

APP_NAME = "CampusVoice"

# Model IDs checked against https://ai.google.dev/gemini-api/docs/models
# and the per-model pages (both last updated 2026-09-15, re-fetched 2026-10-16).
# Re-check that page if a connect call fails with "model not found", because
# Live model names change faster than SDKs.
MODEL_DEFAULT = "gemini-3.8-live"  # default low-latency voice model
MODEL_EXTENDED = "gemini-3.8-live-extended-thinking"  # higher-reasoning, async tools only
MODEL_LEGACY = "gemini-3.1-flash-live-preview"  # legacy preview, avoid


@dataclass(frozen=True)
class ModelCapabilities:
    """What a Live model is documented to support.

    Source: https://ai.google.dev/gemini-api/docs/models/<model-id> capability
    tables. `async_tools_only` reflects that gemini-3.8-live-extended-thinking
    only supports NON_BLOCKING function calling; synchronous (BLOCKING)
    declarations are a hard error on that model.
    """

    audio_in: bool = True
    audio_out: bool = True
    tool_calling: bool = True
    async_tools_only: bool = False
    thinking: bool = False


MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    MODEL_DEFAULT: ModelCapabilities(),
    MODEL_EXTENDED: ModelCapabilities(async_tools_only=True, thinking=True),
    MODEL_LEGACY: ModelCapabilities(),
}

THINKING_LEVELS = ("low", "medium", "high")  # "minimal" is documented as unsupported


def capabilities_for(model: str) -> ModelCapabilities:
    """Capabilities for a model id, defaulting to the safest (most restrictive)
    assumption for unknown model ids so we never silently rely on an
    unsupported feature."""
    known = MODEL_CAPABILITIES.get(model)
    if known is not None:
        return known
    log.warning(
        "Model '%s' is not in CampusVoice's known capability table; "
        "assuming synchronous tools and no thinking support.",
        model,
    )
    return ModelCapabilities(thinking=False, async_tools_only=False)


def resolve_model(requested: str) -> tuple[str, Optional[str]]:
    """Validate a requested model id.

    Returns (model_to_use, warning_or_None). Unknown model ids are not
    rejected outright (Google may ship a new id before this table is
    updated) but we warn clearly instead of pretending the capability
    table covers them.
    """
    if requested in MODEL_CAPABILITIES:
        return requested, None
    warning = (
        f"Model '{requested}' is not a model CampusVoice recognizes "
        f"(known: {', '.join(MODEL_CAPABILITIES)}). It may be a newer or "
        "renamed model: connecting anyway, but thinking mode and async tool "
        "calling will be disabled unless the model supports the defaults."
    )
    return requested, warning


def resolve_thinking_level(requested: str) -> tuple[str, Optional[str]]:
    level = (requested or "low").strip().lower()
    if level in THINKING_LEVELS:
        return level, None
    return "low", (
        f"CAMPUSVOICE_THINKING_LEVEL='{requested}' is not one of "
        f"{THINKING_LEVELS}; using 'low'."
    )


MODEL_ID, MODEL_ID_WARNING = resolve_model(os.environ.get("CAMPUSVOICE_MODEL", MODEL_DEFAULT))
THINKING_LEVEL, THINKING_LEVEL_WARNING = resolve_thinking_level(
    os.environ.get("CAMPUSVOICE_THINKING_LEVEL", "low")
)
VOICE_NAME = os.environ.get("CAMPUSVOICE_VOICE", "Kore")

# Audio formats required by the Live API: 16 kHz in, 24 kHz out, 16-bit mono PCM.
INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
MIC_BLOCK_FRAMES = 800  # 50 ms of microphone audio per chunk
SPEAKER_BLOCK_FRAMES = 1024  # about 43 ms, keeps barge-in latency low

# Server-side voice activity detection. The docs recommend 500-800 ms of
# silence so natural pauses are not split into fragments.
VAD_SILENCE_MS = 600
VAD_PREFIX_PADDING_MS = 100

# Set CAMPUSVOICE_HALF_DUPLEX=1 to stop sending mic audio while the model is
# speaking. This prevents speaker echo from self-interrupting, but it also
# disables barge-in. Prefer headphones and leave this off.
HALF_DUPLEX = os.environ.get("CAMPUSVOICE_HALF_DUPLEX", "0") == "1"

SYSTEM_PROMPT = """\
You are CampusVoice, a voice study partner for a college student.

Speak naturally and briefly: one to three sentences per turn unless the student \
asks you to go deeper. You are speaking aloud, so never read out markdown, \
symbols, or long lists.

Priorities:
1. Explain concepts clearly with one concrete example, then check understanding \
with one short question.
2. When asked, quiz the student one question at a time, wait for the answer, \
then give feedback.
3. Help plan study sessions in small steps (for example 25 minutes of focus, \
5 minutes of break) and keep track of deadlines.

Tools: use add_task for deadlines and to-dos, list_tasks and complete_task to \
manage them, save_note for short summaries or flashcard-style facts the student \
wants kept, search_notes to recall them, and get_current_datetime to resolve \
words like "tomorrow" or "Friday". Confirm out loud after you change the task \
list. Never invent tasks or notes; read them from the tools.

Be honest about uncertainty. Do not make up citations, formulas, or details \
about the student's syllabus or professor. If the student wants graded work \
done for them, help them learn it instead: outlines, hints, and feedback on \
their own drafts.

If the student interrupts, stop and listen. Reply in the language the student \
speaks.
"""


def data_dir() -> Path:
    """Folder for tasks, notes and logs. Windows: %APPDATA%\\CampusVoice."""
    override = os.environ.get("CAMPUSVOICE_DATA_DIR")
    if override:
        path = Path(override)
    elif os.environ.get("APPDATA"):
        path = Path(os.environ["APPDATA"]) / APP_NAME
    else:
        path = Path.home() / ".campusvoice"
    path.mkdir(parents=True, exist_ok=True)
    return path
