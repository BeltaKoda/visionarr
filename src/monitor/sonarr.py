"""
Visionarr Sonarr Monitor

Polls Sonarr API for recently imported episodes and triggers rescans.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import requests

from .base import BaseMonitor, ImportedMedia, MediaType

logger = logging.getLogger(__name__)


class SonarrMonitor(BaseMonitor):
    """Monitor for Sonarr episode imports."""
    
    @property
    def name(self) -> str:
        return "Sonarr"
    
    def _api_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None
    ) -> Optional[dict]:
        """Make an API request to Sonarr."""
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
            logger.error(f"Sonarr API request failed: {e}")
            return None
    
    def test_connection(self) -> bool:
        """Test connectivity to Sonarr."""
        result = self._api_request("GET", "system/status")
        if result:
            version = result.get("version", "unknown")
            logger.info(f"Connected to Sonarr v{version}")
            return True
        return False
    
    def get_recent_imports(self, since_minutes: int) -> List[ImportedMedia]:
        """Get recently imported episodes from Sonarr history."""
        imports = []
        
        # Calculate cutoff time
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        
        # Fetch history
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
                    
                record_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                
                # Skip if older than cutoff
                if record_date < cutoff:
                    continue
                
                # Get series and episode info
                series = record.get("series", {})
                episode = record.get("episode", {})
                series_id = series.get("id")
                episode_id = episode.get("id")
                
                series_title = series.get("title", "Unknown")
                season_num = episode.get("seasonNumber", 0)
                episode_num = episode.get("episodeNumber", 0)
                episode_title = episode.get("title", "")
                
                title = f"{series_title} - S{season_num:02d}E{episode_num:02d}"
                if episode_title:
                    title += f" - {episode_title}"
                
                # Get file info
                data = record.get("data", {})
                imported_path = data.get("importedPath")
                
                if not imported_path or not series_id:
                    continue
                
                file_path = Path(imported_path)
                
                # Get file size from episode file
                file_size = 0
                if episode_id:
                    ep_file = self._api_request("GET", f"episodefile/{data.get('episodeFileId', 0)}")
                    if ep_file:
                        file_size = ep_file.get("size", 0)
                
                imports.append(ImportedMedia(
                    file_path=file_path,
                    file_size_bytes=file_size,
                    media_type=MediaType.EPISODE,
                    media_id=series_id,  # Use series ID for rescan
                    title=title,
                    imported_at=record_date,
                    quality=record.get("quality", {}).get("quality", {}).get("name"),
                    source_title=record.get("sourceTitle")
                ))
                
            except Exception as e:
                logger.warning(f"Error parsing Sonarr history record: {e}")
                continue
        
        logger.info(f"Found {len(imports)} recent imports in Sonarr")
        return imports
    
    def trigger_rescan(self, media_id: int) -> bool:
        """Trigger a rescan of a series in Sonarr."""
        logger.info(f"Triggering Sonarr rescan for series ID {media_id}")
        
        result = self._api_request(
            "POST",
            "command",
            json_data={
                "name": "RescanSeries",
                "seriesId": media_id
            }
        )
        
        if result:
            logger.info(f"Sonarr rescan command accepted (ID: {result.get('id', 'unknown')})")
            return True
        return False
    
    def get_library_paths(self) -> List[Path]:
        """Get all root folder paths from Sonarr."""
        result = self._api_request("GET", "rootfolder")
        if not result:
            return []
        
        return [Path(folder.get("path", "")) for folder in result if folder.get("path")]
    
    def get_all_episodes_with_files(self) -> List[ImportedMedia]:
        """
        Get all episodes that have files (for full library scan).
        
        This is a VERY heavy operation for TV libraries - use sparingly.
        """
        # First get all series
        series_list = self._api_request("GET", "series")
        if not series_list:
            return []
        
        episodes = []
        
        for series in series_list:
            series_id = series.get("id")
            series_title = series.get("title", "Unknown")
            
            # Get episode files for this series
            ep_files = self._api_request("GET", "episodefile", params={"seriesId": series_id})
            if not ep_files:
                continue
            
            for ep_file in ep_files:
                file_path = ep_file.get("path")
                if not file_path:
                    continue
                
                # Build title from relative path since we don't have episode details
                rel_path = ep_file.get("relativePath", Path(file_path).name)
                title = f"{series_title} - {rel_path}"
                
                episodes.append(ImportedMedia(
                    file_path=Path(file_path),
                    file_size_bytes=ep_file.get("size", 0),
                    media_type=MediaType.EPISODE,
                    media_id=series_id,
                    title=title,
                    imported_at=datetime.now(timezone.utc),
                    quality=ep_file.get("quality", {}).get("quality", {}).get("name")
                ))
        
        logger.info(f"Found {len(episodes)} episodes with files in Sonarr library")
        return episodes
