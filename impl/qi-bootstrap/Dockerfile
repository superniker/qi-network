# Qi Bootstrap Dockerfile
# 部署: docker build -t qi-bootstrap . && docker run -p 8733:8733 qi-bootstrap
#
# Build context: qi-network/ (repo root)
#   docker build -f impl/qi-bootstrap/Dockerfile -t qi-bootstrap .

FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy qi-core first (dependency)
COPY impl/qi-core /app/qi-core
RUN pip install --no-cache-dir /app/qi-core

# Copy bootstrap
COPY impl/qi-bootstrap /app/qi-bootstrap
RUN pip install --no-cache-dir /app/qi-bootstrap

EXPOSE 8733

ENV QI_BOOTSTRAP_HOST=0.0.0.0
ENV QI_BOOTSTRAP_PORT=8733

HEALTHCHECK --interval=30s --timeout=3s \
    CMD curl -f http://localhost:8733/health || exit 1

CMD ["python", "-m", "qi_bootstrap.main"]
