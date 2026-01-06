"""
Visionarr Notifications

Optional webhook notifications for conversion events.
Supports Discord, Slack, and generic JSON webhooks.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """Type of notification event."""
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    CONVERSION_SUCCESS = "conversion_success"
    CONVERSION_FAILED = "conversion_failed"
    ERROR = "error"


@dataclass
class NotificationPayload:
    """Payload for a notification."""
    type: NotificationType
    title: str
    message: str
    file_path: Optional[Path] = None
    error: Optional[str] = None
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class Notifier:
    """
    Sends webhook notifications for Visionarr events.
    
    Auto-detects webhook type based on URL:
    - Discord: discord.com/api/webhooks
    - Slack: hooks.slack.com
    - Generic: any other URL (sends JSON)
    """
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.webhook_type = self._detect_webhook_type()
        logger.info(f"Notifications enabled ({self.webhook_type})")
    
    def _detect_webhook_type(self) -> str:
        """Detect webhook type from URL."""
        url_lower = self.webhook_url.lower()
        
        if "discord.com/api/webhooks" in url_lower:
            return "discord"
        elif "hooks.slack.com" in url_lower:
            return "slack"
        else:
            return "generic"
    
    def send(self, payload: NotificationPayload) -> bool:
        """Send a notification."""
        try:
            if self.webhook_type == "discord":
                return self._send_discord(payload)
            elif self.webhook_type == "slack":
                return self._send_slack(payload)
            else:
                return self._send_generic(payload)
                
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
            return False
    
    def _get_color(self, notification_type: NotificationType) -> int:
        """Get color code for notification type (Discord embed color)."""
        colors = {
            NotificationType.STARTUP: 0x00FF00,  # Green
            NotificationType.SHUTDOWN: 0x808080,  # Gray
            NotificationType.CONVERSION_SUCCESS: 0x00FF00,  # Green
            NotificationType.CONVERSION_FAILED: 0xFF0000,  # Red
            NotificationType.ERROR: 0xFF0000,  # Red
        }
        return colors.get(notification_type, 0x0000FF)
    
    def _get_emoji(self, notification_type: NotificationType) -> str:
        """Get emoji for notification type."""
        emojis = {
            NotificationType.STARTUP: "🚀",
            NotificationType.SHUTDOWN: "🛑",
            NotificationType.CONVERSION_SUCCESS: "✅",
            NotificationType.CONVERSION_FAILED: "❌",
            NotificationType.ERROR: "⚠️",
        }
        return emojis.get(notification_type, "ℹ️")
    
    def _send_discord(self, payload: NotificationPayload) -> bool:
        """Send Discord webhook notification."""
        embed = {
            "title": f"{self._get_emoji(payload.type)} {payload.title}",
            "description": payload.message,
            "color": self._get_color(payload.type),
            "timestamp": payload.timestamp.isoformat(),
            "footer": {"text": "Visionarr"}
        }
        
        if payload.file_path:
            embed["fields"] = [{
                "name": "File",
                "value": f"`{payload.file_path.name}`",
                "inline": False
            }]
        
        if payload.error:
            embed["fields"] = embed.get("fields", []) + [{
                "name": "Error",
                "value": f"```{payload.error[:500]}```",
                "inline": False
            }]
        
        data = {"embeds": [embed]}
        
        response = requests.post(
            self.webhook_url,
            json=data,
            timeout=10
        )
        
        return response.status_code in (200, 204)
    
    def _send_slack(self, payload: NotificationPayload) -> bool:
        """Send Slack webhook notification."""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{self._get_emoji(payload.type)} {payload.title}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": payload.message
                }
            }
        ]
        
        if payload.file_path:
            blocks.append({
                "type": "section",
                "fields": [{
                    "type": "mrkdwn",
                    "text": f"*File:*\n`{payload.file_path.name}`"
                }]
            })
        
        if payload.error:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Error:*\n```{payload.error[:500]}```"
                }
            })
        
        data = {"blocks": blocks}
        
        response = requests.post(
            self.webhook_url,
            json=data,
            timeout=10
        )
        
        return response.status_code == 200
    
    def _send_generic(self, payload: NotificationPayload) -> bool:
        """Send generic JSON webhook notification."""
        data = {
            "event": payload.type.value,
            "title": payload.title,
            "message": payload.message,
            "timestamp": payload.timestamp.isoformat(),
            "source": "visionarr"
        }
        
        if payload.file_path:
            data["file_path"] = str(payload.file_path)
            data["file_name"] = payload.file_path.name
        
        if payload.error:
            data["error"] = payload.error
        
        response = requests.post(
            self.webhook_url,
            json=data,
            timeout=10
        )
        
        return response.status_code in (200, 201, 204)
    
    # -------------------------------------------------------------------------
    # Convenience methods
    # -------------------------------------------------------------------------
    
    def notify_startup(self) -> bool:
        """Send startup notification."""
        return self.send(NotificationPayload(
            type=NotificationType.STARTUP,
            title="Visionarr Started",
            message="Dolby Vision profile converter is now running."
        ))
    
    def notify_shutdown(self) -> bool:
        """Send shutdown notification."""
        return self.send(NotificationPayload(
            type=NotificationType.SHUTDOWN,
            title="Visionarr Stopped",
            message="Dolby Vision profile converter has stopped."
        ))
    
    def notify_conversion_success(
        self,
        file_path: Path,
        title: str,
        duration_seconds: Optional[float] = None
    ) -> bool:
        """Send conversion success notification."""
        message = f"Successfully converted **{title}** to Profile 8."
        if duration_seconds:
            message += f"\nDuration: {duration_seconds:.0f} seconds"
        
        return self.send(NotificationPayload(
            type=NotificationType.CONVERSION_SUCCESS,
            title="Conversion Complete",
            message=message,
            file_path=file_path
        ))
    
    def notify_conversion_failed(
        self,
        file_path: Path,
        title: str,
        error: str
    ) -> bool:
        """Send conversion failure notification."""
        return self.send(NotificationPayload(
            type=NotificationType.CONVERSION_FAILED,
            title="Conversion Failed",
            message=f"Failed to convert **{title}**",
            file_path=file_path,
            error=error
        ))
