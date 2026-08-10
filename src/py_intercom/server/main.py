import argparse

from ..common.logging import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(prog="py-intercom-server")
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    setup_logging(bool(args.debug), component="server")

    from .gui import run_gui

    return run_gui(minimized=bool(args.minimized))
