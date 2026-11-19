FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY scripts ./scripts

RUN pip install --upgrade pip && pip install --no-cache-dir . \
    && groupadd --system appgroup \
    && useradd --system --gid appgroup --create-home appuser \
    && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8102

CMD ["sh", "-c", "alembic upgrade head && if [ \"${SEED_DEMO_ON_BOOT:-false}\" = \"true\" ]; then python -m scripts.seed_demo; fi && uvicorn slotguard.main:app --host 0.0.0.0 --port ${PORT:-8102}"]
