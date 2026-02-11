import argparse

from loguru import logger

from ..common.logging import setup_logging
from .app import create_app


def main() -> int:
    parser = argparse.ArgumentParser(prog="py-intercom-web")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    setup_logging(bool(args.debug))

    app, socketio = create_app()

    if args.debug:
        logger.info("starting web client server on {}:{}", args.host, args.port)

    socketio.run(app, host=args.host, port=int(args.port), debug=bool(args.debug), allow_unsafe_werkzeug=True)
    return 0
