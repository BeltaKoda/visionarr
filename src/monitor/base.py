"""
Visionarr Monitor Base

Abstract base class defining the interface for Radarr/Sonarr monitors.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional


class MediaType(Enum):
    """Type of media being monitored."""
    MOVIE = "movie"
    EPISODE = "episode"


@dataclass
class ImportedMedia:
    """Represents a recently imported media file."""
    
    # File information
    file_path: Path
    file_size_bytes: int
    
    # Media information
    media_type: MediaType
    media_id: int  # Radarr movie ID or Sonarr episode ID
    title: str
    
    # Import information
    imported_at: datetime
    
    # Optional metadata
    quality: Optional[str] = None
    source_title: Optional[str] = None  # Release name
    
    def __str__(self) -> str:
        return f"{self.title} ({self.file_path.name})"


class BaseMonitor(ABC):
    """
    Abstract base class for *arr API monitors.
    
    Implementations should poll the respective API for recent imports
    and provide methods to trigger rescans after conversion.
    """
    
    def __init__(self, base_url: str, api_key: str):
        """
        Initialize the monitor.
        
        Args:
            base_url: Base URL of the *arr instance (e.g., http://localhost:7878)
            api_key: API key for authentication
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of this monitor (e.g., 'Radarr', 'Sonarr')."""
        pass
    
    @abstractmethod
    def test_connection(self) -> bool:
        """
        Test connectivity to the *arr API.
        
        Returns:
            True if connection successful, False otherwise.
        """
        pass
    
    @abstractmethod
    def get_recent_imports(self, since_minutes: int) -> List[ImportedMedia]:
        """
        Get list of recently imported media files.
        
        Args:
            since_minutes: How far back to look in history
            
        Returns:
            List of ImportedMedia objects representing recent imports
        """
        pass
    
    @abstractmethod
    def trigger_rescan(self, media_id: int) -> bool:
        """
        Trigger a rescan of the specified media item.
        
        This should be called after conversion to update the *arr
        database with new file information (size, hash, etc.).
        
        Args:
            media_id: The ID of the movie/series to rescan
            
        Returns:
            True if rescan command was accepted, False otherwise
        """
        pass
    
    @abstractmethod
    def get_library_paths(self) -> List[Path]:
        """
        Get all root folder paths configured in the *arr instance.
        
        Used for full library scans in manual mode.
        
        Returns:
            List of Path objects for all configured root folders
        """
        pass
