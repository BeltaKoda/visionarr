#!/bin/bash
# Visionarr entrypoint script
# Handles PUID/PGID for Unraid compatibility

PUID=${PUID:-99}
PGID=${PGID:-100}

echo "Starting Visionarr with UID: $PUID, GID: $PGID"

# Create/modify group
groupadd -o -g "$PGID" visionarr 2>/dev/null || groupmod -o -g "$PGID" visionarr 2>/dev/null || true

# Create/modify user  
id -u visionarr &>/dev/null || useradd -o -u "$PUID" -g "$PGID" -d /home/visionarr -s /bin/bash visionarr
usermod -o -u "$PUID" -g "$PGID" visionarr 2>/dev/null || true

# Ensure home directory exists
mkdir -p /home/visionarr
chown "$PUID:$PGID" /home/visionarr

# Fix ownership of app directories
chown -R "$PUID:$PGID" /app /config /temp 2>/dev/null || true

# Run as the configured user
exec gosu "$PUID:$PGID" python -m src.main "$@"
