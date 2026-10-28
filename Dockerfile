FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY nwp_analysis_archive ./nwp_analysis_archive
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    NWP_ARCHIVE_WATCH=true

ENTRYPOINT ["python", "-m", "nwp_analysis_archive"]
