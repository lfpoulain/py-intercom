#!/bin/sh
set -eu

REPO_URL="${GITEA_REPO_URL:-http://10.0.0.4:3000/lfpoulain/py-intercom.git}"
APP_DIR="${APP_DIR:-/opt/py-intercom}"
WEB_PORT="${WEB_PORT:-8443}"

mkdir -p "${APP_DIR}"

if [ ! -d "${APP_DIR}/.git" ]; then
  echo "[web-entrypoint] clone ${REPO_URL} -> ${APP_DIR}"
  git clone "${REPO_URL}" "${APP_DIR}"
else
  echo "[web-entrypoint] pull ${REPO_URL}"
  git -C "${APP_DIR}" remote set-url origin "${REPO_URL}" || true
  git -C "${APP_DIR}" pull --ff-only
fi

if [ -f "${APP_DIR}/docker/web/requirements.txt" ]; then
  pip install -r "${APP_DIR}/docker/web/requirements.txt"
fi

export PYTHONPATH="${APP_DIR}/src"

if [ "$#" -eq 0 ]; then
  exec python -m py_intercom.web.main --host 0.0.0.0 --port "${WEB_PORT}" --ssl-adhoc
fi

exec "$@"
