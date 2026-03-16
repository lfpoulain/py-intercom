#!/bin/sh
set -eu

HOST="${PY_INTERCOM_WEB_HOST:-0.0.0.0}"
PORT="${PY_INTERCOM_WEB_PORT:-8741}"
DEBUG="${PY_INTERCOM_WEB_DEBUG:-0}"
SSL_MODE="${PY_INTERCOM_WEB_SSL_MODE:-plain}"

set -- python run_web.py --host "$HOST" --port "$PORT"

if [ "$DEBUG" = "1" ]; then
  set -- "$@" --debug
fi

case "$SSL_MODE" in
  plain)
    ;;
  adhoc)
    set -- "$@" --ssl-adhoc
    ;;
  *)
    echo "Unsupported PY_INTERCOM_WEB_SSL_MODE: $SSL_MODE" >&2
    exit 1
    ;;
esac

exec "$@"
