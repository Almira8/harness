from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    openai_api_key: str = ""
    openai_model: str = "gpt-6.2-astra"
    transcription_model: str = "whisper-1"
    transcription_language: str = "ru"
    heygen_api_key: str = ""
    heygen_avatar_id: str = ""
    heygen_voice_id: str = ""
    heygen_engine: str = "avatar_iv"
    metricool_token: str = ""
    metricool_user_id: str = ""
    metricool_blog_id: str = "8265789"
    database_url: str = "sqlite:///data/harness.db"
    data_dir: Path = ROOT / "data"
    media_base_url: str = "http://127.0.0.1:8000"
    pipeline_workspace: Path = ROOT / "workspace"
    generate_backgrounds: bool = False
    background_image_model: str = "gpt-image-1"
    max_upload_mb: int = 500


settings = Settings()
settings.data_dir = settings.data_dir.resolve()
for directory in ("uploads", "renders", "work"):
    (settings.data_dir / directory).mkdir(parents=True, exist_ok=True)
