from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "Video Extractor API"
    debug: bool = False
    allowed_origins: list[str] = ["*"]
    ytdl_timeout: int = 30
    max_formats: int = 10


settings = Settings()
