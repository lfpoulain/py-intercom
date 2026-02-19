#!/bin/sh
set -e

HOST="${WEB_HOST:-0.0.0.0}"
PORT="${WEB_PORT:-8443}"

EXTRA_ARGS=""

if [ -n "$SSL_CERT" ] && [ -n "$SSL_KEY" ]; then
    EXTRA_ARGS="--ssl-cert $SSL_CERT --ssl-key $SSL_KEY"
elif [ "${SSL_ADHOC:-1}" = "1" ]; then
    EXTRA_ARGS="--ssl-adhoc"
fi

exec python run_web.py --host "$HOST" --port "$PORT" $EXTRA_ARGS
