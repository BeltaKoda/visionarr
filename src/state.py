"""
Visionarr State Management

SQLite database for tracking processed files to prevent reprocessing.
Unlike Unpackerr, file paths don't change after conversion, so we must
track what's been processed.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Generator, List, Optional


@dataclass
class ProcessedFile:
    """Record of a successfully processed file."""
    id: int
    file_path: str
    original_profile: str
    new_profile: str
    processed_at: datetime
    file_size_bytes: int


@dataclass
class FailedFile:
    """Record of a failed processing attempt."""
    id: int
    file_path: str
    error_message: str
    failed_at: datetime
    retry_count: int


class StateDB:
    """SQLite-based state tracking for processed files."""
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()
        self._init_settings_defaults()
    
    def _init_db(self) -> None:
        """Initialize database schema if not exists."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS processed_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    original_profile TEXT NOT NULL,
                    new_profile TEXT NOT NULL,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    file_size_bytes INTEGER NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS failed_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    error_message TEXT NOT NULL,
                    failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    retry_count INTEGER DEFAULT 0
                );
                
                CREATE TABLE IF NOT EXISTS discovered_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS scanned_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    has_dovi BOOLEAN NOT NULL,
                    dovi_profile TEXT,
                    file_size_bytes INTEGER NOT NULL,
                    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS current_conversion (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    file_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                
                CREATE INDEX IF NOT EXISTS idx_processed_path ON processed_files(file_path);
                CREATE INDEX IF NOT EXISTS idx_failed_path ON failed_files(file_path);
                CREATE INDEX IF NOT EXISTS idx_discovered_path ON discovered_files(file_path);
                CREATE INDEX IF NOT EXISTS idx_scanned_path ON scanned_files(file_path);
            """)


    
    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    # -------------------------------------------------------------------------
    # Settings (Persistent)
    # -------------------------------------------------------------------------
    
    # Default settings values
    SETTINGS_DEFAULTS = {
        "auto_process_mode": "off",      # off, all, movies, shows
        "backup_enabled": "true",         # true, false
        "delta_scan_interval": "30",      # minutes
        "full_scan_day": "sunday",        # day name
        "full_scan_time": "03:00",        # HH:MM
    }
    
    def _init_settings_defaults(self) -> None:
        """Initialize settings with defaults if not already set."""
        for key, default_value in self.SETTINGS_DEFAULTS.items():
            if self.get_setting(key) is None:
                self.set_setting(key, default_value)
    
    def get_setting(self, key: str) -> Optional[str]:
        """Get a setting value by key. Returns None if not found."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,)
            )
            row = cursor.fetchone()
            return row["value"] if row else None
    
    def set_setting(self, key: str, value: str) -> None:
        """Set a setting value."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
    
    def get_all_settings(self) -> dict:
        """Get all settings as a dictionary."""
        settings = {}
        for key in self.SETTINGS_DEFAULTS:
            value = self.get_setting(key)
            settings[key] = value if value is not None else self.SETTINGS_DEFAULTS[key]
        return settings
    
    # -------------------------------------------------------------------------
    # Processed Files
    # -------------------------------------------------------------------------
    
    def is_processed(self, file_path: str) -> bool:
        """Check if a file has already been processed."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM processed_files WHERE file_path = ?",
                (file_path,)
            )
            return cursor.fetchone() is not None
    
    def mark_processed(
        self,
        file_path: str,
        original_profile: str,
        new_profile: str,
        file_size_bytes: int
    ) -> None:
        """Mark a file as successfully processed."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO processed_files 
                (file_path, original_profile, new_profile, file_size_bytes, processed_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (file_path, original_profile, new_profile, file_size_bytes))
            
            # Remove from failed if it was there
            conn.execute("DELETE FROM failed_files WHERE file_path = ?", (file_path,))
    
    def get_processed_files(self, limit: int = 100) -> List[ProcessedFile]:
        """Get list of processed files, most recent first."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT id, file_path, original_profile, new_profile, processed_at, file_size_bytes
                FROM processed_files
                ORDER BY processed_at DESC
                LIMIT ?
            """, (limit,))
            
            return [
                ProcessedFile(
                    id=row["id"],
                    file_path=row["file_path"],
                    original_profile=row["original_profile"],
                    new_profile=row["new_profile"],
                    processed_at=datetime.fromisoformat(row["processed_at"]),
                    file_size_bytes=row["file_size_bytes"]
                )
                for row in cursor.fetchall()
            ]
    
    def clear_processed(self, file_path: str) -> bool:
        """Remove a single file from processed list (allows reprocessing)."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM processed_files WHERE file_path = ?",
                (file_path,)
            )
            return cursor.rowcount > 0
    
    def clear_all_processed(self) -> int:
        """Clear all processed records. Returns count of deleted rows."""
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM processed_files")
            return cursor.rowcount
    
    # -------------------------------------------------------------------------
    # Failed Files
    # -------------------------------------------------------------------------
    
    def is_failed(self, file_path: str) -> Optional[FailedFile]:
        """Check if a file has failed processing. Returns failure record or None."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM failed_files WHERE file_path = ?",
                (file_path,)
            )
            row = cursor.fetchone()
            if row:
                return FailedFile(
                    id=row["id"],
                    file_path=row["file_path"],
                    error_message=row["error_message"],
                    failed_at=datetime.fromisoformat(row["failed_at"]),
                    retry_count=row["retry_count"]
                )
            return None
    
    def mark_failed(self, file_path: str, error_message: str) -> None:
        """Mark a file as failed. Increments retry count if already failed."""
        with self._get_connection() as conn:
            # Check if already failed
            cursor = conn.execute(
                "SELECT retry_count FROM failed_files WHERE file_path = ?",
                (file_path,)
            )
            existing = cursor.fetchone()
            
            if existing:
                conn.execute("""
                    UPDATE failed_files 
                    SET error_message = ?, failed_at = CURRENT_TIMESTAMP, retry_count = retry_count + 1
                    WHERE file_path = ?
                """, (error_message, file_path))
            else:
                conn.execute("""
                    INSERT INTO failed_files (file_path, error_message)
                    VALUES (?, ?)
                """, (file_path, error_message))
    
    def get_failed_files(self, limit: int = 100) -> List[FailedFile]:
        """Get list of failed files, most recent first."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT id, file_path, error_message, failed_at, retry_count
                FROM failed_files
                ORDER BY failed_at DESC
                LIMIT ?
            """, (limit,))
            
            return [
                FailedFile(
                    id=row["id"],
                    file_path=row["file_path"],
                    error_message=row["error_message"],
                    failed_at=datetime.fromisoformat(row["failed_at"]),
                    retry_count=row["retry_count"]
                )
                for row in cursor.fetchall()
            ]
    
    def clear_failed(self, file_path: Optional[str] = None) -> int:
        """Clear failed records. If file_path is None, clears all."""
        with self._get_connection() as conn:
            if file_path:
                cursor = conn.execute(
                    "DELETE FROM failed_files WHERE file_path = ?",
                    (file_path,)
                )
            else:
                cursor = conn.execute("DELETE FROM failed_files")
            return cursor.rowcount
    
    # -------------------------------------------------------------------------
    # Export/Import
    # -------------------------------------------------------------------------
    
    def export_to_json(self) -> str:
        """Export database to JSON for backup/debugging."""
        with self._get_connection() as conn:
            processed = conn.execute("SELECT * FROM processed_files").fetchall()
            failed = conn.execute("SELECT * FROM failed_files").fetchall()
            
            data = {
                "exported_at": datetime.now().isoformat(),
                "processed_files": [dict(row) for row in processed],
                "failed_files": [dict(row) for row in failed]
            }
            
            return json.dumps(data, indent=2, default=str)
    
    def get_stats(self) -> dict:
        """Get database statistics."""
        with self._get_connection() as conn:
            processed_count = conn.execute(
                "SELECT COUNT(*) FROM processed_files"
            ).fetchone()[0]
            
            failed_count = conn.execute(
                "SELECT COUNT(*) FROM failed_files"
            ).fetchone()[0]
            
            total_bytes = conn.execute(
                "SELECT COALESCE(SUM(file_size_bytes), 0) FROM processed_files"
            ).fetchone()[0]
            
            return {
                "processed_count": processed_count,
                "failed_count": failed_count,
                "total_bytes_processed": total_bytes
            }
    
    # -------------------------------------------------------------------------
    # Initial Setup / First Run Protection
    # -------------------------------------------------------------------------
    
    @property
    def is_initial_setup_complete(self) -> bool:
        """
        Check if initial setup has been completed.
        
        On first run (new database), this returns False.
        The daemon will NOT auto-convert until the user runs manual mode
        and confirms the initial batch of detected files.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT value FROM settings WHERE key = 'initial_setup_complete'"
            )
            row = cursor.fetchone()
            return row is not None and row[0] == "true"
    
    def mark_initial_setup_complete(self) -> None:
        """Mark that initial setup has been completed via manual mode."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO settings (key, value)
                VALUES ('initial_setup_complete', 'true')
            """)
    
    def reset_initial_setup(self) -> None:
        """Reset initial setup flag (for testing or re-verification)."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM settings WHERE key = 'initial_setup_complete'")

    def clear_database(self) -> int:
        """
        Clear entire database - all processed, failed, discovered, and settings.
        Returns total number of records cleared.
        Use this for a complete fresh start.
        """
        total = 0
        with self._get_connection() as conn:
            # Count records first
            processed = conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM failed_files").fetchone()[0]
            discovered = conn.execute("SELECT COUNT(*) FROM discovered_files").fetchone()[0]
            total = processed + failed + discovered
            
            # Clear all tables
            conn.execute("DELETE FROM processed_files")
            conn.execute("DELETE FROM failed_files")
            conn.execute("DELETE FROM discovered_files")
            conn.execute("DELETE FROM settings WHERE key = 'initial_setup_complete'")
        
        return total

    # ==================== Discovered Files Methods ====================
    
    def add_discovered(self, file_path: str, title: str) -> bool:
        """Add a discovered Profile 7 file. Returns True if new, False if already exists."""
        with self._get_connection() as conn:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO discovered_files (file_path, title) VALUES (?, ?)",
                    (file_path, title)
                )
                return conn.total_changes > 0
            except sqlite3.Error:
                return False

    def get_discovered(self) -> List[dict]:
        """Get all discovered Profile 7 files not yet processed."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT d.id, d.file_path, d.title, d.discovered_at
                FROM discovered_files d
                WHERE d.file_path NOT IN (SELECT file_path FROM processed_files)
                ORDER BY d.discovered_at DESC
            """).fetchall()
            return [dict(row) for row in rows]

    def is_discovered(self, file_path: str) -> bool:
        """Check if a file is already in the discovered list."""
        with self._get_connection() as conn:
            result = conn.execute(
                "SELECT 1 FROM discovered_files WHERE file_path = ?",
                (file_path,)
            ).fetchone()
            return result is not None

    def remove_discovered(self, file_path: str) -> bool:
        """Remove a file from discovered list (after queuing for conversion)."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM discovered_files WHERE file_path = ?", (file_path,))
            return conn.total_changes > 0

    def clear_discovered(self) -> int:
        """Clear all discovered files. Returns count deleted."""
        with self._get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM discovered_files").fetchone()[0]
            conn.execute("DELETE FROM discovered_files")
            return count

    # ==================== Scanned Files Methods ====================
    
    def add_scanned(self, file_path: str, has_dovi: bool, dovi_profile: Optional[str], file_size_bytes: int) -> bool:
        """
        Record a scanned file with its DoVi profile status.
        Returns True if new record, False if already existed (updated).
        """
        with self._get_connection() as conn:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO scanned_files 
                    (file_path, has_dovi, dovi_profile, file_size_bytes, scanned_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (file_path, has_dovi, dovi_profile, file_size_bytes))
                return True
            except Exception:
                return False

    def is_scanned(self, file_path: str) -> bool:
        """Check if a file has been previously scanned."""
        with self._get_connection() as conn:
            result = conn.execute(
                "SELECT 1 FROM scanned_files WHERE file_path = ?",
                (file_path,)
            ).fetchone()
            return result is not None

    def get_all_scanned_paths(self) -> set:
        """
        Return a set of all scanned file paths for efficient O(1) lookups.
        Used by Delta Scan to skip previously analyzed files.
        """
        with self._get_connection() as conn:
            rows = conn.execute("SELECT file_path FROM scanned_files").fetchall()
            return {row[0] for row in rows}

    def clear_scanned(self) -> int:
        """Clear all scanned file records. Returns count deleted."""
        with self._get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0]
            conn.execute("DELETE FROM scanned_files")
            return count

    def get_scanned_stats(self) -> dict:
        """Get statistics about scanned files."""
        with self._get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0]
            with_dovi = conn.execute("SELECT COUNT(*) FROM scanned_files WHERE has_dovi = 1").fetchone()[0]
            profile_7 = conn.execute("SELECT COUNT(*) FROM scanned_files WHERE dovi_profile = '7'").fetchone()[0]
            profile_8 = conn.execute("SELECT COUNT(*) FROM scanned_files WHERE dovi_profile = '8'").fetchone()[0]
            return {
                "total": total,
                "with_dovi": with_dovi,
                "profile_7": profile_7,
                "profile_8": profile_8,
                "no_dovi": total - with_dovi
            }

    # ==================== Current Conversion Methods ====================
    
    def set_current_conversion(self, file_path: str, title: str) -> None:
        """Mark a file as currently being converted."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM current_conversion")
            conn.execute("""
                INSERT INTO current_conversion (id, file_path, title, started_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            """, (file_path, title))

    def clear_current_conversion(self) -> None:
        """Clear the current conversion marker."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM current_conversion")

    def get_current_conversion(self) -> Optional[dict]:
        """Get the currently converting file, if any."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT file_path, title, started_at 
                FROM current_conversion 
                WHERE id = 1
            """).fetchone()
            if row:
                return {
                    "file_path": row["file_path"],
                    "title": row["title"],
                    "started_at": row["started_at"]
                }
            return None
