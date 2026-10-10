FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite:////data/habitor.db \
    UPLOAD_DIR=/data/uploads \
    SECRET_KEY_FILE=/data/.habitor_secret \
    PORT=8000 \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt "psycopg[binary]>=3.2"

# Copy only the application — never local databases, uploads, secret key files or virtualenvs.
COPY main.py game.py ./
COPY templates ./templates
COPY static ./static
RUN useradd --create-home --uid 10001 habitor \
    && mkdir -p /data \
    && chown -R habitor:habitor /data /app
USER habitor

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health')" || exit 1

# Two workers; the scheduler elects a single leader through the database.
# X-Forwarded-For is trusted only from FORWARDED_ALLOW_IPS — set it to your proxy/load balancer address(es),
# otherwise clients could fake their IP and dodge rate limits.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 2 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS}\""]
