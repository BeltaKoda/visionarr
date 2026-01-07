# Visionarr Dockerfile
# Dolby Vision Profile 7 to Profile 8 Converter

FROM python:3.12-slim-bookworm

# Labels
LABEL maintainer="BeltaKoda"
LABEL org.opencontainers.image.title="Visionarr"
LABEL org.opencontainers.image.description="Dolby Vision Profile 7 to Profile 8 Converter"
LABEL org.opencontainers.image.source="https://github.com/BeltaKoda/visionarr"

# Environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mkvtoolnix \
    mediainfo \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install dovi_tool
ARG DOVI_TOOL_VERSION=2.1.2
RUN ARCH=$(dpkg --print-architecture) && \
    case "$ARCH" in \
        amd64) DOVI_ARCH="x86_64-unknown-linux-musl" ;; \
        arm64) DOVI_ARCH="aarch64-unknown-linux-musl" ;; \
        *) echo "Unsupported architecture: $ARCH" && exit 1 ;; \
    esac && \
    wget -q "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-${DOVI_ARCH}.tar.gz" \
        -O /tmp/dovi_tool.tar.gz && \
    tar -xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin && \
    chmod +x /usr/local/bin/dovi_tool && \
    rm /tmp/dovi_tool.tar.gz && \
    dovi_tool --version

# Create non-root user
RUN useradd -m -s /bin/bash visionarr

# Create mount point directories
# These MUST be mounted to external volumes on Unraid!
RUN mkdir -p /config /temp /media && \
    chown -R visionarr:visionarr /config /temp

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/

# Create bashrc with welcome message for console users
RUN echo '#!/bin/bash' > /home/visionarr/.bashrc && \
    echo 'echo ""' >> /home/visionarr/.bashrc && \
    echo 'echo " __      ___     _                            "' >> /home/visionarr/.bashrc && \
    echo 'echo " \\ \\    / (_)   (_)                           "' >> /home/visionarr/.bashrc && \
    echo 'echo "  \\ \\  / / _ ___ _  ___  _ __   __ _ _ __ _ __ "' >> /home/visionarr/.bashrc && \
    echo 'echo "   \\ \\/ / | / __| |/ _ \\| ._  \\/ _. | .__| .__|"' >> /home/visionarr/.bashrc && \
    echo 'echo "    \\  /  | \\__ \\ | (_) | | | | (_| |  |  |   "' >> /home/visionarr/.bashrc && \
    echo 'echo "     \\/   |_|___/_|\\___/|_| |_|\\__,_|_|  |_|   "' >> /home/visionarr/.bashrc && \
    echo 'echo ""' >> /home/visionarr/.bashrc && \
    echo 'echo "  Dolby Vision Profile Converter"' >> /home/visionarr/.bashrc && \
    echo 'echo "  by BeltaKoda | github.com/BeltaKoda/visionarr"' >> /home/visionarr/.bashrc && \
    echo 'echo ""' >> /home/visionarr/.bashrc && \
    echo 'echo "  Type: menu   - Launch interactive menu"' >> /home/visionarr/.bashrc && \
    echo 'echo ""' >> /home/visionarr/.bashrc && \
    echo 'alias menu="python -m src.main --manual"' >> /home/visionarr/.bashrc

# Set ownership
RUN chown -R visionarr:visionarr /app /home/visionarr

# Run as root for Unraid compatibility (media files often owned by different users)
# USER visionarr

# Default environment variables
ENV RADARR_URL=""
ENV RADARR_API_KEY=""
ENV SONARR_URL=""
ENV SONARR_API_KEY=""
ENV DRY_RUN="true"

ENV POLL_INTERVAL_SECONDS="300"
ENV LOOKBACK_MINUTES="60"
ENV PROCESS_CONCURRENCY="1"
ENV MIN_FREE_SPACE_GB="50"
ENV CONFIG_DIR="/config"
ENV TEMP_DIR="/temp"
ENV MEDIA_DIR="/media"
ENV BACKUP_ENABLED="true"
ENV BACKUP_RETENTION_DAYS="7"
ENV LOG_LEVEL="INFO"

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
