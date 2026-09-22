from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # System
    APP_NAME: str = "JALWE AI TRADER V4"
    ENVIRONMENT: str = os.getenv("JALWE_ENVIRONMENT", "development")
    LOG_LEVEL: str = os.getenv("JALWE_LOG_LEVEL", "INFO")

    # Paper Trading only
    PAPER_TRADING: bool = _get_bool("JALWE_PAPER_TRADING", True)
    ALLOW_LIVE_TRADING: bool = False

    # Alpaca
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
    ALPACA_BASE_URL: str = os.getenv(
        "ALPACA_BASE_URL",
        "https://paper-api.alpaca.markets",
    )
    ALPACA_DATA_FEED: str = os.getenv("ALPACA_DATA_FEED", "iex")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Database
    DATABASE_PATH: str = os.getenv(
        "JALWE_DATABASE_PATH",
        str(BASE_DIR / "data" / "jalwe_v4.db"),
    )

    # Market Scanner
    MIN_STOCK_PRICE: float = _get_float("MIN_STOCK_PRICE", 0.50)
    MAX_STOCK_PRICE: float = _get_float("MAX_STOCK_PRICE", 100.00)
    MIN_DOLLAR_VOLUME: float = _get_float(
        "MIN_DOLLAR_VOLUME",
        1_000_000,
    )
    MAX_SPREAD_PCT: float = _get_float("MAX_SPREAD_PCT", 1.0)

    # Signal
    MIN_SIGNAL_SCORE: float = _get_float("MIN_SIGNAL_SCORE", 82.0)
    MIN_RVOL: float = _get_float("MIN_RVOL", 1.30)
    STRONG_RVOL: float = _get_float("STRONG_RVOL", 2.00)

    # Risk
    RISK_PER_TRADE_PCT: float = _get_float(
        "RISK_PER_TRADE_PCT",
        0.50,
    )
    MAX_DAILY_LOSS_PCT: float = _get_float(
        "MAX_DAILY_LOSS_PCT",
        4.0,
    )
    MAX_OPEN_POSITIONS: int = _get_int(
        "MAX_OPEN_POSITIONS",
        3,
    )
    MAX_POSITION_ALLOCATION_PCT: float = _get_float(
        "MAX_POSITION_ALLOCATION_PCT",
        20.0,
    )
    MAX_PORTFOLIO_EXPOSURE_PCT: float = _get_float(
        "MAX_PORTFOLIO_EXPOSURE_PCT",
        60.0,
    )

    # Trade Management
    DEFAULT_STOP_ATR_MULTIPLIER: float = _get_float(
        "DEFAULT_STOP_ATR_MULTIPLIER",
        1.5,
    )
    TARGET_1_R: float = _get_float("TARGET_1_R", 1.5)
    TARGET_2_R: float = _get_float("TARGET_2_R", 2.5)
    TARGET_3_R: float = _get_float("TARGET_3_R", 4.0)
    TRAILING_STOP_PCT: float = _get_float(
        "TRAILING_STOP_PCT",
        3.0,
    )

    # Engine intervals
    DISCOVERY_INTERVAL_SECONDS: int = _get_int(
        "DISCOVERY_INTERVAL_SECONDS",
        300,
    )
    TRADE_MONITOR_INTERVAL_SECONDS: int = _get_int(
        "TRADE_MONITOR_INTERVAL_SECONDS",
        30,
    )
    RECONCILIATION_INTERVAL_SECONDS: int = _get_int(
        "RECONCILIATION_INTERVAL_SECONDS",
        30,
    )

    # AI
    AI_ENABLED: bool = _get_bool("AI_ENABLED", True)
    MIN_AI_PROBABILITY: float = _get_float(
        "MIN_AI_PROBABILITY",
        0.65,
    )
    MIN_TRAINING_SAMPLES: int = _get_int(
        "MIN_TRAINING_SAMPLES",
        100,
    )

    # Safety
    BLOCK_ON_STALE_DATA: bool = _get_bool(
        "BLOCK_ON_STALE_DATA",
        True,
    )
    BLOCK_ON_BROKER_ERROR: bool = _get_bool(
        "BLOCK_ON_BROKER_ERROR",
        True,
    )
    BLOCK_ON_UNKNOWN_REGIME: bool = _get_bool(
        "BLOCK_ON_UNKNOWN_REGIME",
        True,
    )


settings = Settings()


def validate_settings() -> None:
    errors: list[str] = []

    # Hard Paper Trading lock
    if not settings.PAPER_TRADING:
        errors.append("JALWE V4 must run in Paper Trading mode.")

    if settings.ALLOW_LIVE_TRADING:
        errors.append("Live trading must remain disabled.")

    if "paper-api.alpaca.markets" not in settings.ALPACA_BASE_URL.lower():
        errors.append(
            "ALPACA_BASE_URL must use Alpaca Paper Trading."
        )

    # Risk validation
    if not 0 < settings.RISK_PER_TRADE_PCT <= 2:
        errors.append(
            "RISK_PER_TRADE_PCT must be greater than 0 and <= 2%."
        )

    if not 0 < settings.MAX_DAILY_LOSS_PCT <= 10:
        errors.append(
            "MAX_DAILY_LOSS_PCT must be greater than 0 and <= 10%."
        )

    if settings.MAX_OPEN_POSITIONS < 1:
        errors.append(
            "MAX_OPEN_POSITIONS must be at least 1."
        )

    if not 0 < settings.MAX_POSITION_ALLOCATION_PCT <= 100:
        errors.append(
            "MAX_POSITION_ALLOCATION_PCT is invalid."
        )

    if not 0 < settings.MAX_PORTFOLIO_EXPOSURE_PCT <= 100:
        errors.append(
            "MAX_PORTFOLIO_EXPOSURE_PCT is invalid."
        )

    # Market validation
    if settings.MIN_STOCK_PRICE <= 0:
        errors.append(
            "MIN_STOCK_PRICE must be greater than zero."
        )

    if settings.MAX_STOCK_PRICE <= settings.MIN_STOCK_PRICE:
        errors.append(
            "MAX_STOCK_PRICE must be greater than MIN_STOCK_PRICE."
        )

    if not 0 <= settings.MIN_SIGNAL_SCORE <= 100:
        errors.append(
            "MIN_SIGNAL_SCORE must be between 0 and 100."
        )

    if not 0 <= settings.MIN_AI_PROBABILITY <= 1:
        errors.append(
            "MIN_AI_PROBABILITY must be between 0 and 1."
        )

    if errors:
        raise RuntimeError(
            "Invalid JALWE V4 configuration:\n"
            + "\n".join(f"- {error}" for error in errors)
        )


def credentials_ready() -> bool:
    return bool(
        settings.ALPACA_API_KEY
        and settings.ALPACA_SECRET_KEY
    )
