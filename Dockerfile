# Backend group image: one FastAPI process per container; run several
# containers (API_REPLICAS) instead of several processes in one. The same
# image runs the one-off "migrate" job (alembic upgrade head).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 app && mkdir -p /data/uploads && chown app /data/uploads
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
COPY web ./web
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status == 200 else 1)"
# Only Caddy can reach these containers (their own network), so the
# X-Forwarded-For it sets is the only one that arrives.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "5", "--no-server-header"]
