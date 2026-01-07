#!/bin/bash
# Visionarr entrypoint script
# Handles PUID/PGID for Unraid compatibility

PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo "Starting Visionarr with UID: $PUID, GID: $PGID"

# Create group if it doesn't exist
if ! getent group visionarr > /dev/null 2>&1; then
    groupadd -g "$PGID" visionarr
else
    groupmod -o -g "$PGID" visionarr
fi

# Create/modify user
if ! id -u visionarr > /dev/null 2>&1; then
    useradd -o -u "$PUID" -g visionarr -d /home/visionarr -s /bin/bash visionarr
else
    usermod -o -u "$PUID" visionarr
fi

# Fix ownership of app directories
chown -R visionarr:visionarr /app /config /temp /home/visionarr 2>/dev/null || true

# Run as the configured user
exec gosu visionarr python -m src.main "$@"
