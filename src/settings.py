from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
INPUT_DIR = BASE_DIR / "Input"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _read_env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name) or os.getenv(name.upper()) or default


def _path_from_env(name: str, default: Path) -> Path:
    raw_value = _read_env(name)
    if not raw_value:
        return default

    candidate = Path(raw_value).expanduser()
    return candidate if candidate.is_absolute() else (BASE_DIR / candidate)


PLAYBOOK_PATH = _path_from_env(
    "PLAYBOOK_PATH",
    INPUT_DIR / "GILEAD_Field_Inquiry_Playbook.json",
)
DATA_DIR = _path_from_env("DATA_DIR", BASE_DIR / "chat")
DB_PATH = _path_from_env("DB_PATH", DATA_DIR / "chat_history.db")
EMBED_CACHE_PATH = _path_from_env("EMBED_CACHE_PATH", DATA_DIR / "embedding_cache.json")


@dataclass
class Settings:
    azure_openai_key: str
    azure_openai_endpoint: str
    embedding_deployment: str
    chat_deployment: str
    api_version: str

    @property
    def can_use_azure(self) -> bool:
        return all(
            [
                self.azure_openai_key,
                self.azure_openai_endpoint,
                self.embedding_deployment,
                self.chat_deployment,
                self.api_version,
            ]
        ) and self.azure_openai_key.lower() != "x"


def load_settings() -> Settings:
    load_dotenv(CONFIG_DIR / ".env")
    load_dotenv(BASE_DIR / ".env")

    return Settings(
        azure_openai_key=_read_env("azure_openai_key", ""),
        azure_openai_endpoint=_read_env("azure_openai_endpoint", ""),
        embedding_deployment=_read_env("embedding_deployment", ""),
        chat_deployment=_read_env("chat_deployment", ""),
        api_version=_read_env("api_version", "2024-02-15-preview"),
    )
