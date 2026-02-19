FROM python:3.11-slim

ARG REPO_URL=https://git.diy.yt/lfpoulain/py-intercom
ARG APP_DIR=/opt/py-intercom

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        libopus0 \
    && rm -rf /var/lib/apt/lists/*

RUN git clone ${REPO_URL} ${APP_DIR}

WORKDIR ${APP_DIR}

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

EXPOSE 8443

CMD ["python", "run_web.py", "--host", "0.0.0.0", "--port", "8443", "--ssl-adhoc"]
