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


from core.config import (
    auto_paper_execution_ready,
    settings,
)
from core.database import database
from core.runtime_controls import (
    clear_emergency_close_request,
    emergency_close_requested,
    load_runtime_controls,
    new_entries_allowed,
)

from intelligence.decision_engine import (
    get_decision_engine,
)

from intelligence.external_research_bridge import (
    get_external_research_bridge,
)

from market.market_data import get_market_data

from trading.execution_engine import (
    get_execution_engine,
)

from trading.paper_trade_orchestrator import (
    get_paper_trade_orchestrator,
)

from trading.reconciliation_engine import (
    get_reconciliation_engine,
)

from trading.recovery_engine import (
    get_recovery_engine,
)

from trading.trade_manager import (
    EXIT_ACTIONS,
    TradeAction,
    TradeManagementDecision,
    get_trade_manager,
)


# ============================================================
# JALWE RESEARCH WATCHER V2
# ============================================================

VERSION = "2.2"


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
            "15",
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

WATCHING_UPDATE_SECONDS = max(
    60,
    int(
        os.getenv(
            "JALWE_WATCHING_UPDATE_SECONDS",
            "300",
        )
    ),
)

# Repeated WATCHING heartbeats are OFF by default.
# The watcher still re-analyzes in the background, but Telegram
# stays quiet unless the setup changes materially.
WATCHING_HEARTBEAT_ENABLED = (
    os.getenv(
        "JALWE_WATCHING_HEARTBEAT_ENABLED",
        "false",
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

# Small score noise should not create a new Telegram alert.
NOTIFICATION_SCORE_STEP = max(
    1.0,
    float(
        os.getenv(
            "JALWE_NOTIFICATION_SCORE_STEP",
            "5.0",
        )
    ),
)

LIFECYCLE_ALERTS_ENABLED = (
    os.getenv(
        "JALWE_LIFECYCLE_ALERTS",
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

ORDER_EXECUTION_ENABLED = bool(
    auto_paper_execution_ready()
)

CALL_EXECUTION_ENGINE = (
    ORDER_EXECUTION_ENABLED
)


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

        "watching": {},

        "last_watch_update": {},

        "lifecycle_events": {},
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

        watching = data.get(
            "watching",
            {},
        )

        if not isinstance(
            watching,
            dict,
        ):

            watching = {}

        last_watch_update = data.get(
            "last_watch_update",
            {},
        )

        if not isinstance(
            last_watch_update,
            dict,
        ):

            last_watch_update = {}

        lifecycle_events = data.get(
            "lifecycle_events",
            {},
        )

        if not isinstance(
            lifecycle_events,
            dict,
        ):

            lifecycle_events = {}

        return {

            "processed":
                processed,

            "last_notified":
                last_notified,

            "watching":
                watching,

            "last_watch_update":
                last_watch_update,

            "lifecycle_events":
                lifecycle_events,
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

        "watching":
            state.get(
                "watching",
                {},
            ),

        "last_watch_update":
            state.get(
                "last_watch_update",
                {},
            ),

        "lifecycle_events":
            state.get(
                "lifecycle_events",
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

        watching = safe_dict(
            state.get(
                "watching",
                {},
            )
        )

        if bool(
            watching.get(
                symbol,
                False,
            )
        ):
            return (
                True,
                "FAST_TRIGGER_RECHECK",
            )

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

            "verdict": (
                research_metadata.get(
                    "apex_verdict"
                )
                or
                research_metadata.get(
                    "verdict"
                )
            ),

            "score": (
                research_metadata.get(
                    "apex_research_score"
                )
                if research_metadata.get(
                    "apex_research_score"
                ) is not None
                else research_metadata.get(
                    "score"
                )
            ),

            "confidence_pct": (
                research_metadata.get(
                    "apex_confidence_pct"
                )
                if research_metadata.get(
                    "apex_confidence_pct"
                ) is not None
                else (
                    float(
                        getattr(
                            research,
                            "confidence",
                            0.0,
                        )
                        or 0.0
                    )
                    * 100.0
                )
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
                ORDER_EXECUTION_ENABLED,

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

    decision_metadata = safe_dict(
        jalwe.get(
            "metadata",
            {},
        )
    )

    trigger_watch = safe_dict(
        decision_metadata.get(
            "trigger_watch",
            {},
        )
    )

    if (
        state == "WATCHING"
        and trigger_watch
    ):
        current_price = safe_float(
            trigger_watch.get(
                "current_price"
            )
        )

        trigger_price = safe_float(
            trigger_watch.get(
                "trigger_price"
            )
        )

        distance_pct = safe_float(
            trigger_watch.get(
                "distance_to_trigger_pct"
            )
        )

        lines.extend(
            [
                "",
                "⏱ متابعة لحظية للـ Trigger",
                (
                    "السعر الحالي: $"
                    + format_number(
                        current_price
                    )
                ),
                (
                    "سعر الـ Trigger: $"
                    + format_number(
                        trigger_price
                    )
                ),
            ]
        )

        if distance_pct is not None:
            lines.append(
                "المتبقي للاختراق: "
                + format_number(
                    abs(
                        distance_pct
                    ),
                    3,
                )
                + "%"
            )

        lines.append(
            "إعادة الفحص: كل "
            + str(
                POLL_SECONDS
            )
            + " ثانية"
        )

    backfill = safe_dict(
        decision_metadata.get(
            "market_data_backfill",
            {},
        )
    )

    if bool(
        backfill.get(
            "attempted",
            False,
        )
    ):
        succeeded = bool(
            backfill.get(
                "succeeded",
                False,
            )
        )

        lines.extend(
            [
                "",
                "🧩 Smart Backfill",
                (
                    "الحالة: "
                    + (
                        "✅ نجح"
                        if succeeded
                        else "⚠️ لم يكتمل"
                    )
                ),
                (
                    "Bars: "
                    f"{backfill.get('initial_valid_rows', 0)}"
                    " → "
                    f"{backfill.get('final_valid_rows', 0)}"
                    " / "
                    f"{backfill.get('required_rows', 0)}"
                ),
                (
                    "المحاولات: "
                    f"{backfill.get('attempt_count', 0)}"
                ),
                "المصدر: شموع Alpaca حقيقية فقط",
            ]
        )

    feature_diagnostics = safe_dict(
        decision_metadata.get(
            "feature_diagnostics",
            {},
        )
    )

    if feature_diagnostics:
        lines.extend(
            [
                "",
                "🧪 فحص بيانات JALWE",
                (
                    "Bars: "
                    f"{feature_diagnostics.get('valid_rows', 0)} "
                    "/ "
                    f"{feature_diagnostics.get('required_rows', 0)} "
                    "مطلوبة"
                ),
                (
                    "آخر شمعة: "
                    + (
                        f"{format_number(feature_diagnostics.get('latest_bar_age_minutes'))} دقيقة"
                        if feature_diagnostics.get(
                            "latest_bar_age_minutes"
                        ) is not None
                        else "N/A"
                    )
                ),
            ]
        )

        missing_columns = safe_list(
            feature_diagnostics.get(
                "missing_columns",
                [],
            )
        )

        if missing_columns:
            lines.append(
                "أعمدة ناقصة: "
                + ", ".join(
                    str(item)
                    for item in missing_columns
                )
            )

        if not bool(
            feature_diagnostics.get(
                "freshness_enforced",
                False,
            )
        ):
            lines.append(
                "ملاحظة: الرفض الحالي من جودة البيانات، "
                "وليس من حد زمني مستقل للشمعة."
            )

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
            (
                "📄 تنفيذ PAPER الآلي: مفعّل"
                if ORDER_EXECUTION_ENABLED
                else "🔒 تنفيذ PAPER الآلي: معطّل"
            ),
            (
                "هذا التنبيه تحليلي؛ تنفيذ الأمر يتم فقط "
                "إذا اجتازت الصفقة جميع بوابات JALWE."
                if ORDER_EXECUTION_ENABLED
                else "لا يوجد أمر شراء أو بيع من هذا التنبيه."
            ),
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
# NOTIFICATION MATERIALITY
# ============================================================

def notification_score_bucket(
    value: Any,
) -> Optional[float]:
    number = safe_float(
        value
    )

    if number is None:
        return None

    step = float(
        NOTIFICATION_SCORE_STEP
    )

    return round(
        round(
            number
            /
            step
        )
        *
        step,
        2,
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

        # Bucket scores so tiny changes do not spam Telegram.
        "apex_score_bucket":
            notification_score_bucket(
                apex.get(
                    "score"
                )
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

    jalwe = safe_dict(
        payload.get(
            "jalwe",
            {},
        )
    )

    jalwe_state = str(
        jalwe.get(
            "state",
            "",
        )
        or ""
    ).upper()

    last_watch_update = state.setdefault(
        "last_watch_update",
        {},
    )

    now_epoch = time.time()

    last_watch_epoch = safe_float(
        last_watch_update.get(
            symbol
        )
    ) or 0.0

    watching_heartbeat_due = bool(
        WATCHING_HEARTBEAT_ENABLED
        and jalwe_state == "WATCHING"
        and (
            now_epoch
            - last_watch_epoch
            >= WATCHING_UPDATE_SECONDS
        )
    )

    same_fingerprint = (
        previous == fingerprint
    )

    if (
        same_fingerprint
        and not watching_heartbeat_due
    ):

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

        if jalwe_state == "WATCHING":
            last_watch_update[
                symbol
            ] = now_epoch
        else:
            last_watch_update.pop(
                symbol,
                None,
            )

        save_state(
            state
        )

        if (
            same_fingerprint
            and watching_heartbeat_due
        ):
            return (
                "WATCHING_HEARTBEAT_SENT"
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
# PAPER LIFECYCLE TELEGRAM ALERTS
# ============================================================

def _strategy_equity_for_alert() -> float:
    realized = (
        database
        .get_strategy_realized_pnl_total()
    )

    adjustments = 0.0

    if str(
        getattr(
            settings,
            "CAPITAL_MODE",
            "strategy_wallet",
        )
        or "strategy_wallet"
    ).strip().lower() == "strategy_wallet":
        adjustments = (
            database
            .get_strategy_capital_adjustment_total()
        )

    return float(
        settings.STRATEGY_STARTING_CAPITAL
    ) + float(
        adjustments
    ) + float(
        realized
    )


def send_lifecycle_once(
    state: dict,
    event_key: str,
    message: str,
) -> str:
    if not LIFECYCLE_ALERTS_ENABLED:
        return "LIFECYCLE_ALERTS_DISABLED"

    event_key = str(
        event_key or ""
    ).strip()

    if not event_key:
        return "EMPTY_EVENT_KEY"

    lifecycle_events = state.setdefault(
        "lifecycle_events",
        {},
    )

    if event_key in lifecycle_events:
        return "ALREADY_SENT"

    success, error = send_telegram(
        message
    )

    if not success:
        if error == "TELEGRAM_NOT_CONFIGURED":
            return "TELEGRAM_NOT_CONFIGURED"

        logger.warning(
            "Lifecycle Telegram alert failed | "
            "event=%s error=%s",
            event_key,
            error,
        )

        return "FAILED"

    lifecycle_events[
        event_key
    ] = utc_now_iso()

    if len(
        lifecycle_events
    ) > 500:
        items = list(
            lifecycle_events.items()
        )[-500:]

        state[
            "lifecycle_events"
        ] = dict(
            items
        )

    save_state(
        state
    )

    return "SENT"


def _managed_trade_snapshot(
    managed_trade_id: Optional[str],
) -> Optional[Any]:
    trade_id = str(
        managed_trade_id or ""
    ).strip()

    if not trade_id:
        return None

    try:
        return database.load_managed_trade(
            trade_id
        )
    except Exception:
        logger.exception(
            "Unable to load managed trade for lifecycle alert | "
            "trade_id=%s",
            trade_id,
        )
        return None


def notify_execution_lifecycle(
    payload: dict,
    state: dict,
) -> None:
    jalwe = safe_dict(
        payload.get(
            "jalwe",
            {},
        )
    )

    execution = safe_dict(
        payload.get(
            "execution",
            {},
        )
    )

    symbol = str(
        payload.get(
            "symbol",
            "",
        )
        or ""
    ).upper()

    if not symbol:
        return

    if bool(
        jalwe.get(
            "ready_for_execution",
            False,
        )
    ):
        ready_key = (
            "READY:"
            + symbol
            + ":"
            + str(
                jalwe.get(
                    "entry_price",
                    "",
                )
            )
            + ":"
            + str(
                jalwe.get(
                    "stop_price",
                    "",
                )
            )
        )

        send_lifecycle_once(
            state,
            ready_key,
            (
                "🎯 JALWE — Trigger مؤكد\n\n"
                f"📌 السهم: ${symbol}\n"
                f"⭐ الدرجة: ${jalwe.get('setup_grade') or 'N/A'}\n"
                f"💲 Entry: ${format_number(jalwe.get('entry_price'))}\n"
                f"🛑 Stop: ${format_number(jalwe.get('stop_price'))}\n"
                f"🎯 T1: ${format_number(jalwe.get('target_1'))}\n"
                f"🎯 T2: ${format_number(jalwe.get('target_2'))}\n"
                f"🎯 T3: ${format_number(jalwe.get('target_3'))}\n"
                f"🔢 Qty: ${jalwe.get('quantity', 0)}\n"
                "📄 ينتقل الآن لمسار تنفيذ PAPER."
            ),
        )

    orchestrator_state = str(
        execution.get(
            "orchestrator_state",
            "",
        )
        or ""
    ).upper()

    if orchestrator_state != "ENTRY_MANAGED":
        return

    managed_trade_id = str(
        execution.get(
            "managed_trade_id",
            "",
        )
        or ""
    )

    broker_order_id = str(
        execution.get(
            "broker_order_id",
            "",
        )
        or ""
    )

    trade = _managed_trade_snapshot(
        managed_trade_id
    )

    if trade is None:
        return

    entry_key = (
        "ENTRY_MANAGED:"
        + (
            broker_order_id
            or managed_trade_id
            or symbol
        )
    )

    send_lifecycle_once(
        state,
        entry_key,
        (
            "🟢 JALWE — تم الدخول PAPER\n\n"
            f"📌 السهم: ${symbol}\n"
            f"💲 سعر الدخول الفعلي: ${format_number(trade.entry_price)}\n"
            f"🔢 الكمية: ${trade.initial_quantity}\n"
            f"🛑 الستوب: ${format_number(trade.current_stop)}\n"
            f"🎯 T1: ${format_number(trade.target_1)}\n"
            f"🎯 T2: ${format_number(trade.target_2)}\n"
            f"🎯 T3: ${format_number(trade.target_3)}\n"
            f"💼 Trade ID: ${managed_trade_id}"
        ),
    )

    execution_metadata = safe_dict(
        execution.get(
            "metadata",
            {},
        )
    )

    protective = safe_dict(
        execution_metadata.get(
            "protective_stop",
            {},
        )
    )

    if bool(
        protective.get(
            "active",
            False,
        )
    ):
        stop_order_id = str(
            protective.get(
                "order_id",
                "",
            )
            or ""
        )

        stop_key = (
            "PROTECTIVE_STOP:"
            + (
                stop_order_id
                or managed_trade_id
                or symbol
            )
        )

        send_lifecycle_once(
            state,
            stop_key,
            (
                "🛡 JALWE — حماية Alpaca مفعلة\n\n"
                f"📌 السهم: ${symbol}\n"
                f"🛑 Stop: ${format_number(protective.get('stop_price'))}\n"
                f"🔢 الكمية المحمية: ${protective.get('quantity', trade.remaining_quantity)}\n"
                "✅ أمر GTC موجود عند الوسيط حتى لو توقف JALWE مؤقتًا."
            ),
        )


def _management_action_title(
    action: str,
) -> str:
    titles = {
        "TAKE_PROFIT_1": "🎯 تم تحقيق Target 1",
        "TAKE_PROFIT_2": "🎯 تم تحقيق Target 2",
        "TAKE_PROFIT_3": "🎯 تم تحقيق Target 3",
        "EXIT_STOP": "🛑 تم الخروج على Stop",
        "EXIT_TRAILING": "📉 تم الخروج على Trailing Stop",
        "EXIT_TARGET_3": "🎯 تم إغلاق Target 3",
        "LOCK_T1": "🔒 تم نقل الحماية إلى Break-even",
        "LOCK_T2": "🔒 تم رفع الحماية إلى Target 1",
        "START_RUNNER": "🏃 بدأ Runner",
        "START_TRAILING": "📈 بدأ Trailing",
    }

    return titles.get(
        action,
        "📌 تحديث إدارة الصفقة",
    )


def notify_management_lifecycle(
    state: dict,
    trade_id: str,
    trade: Any,
    decision: Any,
    *,
    broker_order_id: Optional[str] = None,
    fill_price: Optional[float] = None,
    realized_increment: float = 0.0,
) -> None:
    action = str(
        getattr(
            getattr(
                decision,
                "action",
                "",
            ),
            "value",
            getattr(
                decision,
                "action",
                "",
            ),
        )
        or ""
    ).upper()

    if action in {
        "",
        "HOLD",
    }:
        return

    event_key = (
        "MANAGE:"
        + str(
            broker_order_id
            or (
                str(trade_id)
                + ":"
                + action
                + ":"
                + str(
                    getattr(
                        trade,
                        "stage",
                        "",
                    )
                )
                + ":"
                + str(
                    getattr(
                        trade,
                        "remaining_quantity",
                        "",
                    )
                )
            )
        )
    )

    realized_total = safe_float(
        safe_dict(
            getattr(
                trade,
                "metadata",
                {},
            )
        ).get(
            "realized_pnl"
        )
    ) or 0.0

    lines = [
        _management_action_title(
            action
        ),
        "",
        (
            "📌 السهم: "
            + str(
                getattr(
                    trade,
                    "symbol",
                    "",
                )
            )
        ),
        (
            "💲 السعر الحالي: $"
            + format_number(
                getattr(
                    decision,
                    "current_price",
                    None,
                )
            )
        ),
        (
            "🔢 الكمية المتبقية: "
            + str(
                getattr(
                    trade,
                    "remaining_quantity",
                    0,
                )
            )
        ),
        (
            "🛡 الستوب الحالي: $"
            + format_number(
                getattr(
                    trade,
                    "current_stop",
                    None,
                )
            )
        ),
    ]

    if fill_price is not None:
        lines.append(
            "💲 سعر التنفيذ: $"
            + format_number(
                fill_price
            )
        )

    if abs(
        float(
            realized_increment
        )
    ) > 0.0000001:
        lines.append(
            "💵 PnL لهذه العملية: $"
            + format_number(
                realized_increment
            )
        )

    lines.append(
        "💰 PnL المحقق للصفقة: $"
        + format_number(
            realized_total
        )
    )

    if int(
        getattr(
            trade,
            "remaining_quantity",
            0,
        )
        or 0
    ) == 0:
        try:
            equity = _strategy_equity_for_alert()

            lines.extend(
                [
                    "",
                    "✅ الصفقة أغلقت بالكامل",
                    (
                        "💼 قيمة محفظة JALWE الآن: $"
                        + format_number(
                            equity
                        )
                    ),
                    (
                        "🧠 تم تسجيل النتيجة في سجل JALWE "
                        "وستدخل دورة التعلم عند توفر شروط التعلم."
                    ),
                ]
            )

        except Exception as exc:
            logger.warning(
                "Unable to calculate strategy equity for "
                "lifecycle alert: %s",
                exc,
            )

    send_lifecycle_once(
        state,
        event_key,
        "\n".join(
            lines
        ),
    )


def notify_recovered_protective_stop_fills(
    state: dict,
    recovery_result: dict,
) -> None:
    for item in safe_list(
        recovery_result.get(
            "results",
            [],
        )
    ):
        item = safe_dict(
            item
        )

        protective = safe_dict(
            item.get(
                "protective_stop",
                {},
            )
        )

        if not bool(
            protective.get(
                "filled",
                False,
            )
        ):
            continue

        trade_id = str(
            item.get(
                "trade_id",
                "",
            )
            or ""
        )

        order_id = str(
            protective.get(
                "order_id",
                "",
            )
            or ""
        )

        row = database.get_managed_trade_row(
            trade_id
        )

        if not row:
            continue

        metadata = {}

        try:
            metadata = json.loads(
                row.get(
                    "metadata_json",
                    "{}",
                )
                or "{}"
            )
        except Exception:
            metadata = {}

        realized_total = safe_float(
            metadata.get(
                "realized_pnl"
            )
        ) or 0.0

        try:
            equity_text = (
                "$"
                + format_number(
                    _strategy_equity_for_alert()
                )
            )
        except Exception:
            equity_text = "N/A"

        send_lifecycle_once(
            state,
            (
                "PROTECTIVE_STOP_FILLED:"
                + (
                    order_id
                    or trade_id
                )
            ),
            (
                "🛡 JALWE — Alpaca نفذ Protective Stop\n\n"
                f"📌 السهم: ${row.get('symbol', '')}\n"
                f"💲 سعر التنفيذ: ${format_number(protective.get('fill_price'))}\n"
                f"🔢 الكمية المنفذة: ${protective.get('filled_quantity', 0)}\n"
                f"💰 PnL المحقق للصفقة: ${format_number(realized_total)}\n"
                f"💼 قيمة محفظة JALWE الآن: ${equity_text}\n"
                "🧠 النتيجة محفوظة في سجل JALWE للتعلم."
            ),
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
        (
            "PAPER AUTO"
            if ORDER_EXECUTION_ENABLED
            else "DISABLED"
        ),
    )

    print(
        "======================================"
    )


# ============================================================
# BROKER PROTECTIVE STOP SYNC
# ============================================================

def _raw_order_status(
    order: Any,
) -> str:
    return str(
        getattr(
            getattr(
                order,
                "status",
                "",
            ),
            "value",
            getattr(
                order,
                "status",
                "",
            ),
        )
        or ""
    ).strip().lower()


def _cancel_protective_stop(
    trade_id: str,
    trade: Any,
    execution_engine: Any,
) -> str:
    metadata = (
        trade.metadata
        if isinstance(
            trade.metadata,
            dict,
        )
        else {}
    )

    order_id = str(
        metadata.get(
            "protective_stop_order_id",
            "",
        )
        or ""
    ).strip()

    if not order_id:
        return "NONE"

    try:
        raw = (
            execution_engine
            .broker
            .get_order(
                order_id
            )
        )

        status = _raw_order_status(
            raw
        )

        if status == "filled":
            metadata[
                "protective_stop_active"
            ] = False
            metadata[
                "protective_stop_status"
            ] = "filled"
            trade.metadata = metadata
            database.save_managed_trade(
                trade_id,
                trade,
            )
            return "FILLED"

        if status in {
            "canceled",
            "cancelled",
            "expired",
            "done_for_day",
            "rejected",
            "replaced",
        }:
            metadata[
                "protective_stop_active"
            ] = False
            metadata[
                "protective_stop_status"
            ] = status
            trade.metadata = metadata
            database.save_managed_trade(
                trade_id,
                trade,
            )
            return "CANCELED"

        execution_engine.cancel_protective_stop(
            order_id
        )

        deadline = (
            time.time()
            + 5.0
        )

        while time.time() < deadline:
            time.sleep(
                0.25
            )

            raw = (
                execution_engine
                .broker
                .get_order(
                    order_id
                )
            )

            status = _raw_order_status(
                raw
            )

            if status == "filled":
                metadata[
                    "protective_stop_active"
                ] = False
                metadata[
                    "protective_stop_status"
                ] = "filled"
                trade.metadata = metadata
                database.save_managed_trade(
                    trade_id,
                    trade,
                )
                return "FILLED"

            if status in {
                "canceled",
                "cancelled",
                "expired",
                "done_for_day",
                "rejected",
                "replaced",
            }:
                metadata[
                    "protective_stop_active"
                ] = False
                metadata[
                    "protective_stop_status"
                ] = status
                trade.metadata = metadata
                database.save_managed_trade(
                    trade_id,
                    trade,
                )
                return "CANCELED"

        raise RuntimeError(
            "Protective stop cancellation "
            "did not reach a terminal state."
        )

    except Exception:
        logger.exception(
            "Protective stop cancellation failed | "
            "trade_id=%s symbol=%s order_id=%s",
            trade_id,
            getattr(
                trade,
                "symbol",
                "",
            ),
            order_id,
        )
        raise


def _sync_protective_stop(
    trade_id: str,
    trade: Any,
    execution_engine: Any,
) -> dict[str, Any]:
    if not bool(
        settings
        .BROKER_PROTECTIVE_STOP_ENABLED
    ):
        return {
            "enabled": False,
            "active": False,
            "reason": "DISABLED",
        }

    quantity = int(
        trade.remaining_quantity
    )

    if quantity <= 0:
        return {
            "enabled": True,
            "active": False,
            "reason": "NO_REMAINING_POSITION",
        }

    desired_stop = float(
        trade.current_stop
    )

    metadata = (
        trade.metadata
        if isinstance(
            trade.metadata,
            dict,
        )
        else {}
    )

    order_id = str(
        metadata.get(
            "protective_stop_order_id",
            "",
        )
        or ""
    ).strip()

    stored_qty = int(
        metadata.get(
            "protective_stop_quantity",
            0,
        )
        or 0
    )

    try:
        stored_stop = float(
            metadata.get(
                "protective_stop_price",
                0.0,
            )
            or 0.0
        )
    except (
        TypeError,
        ValueError,
    ):
        stored_stop = 0.0

    if order_id:
        try:
            raw = (
                execution_engine
                .broker
                .get_order(
                    order_id
                )
            )

            status = _raw_order_status(
                raw
            )

            active = status in {
                "new",
                "accepted",
                "pending_new",
                "accepted_for_bidding",
                "held",
                "partially_filled",
                "pending_replace",
                "pending_cancel",
            }

            if (
                active
                and stored_qty == quantity
                and abs(
                    stored_stop
                    - desired_stop
                )
                <= 0.0001
            ):
                metadata[
                    "protective_stop_active"
                ] = True
                metadata[
                    "protective_stop_status"
                ] = status
                trade.metadata = metadata

                return {
                    "enabled": True,
                    "active": True,
                    "order_id": order_id,
                    "quantity": quantity,
                    "stop_price": desired_stop,
                    "status": status,
                    "changed": False,
                }

            if status == "filled":
                metadata[
                    "protective_stop_active"
                ] = False
                metadata[
                    "protective_stop_status"
                ] = "filled"
                trade.metadata = metadata
                database.save_managed_trade(
                    trade_id,
                    trade,
                )

                return {
                    "enabled": True,
                    "active": False,
                    "filled": True,
                    "order_id": order_id,
                }

        except Exception:
            logger.exception(
                "Unable to inspect protective stop | "
                "trade_id=%s symbol=%s",
                trade_id,
                trade.symbol,
            )
            raise

        cancel_state = (
            _cancel_protective_stop(
                trade_id,
                trade,
                execution_engine,
            )
        )

        if cancel_state == "FILLED":
            return {
                "enabled": True,
                "active": False,
                "filled": True,
                "order_id": order_id,
            }

    stop_order = (
        execution_engine
        .submit_protective_stop(
            symbol=trade.symbol,
            quantity=quantity,
            stop_price=desired_stop,
        )
    )

    database.save_broker_order(
        stop_order
    )

    metadata = (
        trade.metadata
        if isinstance(
            trade.metadata,
            dict,
        )
        else {}
    )

    metadata[
        "protective_stop_order_id"
    ] = stop_order.order_id

    metadata[
        "protective_stop_client_order_id"
    ] = stop_order.client_order_id

    metadata[
        "protective_stop_quantity"
    ] = quantity

    metadata[
        "protective_stop_price"
    ] = desired_stop

    metadata[
        "protective_stop_status"
    ] = stop_order.status.value

    metadata[
        "protective_stop_active"
    ] = True

    metadata[
        "protective_stop_fill_applied"
    ] = False

    trade.metadata = metadata

    database.save_managed_trade(
        trade_id,
        trade,
    )

    database.log_event(
        event_type=(
            "BROKER_PROTECTIVE_STOP_SYNC"
        ),
        severity="INFO",
        message=(
            f"{trade.symbol}: broker protective "
            f"stop synced qty={quantity} "
            f"stop={desired_stop}"
        ),
        metadata={
            "trade_id": trade_id,
            "symbol": trade.symbol,
            "quantity": quantity,
            "stop_price": desired_stop,
            "order_id": stop_order.order_id,
        },
    )

    return {
        "enabled": True,
        "active": True,
        "order_id": stop_order.order_id,
        "quantity": quantity,
        "stop_price": desired_stop,
        "status": stop_order.status.value,
        "changed": True,
    }


# ============================================================
# ACTIVE PAPER TRADE MANAGEMENT
# ============================================================

def manage_active_paper_trades(
    state: dict,
) -> int:
    """
    Manage confirmed Alpaca PAPER positions.

    Safe lifecycle:
        load persisted ManagedTrade
        -> refresh price
        -> TradeManager decision
        -> optional PAPER exit
        -> broker reconciliation
        -> persist updated state

    No live-trading path exists here.
    """

    if not auto_paper_execution_ready():
        return 0

    recovery = get_recovery_engine()

    try:
        recovery_result = recovery.recover_all()
    except Exception as exc:
        logger.exception(
            "Active trade recovery failed: %s",
            exc,
        )
        raise

    if not bool(
        recovery_result.get(
            "safe_to_trade",
            False,
        )
    ):
        logger.warning(
            "Active trade management blocked by recovery safety."
        )
        return 0

    notify_recovered_protective_stop_fills(
        state,
        recovery_result,
    )

    active = database.load_active_managed_trades()

    if not active:
        return 0

    market_data = get_market_data()
    trade_manager = get_trade_manager()
    execution_engine = get_execution_engine()
    reconciliation_engine = get_reconciliation_engine()

    managed_count = 0

    for trade_id, trade in active.items():
        try:
            current_price = float(
                market_data.get_last_price(
                    trade.symbol
                )
            )

            decision = trade_manager.evaluate(
                trade,
                current_price,
            )

            # HOLD / stop-ratchet / runner-state changes still
            # need persistence for restart safety.
            if decision.action not in EXIT_ACTIONS:
                database.save_managed_trade(
                    trade_id,
                    trade,
                )

                stop_sync = (
                    _sync_protective_stop(
                        trade_id,
                        trade,
                        execution_engine,
                    )
                )

                if stop_sync.get(
                    "filled",
                    False,
                ):
                    # RecoveryEngine applies the broker fill
                    # on the next loop before any new action.
                    managed_count += 1
                    continue

                notify_management_lifecycle(
                    state,
                    trade_id,
                    trade,
                    decision,
                )

                managed_count += 1
                continue

            if int(decision.quantity or 0) <= 0:
                database.save_managed_trade(
                    trade_id,
                    trade,
                )
                managed_count += 1
                continue

            protective_cancel = (
                _cancel_protective_stop(
                    trade_id,
                    trade,
                    execution_engine,
                )
            )

            if protective_cancel == "FILLED":
                # Broker-side protection won the race.
                # Do not submit a second SELL. RecoveryEngine
                # will reconcile the stop fill next loop.
                managed_count += 1
                continue

            broker_order = execution_engine.submit_exit(
                symbol=trade.symbol,
                quantity=int(decision.quantity),
                requested_price=current_price,
                reason=str(decision.reason or ""),
            )

            trade_manager.register_exit_order(
                trade,
                decision,
                broker_order,
            )

            database.save_managed_trade(
                trade_id,
                trade,
            )

            reconciled = (
                reconciliation_engine
                .wait_for_terminal_state(
                    broker_order,
                    timeout_seconds=30.0,
                    poll_interval_seconds=1.0,
                )
            )

            reconciliation_result = (
                trade_manager
                .apply_exit_reconciliation(
                    trade,
                    reconciled,
                )
            )

            new_fill_quantity = int(
                reconciliation_result.get(
                    "new_fill_quantity",
                    0,
                )
                or 0
            )

            if new_fill_quantity > 0:
                cumulative_filled = int(
                    reconciliation_result.get(
                        "cumulative_filled",
                        0,
                    )
                    or 0
                )

                event_key = (
                    f"{reconciled.order_id}:"
                    f"{cumulative_filled}"
                )

                filled_at = getattr(
                    reconciled,
                    "filled_at",
                    None,
                )

                event_time = (
                    filled_at.isoformat()
                    if filled_at is not None
                    else utc_now_iso()
                )

                database.record_strategy_pnl_event(
                    event_key=event_key,
                    trade_id=trade_id,
                    order_id=reconciled.order_id,
                    symbol=trade.symbol,
                    action=decision.action.value,
                    quantity=new_fill_quantity,
                    fill_price=(
                        reconciliation_result.get(
                            "fill_price"
                        )
                    ),
                    entry_price=trade.entry_price,
                    realized_pnl=float(
                        reconciliation_result.get(
                            "realized_pnl_increment",
                            0.0,
                        )
                        or 0.0
                    ),
                    event_time=event_time,
                    metadata={
                        "stage": trade.stage.value,
                        "remaining_quantity": (
                            trade.remaining_quantity
                        ),
                    },
                )

            database.save_managed_trade(
                trade_id,
                trade,
            )

            if int(
                trade.remaining_quantity
            ) > 0:
                _sync_protective_stop(
                    trade_id,
                    trade,
                    execution_engine,
                )

            notify_management_lifecycle(
                state,
                trade_id,
                trade,
                decision,
                broker_order_id=(
                    reconciled.order_id
                ),
                fill_price=(
                    reconciliation_result.get(
                        "fill_price"
                    )
                ),
                realized_increment=float(
                    reconciliation_result.get(
                        "realized_pnl_increment",
                        0.0,
                    )
                    or 0.0
                ),
            )

            database.log_event(
                event_type="PAPER_TRADE_MANAGEMENT",
                severity="INFO",
                message=(
                    f"{trade.symbol}: "
                    f"{decision.action.value} "
                    f"qty={decision.quantity} "
                    f"price={current_price}"
                ),
                metadata={
                    "trade_id": trade_id,
                    "symbol": trade.symbol,
                    "action": decision.action.value,
                    "quantity": decision.quantity,
                    "current_price": current_price,
                    "stage": trade.stage.value,
                    "remaining_quantity": (
                        trade.remaining_quantity
                    ),
                    "broker_order_id": (
                        reconciled.order_id
                    ),
                    "broker_status": (
                        reconciled.status.value
                    ),
                },
            )

            managed_count += 1

        except Exception as exc:
            logger.exception(
                "Active trade management failed | "
                "trade_id=%s symbol=%s error=%s",
                trade_id,
                getattr(trade, "symbol", ""),
                exc,
            )

            try:
                database.log_event(
                    event_type="PAPER_TRADE_MANAGEMENT_ERROR",
                    severity="ERROR",
                    message=str(exc),
                    metadata={
                        "trade_id": trade_id,
                        "symbol": getattr(
                            trade,
                            "symbol",
                            "",
                        ),
                    },
                )
            except Exception:
                pass

    return managed_count


# ============================================================
# EMERGENCY PAPER CLOSE
# ============================================================

def process_emergency_paper_close() -> int:
    """
    Close all JALWE-managed Alpaca PAPER positions.

    The request persists until every managed trade is closed.
    New entries are paused separately by RuntimeControls.
    """

    if not emergency_close_requested():
        return 0

    if not auto_paper_execution_ready():
        logger.warning(
            "Emergency close requested but PAPER execution "
            "configuration is not ready."
        )
        return 0

    recovery = get_recovery_engine()

    try:
        recovery_result = recovery.recover_all()
    except Exception as exc:
        logger.exception(
            "Emergency close recovery failed: %s",
            exc,
        )
        return 0

    if not bool(
        recovery_result.get(
            "safe_to_trade",
            False,
        )
    ):
        logger.warning(
            "Emergency close waiting for recovery safety."
        )
        return 0

    active = database.load_active_managed_trades()

    if not active:
        clear_emergency_close_request(
            updated_by="JALWE_WATCHER",
        )

        try:
            database.log_event(
                event_type="PAPER_EMERGENCY_CLOSE_COMPLETE",
                severity="INFO",
                message=(
                    "Emergency PAPER close completed; "
                    "no active managed trades remain."
                ),
                metadata={
                    "timestamp": utc_now_iso(),
                },
            )
        except Exception:
            pass

        return 0

    market_data = get_market_data()
    trade_manager = get_trade_manager()
    execution_engine = get_execution_engine()
    reconciliation_engine = get_reconciliation_engine()

    processed = 0
    errors = 0

    for trade_id, trade in active.items():
        try:
            if trade.has_pending_exit:
                continue

            quantity = int(
                trade.remaining_quantity
            )

            if quantity <= 0:
                database.save_managed_trade(
                    trade_id,
                    trade,
                )
                continue

            current_price = float(
                market_data.get_last_price(
                    trade.symbol
                )
            )

            decision = TradeManagementDecision(
                symbol=trade.symbol,
                action=TradeAction.EXIT_TRAILING,
                quantity=quantity,
                current_price=current_price,
                stage_before=trade.stage,
                stage_after=trade.stage,
                stop_before=trade.current_stop,
                stop_after=trade.current_stop,
                reason="EMERGENCY_PAPER_CLOSE",
                metadata={
                    "emergency": True,
                },
            )

            broker_order = execution_engine.submit_exit(
                symbol=trade.symbol,
                quantity=quantity,
                requested_price=current_price,
                reason="EMERGENCY_PAPER_CLOSE",
            )

            trade_manager.register_exit_order(
                trade,
                decision,
                broker_order,
            )

            database.save_managed_trade(
                trade_id,
                trade,
            )

            reconciled = (
                reconciliation_engine
                .wait_for_terminal_state(
                    broker_order,
                    timeout_seconds=30.0,
                    poll_interval_seconds=1.0,
                )
            )

            reconciliation_result = (
                trade_manager
                .apply_exit_reconciliation(
                    trade,
                    reconciled,
                )
            )

            new_fill_quantity = int(
                reconciliation_result.get(
                    "new_fill_quantity",
                    0,
                )
                or 0
            )

            if new_fill_quantity > 0:
                cumulative_filled = int(
                    reconciliation_result.get(
                        "cumulative_filled",
                        0,
                    )
                    or 0
                )

                event_key = (
                    f"{reconciled.order_id}:"
                    f"{cumulative_filled}"
                )

                filled_at = getattr(
                    reconciled,
                    "filled_at",
                    None,
                )

                event_time = (
                    filled_at.isoformat()
                    if filled_at is not None
                    else utc_now_iso()
                )

                database.record_strategy_pnl_event(
                    event_key=event_key,
                    trade_id=trade_id,
                    order_id=reconciled.order_id,
                    symbol=trade.symbol,
                    action="EMERGENCY_CLOSE",
                    quantity=new_fill_quantity,
                    fill_price=(
                        reconciliation_result.get(
                            "fill_price"
                        )
                    ),
                    entry_price=trade.entry_price,
                    realized_pnl=float(
                        reconciliation_result.get(
                            "realized_pnl_increment",
                            0.0,
                        )
                        or 0.0
                    ),
                    event_time=event_time,
                    metadata={
                        "emergency": True,
                        "stage": trade.stage.value,
                        "remaining_quantity": (
                            trade.remaining_quantity
                        ),
                    },
                )

            database.save_managed_trade(
                trade_id,
                trade,
            )

            database.log_event(
                event_type="PAPER_EMERGENCY_CLOSE_ORDER",
                severity="WARNING",
                message=(
                    f"{trade.symbol}: emergency PAPER "
                    f"close qty={quantity}"
                ),
                metadata={
                    "trade_id": trade_id,
                    "symbol": trade.symbol,
                    "quantity": quantity,
                    "broker_order_id": (
                        reconciled.order_id
                    ),
                    "broker_status": (
                        reconciled.status.value
                    ),
                    "remaining_quantity": (
                        trade.remaining_quantity
                    ),
                },
            )

            processed += 1

        except Exception as exc:
            errors += 1

            logger.exception(
                "Emergency PAPER close failed | "
                "trade_id=%s symbol=%s error=%s",
                trade_id,
                getattr(
                    trade,
                    "symbol",
                    "",
                ),
                exc,
            )

            try:
                database.log_event(
                    event_type="PAPER_EMERGENCY_CLOSE_ERROR",
                    severity="ERROR",
                    message=str(exc),
                    metadata={
                        "trade_id": trade_id,
                        "symbol": getattr(
                            trade,
                            "symbol",
                            "",
                        ),
                    },
                )
            except Exception:
                pass

    remaining = database.load_active_managed_trades()

    if not remaining and errors == 0:
        clear_emergency_close_request(
            updated_by="JALWE_WATCHER",
        )

        database.log_event(
            event_type="PAPER_EMERGENCY_CLOSE_COMPLETE",
            severity="INFO",
            message=(
                "All JALWE-managed PAPER positions "
                "were closed."
            ),
            metadata={
                "processed": processed,
                "timestamp": utc_now_iso(),
            },
        )

    return processed


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
        # JALWE DECISION / OPTIONAL PAPER EXECUTION
        # ====================================================

        execution_result = None

        if (
            auto_paper_execution_ready()
            and new_entries_allowed()
        ):
            execution_result = (
                get_paper_trade_orchestrator()
                .run_symbol(
                    symbol
                )
            )

            decision = (
                execution_result.decision
            )

            if decision is None:
                raise RuntimeError(
                    "PaperTradeOrchestrator returned "
                    "no FinalTradeDecision."
                )

        else:
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

        jalwe_state = str(
            safe_dict(
                payload.get(
                    "jalwe",
                    {},
                )
            ).get(
                "state",
                "",
            )
            or ""
        ).upper()

        watching = state.setdefault(
            "watching",
            {},
        )

        if jalwe_state == "WATCHING":
            watching[
                symbol
            ] = True
        else:
            watching.pop(
                symbol,
                None,
            )

        runtime_controls = (
            load_runtime_controls()
        )

        payload[
            "runtime_controls"
        ] = runtime_controls

        if (
            auto_paper_execution_ready()
            and not new_entries_allowed()
        ):
            payload["execution"][
                "blocked_by_runtime_pause"
            ] = True

        if execution_result is not None:
            execution_state = getattr(
                execution_result.state,
                "value",
                str(execution_result.state),
            )

            payload["execution"] = {
                "watcher_execution_enabled": True,
                "execution_engine_called": True,
                "broker_order_submitted": bool(
                    execution_result.broker_order_id
                ),
                "orchestrator_state": execution_state,
                "intent_id": execution_result.intent_id,
                "client_order_id": (
                    execution_result.client_order_id
                ),
                "broker_order_id": (
                    execution_result.broker_order_id
                ),
                "managed_trade_id": (
                    execution_result.managed_trade_id
                ),
                "message": execution_result.message,
                "warnings": list(
                    execution_result.warnings or []
                ),
                "metadata": dict(
                    execution_result.metadata or {}
                ),
            }

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

        notify_execution_lifecycle(
            payload,
            state,
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
        (
            "PAPER_AUTO"
            if ORDER_EXECUTION_ENABLED
            else "DISABLED"
        ),
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
        "ORDER EXECUTION:",
        (
            "PAPER AUTO ENABLED"
            if ORDER_EXECUTION_ENABLED
            else "DISABLED"
        ),
    )

    print(
        "EXECUTION ENGINE CALLED:",
        (
            "TRUE WHEN JALWE IS READY"
            if CALL_EXECUTION_ENGINE
            else "FALSE"
        ),
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

            emergency_closes = (
                process_emergency_paper_close()
            )

            managed_trades = (
                manage_active_paper_trades(
                    state
                )
            )

            if emergency_closes:
                print(
                    utc_now_iso(),
                    "| emergency PAPER closes:",
                    emergency_closes,
                )

            heartbeat(
                discovered,
                processed,
            )

            if managed_trades:
                print(
                    utc_now_iso(),
                    "| active paper trades managed:",
                    managed_trades,
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