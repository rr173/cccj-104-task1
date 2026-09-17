# ---------- 运行时镜像 ----------
FROM python:3.11-slim AS runtime
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV OTA_DATA_DIR=/data \
    OTA_CHUNK_SIZE=262144 \
    OTA_FAIL_THRESHOLD=0.05 \
    OTA_MIN_SAMPLES=20
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

# ---------- 测试镜像：docker compose run --rm test ----------
FROM runtime AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
CMD ["python", "-m", "pytest", "tests/", "-q"]
