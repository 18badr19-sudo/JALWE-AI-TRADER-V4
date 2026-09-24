from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.parse
import urllib.request

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


from core.config import settings
from core.database import database

from intelligence.decision_engine import (
    get_decision_engine,
)

from intelligence.external_research_bridge import (
    get_external_research_bridge,
)


# ============================================================
# JALWE RESEARCH WATCHER V2
# ============================================================

VERSION = "2.0"


# ============================================================
# PATHS
# ============================================================

BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

DATA_DIR = (
    BASE_DIR
    / "data"
)

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

STATE_FILE = (
    DATA_DIR
    / "jalwe_research_watcher_state.json"
)


# ============================================================
# CONFIG
# ============================================================

POLL_SECONDS = max(
    10,
    int(
        os.getenv(
            "JALWE_RESEARCH_POLL_SECONDS",
            "60",
        )
    ),
)

MAX_RESEARCH_AGE_MINUTES = max(
    1,
    int(
        os.getenv(
            "JALWE_RESEARCH_MAX_AGE_MINUTES",
            "30",
        )
    ),
)

MAX_REPORTS_PER_SCAN = max(
    1,
    int(
        os.getenv(
            "JALWE_RESEARCH_MAX_REPORTS",
            "20",
        )
    ),
)


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_TOKEN = str(
    getattr(
        settings,
        "TELEGRAM_BOT_TOKEN",
        "",
    )
    or
    ""
).strip()

TELEGRAM_CHAT_ID = str(
    getattr(
        settings,
        "TELEGRAM_CHAT_ID",
        "",
    )
    or
    ""
).strip()

TELEGRAM_ENABLED = bool(
    TELEGRAM_TOKEN
    and
    TELEGRAM_CHAT_ID
)


# ============================================================
# IMPORTANT SAFETY
# ============================================================

ORDER_EXECUTION_ENABLED = False

CALL_EXECUTION_ENGINE = False


# ============================================================
# ALLOWED APEX VERDICTS
# ============================================================

ALLOWED_APEX_VERDICTS = {

    "WATCH",

    "RESEARCH_CANDIDATE",

    "HIGH_PRIORITY_RESEARCH",
}


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
# BASIC HELPERS
# ============================================================

def utc_now_iso() -> str:

    return (
        datetime.now(
            timezone.utc
        ).isoformat()
    )


def safe_dict(
    value: Any,
) -> dict:

    if isinstance(
        value,
        dict,
    ):

        return value

    return {}


def safe_list(
    value: Any,
) -> list:

    if value is None:

        return []

    if isinstance(
        value,
        list,
    ):

        return value

    if isinstance(
        value,
        tuple,
    ):

        return list(
            value
        )

    return [
        value
    ]


def safe_float(
    value: Any,
) -> Optional[float]:

    try:

        if value is None:

            return None

        return float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):

        return None


def format_number(
    value: Any,
    decimals: int = 2,
) -> str:

    number = safe_float(
        value
    )

    if number is None:

        return "N/A"

    return (
        f"{number:.{decimals}f}"
    )


# ============================================================
# STATE
# ============================================================

def load_state() -> dict:

    default_state = {

        "processed": {},

        "last_notified": {},
    }

    if not STATE_FILE.exists():

        return default_state

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(
                file
            )

        if not isinstance(
            data,
            dict,
        ):

            return default_state

        processed = data.get(
            "processed",
            {},
        )

        if not isinstance(
            processed,
            dict,
        ):

            processed = {}

        last_notified = data.get(
            "last_notified",
            {},
        )

        if not isinstance(
            last_notified,
            dict,
        ):

            last_notified = {}

        return {

            "processed":
                processed,

            "last_notified":
                last_notified,
        }

    except Exception as exc:

        logger.warning(
            "Unable to load watcher state: %s",
            exc,
        )

        return default_state


def save_state(
    state: dict,
) -> None:

    temp_file = (
        STATE_FILE
        .with_suffix(
            ".tmp"
        )
    )

    payload = {

        "version":
            VERSION,

        "updated_at":
            utc_now_iso(),

        "processed":
            state.get(
                "processed",
                {},
            ),

        "last_notified":
            state.get(
                "last_notified",
                {},
            ),
    }

    try:

        with temp_file.open(
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                payload,
                file,
                ensure_ascii=False,
                indent=2,
            )

        temp_file.replace(
            STATE_FILE
        )

    except Exception as exc:

        logger.warning(
            "Unable to save watcher state: %s",
            exc,
        )


# ============================================================
# RESEARCH VERSION
# ============================================================

def research_version(
    research: Any,
) -> str:

    created_at = str(
        getattr(
            research,
            "created_at",
            "",
        )
        or
        ""
    ).strip()

    if created_at:

        return created_at

    metadata = safe_dict(
        getattr(
            research,
            "metadata",
            {},
        )
    )

    return str(
        metadata.get(
            "created_at",
            "",
        )
        or
        ""
    )


# ============================================================
# RESEARCH FILTER
# ============================================================

def should_process(
    research: Any,
    state: dict,
) -> tuple[
    bool,
    str,
]:

    symbol = str(
        getattr(
            research,
            "symbol",
            "",
        )
        or
        ""
    ).strip().upper()

    if not symbol:

        return (
            False,
            "EMPTY_SYMBOL",
        )

    source = str(
        getattr(
            research,
            "source",
            "",
        )
        or
        ""
    ).strip().upper()

    if source != "APEX":

        return (
            False,
            "NON_APEX_SOURCE",
        )

    metadata = safe_dict(
        getattr(
            research,
            "metadata",
            {},
        )
    )

    verdict = str(
        metadata.get("apex_verdict")
        or metadata.get("verdict")
        or ""
    ).strip().upper()

    if (
        verdict
        not in
        ALLOWED_APEX_VERDICTS
    ):

        return (
            False,
            "APEX_VERDICT_NOT_ALLOWED",
        )

    critical_risk = bool(
        metadata.get(
            "critical_risk",
            False,
        )
    )

    if critical_risk:

        return (
            False,
            "APEX_CRITICAL_RISK",
        )

    version = (
        research_version(
            research
        )
    )

    if not version:

        return (
            False,
            "NO_TIMESTAMP",
        )

    processed = safe_dict(
        state.get(
            "processed",
            {},
        )
    )

    old_version = str(
        processed.get(
            symbol,
            "",
        )
        or
        ""
    )

    if old_version == version:

        return (
            False,
            "ALREADY_PROCESSED",
        )

    return (
        True,
        "NEW_RESEARCH",
    )


# ============================================================
# DECISION STATE
# ============================================================

def decision_state_value(
    decision: Any,
) -> str:

    state = getattr(
        decision,
        "state",
        None,
    )

    value = getattr(
        state,
        "value",
        state,
    )

    return str(
        value
        or
        "UNKNOWN"
    ).upper()


# ============================================================
# DECISION PAYLOAD
# ============================================================

def build_decision_payload(
    research: Any,
    decision: Any,
) -> dict:

    research_metadata = safe_dict(
        getattr(
            research,
            "metadata",
            {},
        )
    )

    decision_metadata = safe_dict(
        getattr(
            decision,
            "metadata",
            {},
        )
    )

    return {

        "timestamp":
            utc_now_iso(),

        "source":
            "APEX_TO_JALWE",

        "symbol":
            str(
                getattr(
                    research,
                    "symbol",
                    "",
                )
                or
                ""
            ).upper(),

        # ----------------------------------------------------
        # APEX
        # ----------------------------------------------------

        "apex": {

            "verdict":
                research_metadata.get(
                    "apex_verdict"
                ),

            "score":
                research_metadata.get(
                    "apex_research_score"
                ),

            "confidence_pct":
                research_metadata.get(
                    "apex_confidence_pct"
                ),

            "bias":
                getattr(
                    research,
                    "market_bias",
                    None,
                ),

            "news_score":
                getattr(
                    research,
                    "news_score",
                    None,
                ),

            "sentiment":
                getattr(
                    research,
                    "sentiment",
                    None,
                ),

            "risk_flags":
                safe_list(
                    getattr(
                        research,
                        "risk_flags",
                        [],
                    )
                ),

            "created_at":
                getattr(
                    research,
                    "created_at",
                    None,
                ),
        },

        # ----------------------------------------------------
        # JALWE
        # ----------------------------------------------------

        "jalwe": {

            "state":
                decision_state_value(
                    decision
                ),

            "ready_for_execution":
                bool(
                    getattr(
                        decision,
                        "ready_for_execution",
                        False,
                    )
                ),

            "reason":
                str(
                    getattr(
                        decision,
                        "reason",
                        "",
                    )
                    or
                    ""
                ),

            "market_regime":
                getattr(
                    decision,
                    "market_regime",
                    None,
                ),

            "strategy":
                getattr(
                    decision,
                    "strategy",
                    None,
                ),

            "setup_grade":
                getattr(
                    decision,
                    "setup_grade",
                    None,
                ),

            "opportunity_score":
                getattr(
                    decision,
                    "opportunity_score",
                    None,
                ),

            "ai_score":
                getattr(
                    decision,
                    "ai_score",
                    None,
                ),

            "session_strategy_score":
                getattr(
                    decision,
                    "session_strategy_score",
                    None,
                ),

            "breakout_score":
                getattr(
                    decision,
                    "breakout_score",
                    None,
                ),

            "risk_pct":
                getattr(
                    decision,
                    "risk_pct",
                    0.0,
                ),

            "quantity":
                getattr(
                    decision,
                    "quantity",
                    0,
                ),

            "entry_price":
                getattr(
                    decision,
                    "entry_price",
                    None,
                ),

            "stop_price":
                getattr(
                    decision,
                    "stop_price",
                    None,
                ),

            "target_1":
                getattr(
                    decision,
                    "target_1",
                    None,
                ),

            "target_2":
                getattr(
                    decision,
                    "target_2",
                    None,
                ),

            "target_3":
                getattr(
                    decision,
                    "target_3",
                    None,
                ),

            "gates":
                safe_dict(
                    getattr(
                        decision,
                        "gates",
                        {},
                    )
                ),

            "warnings":
                safe_list(
                    getattr(
                        decision,
                        "warnings",
                        [],
                    )
                ),

            "metadata":
                decision_metadata,
        },

        # ----------------------------------------------------
        # HARD SAFETY
        # ----------------------------------------------------

        "execution": {

            "watcher_execution_enabled":
                False,

            "execution_engine_called":
                False,

            "broker_order_submitted":
                False,
        },
    }


# ============================================================
# DATABASE LOG
# ============================================================

def save_decision_to_database(
    payload: dict,
) -> bool:

    symbol = str(
        payload.get(
            "symbol",
            "",
        )
        or
        ""
    )

    jalwe = safe_dict(
        payload.get(
            "jalwe",
            {},
        )
    )

    state = str(
        jalwe.get(
            "state",
            "UNKNOWN",
        )
    )

    reason = str(
        jalwe.get(
            "reason",
            "",
        )
        or
        ""
    )

    message = (
        f"{symbol} | "
        f"JALWE={state} | "
        f"{reason}"
    )

    try:

        database.log_event(

            event_type=(
                "APEX_RESEARCH_DECISION"
            ),

            message=(
                message
            ),

            severity=(
                "INFO"
            ),

            metadata=(
                payload
            ),
        )

        return True

    except Exception as exc:

        logger.exception(
            "Database decision log failed: %s",
            exc,
        )

        return False


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def build_telegram_message(
    payload: dict,
) -> str:

    symbol = str(
        payload.get(
            "symbol",
            "",
        )
        or
        ""
    )

    apex = safe_dict(
        payload.get(
            "apex",
            {},
        )
    )

    jalwe = safe_dict(
        payload.get(
            "jalwe",
            {},
        )
    )

    state = str(
        jalwe.get(
            "state",
            "UNKNOWN",
        )
    )

    if (
        state
        ==
        "READY_FOR_PAPER_EXECUTION"
    ):

        state_icon = "🟢"

    elif state == "WATCHING":

        state_icon = "🟡"

    elif state == "REJECTED":

        state_icon = "🔴"

    else:

        state_icon = "⚪"

    lines = [

        "🤖 JALWE V4 — تحليل Apex تلقائي",

        "",

        f"📌 السهم: {symbol}",

        "",

        "🔎 APEX",

        (
            "الحالة: "
            f"{apex.get('verdict', 'N/A')}"
        ),

        (
            "Score: "
            f"{format_number(apex.get('score'))}/100"
        ),

        (
            "Confidence: "
            f"{format_number(apex.get('confidence_pct'))}%"
        ),

        (
            "Bias: "
            f"{apex.get('bias', 'N/A')}"
        ),

        "",

        f"{state_icon} JALWE",

        (
            "القرار: "
            f"{state}"
        ),

        (
            "السبب: "
            f"{jalwe.get('reason', '')}"
        ),

        (
            "Opportunity: "
            f"{format_number(jalwe.get('opportunity_score'))}"
        ),

        (
            "AI: "
            f"{format_number(jalwe.get('ai_score'))}"
        ),
    ]

    if jalwe.get(
        "strategy"
    ):

        lines.append(
            "Strategy: "
            +
            str(
                jalwe.get(
                    "strategy"
                )
            )
        )

    if jalwe.get(
        "market_regime"
    ):

        lines.append(
            "Market: "
            +
            str(
                jalwe.get(
                    "market_regime"
                )
            )
        )

    # --------------------------------------------------------
    # READY INFORMATION
    # --------------------------------------------------------

    if bool(
        jalwe.get(
            "ready_for_execution",
            False,
        )
    ):

        lines.extend(
            [

                "",

                "🎯 PAPER SETUP READY",

                (
                    "Entry: $"
                    f"{format_number(jalwe.get('entry_price'))}"
                ),

                (
                    "Stop: $"
                    f"{format_number(jalwe.get('stop_price'))}"
                ),

                (
                    "Target 1: $"
                    f"{format_number(jalwe.get('target_1'))}"
                ),

                (
                    "Target 2: $"
                    f"{format_number(jalwe.get('target_2'))}"
                ),

                (
                    "Target 3: $"
                    f"{format_number(jalwe.get('target_3'))}"
                ),

                (
                    "Qty: "
                    f"{jalwe.get('quantity', 0)}"
                ),

                (
                    "Risk: "
                    f"{format_number(jalwe.get('risk_pct'))}%"
                ),
            ]
        )

    warnings = safe_list(
        jalwe.get(
            "warnings",
            [],
        )
    )

    if warnings:

        lines.extend(
            [
                "",
                "⚠️ التحذيرات:",
            ]
        )

        for warning in (
            warnings[:6]
        ):

            lines.append(
                "• "
                +
                str(
                    warning
                )
            )

    lines.extend(
        [

            "",

            "🔒 التنفيذ الآلي من Watcher: معطل",

            "لا يوجد أمر شراء أو بيع من هذا التنبيه.",
        ]
    )

    return "\n".join(
        lines
    )


# ============================================================
# TELEGRAM SEND
# ============================================================

def send_telegram(
    message: str,
) -> tuple[
    bool,
    Optional[str],
]:

    if not TELEGRAM_ENABLED:

        return (
            False,
            "TELEGRAM_NOT_CONFIGURED",
        )

    url = (
        "https://api.telegram.org/bot"
        +
        TELEGRAM_TOKEN
        +
        "/sendMessage"
    )

    payload = urllib.parse.urlencode(
        {

            "chat_id":
                TELEGRAM_CHAT_ID,

            "text":
                message,

            "disable_web_page_preview":
                "true",
        }
    ).encode(
        "utf-8"
    )

    request = urllib.request.Request(
        url=url,
        data=payload,
        method="POST",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:

            body = response.read()

            if (
                response.status
                !=
                200
            ):

                return (
                    False,
                    f"HTTP_{response.status}",
                )

            if not body:

                return (
                    True,
                    None,
                )

            try:

                data = json.loads(
                    body.decode(
                        "utf-8"
                    )
                )

                if not bool(
                    data.get(
                        "ok",
                        False,
                    )
                ):

                    return (
                        False,
                        str(
                            data.get(
                                "description",
                                "Telegram error",
                            )
                        ),
                    )

            except Exception:

                pass

            return (
                True,
                None,
            )

    except Exception as exc:

        return (
            False,
            str(
                exc
            ),
        )


# ============================================================
# NOTIFICATION FINGERPRINT
# ============================================================

def notification_fingerprint(
    payload: dict,
) -> str:

    apex = safe_dict(
        payload.get(
            "apex",
            {},
        )
    )

    jalwe = safe_dict(
        payload.get(
            "jalwe",
            {},
        )
    )

    important = {

        "symbol":
            payload.get(
                "symbol"
            ),

        "apex_verdict":
            apex.get(
                "verdict"
            ),

        "apex_score":
            apex.get(
                "score"
            ),

        "jalwe_state":
            jalwe.get(
                "state"
            ),

        "ready":
            jalwe.get(
                "ready_for_execution"
            ),

        "reason":
            jalwe.get(
                "reason"
            ),

        "strategy":
            jalwe.get(
                "strategy"
            ),

        "entry":
            jalwe.get(
                "entry_price"
            ),

        "stop":
            jalwe.get(
                "stop_price"
            ),

        "t1":
            jalwe.get(
                "target_1"
            ),

        "t2":
            jalwe.get(
                "target_2"
            ),

        "t3":
            jalwe.get(
                "target_3"
            ),

        "quantity":
            jalwe.get(
                "quantity"
            ),
    }

    raw = json.dumps(
        important,
        sort_keys=True,
        default=str,
    ).encode(
        "utf-8"
    )

    return (
        hashlib.sha256(
            raw
        )
        .hexdigest()
    )


# ============================================================
# NOTIFY IF CHANGED
# ============================================================

def notify_if_changed(
    payload: dict,
    state: dict,
) -> str:

    symbol = str(
        payload.get(
            "symbol",
            "",
        )
        or
        ""
    ).upper()

    fingerprint = (
        notification_fingerprint(
            payload
        )
    )

    last_notified = state.setdefault(
        "last_notified",
        {},
    )

    previous = str(
        last_notified.get(
            symbol,
            "",
        )
        or
        ""
    )

    if previous == fingerprint:

        return (
            "UNCHANGED_NO_NOTIFICATION"
        )

    message = (
        build_telegram_message(
            payload
        )
    )

    success, error = (
        send_telegram(
            message
        )
    )

    if success:

        last_notified[
            symbol
        ] = fingerprint

        save_state(
            state
        )

        return (
            "SENT"
        )

    # --------------------------------------------------------
    # Telegram not configured is not fatal.
    # --------------------------------------------------------

    if (
        error
        ==
        "TELEGRAM_NOT_CONFIGURED"
    ):

        return (
            "TELEGRAM_NOT_CONFIGURED"
        )

    logger.warning(
        "Telegram notification failed "
        "for %s: %s",
        symbol,
        error,
    )

    try:

        database.log_event(

            event_type=(
                "TELEGRAM_NOTIFICATION_ERROR"
            ),

            message=(
                f"{symbol}: {error}"
            ),

            severity=(
                "WARNING"
            ),

            metadata={

                "symbol":
                    symbol,

                "error":
                    error,

                "source":
                    "JALWE_RESEARCH_WATCHER",
            },
        )

    except Exception:

        pass

    return (
        "FAILED"
    )


# ============================================================
# PRINT DECISION
# ============================================================

def print_decision(
    payload: dict,
    telegram_status: str,
    database_saved: bool,
) -> None:

    apex = safe_dict(
        payload.get(
            "apex",
            {},
        )
    )

    jalwe = safe_dict(
        payload.get(
            "jalwe",
            {},
        )
    )

    print()

    print(
        "======================================"
    )

    print(
        "JALWE AUTOMATIC RESEARCH DECISION V2"
    )

    print(
        "======================================"
    )

    print(
        "SYMBOL:",
        payload.get(
            "symbol"
        ),
    )

    print(
        "APEX VERDICT:",
        apex.get(
            "verdict"
        ),
    )

    print(
        "APEX SCORE:",
        apex.get(
            "score"
        ),
    )

    print(
        "APEX CONF:",
        apex.get(
            "confidence_pct"
        ),
    )

    print(
        "APEX BIAS:",
        apex.get(
            "bias"
        ),
    )

    print()

    print(
        "JALWE STATE:",
        jalwe.get(
            "state"
        ),
    )

    print(
        "READY:",
        jalwe.get(
            "ready_for_execution"
        ),
    )

    print(
        "REASON:",
        jalwe.get(
            "reason"
        ),
    )

    print(
        "OPPORTUNITY SCORE:",
        jalwe.get(
            "opportunity_score"
        ),
    )

    print(
        "AI SCORE:",
        jalwe.get(
            "ai_score"
        ),
    )

    print(
        "SESSION SCORE:",
        jalwe.get(
            "session_strategy_score"
        ),
    )

    print(
        "BREAKOUT SCORE:",
        jalwe.get(
            "breakout_score"
        ),
    )

    print(
        "RISK %:",
        jalwe.get(
            "risk_pct"
        ),
    )

    print(
        "QUANTITY:",
        jalwe.get(
            "quantity"
        ),
    )

    print(
        "ENTRY:",
        jalwe.get(
            "entry_price"
        ),
    )

    print(
        "STOP:",
        jalwe.get(
            "stop_price"
        ),
    )

    print(
        "TARGETS:",
        jalwe.get(
            "target_1"
        ),
        jalwe.get(
            "target_2"
        ),
        jalwe.get(
            "target_3"
        ),
    )

    print(
        "GATES:",
        jalwe.get(
            "gates"
        ),
    )

    warnings = safe_list(
        jalwe.get(
            "warnings",
            [],
        )
    )

    if warnings:

        print(
            "WARNINGS:"
        )

        for warning in warnings:

            print(
                " -",
                warning,
            )

    print()

    print(
        "DATABASE SAVED:",
        database_saved,
    )

    print(
        "TELEGRAM:",
        telegram_status,
    )

    print(
        "EXECUTION:",
        "DISABLED",
    )

    print(
        "======================================"
    )


# ============================================================
# PROCESS ONE REPORT
# ============================================================

def process_research(
    research: Any,
    decision_engine: Any,
    state: dict,
) -> bool:

    symbol = str(
        getattr(
            research,
            "symbol",
            "",
        )
        or
        ""
    ).strip().upper()

    try:

        print()

        print(
            utc_now_iso(),
            "| NEW APEX REPORT |",
            symbol,
            "| running JALWE DecisionEngine..."
        )

        # ====================================================
        # JALWE DECISION ONLY
        #
        # NO ExecutionEngine call exists in this file.
        # ====================================================

        decision = (
            decision_engine
            .analyze(
                symbol
            )
        )

        # ====================================================
        # BUILD AUDIT PAYLOAD
        # ====================================================

        payload = (
            build_decision_payload(
                research,
                decision,
            )
        )

        # ====================================================
        # SAVE TO JALWE DATABASE
        # ====================================================

        database_saved = (
            save_decision_to_database(
                payload
            )
        )

        # ====================================================
        # TELEGRAM
        # ====================================================

        telegram_status = (
            notify_if_changed(
                payload,
                state,
            )
        )

        # ====================================================
        # SCREEN
        # ====================================================

        print_decision(
            payload,
            telegram_status,
            database_saved,
        )

        # ====================================================
        # MARK REPORT PROCESSED
        # ====================================================

        state.setdefault(
            "processed",
            {},
        )

        state[
            "processed"
        ][
            symbol
        ] = research_version(
            research
        )

        save_state(
            state
        )

        return True

    except Exception as exc:

        logger.exception(
            "JALWE decision processing failed "
            "for %s: %s",
            symbol,
            exc,
        )

        try:

            database.log_event(

                event_type=(
                    "APEX_RESEARCH_WATCHER_ERROR"
                ),

                message=(
                    f"{symbol}: {str(exc)}"
                ),

                severity=(
                    "ERROR"
                ),

                metadata={

                    "symbol":
                        symbol,

                    "source":
                        "JALWE_RESEARCH_WATCHER",

                    "timestamp":
                        utc_now_iso(),
                },
            )

        except Exception:

            pass

        # Do not mark as processed.
        # It can retry later.

        return False


# ============================================================
# SCAN BRIDGE
# ============================================================

def scan_bridge(
    bridge: Any,
    decision_engine: Any,
    state: dict,
) -> tuple[
    int,
    int,
]:

    research_items = bridge.list_recent_research(
        limit=MAX_REPORTS_PER_SCAN,
        source="APEX",
    )

    fresh_items = []

    for research in research_items:
        try:
            age_minutes = bridge.get_age_minutes(
                research
            )
        except Exception:
            age_minutes = None

        if (
            age_minutes is not None
            and age_minutes <= float(
                MAX_RESEARCH_AGE_MINUTES
            )
        ):
            fresh_items.append(
                research
            )

    research_items = fresh_items

    discovered = len(
        research_items
    )

    processed_count = 0

    for research in research_items:

        allowed, _reason = (
            should_process(
                research,
                state,
            )
        )

        if not allowed:

            continue

        if process_research(
            research,
            decision_engine,
            state,
        ):

            processed_count += 1

    return (
        discovered,
        processed_count,
    )


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat(
    discovered: int,
    processed: int,
) -> None:

    print(

        utc_now_iso(),

        "| JALWE WATCHER V2",

        "| fresh reports:",
        discovered,

        "| new processed:",
        processed,

        "| telegram:",
        (
            "ON"
            if TELEGRAM_ENABLED
            else "OFF"
        ),

        "| execution:",
        "DISABLED",
    )


# ============================================================
# HEALTH
# ============================================================

def print_startup_health() -> None:

    bridge = (
        get_external_research_bridge()
    )

    health = bool(
        bridge.health_check()
    )

    try:
        reports = int(
            bridge.count_latest()
        )
    except Exception:
        reports = 0

    print(
        "BRIDGE OK:",
        health,
    )

    print(
        "BRIDGE REPORTS:",
        reports,
    )

    print(
        "TELEGRAM:",
        (
            "READY"
            if TELEGRAM_ENABLED
            else "NOT CONFIGURED"
        )
    )

    print(
        "DATABASE:",
        getattr(
            database,
            "database_path",
            "UNKNOWN",
        )
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print(
        "======================================"
    )

    print(
        f"JALWE RESEARCH WATCHER V{VERSION}"
    )

    print(
        "======================================"
    )

    print(
        "POLL:",
        POLL_SECONDS,
        "seconds",
    )

    print(
        "MAX RESEARCH AGE:",
        MAX_RESEARCH_AGE_MINUTES,
        "minutes",
    )

    print(
        "SOURCE: APEX"
    )

    print(
        "MODE: DECISION ANALYSIS + LOGGING"
    )

    print(
        "TELEGRAM:",
        (
            "ENABLED"
            if TELEGRAM_ENABLED
            else "DISABLED"
        )
    )

    print(
        "ORDER EXECUTION: DISABLED"
    )

    print(
        "EXECUTION ENGINE CALLED: FALSE"
    )

    print(
        "Press CTRL+C to stop."
    )

    print(
        "======================================"
    )

    print_startup_health()

    print(
        "======================================"
    )

    bridge = (
        get_external_research_bridge()
    )

    decision_engine = (
        get_decision_engine()
    )

    state = (
        load_state()
    )

    while True:

        try:

            (
                discovered,
                processed,
            ) = (
                scan_bridge(
                    bridge,
                    decision_engine,
                    state,
                )
            )

            heartbeat(
                discovered,
                processed,
            )

        except KeyboardInterrupt:

            print()

            print(
                "JALWE research watcher "
                "stopped by user."
            )

            break

        except Exception as exc:

            logger.exception(
                "Watcher loop error: %s",
                exc,
            )

            try:

                database.log_event(

                    event_type=(
                        "RESEARCH_WATCHER_LOOP_ERROR"
                    ),

                    message=(
                        str(
                            exc
                        )
                    ),

                    severity=(
                        "ERROR"
                    ),

                    metadata={

                        "timestamp":
                            utc_now_iso(),

                        "source":
                            "JALWE_RESEARCH_WATCHER",
                    },
                )

            except Exception:

                pass

        try:

            time.sleep(
                POLL_SECONDS
            )

        except KeyboardInterrupt:

            print()

            print(
                "JALWE research watcher "
                "stopped by user."
            )

            break


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()