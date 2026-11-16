from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    trader_name: str
    portfolio_id: str
    binance_base_url: str
    binance_referer: str
    binance_timeout_seconds: float

    poll_interval_seconds: float
    page_size: int
    lookback_days: int
    max_catchup_pages: int

    database_path: str
    keep_event_days: int
    keep_event_count: int

    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password: str
    mail_from: str
    mail_to: str
    mail_subject_prefix: str

    api_host: str
    api_port: int
    status_api_token: str
    public_base_url: str

    log_level: str
    timezone: str

    @property
    def email_enabled(self) -> bool:
        return bool(
            self.smtp_host
            and self.mail_from
            and self.mail_to
        )

    @classmethod
    def from_env(cls) -> "Settings":
        portfolio_id = os.getenv(
            "PORTFOLIO_ID", "5075281354358777856"
        ).strip()

        default_referer = (
            "https://www.binance.com/zh-CN/copy-trading/lead-details/"
            f"{portfolio_id}?timeRange=30D"
        )

        return cls(
            trader_name=os.getenv("TRADER_NAME", "熬鹰资本").strip(),
            portfolio_id=portfolio_id,
            binance_base_url=os.getenv(
                "BINANCE_BASE_URL",
                "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade",
            ).rstrip("/"),
            binance_referer=os.getenv(
                "BINANCE_REFERER", default_referer
            ).strip(),
            binance_timeout_seconds=max(
                3.0, _float("BINANCE_TIMEOUT_SECONDS", 15.0)
            ),
            poll_interval_seconds=max(
                3.0, _float("POLL_INTERVAL_SECONDS", 5.0)
            ),
            page_size=min(20, max(1, _int("PAGE_SIZE", 10))),
            lookback_days=max(1, _int("LOOKBACK_DAYS", 30)),
            max_catchup_pages=min(
                10, max(0, _int("MAX_CATCHUP_PAGES", 3))
            ),
            database_path=os.getenv(
                "DATABASE_PATH", "./data/monitor.db"
            ).strip(),
            keep_event_days=max(1, _int("KEEP_EVENT_DAYS", 30)),
            keep_event_count=max(50, _int("KEEP_EVENT_COUNT", 500)),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=_int("SMTP_PORT", 465),
            smtp_security=os.getenv(
                "SMTP_SECURITY", "ssl"
            ).strip().lower(),
            smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            mail_from=os.getenv("MAIL_FROM", "").strip(),
            mail_to=os.getenv("MAIL_TO", "").strip(),
            mail_subject_prefix=os.getenv(
                "MAIL_SUBJECT_PREFIX", "[带单监控]"
            ).strip(),
            api_host=os.getenv("API_HOST", "127.0.0.1").strip(),
            api_port=_int("API_PORT", 8765),
            status_api_token=os.getenv(
                "STATUS_API_TOKEN", ""
            ).strip(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            timezone=os.getenv("TIMEZONE", "Asia/Shanghai").strip(),
        )
