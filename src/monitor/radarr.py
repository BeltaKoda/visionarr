"""
Visionarr Radarr Monitor

Polls Radarr API for recently imported movies and triggers rescans.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import requests

from .base import BaseMonitor, ImportedMedia, MediaType

logger = logging.getLogger(__name__)


class RadarrMonitor(BaseMonitor):
    """Monitor for Radarr movie imports."""
    
    @property
    def name(self) -> str:
        return "Radarr"
    
    def _api_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None
    ) -> Optional[dict]:
        """Make an API request to Radarr."""
        url = f"{self.base_url}/api/v3/{endpoint}"
        headers = {"X-Api-Key": self.api_key}
        
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=30
            )
            response.raise_for_status()
            return response.json() if response.content else {}
        except requests.RequestException as e:
            logger.error(f"Radarr API request failed: {e}")
            return None
    
    def test_connection(self) -> bool:
        """Test connectivity to Radarr."""
        result = self._api_request("GET", "system/status")
        if result:
            version = result.get("version", "unknown")
            logger.info(f"Connected to Radarr v{version}")
            return True
        return False
    
    def get_recent_imports(self, since_minutes: int) -> List[ImportedMedia]:
        """Get recently imported movies from Radarr history."""
        imports = []
        
        # Calculate cutoff time
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        
        # Fetch history - we need to paginate if there's a lot
        params = {
            "pageSize": 100,
            "sortKey": "date",
            "sortDirection": "descending",
            "eventType": 3,  # downloadFolderImported
        }
        
        result = self._api_request("GET", "history", params=params)
        if not result:
            return imports
        
        records = result.get("records", [])
        
        for record in records:
            try:
                # Parse the date
                date_str = record.get("date", "")
                if not date_str:
                    continue
                    
                # Radarr returns ISO format dates
                record_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                
                # Skip if older than cutoff
                if record_date < cutoff:
                    continue
                
                # Get movie info
                movie = record.get("movie", {})
                movie_id = movie.get("id")
                title = movie.get("title", "Unknown")
                
                # Get file info from sourceTitle or data
                data = record.get("data", {})
                imported_path = data.get("importedPath") or data.get("movieFile", {}).get("path")
                
                if not imported_path or not movie_id:
                    continue
                
                file_path = Path(imported_path)
                
                # Get file size - need to query movie file
                file_size = 0
                movie_data = self._api_request("GET", f"movie/{movie_id}")
                if movie_data and movie_data.get("movieFile"):
                    file_size = movie_data["movieFile"].get("size", 0)
                
                imports.append(ImportedMedia(
                    file_path=file_path,
                    file_size_bytes=file_size,
                    media_type=MediaType.MOVIE,
                    media_id=movie_id,
                    title=title,
                    imported_at=record_date,
                    quality=record.get("quality", {}).get("quality", {}).get("name"),
                    source_title=record.get("sourceTitle")
                ))
                
            except Exception as e:
                logger.warning(f"Error parsing Radarr history record: {e}")
                continue
        
        logger.info(f"Found {len(imports)} recent imports in Radarr")
        return imports
    
    def trigger_rescan(self, media_id: int) -> bool:
        """Trigger a rescan of a movie in Radarr."""
        logger.info(f"Triggering Radarr rescan for movie ID {media_id}")
        
        result = self._api_request(
            "POST",
            "command",
            json_data={
                "name": "RescanMovie",
                "movieId": media_id
            }
        )
        
        if result:
            logger.info(f"Radarr rescan command accepted (ID: {result.get('id', 'unknown')})")
            return True
        return False
    
    def get_library_paths(self) -> List[Path]:
        """Get all root folder paths from Radarr."""
        result = self._api_request("GET", "rootfolder")
        if not result:
            return []
        
        return [Path(folder.get("path", "")) for folder in result if folder.get("path")]
    
    def get_all_movies_with_files(self) -> List[ImportedMedia]:
        """
        Get all movies that have files (for full library scan).
        
        This is a heavy operation - use sparingly.
        """
        result = self._api_request("GET", "movie")
        if not result:
            return []
        
        movies = []
        for movie in result:
            if not movie.get("hasFile"):
                continue
            
            movie_file = movie.get("movieFile", {})
            file_path = movie_file.get("path")
            
            if not file_path:
                continue
            
            movies.append(ImportedMedia(
                file_path=Path(file_path),
                file_size_bytes=movie_file.get("size", 0),
                media_type=MediaType.MOVIE,
                media_id=movie.get("id"),
                title=movie.get("title", "Unknown"),
                imported_at=datetime.now(timezone.utc),  # Not accurate but needed
                quality=movie_file.get("quality", {}).get("quality", {}).get("name")
            ))
        
        logger.info(f"Found {len(movies)} movies with files in Radarr library")
        return movies
