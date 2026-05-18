FROM python:3.12-slim

# System deps for cryptography + plyer notifications (best-effort on Linux)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer-cached unless pyproject.toml changes)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir -e .

# Copy application source
COPY src/ ./src/
COPY config/ ./config/
COPY .claude/ ./.claude/

# Runtime directories (will be overridden by volume mounts in compose)
RUN mkdir -p data logs metrics reports

# Non-root user for security
RUN useradd -m -u 1000 kalshi && chown -R kalshi:kalshi /app
USER kalshi

EXPOSE 8000

CMD ["uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
