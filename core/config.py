from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# ============================================================
# PATHS
# ============================================================

BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)

ENV_FILE = BASE_DIR / ".env"

load_dotenv(
    ENV_FILE
)


# ============================================================
# ENV HELPERS
# ============================================================

def _get_bool(
    name: str,
    default: bool = False,
) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return (
        value
        .strip()
        .lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )


def _get_int(
    name: str,
    default: int,
) -> int:
    try:
        return int(
            os.getenv(
                name,
                str(default),
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return default


def _get_float(
    name: str,
    default: float,
) -> float:
    try:
        return float(
            os.getenv(
                name,
                str(default),
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return default


def _get_str(
    name: str,
    default: str = "",
) -> str:
    return str(
        os.getenv(
            name,
            default,
        )
        or ""
    ).strip()


# ============================================================
# SECRET / PLACEHOLDER SAFETY
# ============================================================

_PLACEHOLDER_WORDS = {
    "",
    "none",
    "null",
    "changeme",
    "change_me",
    "your_key",
    "your_secret",
    "your_token",
    "keep_your_existing_api_key",
    "keep_your_existing_secret_key",
    "keep_your_existing_telegram_token",
}


def _looks_like_real_secret(
    value: str,
) -> bool:
    value = str(
        value or ""
    ).strip()

    if not value:
        return False

    lower_value = (
        value.lower()
    )

    if lower_value in _PLACEHOLDER_WORDS:
        return False

    if (
        "حط" in value
        or "ضع" in value
        or "<<<" in value
        or ">>>" in value
    ):
        return False

    return True


# ============================================================
# SETTINGS
# ============================================================

@dataclass(frozen=True)
class Settings:

    # ========================================================
    # SYSTEM
    # ========================================================

    APP_NAME: str = (
        "JALWE AI TRADER V4"
    )

    ENVIRONMENT: str = (
        _get_str(
            "JALWE_ENVIRONMENT",
            "development",
        )
    )

    LOG_LEVEL: str = (
        _get_str(
            "JALWE_LOG_LEVEL",
            "INFO",
        )
    )

    # ========================================================
    # PAPER TRADING HARD LOCK
    # ========================================================

    PAPER_TRADING: bool = (
        _get_bool(
            "JALWE_PAPER_TRADING",
            True,
        )
    )

    # HARD CODED.
    # Never controlled from .env.
    ALLOW_LIVE_TRADING: bool = False

    # ========================================================
    # PAPER AUTO EXECUTION LOCKS
    # ========================================================

    AUTO_PAPER_EXECUTION: bool = (
        _get_bool(
            "JALWE_AUTO_PAPER_EXECUTION",
            _get_bool(
                "AUTO_PAPER_EXECUTION",
                False,
            ),
        )
    )

    BROKER_SUBMISSION_ENABLED: bool = (
        _get_bool(
            "JALWE_BROKER_SUBMISSION",
            _get_bool(
                "BROKER_SUBMISSION_ENABLED",
                False,
            ),
        )
    )

    # ========================================================
    # SMALL ACCOUNT
    # ========================================================

    STRATEGY_STARTING_CAPITAL: float = (
        _get_float(
            "JALWE_STARTING_CAPITAL",
            100.0,
        )
    )

    # Source of truth for position sizing.
    #
    # strategy_wallet (default):
    #   JALWE starts from STRATEGY_STARTING_CAPITAL, compounds
    #   its own realized PnL, and applies NEW cash deposit /
    #   withdrawal adjustments detected after the wallet
    #   baseline is initialized. It never adopts Alpaca's
    #   pre-existing $100k PAPER balance as JALWE capital.
    #
    # virtual:
    #   Starting capital + JALWE realized PnL only.
    CAPITAL_MODE: str = (
        _get_str(
            "JALWE_CAPITAL_MODE",
            "strategy_wallet",
        ).lower()
    )

    # ========================================================
    # ALPACA
    # ========================================================

    ALPACA_API_KEY: str = (
        _get_str(
            "ALPACA_API_KEY",
            "",
        )
    )

    ALPACA_SECRET_KEY: str = (
        _get_str(
            "ALPACA_SECRET_KEY",
            "",
        )
    )

    ALPACA_BASE_URL: str = (
        _get_str(
            "ALPACA_BASE_URL",
            "https://paper-api.alpaca.markets",
        )
    )

    ALPACA_DATA_FEED: str = (
        _get_str(
            "ALPACA_DATA_FEED",
            "iex",
        )
    )

    # ========================================================
    # TELEGRAM
    # ========================================================

    TELEGRAM_BOT_TOKEN: str = (
        _get_str(
            "TELEGRAM_BOT_TOKEN",
            "",
        )
    )

    TELEGRAM_CHAT_ID: str = (
        _get_str(
            "TELEGRAM_CHAT_ID",
            "",
        )
    )

    # ========================================================
    # DATABASE
    # ========================================================

    DATABASE_PATH: str = (
        _get_str(
            "JALWE_DATABASE_PATH",
            str(
                BASE_DIR
                / "data"
                / "jalwe_v4.db"
            ),
        )
    )

    # ========================================================
    # MARKET SCANNER
    # ========================================================

    MIN_STOCK_PRICE: float = (
        _get_float(
            "MIN_STOCK_PRICE",
            0.50,
        )
    )

    MAX_STOCK_PRICE: float = (
        _get_float(
            "MAX_STOCK_PRICE",
            100.00,
        )
    )

    MIN_DOLLAR_VOLUME: float = (
        _get_float(
            "MIN_DOLLAR_VOLUME",
            1_000_000.0,
        )
    )

    MAX_SPREAD_PCT: float = (
        _get_float(
            "MAX_SPREAD_PCT",
            1.0,
        )
    )

    MAX_DISCOVERY_SYMBOLS: int = (
        _get_int(
            "MAX_DISCOVERY_SYMBOLS",
            500,
        )
    )

    MAX_ANALYSIS_CANDIDATES: int = (
        _get_int(
            "MAX_ANALYSIS_CANDIDATES",
            25,
        )
    )

    # ========================================================
    # SIGNAL QUALITY
    # ========================================================

    MIN_SIGNAL_SCORE: float = (
        _get_float(
            "MIN_SIGNAL_SCORE",
            82.0,
        )
    )

    MIN_RVOL: float = (
        _get_float(
            "MIN_RVOL",
            1.30,
        )
    )

    STRONG_RVOL: float = (
        _get_float(
            "STRONG_RVOL",
            2.00,
        )
    )

    # ========================================================
    # SMALL ACCOUNT RISK ENGINE
    # ========================================================

    # Default A setup risk.
    RISK_PER_TRADE_PCT: float = (
        _get_float(
            "RISK_PER_TRADE_PCT",
            1.0,
        )
    )

    MAX_RISK_PER_TRADE_PCT: float = (
        _get_float(
            "MAX_RISK_PER_TRADE_PCT",
            1.5,
        )
    )

    MAX_DAILY_LOSS_PCT: float = (
        _get_float(
            "MAX_DAILY_LOSS_PCT",
            3.0,
        )
    )

    MAX_OPEN_POSITIONS: int = (
        _get_int(
            "MAX_OPEN_POSITIONS",
            1,
        )
    )

    MAX_POSITION_ALLOCATION_PCT: float = (
        _get_float(
            "MAX_POSITION_ALLOCATION_PCT",
            75.0,
        )
    )

    MAX_PORTFOLIO_EXPOSURE_PCT: float = (
        _get_float(
            "MAX_PORTFOLIO_EXPOSURE_PCT",
            75.0,
        )
    )

    # ========================================================
    # SETUP QUALITY RISK
    # ========================================================

    RISK_B_SETUP_PCT: float = (
        _get_float(
            "RISK_B_SETUP_PCT",
            0.5,
        )
    )

    RISK_A_SETUP_PCT: float = (
        _get_float(
            "RISK_A_SETUP_PCT",
            1.0,
        )
    )

    RISK_A_PLUS_SETUP_PCT: float = (
        _get_float(
            "RISK_A_PLUS_SETUP_PCT",
            1.5,
        )
    )

    # ========================================================
    # STOP SYSTEM
    # ========================================================

    DEFAULT_STOP_ATR_MULTIPLIER: float = (
        _get_float(
            "DEFAULT_STOP_ATR_MULTIPLIER",
            1.5,
        )
    )

    # ========================================================
    # TARGET SYSTEM
    # ========================================================

    TARGET_1_R: float = (
        _get_float(
            "TARGET_1_R",
            2.0,
        )
    )

    TARGET_2_R: float = (
        _get_float(
            "TARGET_2_R",
            3.0,
        )
    )

    TARGET_3_R: float = (
        _get_float(
            "TARGET_3_R",
            4.0,
        )
    )

    # ========================================================
    # EXIT ALLOCATION
    # ========================================================

    TARGET_1_EXIT_PCT: float = (
        _get_float(
            "TARGET_1_EXIT_PCT",
            40.0,
        )
    )

    TARGET_2_EXIT_PCT: float = (
        _get_float(
            "TARGET_2_EXIT_PCT",
            30.0,
        )
    )

    TARGET_3_EXIT_PCT: float = (
        _get_float(
            "TARGET_3_EXIT_PCT",
            20.0,
        )
    )

    RUNNER_EXIT_PCT: float = (
        _get_float(
            "RUNNER_EXIT_PCT",
            10.0,
        )
    )

    # ========================================================
    # STOP MOVEMENT
    # ========================================================

    MOVE_STOP_TO_ENTRY_AFTER_T1: bool = (
        _get_bool(
            "MOVE_STOP_TO_ENTRY_AFTER_T1",
            True,
        )
    )

    MOVE_STOP_TO_T1_AFTER_T2: bool = (
        _get_bool(
            "MOVE_STOP_TO_T1_AFTER_T2",
            True,
        )
    )

    MOVE_STOP_TO_T2_AFTER_T3: bool = (
        _get_bool(
            "MOVE_STOP_TO_T2_AFTER_T3",
            True,
        )
    )

    # ========================================================
    # RUNNER / TRAILING
    # ========================================================

    RUNNER_ENABLED: bool = (
        _get_bool(
            "RUNNER_ENABLED",
            True,
        )
    )

    TRAILING_STOP_ENABLED: bool = (
        _get_bool(
            "TRAILING_STOP_ENABLED",
            True,
        )
    )

    TRAILING_STOP_PCT: float = (
        _get_float(
            "TRAILING_STOP_PCT",
            3.0,
        )
    )

    TRAILING_ATR_MULTIPLIER: float = (
        _get_float(
            "TRAILING_ATR_MULTIPLIER",
            1.5,
        )
    )

    # ========================================================
    # OPPORTUNITY ENGINE
    # ========================================================

    MIN_OPPORTUNITY_SCORE: float = (
        _get_float(
            "MIN_OPPORTUNITY_SCORE",
            74.0,
        )
    )

    # ========================================================
    # SESSION STRATEGY ENGINE
    # ========================================================

    MIN_SESSION_STRATEGY_SCORE: float = (
        _get_float(
            "MIN_SESSION_STRATEGY_SCORE",
            75.0,
        )
    )

    # ========================================================
    # BREAKOUT CONFIRMATION
    # ========================================================

    MIN_BREAKOUT_SCORE: float = (
        _get_float(
            "MIN_BREAKOUT_SCORE",
            70.0,
        )
    )

    MIN_BREAKOUT_VOLUME_RATIO: float = (
        _get_float(
            "MIN_BREAKOUT_VOLUME_RATIO",
            1.20,
        )
    )

    MAX_BREAKOUT_UPPER_WICK_RATIO: float = (
        _get_float(
            "MAX_BREAKOUT_UPPER_WICK_RATIO",
            0.35,
        )
    )

    # ========================================================
    # TRIGGER ENGINE
    # ========================================================

    MAX_CHASE_PCT: float = (
        _get_float(
            "MAX_CHASE_PCT",
            1.5,
        )
    )

    WAITING_ZONE_PCT: float = (
        _get_float(
            "WAITING_ZONE_PCT",
            2.0,
        )
    )

    # ========================================================
    # ENGINE INTERVALS
    # ========================================================

    DISCOVERY_INTERVAL_SECONDS: int = (
        _get_int(
            "DISCOVERY_INTERVAL_SECONDS",
            300,
        )
    )

    TRADE_MONITOR_INTERVAL_SECONDS: int = (
        _get_int(
            "TRADE_MONITOR_INTERVAL_SECONDS",
            30,
        )
    )

    RECONCILIATION_INTERVAL_SECONDS: int = (
        _get_int(
            "RECONCILIATION_INTERVAL_SECONDS",
            30,
        )
    )

    # ========================================================
    # AI
    # ========================================================

    AI_ENABLED: bool = (
        _get_bool(
            "AI_ENABLED",
            True,
        )
    )

    MIN_AI_PROBABILITY: float = (
        _get_float(
            "MIN_AI_PROBABILITY",
            0.65,
        )
    )

    MIN_TRAINING_SAMPLES: int = (
        _get_int(
            "MIN_TRAINING_SAMPLES",
            100,
        )
    )

    # ========================================================
    # POSITION SAFETY
    # ========================================================

    BLOCK_DUPLICATE_SYMBOL: bool = (
        _get_bool(
            "BLOCK_DUPLICATE_SYMBOL",
            True,
        )
    )

    BLOCK_IF_POSITION_OPEN: bool = (
        _get_bool(
            "BLOCK_IF_POSITION_OPEN",
            True,
        )
    )

    BLOCK_IF_ENTRY_PENDING: bool = (
        _get_bool(
            "BLOCK_IF_ENTRY_PENDING",
            True,
        )
    )

    BLOCK_IF_RECOVERY_UNRESOLVED: bool = (
        _get_bool(
            "BLOCK_IF_RECOVERY_UNRESOLVED",
            True,
        )
    )

    # ========================================================
    # DATA / BROKER SAFETY
    # ========================================================

    BLOCK_ON_STALE_DATA: bool = (
        _get_bool(
            "BLOCK_ON_STALE_DATA",
            True,
        )
    )

    BLOCK_ON_BROKER_ERROR: bool = (
        _get_bool(
            "BLOCK_ON_BROKER_ERROR",
            True,
        )
    )

    BLOCK_ON_UNKNOWN_REGIME: bool = (
        _get_bool(
            "BLOCK_ON_UNKNOWN_REGIME",
            True,
        )
    )

    # ========================================================
    # CRASH RECOVERY
    # ========================================================

    ENTRY_RECOVERY_ENABLED: bool = (
        _get_bool(
            "ENTRY_RECOVERY_ENABLED",
            True,
        )
    )

    EXIT_RECOVERY_ENABLED: bool = (
        _get_bool(
            "EXIT_RECOVERY_ENABLED",
            True,
        )
    )

    STARTUP_RECONCILIATION_ENABLED: bool = (
        _get_bool(
            "STARTUP_RECONCILIATION_ENABLED",
            True,
        )
    )

    # ========================================================
    # LOGGING
    # ========================================================

    LOG_DECISIONS: bool = (
        _get_bool(
            "LOG_DECISIONS",
            True,
        )
    )

    LOG_ORDERS: bool = (
        _get_bool(
            "LOG_ORDERS",
            True,
        )
    )

    LOG_REJECTIONS: bool = (
        _get_bool(
            "LOG_REJECTIONS",
            True,
        )
    )

    LOG_RECOVERY: bool = (
        _get_bool(
            "LOG_RECOVERY",
            True,
        )
    )


# ============================================================
# GLOBAL SETTINGS INSTANCE
# ============================================================

settings = Settings()


# ============================================================
# CREDENTIAL STATUS
# ============================================================

def credentials_ready() -> bool:
    """
    Alpaca credentials exist and are not obvious placeholders.
    """

    return bool(
        _looks_like_real_secret(
            settings.ALPACA_API_KEY
        )
        and
        _looks_like_real_secret(
            settings.ALPACA_SECRET_KEY
        )
    )


def telegram_credentials_ready() -> bool:
    """
    Telegram Bot Token + Chat ID exist.

    This only validates configuration presence.
    It does not perform a network request.
    """

    return bool(
        _looks_like_real_secret(
            settings.TELEGRAM_BOT_TOKEN
        )
        and
        str(
            settings.TELEGRAM_CHAT_ID
            or ""
        ).strip()
    )


# ============================================================
# VALIDATION
# ============================================================

def validate_settings() -> None:

    errors: list[str] = []

    # ========================================================
    # HARD PAPER LOCK
    # ========================================================

    if not settings.PAPER_TRADING:
        errors.append(
            "JALWE V4 must run in Paper Trading mode."
        )

    if settings.ALLOW_LIVE_TRADING:
        errors.append(
            "Live trading must remain disabled."
        )

    if (
        "paper-api.alpaca.markets"
        not in
        settings.ALPACA_BASE_URL.lower()
    ):
        errors.append(
            "ALPACA_BASE_URL must use Alpaca Paper Trading."
        )

    # ========================================================
    # AUTO EXECUTION LOCKS
    # ========================================================

    if (
        settings.BROKER_SUBMISSION_ENABLED
        and
        not settings.AUTO_PAPER_EXECUTION
    ):
        errors.append(
            "JALWE_BROKER_SUBMISSION=true requires "
            "JALWE_AUTO_PAPER_EXECUTION=true."
        )

    if (
        settings.AUTO_PAPER_EXECUTION
        and
        not settings.PAPER_TRADING
    ):
        errors.append(
            "Automatic execution is allowed "
            "only in Paper Trading mode."
        )

    if (
        settings.BROKER_SUBMISSION_ENABLED
        and
        settings.ALLOW_LIVE_TRADING
    ):
        errors.append(
            "Broker submission cannot run "
            "while live trading is enabled."
        )

    # ========================================================
    # CAPITAL
    # ========================================================

    if (
        settings.STRATEGY_STARTING_CAPITAL
        <= 0
    ):
        errors.append(
            "JALWE_STARTING_CAPITAL must be greater than zero."
        )

    if settings.CAPITAL_MODE not in {
        "strategy_wallet",
        "virtual",
    }:
        errors.append(
            "JALWE_CAPITAL_MODE must be "
            "strategy_wallet or virtual."
        )

    # ========================================================
    # RISK
    # ========================================================

    if not (
        0
        < settings.RISK_PER_TRADE_PCT
        <= settings.MAX_RISK_PER_TRADE_PCT
    ):
        errors.append(
            "RISK_PER_TRADE_PCT must be > 0 and "
            "<= MAX_RISK_PER_TRADE_PCT."
        )

    if not (
        0
        < settings.MAX_RISK_PER_TRADE_PCT
        <= 2.0
    ):
        errors.append(
            "MAX_RISK_PER_TRADE_PCT must be > 0 and <= 2%."
        )

    if not (
        0
        < settings.MAX_DAILY_LOSS_PCT
        <= 10.0
    ):
        errors.append(
            "MAX_DAILY_LOSS_PCT must be > 0 and <= 10%."
        )

    if settings.MAX_OPEN_POSITIONS < 1:
        errors.append(
            "MAX_OPEN_POSITIONS must be at least 1."
        )

    if not (
        0
        < settings.MAX_POSITION_ALLOCATION_PCT
        <= 100
    ):
        errors.append(
            "MAX_POSITION_ALLOCATION_PCT is invalid."
        )

    if not (
        0
        < settings.MAX_PORTFOLIO_EXPOSURE_PCT
        <= 100
    ):
        errors.append(
            "MAX_PORTFOLIO_EXPOSURE_PCT is invalid."
        )

    if (
        settings.MAX_POSITION_ALLOCATION_PCT
        >
        settings.MAX_PORTFOLIO_EXPOSURE_PCT
    ):
        errors.append(
            "MAX_POSITION_ALLOCATION_PCT cannot exceed "
            "MAX_PORTFOLIO_EXPOSURE_PCT."
        )

    # ========================================================
    # SETUP RISK
    # ========================================================

    setup_risks = {
        "RISK_B_SETUP_PCT": (
            settings.RISK_B_SETUP_PCT
        ),
        "RISK_A_SETUP_PCT": (
            settings.RISK_A_SETUP_PCT
        ),
        "RISK_A_PLUS_SETUP_PCT": (
            settings.RISK_A_PLUS_SETUP_PCT
        ),
    }

    for name, value in setup_risks.items():

        if not (
            0
            < value
            <= settings.MAX_RISK_PER_TRADE_PCT
        ):
            errors.append(
                f"{name} must be > 0 and <= "
                "MAX_RISK_PER_TRADE_PCT."
            )

    if not (
        settings.RISK_B_SETUP_PCT
        <= settings.RISK_A_SETUP_PCT
        <= settings.RISK_A_PLUS_SETUP_PCT
    ):
        errors.append(
            "Setup risk must follow "
            "B <= A <= A+."
        )

    # ========================================================
    # TARGETS
    # ========================================================

    if not (
        0
        < settings.TARGET_1_R
        < settings.TARGET_2_R
        < settings.TARGET_3_R
    ):
        errors.append(
            "Targets must follow "
            "0 < TARGET_1_R < TARGET_2_R < TARGET_3_R."
        )

    # ========================================================
    # EXIT ALLOCATION
    # ========================================================

    exit_values = (
        settings.TARGET_1_EXIT_PCT,
        settings.TARGET_2_EXIT_PCT,
        settings.TARGET_3_EXIT_PCT,
        settings.RUNNER_EXIT_PCT,
    )

    for value in exit_values:
        if not (
            0
            <= value
            <= 100
        ):
            errors.append(
                "All exit allocation percentages "
                "must be between 0 and 100."
            )
            break

    exit_total = sum(
        exit_values
    )

    if abs(
        exit_total - 100.0
    ) > 0.001:
        errors.append(
            "T1 + T2 + T3 + Runner exit "
            "percentages must equal 100%."
        )

    if (
        settings.RUNNER_ENABLED
        and
        settings.RUNNER_EXIT_PCT <= 0
    ):
        errors.append(
            "RUNNER_ENABLED=true requires "
            "RUNNER_EXIT_PCT > 0."
        )

    # ========================================================
    # TRAILING STOP
    # ========================================================

    if (
        settings.TRAILING_STOP_ENABLED
        and
        settings.TRAILING_STOP_PCT <= 0
    ):
        errors.append(
            "TRAILING_STOP_PCT must be > 0 "
            "when trailing stop is enabled."
        )

    if (
        settings.TRAILING_STOP_ENABLED
        and
        settings.TRAILING_ATR_MULTIPLIER <= 0
    ):
        errors.append(
            "TRAILING_ATR_MULTIPLIER must be > 0 "
            "when trailing stop is enabled."
        )

    # ========================================================
    # MARKET
    # ========================================================

    if settings.MIN_STOCK_PRICE <= 0:
        errors.append(
            "MIN_STOCK_PRICE must be greater than zero."
        )

    if (
        settings.MAX_STOCK_PRICE
        <= settings.MIN_STOCK_PRICE
    ):
        errors.append(
            "MAX_STOCK_PRICE must be greater "
            "than MIN_STOCK_PRICE."
        )

    if settings.MIN_DOLLAR_VOLUME < 0:
        errors.append(
            "MIN_DOLLAR_VOLUME cannot be negative."
        )

    if not (
        0
        <= settings.MAX_SPREAD_PCT
        <= 100
    ):
        errors.append(
            "MAX_SPREAD_PCT must be between 0 and 100."
        )

    if settings.MAX_DISCOVERY_SYMBOLS < 1:
        errors.append(
            "MAX_DISCOVERY_SYMBOLS must be at least 1."
        )

    if settings.MAX_ANALYSIS_CANDIDATES < 1:
        errors.append(
            "MAX_ANALYSIS_CANDIDATES must be at least 1."
        )

    # ========================================================
    # SCORES
    # ========================================================

    score_settings = {
        "MIN_SIGNAL_SCORE": (
            settings.MIN_SIGNAL_SCORE
        ),
        "MIN_OPPORTUNITY_SCORE": (
            settings.MIN_OPPORTUNITY_SCORE
        ),
        "MIN_SESSION_STRATEGY_SCORE": (
            settings.MIN_SESSION_STRATEGY_SCORE
        ),
        "MIN_BREAKOUT_SCORE": (
            settings.MIN_BREAKOUT_SCORE
        ),
    }

    for name, value in score_settings.items():

        if not (
            0
            <= value
            <= 100
        ):
            errors.append(
                f"{name} must be between 0 and 100."
            )

    # ========================================================
    # AI
    # ========================================================

    if not (
        0
        <= settings.MIN_AI_PROBABILITY
        <= 1
    ):
        errors.append(
            "MIN_AI_PROBABILITY must be between 0 and 1."
        )

    if settings.MIN_TRAINING_SAMPLES < 1:
        errors.append(
            "MIN_TRAINING_SAMPLES must be at least 1."
        )

    # ========================================================
    # BREAKOUT
    # ========================================================

    if settings.MIN_BREAKOUT_VOLUME_RATIO <= 0:
        errors.append(
            "MIN_BREAKOUT_VOLUME_RATIO must be > 0."
        )

    if not (
        0
        <= settings.MAX_BREAKOUT_UPPER_WICK_RATIO
        <= 1
    ):
        errors.append(
            "MAX_BREAKOUT_UPPER_WICK_RATIO "
            "must be between 0 and 1."
        )

    # ========================================================
    # ENGINE INTERVALS
    # ========================================================

    interval_settings = {
        "DISCOVERY_INTERVAL_SECONDS": (
            settings.DISCOVERY_INTERVAL_SECONDS
        ),
        "TRADE_MONITOR_INTERVAL_SECONDS": (
            settings.TRADE_MONITOR_INTERVAL_SECONDS
        ),
        "RECONCILIATION_INTERVAL_SECONDS": (
            settings.RECONCILIATION_INTERVAL_SECONDS
        ),
    }

    for name, value in interval_settings.items():

        if value <= 0:
            errors.append(
                f"{name} must be greater than zero."
            )

    # ========================================================
    # FINAL ERRORS
    # ========================================================

    if errors:
        raise RuntimeError(
            "Invalid JALWE V4 configuration:\n"
            + "\n".join(
                f"- {error}"
                for error in errors
            )
        )


# ============================================================
# AUTO PAPER EXECUTION STATUS
# ============================================================

def auto_paper_execution_ready() -> bool:
    """
    Configuration-level permission only.

    This does NOT replace:
    - DecisionEngine
    - RiskEngine
    - RecoveryEngine
    - ReconciliationEngine
    - ExecutionEngine
    - PaperTradeOrchestrator

    All of them still have to approve the trade.
    """

    return bool(
        settings.PAPER_TRADING

        and

        not settings.ALLOW_LIVE_TRADING

        and

        settings.AUTO_PAPER_EXECUTION

        and

        settings.BROKER_SUBMISSION_ENABLED

        and

        credentials_ready()

        and

        (
            "paper-api.alpaca.markets"
            in
            settings.ALPACA_BASE_URL.lower()
        )
    )