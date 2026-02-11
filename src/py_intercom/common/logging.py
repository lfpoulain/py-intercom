from __future__ import annotations

import sys

from loguru import logger


def setup_logging(debug: bool) -> None:
    try:
        logger.remove()
    except Exception:
        pass

    if debug:
        logger.add(sys.stderr, level="DEBUG")
