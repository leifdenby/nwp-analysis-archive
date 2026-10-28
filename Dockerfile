FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.16 /uv /usr/local/bin/uv

WORKDIR /app
# there is no .git in the image; the version comes from the git tag
ARG VERSION=0.0.0+unknown
ENV SETUPTOOLS_SCM_PRETEND_VERSION=$VERSION
COPY pyproject.toml uv.lock README.md ./
COPY nwp_analysis_archive ./nwp_analysis_archive
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    NWP_ARCHIVE_WATCH=true \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "nwp_analysis_archive"]
