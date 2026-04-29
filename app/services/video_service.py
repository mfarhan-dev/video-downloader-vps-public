import asyncio
import logging
from typing import Any

import yt_dlp

from app.core.config import settings
from app.models.response_models import VideoFormat, VideoInfo

logger = logging.getLogger(__name__)


class VideoService:
    """Handles video metadata extraction using yt-dlp."""

    def __init__(
        self,
        timeout: int = settings.ytdl_timeout,
        max_formats: int = settings.max_formats,
    ):
        self.timeout = timeout
        self.max_formats = max_formats

    def _extract_sync(self, url: str) -> dict[str, Any]:
        """Synchronous wrapper around yt-dlp extract_info."""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "js_runtimes": {"node": {}},
            "proxy": "http://exwnzzqh:ib3jgwgkjyl1@31.59.20.176:6754",
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web", "ios"],
                }
            },
        }
        # Check for cookies file
        cookies_path = "/app/cookies.txt"
        import os
        if os.path.exists(cookies_path):
            ydl_opts["cookiefile"] = cookies_path

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    async def extract(self, url: str) -> VideoInfo:
        """Extract video metadata asynchronously with timeout handling."""
        loop = asyncio.get_event_loop()
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, self._extract_sync, url),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.error("Extraction timed out for URL: %s", url)
            raise TimeoutError("Video extraction timed out")
        except Exception as exc:
            logger.error("Extraction failed for URL %s: %s", url, exc)
            raise ValueError(f"Failed to extract video info: {exc}")

        formats = self._filter_formats(info.get("formats", []))

        return VideoInfo(
            title=info.get("title") or "Unknown",
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
            formats=formats,
        )

    def _filter_formats(self, formats: list[dict[str, Any]]) -> list[VideoFormat]:
        """
        Filter, sort and limit formats.

        Rules:
        - Must have a direct URL
        - Must contain video (skip audio-only)
        - Protocol must be http/https for direct download
        - Sort by resolution descending, then prefer mp4
        - Limit to top N formats
        """
        valid: list[dict[str, Any]] = []

        for fmt in formats:
            stream_url = fmt.get("url")
            if not stream_url:
                continue

            vcodec = fmt.get("vcodec", "none")
            if vcodec == "none" or vcodec is None:
                continue

            protocol = str(fmt.get("protocol", "")).lower()
            if protocol not in ("http", "https"):
                continue

            ext = fmt.get("ext") or "mp4"
            height = fmt.get("height") or 0

            if not height and fmt.get("resolution"):
                res = fmt["resolution"]
                if "x" in res:
                    try:
                        height = int(res.split("x")[1])
                    except (ValueError, IndexError):
                        pass

            if height:
                quality = f"{height}p"
            else:
                quality = fmt.get("quality_label") or fmt.get("format_note") or "unknown"

            valid.append(
                {
                    "quality": quality,
                    "format_id": fmt.get("format_id", "unknown"),
                    "ext": ext,
                    "url": stream_url,
                    "height": height,
                    "is_mp4": ext.lower() == "mp4",
                }
            )

        # Sort by quality descending, then prefer mp4 for same quality
        valid.sort(key=lambda x: (-x["height"], -x["is_mp4"]))

        result: list[VideoFormat] = []
        for v in valid[: self.max_formats]:
            result.append(
                VideoFormat(
                    quality=v["quality"],
                    format_id=v["format_id"],
                    ext=v["ext"],
                    url=v["url"],
                )
            )

        return result
