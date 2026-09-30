"""Isolated local-demo settings. Production environment variables are not read."""
from dataclasses import dataclass, field
from pathlib import Path
import secrets

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "runtime"


@dataclass(frozen=True)
class Settings:
    DATABASE_URL: str = f"sqlite+aiosqlite:///{RUNTIME_DIR / 'expense_demo.sqlite3'}"
    SECRET_KEY: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    APPROVAL_CODES: str = ""
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    AIRWALLEX_CLIENT_ID: str = ""
    AIRWALLEX_API_KEY: str = ""
    VOUCHER_FILE_ROOT: str = str(RUNTIME_DIR / "voucher_files")
    VOUCHER_MAX_FILE_SIZE: int = 20 * 1024 * 1024
    VOUCHER_FONT_PATH: str = ""
    OFFLINE_DEMO: bool = True

    @property
    def approval_code_list(self) -> list[str]:
        return []


settings = Settings()
