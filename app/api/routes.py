import logging

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
    try:
        return await video_service.extract(url)
    except ValueError as exc:
        error_msg = str(exc).lower()
        # If bot detection or unsupported, try browser fallback
        if any(x in error_msg for x in ["bot", "sign in", "blocked", "unsupported", "confirm"]):
            logger.info("yt-dlp blocked, trying browser fallback for: %s", url)
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

    # Fallback to browser-based extraction
    try:
        return await browser_service.extract(url)
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


@router.get("/download")
async def download_video(
    url: str = Query(..., description="Video URL to download"),
    format_id: str = Query(..., description="Format ID to download"),
) -> StreamingResponse:
    """
    Proxy-download a video format.

    Re-extracts fresh URLs on the server and streams the content back to the client.
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

        # Fallback to browser
        if not info or not info.formats:
            try:
                info = await browser_service.extract(url)
            except RuntimeError as exc:
                logger.error("Browser service not available: %s", exc)
                raise HTTPException(status_code=503, detail=str(exc))
            except Exception as exc:
                logger.warning("Browser extraction failed for download (attempt %s): %s", attempt + 1, exc)

        if info and info.formats:
            break

        if attempt == 0:
            logger.info("Retrying extraction in 3 seconds...")
            import asyncio
            await asyncio.sleep(3)

    if not info or not info.formats:
        raise HTTPException(status_code=404, detail="No download formats available for this video")

    # Find requested format, or default to first available
    fmt = next((f for f in info.formats if f.format_id == format_id), None)
    if not fmt:
        fmt = info.formats[0]

    logger.info("Proxying download for: %s | format: %s | quality: %s", url, fmt.format_id, fmt.quality)

    async def stream_generator():
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
        async with httpx.AsyncClient(
            follow_redirects=True,
            proxy="http://exwnzzqh:ib3jgwgkjyl1@31.59.20.176:6754",
        ) as client:
            async with client.stream("GET", fmt.url, headers=headers, timeout=120) as response:
                if response.status_code >= 400:
                    logger.error("Upstream returned %s for %s", response.status_code, fmt.url)
                    raise HTTPException(
                        status_code=502,
                        detail=f"Upstream video source returned {response.status_code}",
                    )
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    yield chunk

    # Sanitize filename
    safe_title = "".join(c for c in (info.title or "video") if c.isalnum() or c in " ._-").strip()
    filename = f"{safe_title}_{fmt.quality}.{fmt.ext}"

    return StreamingResponse(
        stream_generator(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
