# Hardened sandbox for lint / tests / data tools. Nothing runs on the host.
# Build:  make sandbox      Run: see SANDBOX in the Makefile (read-only, no caps, no network).
# Pinned by digest (python:3.11-slim-bookworm, image created 2026-11-05).
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39

# ---- uvbase: resolver/builder tooling (git is needed for the pinned thrember git dependency)
FROM ${PYTHON_IMAGE} AS uvbase
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.13.6
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/uv-cache
WORKDIR /build

# ---- builder: install locked dependencies into /opt/venv (project code is mounted at runtime)
FROM uvbase AS builder
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --all-groups --no-install-project && rm -rf /tmp/uv-cache

# ---- runtime: minimal, non-root, no git / pip / uv
FROM ${PYTHON_IMAGE} AS runtime
# libgomp1: OpenMP runtime required by LightGBM (imported by thrember).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true \
 && groupadd --system --gid 10001 sentinel \
 && useradd --system --uid 10001 --gid sentinel --no-create-home --shell /usr/sbin/nologin sentinel
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONPATH=/work/src:/work \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    MYPY_CACHE_DIR=/tmp/.mypy_cache \
    RUFF_CACHE_DIR=/tmp/.ruff_cache \
    COVERAGE_FILE=/tmp/.coverage \
    HF_HOME=/tmp/hf \
    SENTINEL_DATA_DIR=/data
USER 10001:10001
WORKDIR /work
