FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim AS runtime

RUN groupadd --gid 10001 codesight \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash codesight

WORKDIR /app

COPY --from=builder --chown=codesight:codesight /app/.venv /app/.venv
COPY --chown=codesight:codesight alembic.ini ./alembic.ini
COPY --chown=codesight:codesight alembic ./alembic

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

USER codesight

EXPOSE 8000

CMD ["uvicorn", "codesight.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
