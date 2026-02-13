FROM python:3.12-slim

# Install libopus (required by opuslib) and git
RUN apt-get update && \
    apt-get install -y --no-install-recommends libopus0 git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone the repository
ARG REPO_URL=http://10.0.0.4:3000/lfpoulain/py-intercom.git
ARG BRANCH=main
RUN git clone --depth 1 --branch ${BRANCH} ${REPO_URL} .

# Install only the Python dependencies needed for the web module
RUN pip install --no-cache-dir -r requirements-web.txt

EXPOSE 8443

ENTRYPOINT ["python", "run_web.py"]
