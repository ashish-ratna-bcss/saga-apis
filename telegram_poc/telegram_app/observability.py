"""Shared structured-logging helper.

`monitoring_service.py` already has its own tiny private version of this for
collection cycles; this shared copy is for the new search/discovery/backfill
services so they don't each hand-roll the same three lines. Never log
session data, api_hash, verification codes, or passwords - callers must not
pass those as fields.
"""
from __future__ import annotations

import json
import logging


def log_structured(logger: logging.Logger, event: str, **fields) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str))
