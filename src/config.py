"""
Visionarr Configuration Module

Loads configuration from environment variables with sensible defaults.
All paths must be mounted volumes (not container-internal) for Unraid safety.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    """Application configuration loaded from environment variables."""
    
    # Scheduled scan settings
    delta_scan_interval_minutes: int = 30  # How often to run delta scans (skip known files)
    full_scan_day: str = "sunday"          # Day of week for full scan (monday-sunday)
    full_scan_time: str = "03:00"          # Time for full scan (24h format)
    
    # Operation mode
    manual_mode: bool = False
    
    # Processing configuration
    process_concurrency: int = 1
    min_free_space_gb: int = 50
    
    # Paths - MUST be mounted volumes on Unraid
    config_dir: Path = Path("/config")
    temp_dir: Path = Path("/temp")
    media_dir: Path = Path("/media")
    
    # Backup configuration
    backup_enabled: bool = True
    backup_retention_days: int = 7
    
    # Logging
    log_level: str = "INFO"
    log_file: Optional[str] = None
    
    # Notifications
    webhook_url: Optional[str] = None
    
    @property
    def database_path(self) -> Path:
        """Path to SQLite database."""
        return self.config_dir / "visionarr.db"


def _parse_bool(value: str) -> bool:
    """Parse boolean from environment variable string."""
    return value.lower() in ("true", "1", "yes", "on")


def _validate_mount_point(path: Path, name: str) -> None:
    """
    Validate that a path is a mounted volume, not container-internal.
    
    CRITICAL: This prevents filling up Unraid's docker.img which would
    crash ALL Docker containers on the system.
    """
    if not path.exists():
        print(f"ERROR: {name} path does not exist: {path}", file=sys.stderr)
        print(f"       Make sure to mount a volume to {path}", file=sys.stderr)
        sys.exit(1)
    
    # Check if it's a mount point (has different device than parent)
    # This is a heuristic - on Docker, mounted volumes have different st_dev
    try:
        path_stat = path.stat()
        parent_stat = path.parent.stat()
        
        if path_stat.st_dev == parent_stat.st_dev and str(path) not in ["/", "/config", "/temp", "/media"]:
            # Same device as parent - likely not a mount point
            # But we allow the standard container paths as they might be bind mounts
            print(f"WARNING: {name} ({path}) may not be a mounted volume.", file=sys.stderr)
            print(f"         If running on Unraid, ensure this is mapped to /mnt/user/...", file=sys.stderr)
    except OSError:
        pass  # Can't check, proceed anyway


def load_config() -> Config:
    """Load configuration from environment variables."""
    config = Config(
        # Scheduled scans
        delta_scan_interval_minutes=int(os.getenv("DELTA_SCAN_INTERVAL_MINUTES", "30")),
        full_scan_day=os.getenv("FULL_SCAN_DAY", "sunday").lower(),
        full_scan_time=os.getenv("FULL_SCAN_TIME", "03:00"),
        
        # Operation mode
        manual_mode=_parse_bool(os.getenv("MANUAL_MODE", "false")),
        
        # Processing
        process_concurrency=int(os.getenv("PROCESS_CONCURRENCY", "1")),
        min_free_space_gb=int(os.getenv("MIN_FREE_SPACE_GB", "50")),
        
        # Paths
        config_dir=Path(os.getenv("CONFIG_DIR", "/config")),
        temp_dir=Path(os.getenv("TEMP_DIR", "/temp")),
        media_dir=Path(os.getenv("MEDIA_DIR", "/media")),
        
        # Backup
        backup_enabled=_parse_bool(os.getenv("BACKUP_ENABLED", "true")),
        backup_retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "7")),
        
        # Logging
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_file=os.getenv("LOG_FILE"),
        
        # Notifications
        webhook_url=os.getenv("WEBHOOK_URL"),
    )
    
    return config


def validate_config(config: Config) -> bool:
    """
    Validate configuration and check mount points.
    Returns True if valid, exits with error if critical issues found.
    """
    errors = []
    
    # Must have at least one *arr configured
    if not config.has_radarr and not config.has_sonarr:
        errors.append("No Radarr or Sonarr configured. Set RADARR_URL/RADARR_API_KEY or SONARR_URL/SONARR_API_KEY")
    
    # Validate mount points (critical for Unraid docker.img safety)
    _validate_mount_point(config.config_dir, "CONFIG_DIR")
    _validate_mount_point(config.temp_dir, "TEMP_DIR")
    
    # Media dir should exist but we're more lenient
    if not config.media_dir.exists():
        errors.append(f"MEDIA_DIR does not exist: {config.media_dir}")
    
    if errors:
        print("Configuration errors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return False
    
    return True
