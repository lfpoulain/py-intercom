from __future__ import annotations

import sys

from loguru import logger


def setup_logging(debug: bool) -> None:
    try:
        logger.remove()
    except Exception:
        pass

    sink = sys.stderr if getattr(sys, "stderr", None) is not None else None
    if sink is None:
        sink = sys.stdout if getattr(sys, "stdout", None) is not None else None
    if sink is None:
        return

    if debug:
        logger.add(sink, level="DEBUG")
    else:
        logger.add(sink, level="WARNING")
