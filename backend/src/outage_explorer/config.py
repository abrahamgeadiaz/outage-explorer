"""Settings loaded from environment variables (or the repo-root .env file).

Secrets never live in code: EIA_API_KEY and JWT_SECRET come only from the environment.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/outage_explorer/config.py -> repo root is three levels above the package
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # SecretStr keeps the key out of repr()/logs; call .get_secret_value() only where it is sent.
    eia_api_key: SecretStr = SecretStr("")
    eia_base_url: str = "https://api.eia.gov/v2/nuclear-outages"
    data_dir: Path = Path("./data")

    @property
    def data_path(self) -> Path:
        """DATA_DIR resolved against the repo root, so it doesn't depend on the current directory."""
        return self.data_dir if self.data_dir.is_absolute() else (REPO_ROOT / self.data_dir).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
