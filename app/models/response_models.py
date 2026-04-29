from pydantic import BaseModel
from typing import Optional


class VideoFormat(BaseModel):
    quality: str
    format_id: str
    ext: str
    url: str


class VideoInfo(BaseModel):
    title: str
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    formats: list[VideoFormat]
