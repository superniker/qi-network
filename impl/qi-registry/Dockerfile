# Qi Registry Dockerfile
# 部署: docker build -t qi-registry . && docker run -p 7881:7881 -v $(pwd)/data:/data qi-registry

FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy qi-core first (dependency)
COPY impl/qi-core /app/qi-core
RUN pip install --no-cache-dir /app/qi-core

# Copy registry
COPY impl/qi-registry /app/qi-registry
RUN pip install --no-cache-dir /app/qi-registry

# Data directory
RUN mkdir -p /data
ENV QI_REGISTRY_DB=/data/qi_registry.db

EXPOSE 7881

HEALTHCHECK --interval=30s --timeout=3s \
    CMD curl -f http://localhost:7881/health || exit 1

CMD ["python", "-m", "uvicorn", "qi_registry.main:app", "--host", "0.0.0.0", "--port", "7881"]
