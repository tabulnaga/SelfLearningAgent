FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    FASTEMBED_CACHE_PATH=/models

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY workers ./workers
COPY scripts ./scripts
COPY data ./data
RUN uv sync --frozen --no-dev

RUN useradd -m agent && mkdir -p /models && chown agent /models
USER agent
ENV PATH="/srv/.venv/bin:${PATH}"

EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
