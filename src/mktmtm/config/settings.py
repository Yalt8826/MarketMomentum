from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"

    data_dir: Path = PROJECT_ROOT / "data"
    raw_data_dir: Path = data_dir / "raw"
    processed_data_dir: Path = data_dir / "processed"
    serving_data_dir: Path = data_dir / "serving"

    models_dir: Path = PROJECT_ROOT / "models"
    candidates_dir: Path = models_dir / "candidates"
    evaluation_dir: Path = models_dir / "evaluation"
    promoted_dir: Path = models_dir / "promoted"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    log_level: str = "INFO"


settings = Settings()
