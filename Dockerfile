FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    TZ=America/Toronto \
    FERRY_DATA=/data

# libgomp1: OpenMP runtime needed by LightGBM
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --system --uid 10001 --home /app ferry && mkdir -p /data && chown ferry /data && chown -R ferry /app/reports
USER ferry

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
