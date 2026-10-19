"""Settings loaded from environment variables (or the repo-root .env file).

Secrets never live in code: EIA_API_KEY and JWT_SECRET come only from the environment.
"""

from datetime import date
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
    eia_timeout_seconds: float = 180.0  # facility/generator pages take ~55s on a good day
    eia_max_retries: int = 5
    eia_max_concurrency: int = 3  # parallel requests; kept low to stay well inside EIA rate limits
    eia_start_date: date = date(2007, 1, 1)  # startPeriod reported by the route metadata
    refresh_lookback_days: int = 30  # incremental runs re-fetch this many days before the watermark
    data_dir: Path = Path("./data")
    # Signs the API's access tokens. The app refuses to start without one (api/auth.py).
    jwt_secret: SecretStr = SecretStr("")
    jwt_ttl_minutes: int = 60
    login_max_failures: int = 5  # per client and username within the window (0 disables)
    login_max_failures_per_client: int = 20  # per client across all usernames (0 disables)
    login_window_seconds: float = 300.0
    query_timeout_seconds: float = 10.0  # user SQL is interrupted after this long
    query_cache_ttl_seconds: float = 300.0  # 0 disables the query result cache
    query_cache_max_rows: int = 200_000  # rows kept across all cached results (memory bound)

    @property
    def data_path(self) -> Path:
        """DATA_DIR resolved against the repo root, so it doesn't depend on the current directory."""
        return self.data_dir if self.data_dir.is_absolute() else (REPO_ROOT / self.data_dir).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
