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

# Install system dependencies including gosu for user switching
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mkvtoolnix \
    mediainfo \
    wget \
    ca-certificates \
    gosu \
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

# Create mount point directories
RUN mkdir -p /config /temp /movies /tv

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and entrypoint
COPY src/ ./src/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create bashrc with welcome message for console users
# Put in multiple locations to work for any shell user
RUN mkdir -p /home/visionarr /etc/skel && \
    echo 'cd /app' > /tmp/bashrc && \
    echo 'echo ""' >> /tmp/bashrc && \
    echo 'echo " __      ___     _                            "' >> /tmp/bashrc && \
    echo 'echo " \\ \\    / (_)   (_)                           "' >> /tmp/bashrc && \
    echo 'echo "  \\ \\  / / _ ___ _  ___  _ __   __ _ _ __ _ __ "' >> /tmp/bashrc && \
    echo 'echo "   \\ \\/ / | / __| |/ _ \\| ._  \\/ _. | .__| .__|"' >> /tmp/bashrc && \
    echo 'echo "    \\  /  | \\__ \\ | (_) | | | | (_| |  |  |   "' >> /tmp/bashrc && \
    echo 'echo "     \\/   |_|___/_|\\___/|_| |_|\\__,_|_|  |_|   "' >> /tmp/bashrc && \
    echo 'echo ""' >> /tmp/bashrc && \
    echo 'echo "  Dolby Vision Profile Converter"' >> /tmp/bashrc && \
    echo 'echo "  by BeltaKoda | github.com/BeltaKoda/visionarr"' >> /tmp/bashrc && \
    echo 'echo ""' >> /tmp/bashrc && \
    echo 'echo "  IMPORTANT First Run Instructions:"' >> /tmp/bashrc && \
    echo 'echo "  1. Type `menu` to launch the interactive menu"' >> /tmp/bashrc && \
    echo 'echo "  2. Run a Test Scan to verify settings (optional)"' >> /tmp/bashrc && \
    echo 'echo "  3. Run Full Library Scan to build the database"' >> /tmp/bashrc && \
    echo 'echo "  4. Complete Setup to enable automatic mode"' >> /tmp/bashrc && \
    echo 'echo ""' >> /tmp/bashrc && \
    echo 'echo "  After setup, Visionarr acts as a daemon and listens"' >> /tmp/bashrc && \
    echo 'echo "  for new imports from Sonarr/Radarr automatically."' >> /tmp/bashrc && \
    echo 'echo ""' >> /tmp/bashrc && \
    echo 'echo "  Type: menu   - Launch interactive menu"' >> /tmp/bashrc && \
    echo 'echo ""' >> /tmp/bashrc && \
    echo 'alias menu="python -m src.main --manual"' >> /tmp/bashrc && \
    cp /tmp/bashrc /root/.bashrc && \
    cp /tmp/bashrc /home/visionarr/.bashrc && \
    cp /tmp/bashrc /etc/skel/.bashrc && \
    rm /tmp/bashrc

# Default environment variables
ENV PUID=99
ENV PGID=100
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
ENV BACKUP_ENABLED="true"
ENV BACKUP_RETENTION_DAYS="7"
ENV LOG_LEVEL="INFO"

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Entry point with PUID/PGID handling
ENTRYPOINT ["/entrypoint.sh"]
