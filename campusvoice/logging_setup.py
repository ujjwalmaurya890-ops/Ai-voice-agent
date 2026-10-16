from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import config
from .secure_key import RedactingFilter


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_campusvoice", False) for h in root.handlers):
        return
    handler = RotatingFileHandler(
        config.data_dir() / "campusvoice.log",
        maxBytes=512_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler._campusvoice = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)
