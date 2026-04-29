import asyncio
import logging
import random
import time
from typing import Any

import yt_dlp

from app.core.config import settings
from app.models.response_models import VideoFormat, VideoInfo

logger = logging.getLogger(__name__)

PROXY_URL = "http://exwnzzqh:ib3jgwgkjyl1@31.59.20.176:6754"


class VideoService:
    """Handles video metadata extraction using yt-dlp."""

    def __init__(
        self,
        timeout: int = settings.ytdl_timeout,
        max_formats: int = settings.max_formats,
    ):
        self.timeout = timeout
        self.max_formats = max_formats

    def _extract_with_opts(self, url: str, ydl_opts: dict) -> dict[str, Any] | None:
        """Try extraction with given options. Returns info or None on failure."""
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            err = str(exc).lower()
            if any(x in err for x in ["bot", "sign in", "blocked", "confirm you're not"]):
                logger.debug("Bot block detected: %s", exc)
            else:
                logger.debug("Extraction failed: %s", exc)
            return None

    def _build_opts(self, use_proxy: bool, strategy: dict) -> dict:
        """Build yt-dlp options."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "js_runtimes": {"node": {}},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "referer": "https://www.youtube.com/",
            "geo_bypass": True,
        }
        if use_proxy:
            opts["proxy"] = PROXY_URL

        import os
        for cookies_path in ("/tmp/yt_cookies.txt", "/app/cookies.txt"):
            if os.path.exists(cookies_path):
                opts["cookiefile"] = cookies_path
                break

        opts.update(strategy)
        return opts

    def _extract_sync(self, url: str) -> dict[str, Any]:
        """Synchronous wrapper around yt-dlp extract_info with multiple strategies."""
        from app.services.cookie_service import CookieService

        # Use fewer strategies to avoid triggering rate limits
        # Order matters: try no-proxy first (often gives best results), then proxy fallback
        strategies = [
            (False, {}, "no-proxy/auto"),
            (False, {"extractor_args": {"youtube": {"player_client": ["web"]}}}, "no-proxy/web"),
            (True, {}, "proxy/auto"),
            (True, {"extractor_args": {"youtube": {"player_client": ["web"]}}}, "proxy/web"),
        ]

        best_info = None
        best_count = 0
        best_source = ""
        bot_detected = False

        for use_proxy, strategy, name in strategies:
            opts = self._build_opts(use_proxy=use_proxy, strategy=strategy)
            info = self._extract_with_opts(url, opts)
            if info:
                raw_formats = info.get("formats", [])
                video_count = sum(
                    1 for f in raw_formats
                    if f.get("vcodec") not in (None, "none") and f.get("url")
                )
                logger.info("Strategy %s: %s video formats", name, video_count)
                if video_count > best_count:
                    best_count = video_count
                    best_info = info
                    best_source = name
                if video_count >= 3:
                    break  # Good enough, stop hammering YouTube
            else:
                bot_detected = True
            # Small random delay between strategies to avoid rate limiting
            time.sleep(random.uniform(0.5, 1.5))

        # If bot detection happened and we got poor results, try once more with fresh cookies
        if bot_detected and best_count < 2 and ("youtube.com" in url or "youtu.be" in url):
            logger.info("Bot detected — refreshing cookies and retrying...")
            try:
                loop = asyncio.get_event_loop()
                cookie_service = CookieService()
                loop.run_until_complete(cookie_service.generate_youtube_cookies())
            except Exception as exc:
                logger.warning("Cookie refresh failed: %s", exc)

            # Retry no-proxy with potentially fresh cookies
            for use_proxy, strategy, name in strategies[:2]:
                opts = self._build_opts(use_proxy=use_proxy, strategy=strategy)
                info = self._extract_with_opts(url, opts)
                if info:
                    raw_formats = info.get("formats", [])
                    video_count = sum(
                        1 for f in raw_formats
                        if f.get("vcodec") not in (None, "none") and f.get("url")
                    )
                    logger.info("Retry strategy %s: %s video formats", name, video_count)
                    if video_count > best_count:
                        best_count = video_count
                        best_info = info
                        best_source = name + "-retry"
                    if video_count >= 3:
                        break
                time.sleep(random.uniform(0.5, 1.5))

        if best_info is None:
            raise ValueError("All extraction strategies failed")

        logger.info("Best source: %s with %s video formats", best_source, best_count)
        return best_info

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
        logger.info(
            "Returning %s formats after filtering (from %s raw)",
            len(formats), len(info.get("formats", [])),
        )

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
        - Protocol must be http/https/m3u8/m3u8_native for playback/download
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
            if protocol not in ("http", "https", "m3u8", "m3u8_native"):
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
                    "protocol": "m3u8" if protocol in ("m3u8", "m3u8_native") else protocol,
                }
            )

        valid.sort(key=lambda x: (-x["height"], -x["is_mp4"]))

        result: list[VideoFormat] = []
        for v in valid[: self.max_formats]:
            result.append(
                VideoFormat(
                    quality=v["quality"],
                    format_id=v["format_id"],
                    ext=v["ext"],
                    url=v["url"],
                    protocol=v["protocol"],
                )
            )

        return result
