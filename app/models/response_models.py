from pydantic import BaseModel
from typing import Optional


class VideoFormat(BaseModel):
    quality: str
    format_id: str
    ext: str
    url: str
    protocol: str = "https"  # "https", "http", or "m3u8"


class VideoInfo(BaseModel):
    title: str
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    formats: list[VideoFormat]
