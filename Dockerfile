FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libopus0 \
        libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY run_web.py ./

EXPOSE 8443

CMD ["python", "run_web.py", "--host", "0.0.0.0", "--port", "8443", "--ssl-adhoc"]
