# ── build stage: resolve and install dependencies into a venv ────────────────
FROM python:3.12-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml ./
# install deps only first so the layer caches across source changes
RUN pip install --upgrade pip setuptools wheel \
 && python - <<'EOF'
import tomllib, subprocess, sys
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
subprocess.check_call([sys.executable, "-m", "pip", "install", *deps])
EOF
COPY . .
RUN pip install --no-deps .

# ── runtime stage: slim, non-root, no build tooling ──────────────────────────
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"
RUN groupadd --system lens && useradd --system --gid lens --home /app --shell /usr/sbin/nologin lens \
 && mkdir -p /app /app/scratch && chown -R lens:lens /app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=lens:lens . .
USER lens
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
