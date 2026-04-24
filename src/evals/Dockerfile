FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/workspace/.venv/bin:$PATH"

WORKDIR /workspace

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY data ./data

RUN uv sync --frozen --no-dev

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /workspace/data/sqlite \
    && chown -R appuser:appuser /workspace

USER appuser

CMD ["uv", "run", "--no-sync", "python", "-m", "evals.main"]
