import argparse

from loguru import logger

from ..common.logging import setup_logging
from .app import create_app


def main() -> int:
    parser = argparse.ArgumentParser(prog="py-intercom-web")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--ssl-adhoc", action="store_true")
    parser.add_argument("--ssl-cert", default=None)
    parser.add_argument("--ssl-key", default=None)

    args = parser.parse_args()

    setup_logging(bool(args.debug))

    app, socketio = create_app()

    if args.debug:
        logger.info("starting web client server on {}:{}", args.host, args.port)

    ssl_context = None
    if args.ssl_cert or args.ssl_key:
        if not args.ssl_cert or not args.ssl_key:
            raise SystemExit("--ssl-cert et --ssl-key doivent être fournis ensemble")
        ssl_context = (str(args.ssl_cert), str(args.ssl_key))
    elif bool(args.ssl_adhoc):
        ssl_context = "adhoc"

    socketio.run(
        app,
        host=args.host,
        port=int(args.port),
        debug=bool(args.debug),
        allow_unsafe_werkzeug=True,
        ssl_context=ssl_context,
    )
    return 0
