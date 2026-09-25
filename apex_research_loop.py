from __future__ import annotations

import logging
import os
import time

from datetime import datetime, time as dt_time, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from research_orchestrator import ResearchOrchestrator
from news_engine import NewsEngine


# ============================================================
# APEX RESEARCH LOOP V2.1
# RESEARCH ONLY
# NO ORDER EXECUTION
# ============================================================

VERSION = "2.1"


# ============================================================
# LOAD LOCAL .ENV
# ============================================================

BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

ENV_FILE = (
    BASE_DIR
    / ".env"
)

load_dotenv(
    dotenv_path=ENV_FILE,
    override=True,
)


# ============================================================
# TIMEZONES
# ============================================================

UTC = timezone.utc

NEW_YORK = ZoneInfo(
    "America/New_York"
)


# ============================================================
# LOOP INTERVALS
# ============================================================

# Pre-market: every 5 minutes
PREMARKET_INTERVAL_SECONDS = max(
    60,
    int(
        os.getenv(
            "APEX_PREMARKET_INTERVAL_SECONDS",
            "300",
        )
    ),
)


# Regular market: every 5 minutes
REGULAR_INTERVAL_SECONDS = max(
    60,
    int(
        os.getenv(
            "APEX_REGULAR_INTERVAL_SECONDS",
            "300",
        )
    ),
)


# After-hours: every 5 minutes
AFTER_HOURS_INTERVAL_SECONDS = max(
    60,
    int(
        os.getenv(
            "APEX_AFTER_HOURS_INTERVAL_SECONDS",
            "300",
        )
    ),
)


# Closed / overnight:
# lightweight news scan every 15 minutes
CLOSED_INTERVAL_SECONDS = max(
    300,
    int(
        os.getenv(
            "APEX_CLOSED_INTERVAL_SECONDS",
            "900",
        )
    ),
)


# ============================================================
# CLOSED SESSION NEWS
# ============================================================

CLOSED_NEWS_ENABLED = (
    os.getenv(
        "APEX_CLOSED_NEWS_ENABLED",
        "true",
    )
    .strip()
    .lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)


DEFAULT_CLOSED_NEWS_SYMBOLS = (
    "SPY,QQQ,IWM,AAPL,NVDA,TSLA"
)


CLOSED_NEWS_SYMBOLS = [

    symbol.strip().upper()

    for symbol in (
        os.getenv(
            "APEX_CLOSED_NEWS_SYMBOLS",
            DEFAULT_CLOSED_NEWS_SYMBOLS,
        )
        .split(",")
    )

    if symbol.strip()
]


# ============================================================
# SESSION TIMES
# NEW YORK TIME
# ============================================================

PREMARKET_START = dt_time(
    hour=4,
    minute=0,
)

STANDARD_REGULAR_OPEN = dt_time(
    hour=9,
    minute=30,
)

STANDARD_REGULAR_CLOSE = dt_time(
    hour=16,
    minute=0,
)

AFTER_HOURS_END = dt_time(
    hour=20,
    minute=0,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    __name__
)


# ============================================================
# SESSION ENUM
# ============================================================

class MarketSession(str, Enum):

    PREMARKET = "PREMARKET"

    REGULAR = "REGULAR"

    AFTER_HOURS = "AFTER_HOURS"

    CLOSED = "CLOSED"

    UNKNOWN = "UNKNOWN"


# ============================================================
# SESSION INFO
# ============================================================

class SessionInfo:

    def __init__(
        self,
        *,
        session: MarketSession,
        market_open: bool,
        trading_day: bool,
        now_et: datetime,
        regular_open: Optional[datetime] = None,
        regular_close: Optional[datetime] = None,
        next_open: Any = None,
        next_close: Any = None,
        source: str = "UNKNOWN",
        error: Optional[str] = None,
    ) -> None:

        self.session = session

        self.market_open = bool(
            market_open
        )

        self.trading_day = bool(
            trading_day
        )

        self.now_et = now_et

        self.regular_open = (
            regular_open
        )

        self.regular_close = (
            regular_close
        )

        self.next_open = (
            next_open
        )

        self.next_close = (
            next_close
        )

        self.source = source

        self.error = error


# ============================================================
# TIME HELPERS
# ============================================================

def utc_now() -> datetime:

    return datetime.now(
        UTC
    )


def ny_now() -> datetime:

    return datetime.now(
        NEW_YORK
    )


def format_dt(
    value: Any,
) -> str:

    if value is None:

        return "None"

    try:

        return str(
            value
        )

    except Exception:

        return "UNKNOWN"


# ============================================================
# CALENDAR HELPERS
# ============================================================

def _combine_et(
    date_value,
    time_value,
) -> datetime:

    return datetime.combine(
        date_value,
        time_value,
        tzinfo=NEW_YORK,
    )


def _calendar_datetime(
    value: Any,
    fallback_date,
) -> Optional[datetime]:

    if value is None:

        return None

    # --------------------------------------------------------
    # datetime
    # --------------------------------------------------------

    if isinstance(
        value,
        datetime,
    ):

        if value.tzinfo is None:

            return value.replace(
                tzinfo=NEW_YORK
            )

        return value.astimezone(
            NEW_YORK
        )

    # --------------------------------------------------------
    # time
    # --------------------------------------------------------

    if isinstance(
        value,
        dt_time,
    ):

        return _combine_et(
            fallback_date,
            value,
        )

    # --------------------------------------------------------
    # string
    # --------------------------------------------------------

    text = str(
        value
    ).strip()

    if not text:

        return None

    # HH:MM
    try:

        parsed_time = (
            datetime.strptime(
                text,
                "%H:%M",
            )
            .time()
        )

        return _combine_et(
            fallback_date,
            parsed_time,
        )

    except Exception:
        pass

    # HH:MM:SS
    try:

        parsed_time = (
            datetime.strptime(
                text,
                "%H:%M:%S",
            )
            .time()
        )

        return _combine_et(
            fallback_date,
            parsed_time,
        )

    except Exception:
        pass

    # ISO datetime
    try:

        parsed_dt = (
            datetime.fromisoformat(
                text.replace(
                    "Z",
                    "+00:00",
                )
            )
        )

        if parsed_dt.tzinfo is None:

            parsed_dt = (
                parsed_dt.replace(
                    tzinfo=NEW_YORK
                )
            )

        return parsed_dt.astimezone(
            NEW_YORK
        )

    except Exception:

        return None


# ============================================================
# GET TODAY MARKET CALENDAR
# ============================================================

def get_today_calendar(
    orchestrator: ResearchOrchestrator,
    now_et: datetime,
) -> tuple[
    bool,
    Optional[datetime],
    Optional[datetime],
]:

    api = (
        orchestrator
        .scanner
        .alpaca
    )

    if api is None:

        return (
            False,
            None,
            None,
        )

    today = now_et.date()

    today_text = (
        today.isoformat()
    )

    try:

        calendar = (
            api.get_calendar(
                start=today_text,
                end=today_text,
            )
        )

    except Exception as exc:

        logger.warning(
            "Unable to load market calendar: %s",
            exc,
        )

        return (
            False,
            None,
            None,
        )

    if not calendar:

        # Weekend / holiday
        return (
            False,
            None,
            None,
        )

    day = calendar[0]

    open_value = getattr(
        day,
        "open",
        None,
    )

    close_value = getattr(
        day,
        "close",
        None,
    )

    regular_open = (
        _calendar_datetime(
            open_value,
            today,
        )
    )

    regular_close = (
        _calendar_datetime(
            close_value,
            today,
        )
    )

    if regular_open is None:

        regular_open = (
            _combine_et(
                today,
                STANDARD_REGULAR_OPEN,
            )
        )

    if regular_close is None:

        regular_close = (
            _combine_et(
                today,
                STANDARD_REGULAR_CLOSE,
            )
        )

    return (
        True,
        regular_open,
        regular_close,
    )


# ============================================================
# DETECT MARKET SESSION
# ============================================================

def detect_market_session(
    orchestrator: ResearchOrchestrator,
) -> SessionInfo:

    now_et = ny_now()

    api = (
        orchestrator
        .scanner
        .alpaca
    )

    market_open = False

    next_open = None

    next_close = None

    clock_error = None

    # --------------------------------------------------------
    # ALPACA CLOCK
    # --------------------------------------------------------

    try:

        clock = (
            api.get_clock()
        )

        market_open = bool(
            getattr(
                clock,
                "is_open",
                False,
            )
        )

        next_open = getattr(
            clock,
            "next_open",
            None,
        )

        next_close = getattr(
            clock,
            "next_close",
            None,
        )

    except Exception as exc:

        clock_error = str(
            exc
        )

        logger.warning(
            "Unable to load Alpaca clock: %s",
            exc,
        )

    # --------------------------------------------------------
    # MARKET CALENDAR
    # --------------------------------------------------------

    (
        trading_day,
        regular_open,
        regular_close,
    ) = (
        get_today_calendar(
            orchestrator,
            now_et,
        )
    )

    # --------------------------------------------------------
    # WEEKEND / HOLIDAY
    # --------------------------------------------------------

    if not trading_day:

        return SessionInfo(

            session=MarketSession.CLOSED,

            market_open=False,

            trading_day=False,

            now_et=now_et,

            next_open=next_open,

            next_close=next_close,

            source="ALPACA_CALENDAR",

            error=clock_error,
        )

    # --------------------------------------------------------
    # FALLBACK TIMES
    # --------------------------------------------------------

    if regular_open is None:

        regular_open = (
            _combine_et(
                now_et.date(),
                STANDARD_REGULAR_OPEN,
            )
        )

    if regular_close is None:

        regular_close = (
            _combine_et(
                now_et.date(),
                STANDARD_REGULAR_CLOSE,
            )
        )

    premarket_start = (
        _combine_et(
            now_et.date(),
            PREMARKET_START,
        )
    )

    after_hours_end = (
        _combine_et(
            now_et.date(),
            AFTER_HOURS_END,
        )
    )

    # --------------------------------------------------------
    # REGULAR
    # Alpaca clock has priority
    # --------------------------------------------------------

    if market_open:

        session = (
            MarketSession.REGULAR
        )

    # --------------------------------------------------------
    # PREMARKET
    # --------------------------------------------------------

    elif (
        premarket_start
        <=
        now_et
        <
        regular_open
    ):

        session = (
            MarketSession.PREMARKET
        )

    # --------------------------------------------------------
    # AFTER HOURS
    # --------------------------------------------------------

    elif (
        regular_close
        <=
        now_et
        <
        after_hours_end
    ):

        session = (
            MarketSession.AFTER_HOURS
        )

    # --------------------------------------------------------
    # CLOSED
    # --------------------------------------------------------

    else:

        session = (
            MarketSession.CLOSED
        )

    return SessionInfo(

        session=session,

        market_open=market_open,

        trading_day=True,

        now_et=now_et,

        regular_open=regular_open,

        regular_close=regular_close,

        next_open=next_open,

        next_close=next_close,

        source=(
            "ALPACA_CLOCK+CALENDAR"
        ),

        error=clock_error,
    )


# ============================================================
# SESSION INTERVAL
# ============================================================

def interval_for_session(
    session: MarketSession,
) -> int:

    if (
        session
        ==
        MarketSession.PREMARKET
    ):

        return (
            PREMARKET_INTERVAL_SECONDS
        )

    if (
        session
        ==
        MarketSession.REGULAR
    ):

        return (
            REGULAR_INTERVAL_SECONDS
        )

    if (
        session
        ==
        MarketSession.AFTER_HOURS
    ):

        return (
            AFTER_HOURS_INTERVAL_SECONDS
        )

    return (
        CLOSED_INTERVAL_SECONDS
    )


# ============================================================
# FULL RESEARCH SESSIONS
# ============================================================

def should_run_full_research(
    session: MarketSession,
) -> bool:

    return session in {

        MarketSession.PREMARKET,

        MarketSession.REGULAR,

        MarketSession.AFTER_HOURS,
    }


# ============================================================
# PRINT SESSION
# ============================================================

def print_session(
    info: SessionInfo,
) -> None:

    print()

    print(
        "--------------------------------------"
    )

    print(
        "NEW YORK:",
        info.now_et.isoformat(),
    )

    print(
        "SESSION:",
        info.session.value,
    )

    print(
        "TRADING DAY:",
        info.trading_day,
    )

    print(
        "REGULAR OPEN:",
        format_dt(
            info.regular_open
        ),
    )

    print(
        "REGULAR CLOSE:",
        format_dt(
            info.regular_close
        ),
    )

    print(
        "NEXT OPEN:",
        format_dt(
            info.next_open
        ),
    )

    print(
        "NEXT CLOSE:",
        format_dt(
            info.next_close
        ),
    )

    if info.error:

        print(
            "CLOCK WARNING:",
            info.error,
        )

    print(
        "--------------------------------------"
    )


# ============================================================
# PRINT FULL RESEARCH CYCLE
# ============================================================

def print_cycle(
    cycle,
    session: MarketSession,
) -> None:

    print()

    print(
        "======================================"
    )

    print(
        "APEX RESEARCH CYCLE"
    )

    print(
        "======================================"
    )

    print(
        "UTC:",
        utc_now().isoformat(),
    )

    print(
        "SESSION:",
        session.value,
    )

    print(
        "STATUS:",
        cycle.status,
    )

    print(
        "RADAR:",
        cycle.radar_count,
    )

    print(
        "VALID PRE:",
        cycle.valid_prebreakout_count,
    )

    print(
        "DEEP:",
        cycle.deep_research_count,
    )

    print(
        "PACKETS:",
        cycle.packet_count,
    )

    print(
        "SHORTLIST:",
        cycle.shortlisted_count,
    )

    print(
        "PUBLISHED:",
        cycle.published_count,
    )

    print(
        "BRIDGE:",
        cycle.bridge_status,
    )

    if cycle.bridge_error:

        print(
            "BRIDGE ERROR:",
            cycle.bridge_error,
        )

    # --------------------------------------------------------
    # RESEARCH RESULTS
    # --------------------------------------------------------

    if cycle.packets:

        print()

        print(
            "----- RESEARCH RESULTS -----"
        )

        for (
            index,
            packet,
        ) in enumerate(
            cycle.packets,
            start=1,
        ):

            print(

                index,

                packet.symbol,

                "| SCORE:",
                packet.research_score,

                "| CONF:",
                packet.confidence,

                "| BIAS:",
                packet.bias,

                "| VERDICT:",
                packet.verdict,

                "| PRE:",
                packet.prebreakout_score,

                "| NEWS:",
                packet.news_score,

                "| LIQ:",
                packet.liquidity_score,
            )

    # --------------------------------------------------------
    # SENT TO JALWE
    # --------------------------------------------------------

    if cycle.shortlist:

        print()

        print(
            "----- SENT TO JALWE -----"
        )

        for packet in (
            cycle.shortlist
        ):

            print(

                packet.symbol,

                "| VERDICT:",
                packet.verdict,

                "| SCORE:",
                packet.research_score,

                "| CONF:",
                packet.confidence,
            )

    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

    if cycle.errors:

        print()

        print(
            "ERRORS:",
            len(
                cycle.errors
            ),
        )

        for error in (
            cycle.errors[:10]
        ):

            print(
                "-",
                error,
            )

    print(
        "======================================"
    )


# ============================================================
# CLOSED SESSION LIGHT NEWS SCAN
# ============================================================

def run_closed_news_scan(
    news_engine: NewsEngine,
) -> None:

    if not CLOSED_NEWS_ENABLED:

        print(
            utc_now().isoformat(),
            "| CLOSED",
            "| overnight news scan disabled",
        )

        return

    print()

    print(
        "======================================"
    )

    print(
        "APEX CLOSED SESSION NEWS SCAN"
    )

    print(
        "======================================"
    )

    print(
        "UTC:",
        utc_now().isoformat(),
    )

    print(
        "SYMBOLS:",
        ", ".join(
            CLOSED_NEWS_SYMBOLS
        ),
    )

    significant_news = 0

    for symbol in (
        CLOSED_NEWS_SYMBOLS
    ):

        try:

            result = (
                news_engine
                .fetch_symbol_news(
                    symbol
                )
            )

            status = str(
                result.get(
                    "status",
                    "UNKNOWN",
                )
            )

            score = result.get(
                "news_score"
            )

            sentiment = result.get(
                "sentiment",
                "UNKNOWN",
            )

            confidence = result.get(
                "confidence",
                0.0,
            )

            catalysts = result.get(
                "catalysts",
                [],
            )

            risk_flags = result.get(
                "risk_flags",
                [],
            )

            fresh_count = result.get(
                "fresh_news_count",
                0,
            )

            # ------------------------------------------------
            # PRINT ONLY USEFUL OVERNIGHT ACTIVITY
            # ------------------------------------------------

            interesting = bool(
                catalysts
                or
                risk_flags
                or
                (
                    isinstance(
                        score,
                        (int, float),
                    )
                    and
                    (
                        score >= 65
                        or
                        score <= 35
                    )
                )
            )

            if interesting:

                significant_news += 1

                print()

                print(
                    symbol,
                    "| STATUS:",
                    status,
                    "| NEWS:",
                    score,
                    "| SENT:",
                    sentiment,
                    "| CONF:",
                    confidence,
                    "| FRESH:",
                    fresh_count,
                )

                if catalysts:

                    print(
                        "  CATALYSTS:",
                        catalysts,
                    )

                if risk_flags:

                    print(
                        "  RISKS:",
                        risk_flags,
                    )

            else:

                print(
                    symbol,
                    "|",
                    status,
                    "| no significant catalyst",
                )

        except Exception as exc:

            logger.warning(
                "Closed news scan failed "
                "for %s: %s",
                symbol,
                exc,
            )

            print(
                symbol,
                "| ERROR:",
                str(
                    exc
                ),
            )

    print()

    print(
        "SIGNIFICANT NEWS:",
        significant_news,
    )

    print(
        "ORDER EXECUTION: DISABLED"
    )

    print(
        "======================================"
    )


# ============================================================
# HEARTBEAT SLEEP
# ============================================================

def heartbeat_sleep(
    seconds: int,
    session: MarketSession,
) -> None:

    remaining = int(
        seconds
    )

    while remaining > 0:

        sleep_for = min(
            60,
            remaining,
        )

        time.sleep(
            sleep_for
        )

        remaining -= (
            sleep_for
        )

        print(

            utc_now().isoformat(),

            "|",

            session.value,

            "| alive | next check in",

            remaining,

            "sec",
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print(
        "======================================"
    )

    print(
        f"APEX RESEARCH LOOP V{VERSION}"
    )

    print(
        "======================================"
    )

    print(
        "ENV:",
        str(
            ENV_FILE
        ),
    )

    print(
        "ENV EXISTS:",
        ENV_FILE.exists(),
    )

    print()

    print(
        "PREMARKET INTERVAL:",
        PREMARKET_INTERVAL_SECONDS,
        "seconds",
    )

    print(
        "REGULAR INTERVAL:",
        REGULAR_INTERVAL_SECONDS,
        "seconds",
    )

    print(
        "AFTER HOURS INTERVAL:",
        AFTER_HOURS_INTERVAL_SECONDS,
        "seconds",
    )

    print(
        "CLOSED INTERVAL:",
        CLOSED_INTERVAL_SECONDS,
        "seconds",
    )

    print()

    print(
        "CLOSED NEWS:",
        (
            "ENABLED"
            if CLOSED_NEWS_ENABLED
            else
            "DISABLED"
        ),
    )

    print(
        "CLOSED NEWS SYMBOLS:",
        ", ".join(
            CLOSED_NEWS_SYMBOLS
        ),
    )

    print()

    print(
        "MODE: RESEARCH ONLY"
    )

    print(
        "ORDER EXECUTION: DISABLED"
    )

    print(
        "JALWE DECISION AUTHORITY: ENABLED"
    )

    print(
        "APEX EXECUTION AUTHORITY: DISABLED"
    )

    print()

    print(
        "Press CTRL+C to stop."
    )

    print(
        "======================================"
    )

    # --------------------------------------------------------
    # ENGINES
    # --------------------------------------------------------

    orchestrator = (
        ResearchOrchestrator()
    )

    news_engine = (
        NewsEngine()
    )

    last_session = None

    while True:

        try:

            # =================================================
            # DETECT SESSION
            # =================================================

            info = (
                detect_market_session(
                    orchestrator
                )
            )

            session = (
                info.session
            )

            interval = (
                interval_for_session(
                    session
                )
            )

            # -------------------------------------------------
            # PRINT SESSION WHEN IT CHANGES
            # -------------------------------------------------

            if session != last_session:

                print_session(
                    info
                )

                last_session = (
                    session
                )

            # =================================================
            # PREMARKET / REGULAR / AFTER HOURS
            # =================================================

            if should_run_full_research(
                session
            ):

                print()

                print(

                    utc_now().isoformat(),

                    "|",

                    session.value,

                    "| starting Apex research..."
                )

                cycle = (
                    orchestrator
                    .run_cycle(
                        publish_to_jalwe=True
                    )
                )

                print_cycle(
                    cycle,
                    session,
                )

            # =================================================
            # CLOSED
            # LIGHTWEIGHT NEWS ONLY
            # =================================================

            else:

                print()

                print(

                    utc_now().isoformat(),

                    "| MARKET CLOSED",

                    "| light news scan",

                    "| next open:",

                    format_dt(
                        info.next_open
                    ),
                )

                run_closed_news_scan(
                    news_engine
                )

            # =================================================
            # WAIT
            # =================================================

            heartbeat_sleep(
                interval,
                session,
            )

        except KeyboardInterrupt:

            print()

            print(
                "======================================"
            )

            print(
                "APEX research loop stopped by user."
            )

            print(
                "======================================"
            )

            break

        except Exception as exc:

            logger.exception(
                "Unexpected Apex loop error: %s",
                exc,
            )

            print()

            print(
                "Loop recovered from error."
            )

            print(
                "Retrying in 60 seconds..."
            )

            try:

                time.sleep(
                    60
                )

            except KeyboardInterrupt:

                print()

                print(
                    "APEX research loop stopped by user."
                )

                break


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()