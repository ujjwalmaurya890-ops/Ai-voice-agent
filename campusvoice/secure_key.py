"""API key handling.

Lookup order: environment variable, then Windows Credential Manager (through
the `keyring` package). The key is never written to a file in the project,
never logged, and scrubbed from any error text shown in the UI.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

SERVICE_NAME = "CampusVoice"
USERNAME = "gemini-api-key"
ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

_URL_KEY = re.compile(r"(key=)[^&\s'\"]+", re.IGNORECASE)
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


class SecureStorageError(RuntimeError):
    """The OS credential store is unavailable or refused the request."""


def _keyring() -> Any:
    import keyring  # imported lazily so tests can replace this function

    return keyring


def validate_key_format(key: str) -> str:
    cleaned = (key or "").strip()
    if not cleaned:
        raise ValueError("The API key is empty.")
    if any(ch.isspace() for ch in cleaned):
        raise ValueError("The API key must not contain spaces or line breaks.")
    if len(cleaned) < 20:
        raise ValueError("That looks too short to be a Gemini API key.")
    return cleaned


def get_api_key() -> tuple[Optional[str], str]:
    """Return (key, source). source is 'env', 'credential-manager' or 'none'."""
    for name in ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value, "env"
    try:
        stored = _keyring().get_password(SERVICE_NAME, USERNAME)
    except Exception:  # no usable backend; treat as "no stored key"
        logging.getLogger(__name__).warning("Credential store unavailable.")
        stored = None
    if stored:
        return stored, "credential-manager"
    return None, "none"


def store_api_key(key: str) -> None:
    cleaned = validate_key_format(key)
    try:
        _keyring().set_password(SERVICE_NAME, USERNAME, cleaned)
    except Exception as exc:
        raise SecureStorageError(
            "Could not save the key to the Windows Credential Manager. "
            "You can set the GEMINI_API_KEY environment variable instead."
        ) from exc


def delete_api_key() -> None:
    try:
        _keyring().delete_password(SERVICE_NAME, USERNAME)
    except Exception:
        pass  # nothing stored, or backend unavailable


def redact(text: object, key: Optional[str] = None) -> str:
    """Remove anything that looks like an API key from text."""
    out = str(text)
    if key:
        out = out.replace(key, "***")
    out = _URL_KEY.sub(r"\1***", out)
    return _GOOGLE_KEY.sub("***", out)


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs key-like strings from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True
