# Nebula 3 is the default container target. The PyQt/X11 maintenance image is
# intentionally not shipped as a release container.
FROM python:3.12-slim-bookworm@sha256:8a7e7cc04fd3e2bd787f7f24e22d5d119aa590d429b50c95dfe12b3abe52f48b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    NEBULA_V3_DATA_DIR=/data \
    LANGGRAPH_STRICT_MSGPACK=true

RUN groupadd --gid 10001 nebula \
    && useradd --uid 10001 --gid nebula --home-dir /home/nebula --create-home nebula

WORKDIR /app

RUN python -m pip install --no-cache-dir \
    "fastapi==0.135.3" \
    "uvicorn[standard]==0.34.0" \
    "pydantic==2.9.2" \
    "sqlalchemy==2.0.37" \
    "psycopg[binary]==3.3.4" \
    "alembic==1.18.5" \
    "boto3==1.36.11" \
    "httpx==0.28.1" \
    "typer==0.26.8" \
    "filelock==3.20.3" \
    "langgraph==1.1.6" \
    "langgraph-checkpoint-sqlite==3.1.0" \
    "jsonschema==4.26.0" \
    "semantic-version==2.10.0"

COPY --chown=nebula:nebula src /app/src
RUN mkdir -p /data && chown nebula:nebula /data

USER nebula
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"

ENTRYPOINT ["python", "-m", "nebula.v3.cli"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000", "--allow-remote"]
