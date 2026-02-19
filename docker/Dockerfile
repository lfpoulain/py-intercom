FROM python:3.11-slim

# System dependencies: libopus for opuslib, git to clone the repo
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libopus0 \
    libopus-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG GITEA_URL=http://10.0.0.4:3000/lfpoulain/py-intercom.git
ARG GITEA_BRANCH=main

# Clone the repo
RUN git clone --depth=1 --branch ${GITEA_BRANCH} ${GITEA_URL} .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8443

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
