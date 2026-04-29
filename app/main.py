import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api.routes import router
from app.core.config import settings
from app.services.cookie_service import CookieService, periodic_cookie_refresh

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.on_event("startup")
async def startup_event():
    """Generate cookies on startup and start periodic refresh."""
    logger = logging.getLogger(__name__)
    logger.info("App starting up — generating initial cookies...")
    try:
        service = CookieService()
        path = await service.generate_youtube_cookies()
        if path:
            logger.info("Initial cookies generated at %s", path)
        else:
            logger.warning("Initial cookie generation returned no cookies")
    except Exception:
        logger.exception("Initial cookie generation failed")

    # Start background periodic refresh (every 30 min)
    asyncio.create_task(periodic_cookie_refresh(interval_seconds=1800))
    logger.info("Periodic cookie refresh scheduled every 30 minutes")


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "static" / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    return {"status": "healthy"}
