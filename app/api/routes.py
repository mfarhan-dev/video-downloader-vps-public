import asyncio
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.models.response_models import VideoInfo
from app.services.browser_service import BrowserVideoService
from app.services.video_service import VideoService

router = APIRouter()
logger = logging.getLogger(__name__)

video_service = VideoService()
browser_service = BrowserVideoService()


@router.get("/extract", response_model=VideoInfo)
async def extract_video(
    url: str = Query(..., description="Video URL to extract metadata from"),
) -> VideoInfo:
    """
    Extract video metadata and direct stream URLs.

    - **url**: Valid HTTP/HTTPS URL of the video
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Invalid URL. Must start with http:// or https://",
        )

    # Try yt-dlp first (fastest)
    info = None
    try:
        info = await video_service.extract(url)
    except ValueError as exc:
        error_msg = str(exc).lower()
        # Any yt-dlp failure on YouTube should try browser fallback
        # (proxy IPs often get rate-limited / bot-detected)
        if any(x in error_msg for x in ["bot", "sign in", "blocked", "unsupported", "confirm", "failed", "all extraction strategies"]):
            logger.info("yt-dlp failed, trying browser fallback for: %s | error: %s", url, exc)
        else:
            logger.warning("Extraction error for URL %s: %s", url, exc)
            raise HTTPException(status_code=400, detail=str(exc))
    except TimeoutError:
        logger.warning("Extraction timeout for URL %s", url)
        raise HTTPException(
            status_code=504,
            detail="Extraction timed out. Please try again later.",
        )
    except Exception:
        logger.exception("Unexpected error during extraction for URL %s", url)

    # Fallback to browser-based extraction (loads page and saves cookies)
    if not info or not info.formats:
        try:
            browser_info = await browser_service.extract(url)
            if browser_info and browser_info.formats:
                info = browser_info
        except RuntimeError as exc:
            logger.error("Browser service not available: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc))
        except ValueError as exc:
            logger.warning("Browser extraction error for URL %s: %s", url, exc)
            raise HTTPException(status_code=400, detail=str(exc))
        except TimeoutError:
            logger.warning("Browser extraction timeout for URL %s", url)
            raise HTTPException(
                status_code=504,
                detail="Browser extraction timed out. Please try again later.",
            )
        except Exception:
            logger.exception("Unexpected browser error during extraction for URL %s", url)
            raise HTTPException(status_code=500, detail="Internal server error")

        # Browser saved cookies — retry yt-dlp for ALL formats
        if info and info.formats:
            try:
                logger.info("Retrying yt-dlp with browser cookies for: %s", url)
                ytdl_info = await video_service.extract(url)
                if ytdl_info and ytdl_info.formats:
                    info = ytdl_info
            except Exception as exc:
                logger.info("yt-dlp with cookies failed, keeping browser result: %s", exc)

    if not info or not info.formats:
        raise HTTPException(status_code=404, detail="No formats available for this video")

    return info


async def _stream_https(url: str, headers: dict):
    """Stream a direct HTTPS video URL. Tries without proxy first, then with proxy."""
    proxy = "http://exwnzzqh:ib3jgwgkjyl1@31.59.20.176:6754"

    # Try without proxy first (extracted URLs are signed and often work directly)
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers, timeout=120) as response:
                if response.status_code < 400:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        yield chunk
                    return
                logger.info("Direct download returned %s, trying proxy", response.status_code)
    except Exception as exc:
        logger.info("Direct download failed, trying proxy: %s", exc)

    # Fallback to proxy
    async with httpx.AsyncClient(follow_redirects=True, proxy=proxy) as client:
        async with client.stream("GET", url, headers=headers, timeout=120) as response:
            if response.status_code >= 400:
                logger.error("Upstream returned %s for %s", response.status_code, url)
                raise HTTPException(
                    status_code=502,
                    detail=f"Upstream video source returned {response.status_code}",
                )
            async for chunk in response.aiter_bytes(chunk_size=8192):
                yield chunk


async def _stream_m3u8_via_ytdl(video_url: str, format_id: str):
    """Download an HLS/m3u8 stream using yt-dlp and yield chunks."""
    proxy = "http://exwnzzqh:ib3jgwgkjyl1@31.59.20.176:6754"
    cmd = [
        "yt-dlp",
        "--proxy", proxy,
        "-f", format_id,
        "-o", "-",           # output to stdout
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--compat-options", "no-live-chat",
        video_url,
    ]

    # Add cookies if available
    for cookies_path in ("/tmp/yt_cookies.txt", "/app/cookies.txt"):
        if os.path.exists(cookies_path):
            cmd.extend(["--cookies", cookies_path])
            break

    logger.info("Streaming m3u8 via yt-dlp: %s | format: %s", video_url, format_id)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            yield chunk
    except Exception as exc:
        logger.error("Error streaming from yt-dlp: %s", exc)
        proc.kill()
        raise
    finally:
        if proc.returncode is None:
            proc.kill()

    await proc.wait()
    if proc.returncode != 0:
        stderr = await proc.stderr.read()
        logger.error("yt-dlp download failed (code %s): %s", proc.returncode, stderr.decode()[:500])
        raise HTTPException(
            status_code=502,
            detail="Failed to download HLS stream",
        )


@router.get("/debug-extract")
async def debug_extract(url: str = Query(...)) -> dict:
    """Debug endpoint to test extraction without proxy."""
    import yt_dlp
    import traceback

    results = []
    strategies = [
        ("no-proxy/auto", {}),
        ("no-proxy/web", {"extractor_args": {"youtube": {"player_client": ["web"]}}}),
        ("proxy/auto", {"proxy": "http://exwnzzqh:ib3jgwgkjyl1@31.59.20.176:6754"}),
    ]

    for name, strategy in strategies:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "js_runtimes": {"node": {}},
            **strategy,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                formats = info.get("formats", [])
                video_count = sum(1 for f in formats if f.get("vcodec") not in (None, "none"))
                results.append({
                    "strategy": name,
                    "status": "ok",
                    "title": info.get("title"),
                    "total_formats": len(formats),
                    "video_formats": video_count,
                })
        except Exception as exc:
            results.append({
                "strategy": name,
                "status": "error",
                "error": str(exc),
                "traceback": traceback.format_exc().splitlines()[-3:],
            })

    return {"url": url, "results": results}


@router.get("/download")
async def download_video(
    url: str = Query(..., description="Video URL to download"),
    format_id: str = Query(..., description="Format ID to download"),
) -> StreamingResponse:
    """
    Proxy-download a video format.

    Re-extracts fresh URLs on the server and streams the content back to the client.
    Supports both direct MP4 and HLS/m3u8 streams.
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Invalid URL. Must start with http:// or https://",
        )

    info = None

    # Retry extraction up to 2 times (proxy can be flaky)
    for attempt in range(2):
        # Try yt-dlp first
        try:
            info = await video_service.extract(url)
        except Exception as exc:
            logger.info("yt-dlp extraction failed for download (attempt %s): %s", attempt + 1, exc)

        # Fallback to browser (saves cookies for yt-dlp)
        if not info or not info.formats:
            try:
                browser_info = await browser_service.extract(url)
                if browser_info and browser_info.formats:
                    info = browser_info
                    # Retry yt-dlp with fresh cookies for ALL formats
                    try:
                        logger.info("Retrying yt-dlp with browser cookies for download")
                        ytdl_info = await video_service.extract(url)
                        if ytdl_info and ytdl_info.formats:
                            info = ytdl_info
                    except Exception as exc2:
                        logger.info("yt-dlp with cookies failed for download: %s", exc2)
            except RuntimeError as exc:
                logger.error("Browser service not available: %s", exc)
                raise HTTPException(status_code=503, detail=str(exc))
            except Exception as exc:
                logger.warning("Browser extraction failed for download (attempt %s): %s", attempt + 1, exc)

        if info and info.formats:
            break

        if attempt == 0:
            logger.info("Retrying extraction in 3 seconds...")
            await asyncio.sleep(3)

    if not info or not info.formats:
        raise HTTPException(status_code=404, detail="No download formats available for this video")

    # Find requested format, or default to first available
    fmt = next((f for f in info.formats if f.format_id == format_id), None)
    if not fmt:
        fmt = info.formats[0]

    logger.info(
        "Proxying download for: %s | format: %s | quality: %s | protocol: %s",
        url, fmt.format_id, fmt.quality, fmt.protocol,
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.youtube.com/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    if fmt.protocol == "m3u8":
        # HLS streams need yt-dlp to download
        stream_gen = _stream_m3u8_via_ytdl(url, fmt.format_id)
        media_type = "video/mp4"
    else:
        # Direct MP4/WebM streams
        stream_gen = _stream_https(fmt.url, headers)
        media_type = "video/mp4"

    # Sanitize filename
    safe_title = "".join(c for c in (info.title or "video") if c.isalnum() or c in " ._-").strip()
    filename = f"{safe_title}_{fmt.quality}.{fmt.ext}"

    return StreamingResponse(
        stream_gen,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
