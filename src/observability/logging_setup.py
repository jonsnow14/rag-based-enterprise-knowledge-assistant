"""Structured-ish logging setup for the API and CLI."""

from __future__ import annotations

import logging
import os
import sys

# Noisy SDK loggers — default to WARNING unless overridden
_QUIET_LOGGERS = (
    "azure",
    "azure.core",
    "azure.core.pipeline",
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "httpx",
    "httpcore",
    "urllib3",
    "openai",
)


def _parse_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    if not level:
        return logging.INFO
    name = str(level).strip().upper()
    return getattr(logging, name, logging.INFO)


def setup_logging(level: str | int | None = None) -> None:
    """
    Configure root logging.

    - LOG_LEVEL (or argument): root app log level (default INFO)
    - AZURE_LOG_LEVEL: level for Azure / HTTP SDK loggers (default WARNING)

    Note: Azure SDK does not read AZURE_LOG_LEVEL by itself; we apply it here.
    """
    root_level = _parse_level(level or os.getenv("LOG_LEVEL", "INFO"))
    azure_level = _parse_level(os.getenv("AZURE_LOG_LEVEL", "WARNING"))

    root = logging.getLogger()
    root.setLevel(root_level)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.setLevel(root_level)
        root.addHandler(handler)
    else:
        for h in root.handlers:
            h.setLevel(root_level)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(azure_level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
