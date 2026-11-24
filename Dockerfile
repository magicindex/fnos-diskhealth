FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends smartmontools tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY bin/perccli64 /usr/local/bin/perccli64
RUN chmod 0755 /usr/local/bin/perccli64

WORKDIR /app
COPY app/ /app/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)" || exit 1

CMD ["python3", "/app/server.py"]
