# Build an optimized, secure container for Grizzly MyJio automation
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PID_FILE=/app/data/.grizzly_myjio.pid

# Install system certificates and create unprivileged application user
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r -g 1001 appuser && \
    useradd -r -u 1001 -g appuser -d /app -s /sbin/nologin appuser

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code and web build
COPY grizzly_myjio.py .
COPY server/ ./server/
COPY web/out/ ./web/out/

# Prepare runtime directory for pid lock and persistence
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

EXPOSE 8000

# Default run with Web Dashboard enabled
CMD ["python", "grizzly_myjio.py", "--web", "--port", "8000"]
