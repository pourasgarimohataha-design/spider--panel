# Spider Gateway - Dockerfile for Railway Deployment
# Multi-stage build for minimal image size

# Build stage
FROM python:3.11-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --user -r requirements.txt

# Production stage
FROM python:3.11-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set working directory
WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Create Xray Core directory with proper permissions
RUN mkdir -p /app/xray-core && chown -R appuser:appuser /app/xray-core

# Copy application code
COPY --chown=appuser:appuser . .

# Switch to non-root user
USER appuser

# Add user site-packages to PATH
ENV PATH="/home/appuser/.local/bin:${PATH}"

# Xray Core installation script (runs at build time)
# This ensures Xray is available in the image
RUN mkdir -p /app/xray-core \
    && wget -q -O /tmp/Xray-linux-64.zip "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip" \
    && unzip -q -q /tmp/Xray-linux-64.zip -d /tmp/xray-extracted \
    && mv /tmp/xray-extracted/xray /app/xray-core/xray \
    && chmod +x /app/xray-core/xray \
    && chown appuser:appuser /app/xray-core/xray \
    && /app/xray-core/xray version \
    && rm -rf /tmp/Xray-linux-64.zip /tmp/xray-extracted

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XRAY_BINARY_PATH=/app/xray-core/xray \
    XRAY_CONFIG_PATH=/app/xray-config/config.json \
    XRAY_ASSETS_DIR=/app/xray-assets \
    XRAY_LOG_DIR=/app/xray-logs \
    RAILWAY_PUBLIC_DOMAIN=${RAILWAY_PUBLIC_DOMAIN:-localhost}

# Create directories for Xray config and assets
RUN mkdir -p /app/xray-config /app/xray-assets /app/xray-logs && chown -R appuser:appuser /app/xray-config /app/xray-assets /app/xray-logs

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=5)" || exit 1

# Start command
CMD ["python", "main.py"]