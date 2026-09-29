from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")

    # Stateless API: no owned database. Optional HF token for NER models only.
    preflight_max_sample_pages: int = 15
    preflight_http_timeout_seconds: float = 10.0
    preflight_browser_timeout_seconds: float = 20.0
    preflight_report_ttl_hours: int = 24

    crawler_user_agent: str = "WebIntelBot/0.1 (+https://example.com/bot)"

    block_private_networks: bool = True
    allowed_schemes: str = "http,https"

    crawl_default_max_pages: int = 100
    crawl_default_max_depth: int = 3
    crawl_default_concurrency: int = 5
    crawl_max_response_bytes: int = 20_000_000
    crawl_worker_poll_timeout_seconds: float = 5.0
    crawl_max_browser_pages: int = 25
    crawl_max_browser_seconds_per_page: float = 20.0
    crawl_max_discovered_urls: int = 5000
    crawl_max_pagination_pages: int = 50
    crawl_max_scrolls: int = 5
    crawl_max_scroll_new_items: int = 200
    crawl_max_items_per_index: int = 50
    crawl_max_concurrent_browser_global: int = 8

    # Hugging Face: used for first-time GLiNER (and optional IndicNER) model
    # download. After cache is warm, local files are reused.
    hf_token: str | None = None

    @property
    def allowed_schemes_set(self) -> set[str]:
        return {s.strip().lower() for s in self.allowed_schemes.split(",") if s.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def apply_hf_token_to_environ() -> None:
    """Push Settings.hf_token into process env so huggingface_hub /
    transformers nested loads authenticate with the .env value."""
    import os

    token = get_settings().hf_token
    if not token:
        return
    os.environ["HF_TOKEN"] = token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = token
