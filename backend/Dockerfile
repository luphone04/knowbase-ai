# One image for the api, worker, and migrate services.
FROM python:3.12-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTEST_ADDOPTS="-p no:cacheprovider"

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer. The dev group
# is included so `docker compose exec api pytest` works in this local stack.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked

COPY . .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data/uploads \
    && chown -R app:app /app/data
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
