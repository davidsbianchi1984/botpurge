# The hosted Bot Purge: website, phone app, store (Stripe) and Purge Console.
# The desktop app is built separately (packaging/); this image is the server only.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BOTPURGE_DB=/data/botpurge.sqlite3 BOTPURGE_KEYFILE=/data/botpurge.key \
    FORWARDED_ALLOW_IPS="*"
WORKDIR /app
COPY pyproject.toml README.md ./
COPY botpurge ./botpurge
RUN pip install --no-cache-dir ".[vision]" && useradd --create-home botpurge && mkdir /data && chown botpurge /data
USER botpurge
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["python", "-m", "botpurge", "--host", "0.0.0.0", "--port", "8000"]
