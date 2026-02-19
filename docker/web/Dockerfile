FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-web.txt /app/requirements-web.txt
RUN python -m pip install --no-cache-dir -r /app/requirements-web.txt

COPY src /app/src

EXPOSE 8443

CMD ["python", "-m", "py_intercom.web.main", "--host", "0.0.0.0", "--port", "8443", "--ssl-adhoc"]
