"""
Browser-based video extraction using Playwright.
This bypasses bot detection by using a real headless browser.
"""

import asyncio
import json
import logging
import re
from typing import Any

from app.models.response_models import VideoFormat, VideoInfo

logger = logging.getLogger(__name__)

STEALTH_SCRIPT = """
(() => {
    // Hide webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    
    // Fake chrome
    window.chrome = { runtime: {} };
    
    // Fake plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    
    // Fake languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en']
    });
    
    // Override permissions
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
    );
    
    // Remove Playwright/Automation traces
    delete navigator.__proto__.webdriver;
    
    // Fake canvas fingerprinting noise
    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        if (type === 'image/png' && this.width === 16 && this.height === 16) {
            return 'data:image/png;base64,00';
        }
        return originalToDataURL.apply(this, arguments);
    };
})();
"""


class BrowserVideoService:
    """Extracts video info using a headless browser to bypass bot detection."""

    def __init__(self, timeout: int = 60, max_formats: int = 10):
        self.timeout = timeout
        self.max_formats = max_formats

    async def extract(self, url: str) -> VideoInfo:
        """Extract video metadata by intercepting network requests in a headless browser."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright not installed")

        video_info = {
            "title": None,
            "thumbnail": None,
            "duration": None,
            "formats": [],
        }

        # For YouTube: the proxy IP is often rate-limited/bot-detected.
        # The VPS IP may be cleaner for page extraction.
        is_youtube = "youtube.com" in url or "youtu.be" in url
        use_proxy = not is_youtube

        async with async_playwright() as p:
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--start-maximized",
                "--disable-extensions",
                "--disable-default-apps",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-breakpad",
                "--disable-component-update",
                "--disable-features=TranslateUI",
                "--disable-hang-monitor",
                "--disable-ipc-flooding-protection",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-renderer-backgrounding",
                "--force-color-profile=srgb",
                "--metrics-recording-only",
                "--enable-automation",
                "--password-store=basic",
                "--use-mock-keychain",
            ]

            if use_proxy:
                browser = await p.chromium.launch(
                    headless=True,
                    proxy={
                        "server": "http://31.59.20.176:6754",
                        "username": "exwnzzqh",
                        "password": "ib3jgwgkjyl1",
                    },
                    args=launch_args,
                )
            else:
                browser = await p.chromium.launch(
                    headless=True,
                    args=launch_args,
                )

            context_opts = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "viewport": {"width": 1920, "height": 1080},
                "locale": "en-US",
                "timezone_id": "America/New_York",
                "permissions": ["geolocation"],
                "java_script_enabled": True,
                "bypass_csp": True,
            }
            if use_proxy:
                context_opts["proxy"] = {
                    "server": "http://31.59.20.176:6754",
                    "username": "exwnzzqh",
                    "password": "ib3jgwgkjyl1",
                }

            context = await browser.new_context(**context_opts)

            page = await context.new_page()
            await page.add_init_script(STEALTH_SCRIPT)

            # Intercept network requests to capture video URLs
            video_urls: list[dict[str, Any]] = []

            async def handle_route(route, request):
                url_requested = request.url
                resource_type = request.resource_type
                if resource_type in ["media", "xhr", "fetch"] or any(ext in url_requested for ext in [".mp4", ".webm", ".m3u8", "videoplayback", "googlevideo", ".ts", "mime=video"]):
                    video_urls.append({"url": url_requested, "type": resource_type})
                await route.continue_()

            await page.route("**/*", handle_route)

            try:
                logger.info("Navigating to: %s", url)
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                
                # Wait for content to load
                await asyncio.sleep(8)
                
                # Try scrolling to trigger lazy loading
                await page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(3)

                if "youtube.com" in url or "youtu.be" in url:
                    video_info = await self._extract_youtube(page, video_urls)
                elif "tiktok.com" in url:
                    video_info = await self._extract_tiktok(page, video_urls)
                elif "instagram.com" in url:
                    video_info = await self._extract_instagram(page, video_urls)
                elif "facebook.com" in url:
                    video_info = await self._extract_facebook(page, video_urls)
                else:
                    video_info = await self._extract_generic(page, video_urls)

            except Exception as e:
                logger.error("Browser extraction error: %s", e)
                raise ValueError(f"Browser extraction failed: {e}")
            finally:
                # Export cookies so yt-dlp can reuse them
                try:
                    cookies = await context.cookies()
                    if cookies:
                        lines = [
                            "# Netscape HTTP Cookie File",
                            "# This file was generated by Playwright",
                        ]
                        for c in cookies:
                            domain = c.get("domain", "")
                            flag = "TRUE" if domain.startswith(".") else "FALSE"
                            path = c.get("path", "/")
                            secure = "TRUE" if c.get("secure") else "FALSE"
                            expires = c.get("expires", 0)
                            if expires is None or expires == -1:
                                expires = 0
                            else:
                                expires = int(expires)
                            name = c.get("name", "")
                            value = c.get("value", "")
                            lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
                        cookie_file = "/tmp/yt_cookies.txt"
                        with open(cookie_file, "w") as f:
                            f.write("\n".join(lines) + "\n")
                        logger.info("Exported %s cookies to %s for yt-dlp", len(cookies), cookie_file)
                except Exception as ce:
                    logger.warning("Failed to export cookies: %s", ce)
                await browser.close()

        if not video_info.get("title"):
            video_info["title"] = "Unknown Video"

        # Validate and deduplicate formats
        import time
        seen = set()
        unique_formats = []
        for fmt in video_info.get("formats", []):
            key = fmt.url
            if not key or key in seen or "generate_204" in key:
                continue
            # Heuristic: reject obviously fake URLs (expiry too far in future)
            expire_match = __import__('re').search(r'expire[=/](\d{10})', key)
            if expire_match:
                expire_ts = int(expire_match.group(1))
                now = int(time.time())
                if expire_ts > now + 86400 * 30:  # More than 30 days in future = fake
                    logger.warning("Rejecting fake URL with expiry %s", expire_ts)
                    continue
                if expire_ts < now:  # Already expired
                    continue
            # Reject URLs with absurd duration values
            dur_match = __import__('re').search(r'dur[=/](\d+)', key)
            if dur_match:
                dur = int(dur_match.group(1))
                if dur > 36000:  # More than 10 hours = fake
                    continue
            seen.add(key)
            unique_formats.append(fmt)

        video_info["formats"] = unique_formats[: self.max_formats]

        return VideoInfo(
            title=video_info["title"],
            thumbnail=video_info.get("thumbnail"),
            duration=video_info.get("duration"),
            formats=video_info["formats"],
        )

    async def _extract_youtube(self, page, video_urls: list[dict]) -> dict[str, Any]:
        info = {"title": None, "thumbnail": None, "duration": None, "formats": []}

        try:
            info["title"] = await page.title()
        except Exception:
            pass

        try:
            video_id = None
            match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', page.url)
            if match:
                video_id = match.group(1)
            else:
                match = re.search(r'youtu\.be/([a-zA-Z0-9_-]{11})', page.url)
                if match:
                    video_id = match.group(1)
            if video_id:
                info["thumbnail"] = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
        except Exception:
            pass

        # Method 1: Try to get video src directly from video element
        try:
            video_src = await page.evaluate("""
                () => {
                    const v = document.querySelector('video');
                    return v ? (v.src || v.currentSrc) : null;
                }
            """)
            if video_src and "blob:" not in video_src:
                info["formats"].append(VideoFormat(quality="direct", format_id="video", ext="mp4", url=video_src, protocol="https"))
        except Exception:
            pass

        # Method 2: Extract from ytInitialPlayerResponse
        try:
            player_response = await page.evaluate("""
                () => {
                    if (window.ytInitialPlayerResponse) return window.ytInitialPlayerResponse;
                    const scripts = document.querySelectorAll('script');
                    for (const script of scripts) {
                        const text = script.innerText || script.textContent || '';
                        if (text.includes('ytInitialPlayerResponse')) {
                            const match = text.match(/ytInitialPlayerResponse\\s*=\\s*({.+?});\\s*$/m);
                            if (match) return JSON.parse(match[1]);
                        }
                    }
                    return null;
                }
            """)

            if player_response and "streamingData" in player_response:
                streaming_data = player_response["streamingData"]
                formats_data = streaming_data.get("formats", []) + streaming_data.get("adaptiveFormats", [])
                logger.info("YouTube player response: %s formats, %s adaptiveFormats", len(streaming_data.get("formats", [])), len(streaming_data.get("adaptiveFormats", [])))

                direct_count = 0
                cipher_count = 0
                for fmt in formats_data:
                    stream_url = fmt.get("url")
                    if not stream_url and "signatureCipher" in fmt:
                        cipher_count += 1
                        cipher = fmt["signatureCipher"]
                        url_match = re.search(r'url=([^&]+)', cipher)
                        if url_match:
                            stream_url = url_match.group(1).replace('%3A', ':').replace('%2F', '/').replace('%3F', '?').replace('%3D', '=').replace('%26', '&')
                    elif stream_url:
                        direct_count += 1
                    
                    if not stream_url:
                        continue

                    quality = fmt.get("qualityLabel") or fmt.get("quality", "unknown")
                    ext = "mp4"
                    mime = fmt.get("mimeType", "")
                    if "webm" in mime:
                        ext = "webm"

                    info["formats"].append(VideoFormat(
                        quality=quality,
                        format_id=str(fmt.get("itag", "unknown")),
                        ext=ext,
                        url=stream_url,
                        protocol="https",
                    ))

                logger.info("Browser extracted: %s direct URLs, %s signatureCiphers skipped | returning %s formats", direct_count, cipher_count, len(info["formats"]))

                if player_response.get("videoDetails", {}).get("lengthSeconds"):
                    info["duration"] = int(player_response["videoDetails"]["lengthSeconds"])
        except Exception as e:
            logger.warning("Player response extraction failed: %s", e)

        # Method 3: Use intercepted network URLs
        if not info["formats"]:
            for vu in video_urls:
                u = vu["url"]
                if "googlevideo" in u and "generate_204" not in u:
                    info["formats"].append(VideoFormat(
                        quality="best", format_id="network", ext="mp4", url=u, protocol="https"
                    ))

        return info

    async def _extract_tiktok(self, page, video_urls: list[dict]) -> dict[str, Any]:
        info = {"title": None, "thumbnail": None, "duration": None, "formats": []}
        try:
            info["title"] = await page.title()
        except Exception:
            pass

        # Try SSR data
        try:
            ssr_data = await page.evaluate("""
                () => {
                    const el = document.getElementById('SIGI_STATE');
                    return el ? JSON.parse(el.innerText) : null;
                }
            """)
            if ssr_data and "ItemModule" in ssr_data:
                items = list(ssr_data["ItemModule"].values())
                if items:
                    item = items[0]
                    info["title"] = item.get("desc") or info["title"]
                    info["thumbnail"] = item.get("cover") or item.get("originCover")
                    info["duration"] = item.get("video", {}).get("duration")
                    play_addr = item.get("video", {}).get("playAddr", "")
                    if play_addr and "playback1" not in play_addr:
                        info["formats"].append(VideoFormat(quality="HD", format_id="0", ext="mp4", url=play_addr, protocol="https"))
        except Exception:
            pass

        # Try video element
        try:
            video_src = await page.evaluate("document.querySelector('video')?.src")
            if video_src and "playback1" not in video_src:
                info["formats"].append(VideoFormat(quality="HD", format_id="video", ext="mp4", url=video_src, protocol="https"))
        except Exception:
            pass

        # Network intercepts
        for vu in video_urls:
            u = vu["url"]
            if ".mp4" in u and "tiktok" in u and "playback1" not in u:
                info["formats"].append(VideoFormat(quality="HD", format_id="network", ext="mp4", url=u, protocol="https"))

        return info

    async def _extract_instagram(self, page, video_urls: list[dict]) -> dict[str, Any]:
        info = {"title": None, "thumbnail": None, "duration": None, "formats": []}
        try:
            info["title"] = await page.title()
        except Exception:
            pass

        # Try video element
        try:
            video_src = await page.evaluate("document.querySelector('video')?.src")
            if video_src:
                info["formats"].append(VideoFormat(quality="HD", format_id="0", ext="mp4", url=video_src, protocol="https"))
        except Exception:
            pass

        # Try meta tags
        try:
            meta_video = await page.evaluate("""
                () => {
                    const meta = document.querySelector('meta[property=\"og:video\"]');
                    return meta ? meta.content : null;
                }
            """)
            if meta_video:
                info["formats"].append(VideoFormat(quality="HD", format_id="meta", ext="mp4", url=meta_video, protocol="https"))
            meta_thumb = await page.evaluate("""
                () => {
                    const meta = document.querySelector('meta[property=\"og:image\"]');
                    return meta ? meta.content : null;
                }
            """)
            if meta_thumb:
                info["thumbnail"] = meta_thumb
        except Exception:
            pass

        # Network intercepts
        for vu in video_urls:
            u = vu["url"]
            if ".mp4" in u and "cdninstagram" in u:
                info["formats"].append(VideoFormat(quality="HD", format_id="network", ext="mp4", url=u, protocol="https"))

        return info

    async def _extract_facebook(self, page, video_urls: list[dict]) -> dict[str, Any]:
        info = {"title": None, "thumbnail": None, "duration": None, "formats": []}
        try:
            info["title"] = await page.title()
        except Exception:
            pass

        # Try video element
        try:
            video_src = await page.evaluate("document.querySelector('video')?.src")
            if video_src:
                info["formats"].append(VideoFormat(quality="HD", format_id="0", ext="mp4", url=video_src, protocol="https"))
        except Exception:
            pass

        # Try meta tags
        try:
            meta_video = await page.evaluate("""
                () => {
                    const meta = document.querySelector('meta[property=\"og:video\"]');
                    return meta ? meta.content : null;
                }
            """)
            if meta_video:
                info["formats"].append(VideoFormat(quality="HD", format_id="meta", ext="mp4", url=meta_video, protocol="https"))
        except Exception:
            pass

        # Network intercepts
        for vu in video_urls:
            u = vu["url"]
            if ".mp4" in u and ("fbcdn" in u or "facebook" in u):
                info["formats"].append(VideoFormat(quality="HD", format_id="network", ext="mp4", url=u, protocol="https"))

        return info

    async def _extract_generic(self, page, video_urls: list[dict]) -> dict[str, Any]:
        info = {"title": None, "thumbnail": None, "duration": None, "formats": []}
        try:
            info["title"] = await page.title()
        except Exception:
            pass

        # Try video element
        try:
            video_src = await page.evaluate("document.querySelector('video')?.src")
            if video_src and "blob:" not in video_src:
                info["formats"].append(VideoFormat(quality="HD", format_id="0", ext="mp4", url=video_src, protocol="https"))
        except Exception:
            pass

        # Try meta tags
        try:
            meta_video = await page.evaluate("""
                () => {
                    const meta = document.querySelector('meta[property=\"og:video\"]');
                    return meta ? meta.content : null;
                }
            """)
            if meta_video:
                info["formats"].append(VideoFormat(quality="HD", format_id="meta", ext="mp4", url=meta_video, protocol="https"))
            meta_thumb = await page.evaluate("""
                () => {
                    const meta = document.querySelector('meta[property=\"og:image\"]');
                    return meta ? meta.content : null;
                }
            """)
            if meta_thumb:
                info["thumbnail"] = meta_thumb
        except Exception:
            pass

        for vu in video_urls:
            u = vu["url"]
            if any(ext in u for ext in [".mp4", ".webm", ".m3u8"]):
                info["formats"].append(VideoFormat(quality="HD", format_id="network", ext="mp4", url=u, protocol="https"))

        return info
