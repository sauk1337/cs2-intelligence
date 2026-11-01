# CS2 Intelligence Python backend
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    API_HOST=0.0.0.0

RUN useradd --create-home --uid 1000 cs2

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
ARG WITH_POSTGRES=false
ARG WITH_REDIS=false
# requirements-dev includes pytest so the tests can be run inside the container
RUN pip install -r requirements-dev.txt \
    && if [ "$WITH_POSTGRES" = "true" ]; then pip install "asyncpg>=0.29"; fi \
    && if [ "$WITH_REDIS" = "true" ]; then pip install "redis>=5.0"; fi

COPY backend ./backend
COPY config ./config
COPY tests ./tests
COPY eggdrop/scripts ./eggdrop/scripts
COPY pytest.ini ./
RUN mkdir -p data logs backups && chown -R cs2:cs2 /app
USER cs2

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "backend.main", "serve"]
