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
    reference_data_dir: Path = data_dir / "reference"

    models_dir: Path = PROJECT_ROOT / "models"
    candidates_dir: Path = models_dir / "candidates"
    evaluation_dir: Path = models_dir / "evaluation"
    promoted_dir: Path = models_dir / "promoted"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    log_level: str = "INFO"

    # PostgreSQL (read from PG_* env vars / .env; pg_dsn overrides host/port/user/password)
    pg_dsn: str | None = None
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "mktmtm"
    pg_user: str = "postgres"
    pg_password: str = ""
    pg_sslmode: str = "disable"

    # Backtest economics. Statutory rates are pinned in backtest/economics.py;
    # brokerage is broker-specific and dominates the total, so it lives here.
    backtest_brokerage_per_order: float = 20.0

    # Pipeline tuning
    batch_size: int = 1000
    max_workers: int = 4


settings = Settings()
