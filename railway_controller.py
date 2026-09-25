from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from core.runtime_controls import (
    load_runtime_controls,
    request_emergency_close,
    set_new_entries_allowed,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.getenv(
        "JALWE_CONTROLLER_DATA_DIR",
        str(BASE_DIR / "data"),
    )
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "railway_controller_state.json"

APEX_SCRIPT = BASE_DIR / "apex_research_loop.py"
JALWE_SCRIPT = BASE_DIR / "jalwe_research_watcher.py"

TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    or os.getenv("TELEGRAM_TOKEN", "").strip()
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ALPACA_API_KEY = (
    os.getenv("ALPACA_API_KEY", "").strip()
    or os.getenv("APCA_API_KEY_ID", "").strip()
)
ALPACA_SECRET_KEY = (
    os.getenv("ALPACA_SECRET_KEY", "").strip()
    or os.getenv("APCA_API_SECRET_KEY", "").strip()
)
ALPACA_BASE_URL = (
    os.getenv(
        "ALPACA_BASE_URL",
        os.getenv(
            "APCA_API_BASE_URL",
            "https://paper-api.alpaca.markets",
        ),
    )
    .strip()
    .rstrip("/")
)

AUTOSTART = os.getenv("JALWE_AUTOSTART", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
AUTO_RESTART = os.getenv("JALWE_AUTO_RESTART", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

ERROR_ALERTS_ENABLED = os.getenv(
    "JALWE_ERROR_ALERTS",
    "true",
).strip().lower() in {
    "1", "true", "yes", "on"
}

ERROR_ALERT_COOLDOWN_SECONDS = max(
    60,
    int(os.getenv("JALWE_ERROR_ALERT_COOLDOWN_SECONDS", "300")),
)

ORDER_ALERTS_ENABLED = os.getenv(
    "JALWE_ORDER_ALERTS",
    "true",
).strip().lower() in {
    "1", "true", "yes", "on"
}

AUTO_DAILY_REPORT = os.getenv(
    "JALWE_DAILY_REPORT",
    "true",
).strip().lower() in {
    "1", "true", "yes", "on"
}

AUTO_WEEKLY_REPORT = os.getenv(
    "JALWE_WEEKLY_REPORT",
    "true",
).strip().lower() in {
    "1", "true", "yes", "on"
}

AUTO_LEARNING = os.getenv(
    "JALWE_LEARNING_ENABLED",
    "true",
).strip().lower() in {
    "1", "true", "yes", "on"
}

LEARNING_INTERVAL_SECONDS = max(
    900,
    int(
        os.getenv(
            "JALWE_LEARNING_INTERVAL_SECONDS",
            "3600",
        )
    ),
)

ORDER_ALERT_POLL_SECONDS = max(
    10,
    int(os.getenv("JALWE_ORDER_ALERT_POLL_SECONDS", "15")),
)

NY_TZ = ZoneInfo("America/New_York")


# ============================================================
# TELEGRAM UI
# ============================================================

BTN_START = "🟢 تشغيل الكل"
BTN_STOP = "🔴 إيقاف الكل"
BTN_RESTART = "♻️ إعادة تشغيل النظام"
BTN_STATUS = "📊 حالة النظام"

BTN_PORTFOLIO = "💼 محفظتي"
BTN_TODAY = "📅 ربح اليوم"
BTN_WEEK = "📆 تقرير الأسبوع"
BTN_TRADES = "🧾 آخر الصفقات"
BTN_LEARNING = "🧠 حالة التعلم"
BTN_LEARN_NOW = "🎓 تعلم الآن"

BTN_PAUSE_ENTRIES = "⏸ منع صفقات جديدة"
BTN_RESUME_ENTRIES = "▶️ السماح بصفقات جديدة"
BTN_EMERGENCY_CLOSE = "🚨 إغلاق صفقات PAPER"
BTN_CONFIRM_EMERGENCY_CLOSE = "✅ تأكيد إغلاق PAPER"

BTN_APEX_ON = "🧠 تشغيل APEX"
BTN_APEX_OFF = "⛔ إيقاف APEX"
BTN_JALWE_ON = "👁 تشغيل JALWE"
BTN_JALWE_OFF = "⛔ إيقاف JALWE"

BTN_BRIDGE = "📡 فحص الربط"
BTN_HELP = "ℹ️ الأوامر"

KEYBOARD = {
    "keyboard": [
        [{"text": BTN_START}, {"text": BTN_STOP}],
        [{"text": BTN_RESTART}, {"text": BTN_STATUS}],
        [{"text": BTN_PORTFOLIO}, {"text": BTN_TODAY}],
        [{"text": BTN_WEEK}, {"text": BTN_TRADES}],
        [{"text": BTN_LEARNING}, {"text": BTN_LEARN_NOW}],
        [{"text": BTN_PAUSE_ENTRIES}, {"text": BTN_RESUME_ENTRIES}],
        [{"text": BTN_EMERGENCY_CLOSE}, {"text": BTN_CONFIRM_EMERGENCY_CLOSE}],
        [{"text": BTN_APEX_ON}, {"text": BTN_APEX_OFF}],
        [{"text": BTN_JALWE_ON}, {"text": BTN_JALWE_OFF}],
        [{"text": BTN_BRIDGE}, {"text": BTN_HELP}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


# ============================================================
# HELPERS
# ============================================================

def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value: Any) -> str:
    number = safe_float(value)

    if number is None:
        return "N/A"

    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def pct(value: Any, *, fraction: bool = False) -> str:
    number = safe_float(value)

    if number is None:
        return "N/A"

    if fraction:
        number *= 100.0

    return f"{number:+.2f}%"


def qty_text(value: Any) -> str:
    number = safe_float(value)

    if number is None:
        return "N/A"

    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):,}"

    return f"{number:,.4f}".rstrip("0").rstrip(".")


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=NY_TZ)

        return parsed
    except Exception:
        return None


# ============================================================
# PERSISTENT CONTROLLER STATE
# ============================================================

def load_state() -> dict[str, Any]:
    default = {
        "notified_order_ids": [],
        "order_alerts_bootstrapped": False,
        "last_order_check_epoch": 0.0,
        "last_daily_report_date": "",
        "last_weekly_report_key": "",
        "last_learning_epoch": 0.0,
        "last_error_key": "",
        "last_error_epoch": 0.0,
    }

    if not STATE_FILE.exists():
        return default

    try:
        raw = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        )

        if not isinstance(raw, dict):
            return default

        default.update(raw)
        return default

    except Exception:
        return default


def save_state(state: dict[str, Any]) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                state,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(STATE_FILE)
    except Exception as exc:
        print(
            f"Controller state save failed: {exc}",
            flush=True,
        )


controller_state = load_state()


# ============================================================
# TELEGRAM HTTP
# ============================================================

def tg_call(
    method: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    timeout: int = 35,
) -> dict[str, Any]:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload or {}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout,
        ) as response:
            body = response.read().decode(
                "utf-8",
                errors="replace",
            )

    except urllib.error.HTTPError as exc:
        error_body = ""

        try:
            error_body = (
                exc.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )
        except Exception:
            pass

        description = ""

        if error_body:
            try:
                parsed_error = json.loads(
                    error_body
                )
                description = str(
                    parsed_error.get(
                        "description",
                        "",
                    )
                    or ""
                ).strip()
            except Exception:
                description = (
                    error_body.strip()
                )

        if description:
            raise RuntimeError(
                f"Telegram {method}: "
                f"{description}"
            ) from exc

        raise RuntimeError(
            f"Telegram {method}: "
            f"HTTP {exc.code} {exc.reason}"
        ) from exc

    result = json.loads(body)

    if not result.get("ok"):
        raise RuntimeError(
            str(result.get("description") or "Telegram API error")
        )

    return result


def send_message(text: str) -> None:
    if not TELEGRAM_CHAT_ID:
        return

    tg_call(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "reply_markup": json.dumps(
                KEYBOARD,
                ensure_ascii=False,
            ),
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )


def notify_error(
    source: str,
    details: Any,
    *,
    force: bool = False,
) -> None:
    if not ERROR_ALERTS_ENABLED:
        return

    now_epoch = time.time()
    key = f"{source}:{type(details).__name__}:{details}"

    last_key = str(
        controller_state.get("last_error_key") or ""
    )
    last_epoch = safe_float(
        controller_state.get("last_error_epoch")
    ) or 0.0

    if (
        not force
        and key == last_key
        and (
            now_epoch - last_epoch
            < ERROR_ALERT_COOLDOWN_SECONDS
        )
    ):
        return

    controller_state["last_error_key"] = key
    controller_state["last_error_epoch"] = now_epoch
    save_state(controller_state)

    message = (
        "⚠️ خطأ في نظام JALWE + APEX\n\n"
        f"📍 المصدر: {source}\n"
        f"🧾 التفاصيل: {str(details)[:2500]}\n\n"
        "سيحاول النظام الاستمرار أو إعادة تشغيل "
        "الخدمة تلقائيًا إذا كان ذلك ممكنًا."
    )

    try:
        send_message(message)
    except Exception as exc:
        print(
            f"Unable to send Telegram error alert: {exc}",
            flush=True,
        )


# ============================================================
# ALPACA PAPER REST
# ============================================================

def alpaca_ready() -> bool:
    return bool(
        ALPACA_API_KEY
        and ALPACA_SECRET_KEY
        and "paper-api.alpaca.markets" in ALPACA_BASE_URL.lower()
    )


def alpaca_get(
    path: str,
    params: Optional[dict[str, Any]] = None,
    *,
    timeout: int = 25,
) -> Any:
    if not alpaca_ready():
        raise RuntimeError(
            "Alpaca PAPER credentials/base URL are not ready."
        )

    query = urllib.parse.urlencode(
        {
            key: value
            for key, value in (params or {}).items()
            if value is not None
        }
    )

    url = (
        f"{ALPACA_BASE_URL}{path}"
        + (f"?{query}" if query else "")
    )

    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Accept": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")

    return json.loads(body)


def get_account() -> dict[str, Any]:
    result = alpaca_get("/v2/account")

    if not isinstance(result, dict):
        raise RuntimeError("Unexpected Alpaca account response.")

    return result


def get_positions() -> list[dict[str, Any]]:
    result = alpaca_get("/v2/positions")

    if not isinstance(result, list):
        raise RuntimeError("Unexpected Alpaca positions response.")

    return [
        item
        for item in result
        if isinstance(item, dict)
    ]


def get_recent_filled_orders(
    limit: int = 50,
) -> list[dict[str, Any]]:
    result = alpaca_get(
        "/v2/orders",
        {
            "status": "closed",
            "limit": max(1, min(int(limit), 500)),
            "direction": "desc",
            "nested": "false",
        },
    )

    if not isinstance(result, list):
        raise RuntimeError("Unexpected Alpaca orders response.")

    filled = []

    for item in result:
        if not isinstance(item, dict):
            continue

        if str(item.get("status", "")).lower() != "filled":
            continue

        filled.append(item)

    filled.sort(
        key=lambda item: str(
            item.get("filled_at")
            or item.get("updated_at")
            or ""
        )
    )

    return filled


def get_portfolio_history(
    period: str,
    timeframe: str,
) -> dict[str, Any]:
    result = alpaca_get(
        "/v2/account/portfolio/history",
        {
            "period": period,
            "timeframe": timeframe,
            "intraday_reporting": "market_hours",
            "pnl_reset": "per_day",
        },
    )

    if not isinstance(result, dict):
        raise RuntimeError(
            "Unexpected Alpaca portfolio history response."
        )

    return result


# ============================================================
# PORTFOLIO / P&L REPORTS
# ============================================================

def portfolio_text() -> str:
    try:
        account = get_account()
        positions = get_positions()
    except Exception as exc:
        return f"💼 تعذر قراءة المحفظة من Alpaca PAPER:\n{exc}"

    equity = safe_float(account.get("equity"))
    cash = safe_float(account.get("cash"))
    buying_power = safe_float(account.get("buying_power"))
    portfolio_value = safe_float(
        account.get("portfolio_value")
    )

    lines = [
        "💼 محفظة Alpaca PAPER",
        "",
        f"💵 الكاش المتاح: {money(cash)}",
        f"💰 قيمة الحساب: {money(equity)}",
        f"📦 قيمة المحفظة: {money(portfolio_value)}",
        f"⚡ القوة الشرائية: {money(buying_power)}",
        f"📊 عدد الأسهم/المراكز المفتوحة: {len(positions)}",
    ]

    if not positions:
        lines.extend(
            [
                "",
                "لا توجد أسهم مفتوحة حاليًا.",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "",
            "📌 الأسهم الموجودة:",
        ]
    )

    for index, position in enumerate(
        positions[:15],
        start=1,
    ):
        symbol = str(position.get("symbol") or "N/A").upper()
        qty = position.get("qty")
        side = str(position.get("side") or "long").upper()
        avg_entry = position.get("avg_entry_price")
        current_price = position.get("current_price")
        market_value = position.get("market_value")
        unrealized_pl = position.get("unrealized_pl")
        unrealized_plpc = position.get("unrealized_plpc")

        lines.extend(
            [
                "",
                f"{index}) {symbol} | {side}",
                f"   الكمية: {qty_text(qty)} سهم",
                f"   متوسط الشراء: {money(avg_entry)}",
                f"   السعر الحالي: {money(current_price)}",
                f"   القيمة الحالية: {money(market_value)}",
                (
                    "   الربح/الخسارة غير المحققة: "
                    f"{money(unrealized_pl)} "
                    f"({pct(unrealized_plpc, fraction=True)})"
                ),
            ]
        )

    if len(positions) > 15:
        lines.append(
            f"\n... ويوجد {len(positions) - 15} مركز إضافي."
        )

    return "\n".join(lines)


def daily_report_text() -> str:
    try:
        account = get_account()
        history = get_portfolio_history(
            "1D",
            "5Min",
        )
    except Exception as exc:
        return f"📅 تعذر حساب تقرير اليوم:\n{exc}"

    profit_loss = history.get("profit_loss") or []
    profit_loss_pct = history.get("profit_loss_pct") or []

    pnl = (
        safe_float(profit_loss[-1])
        if profit_loss
        else None
    )

    pnl_pct = (
        safe_float(profit_loss_pct[-1])
        if profit_loss_pct
        else None
    )

    if pnl is None:
        equity = safe_float(account.get("equity"))
        last_equity = safe_float(account.get("last_equity"))

        if (
            equity is not None
            and last_equity is not None
        ):
            pnl = equity - last_equity

            if last_equity:
                pnl_pct = pnl / last_equity

    label = (
        "ربح"
        if (pnl or 0.0) > 0
        else "خسارة"
        if (pnl or 0.0) < 0
        else "تعادل"
    )

    return (
        "📅 تقرير اليوم — Alpaca PAPER\n\n"
        f"💰 قيمة الحساب: {money(account.get('equity'))}\n"
        f"💵 الكاش: {money(account.get('cash'))}\n"
        f"📈 النتيجة: {label}\n"
        f"💲 ربح/خسارة اليوم: {money(pnl)}\n"
        f"📊 النسبة: {pct(pnl_pct, fraction=True)}"
    )


def weekly_report_text() -> str:
    try:
        account = get_account()
        history = get_portfolio_history(
            "1W",
            "1D",
        )
    except Exception as exc:
        return f"📆 تعذر حساب تقرير الأسبوع:\n{exc}"

    cumulative = [
        safe_float(value)
        for value in (history.get("profit_loss") or [])
    ]
    cumulative = [
        value
        for value in cumulative
        if value is not None
    ]

    pct_values = [
        safe_float(value)
        for value in (history.get("profit_loss_pct") or [])
    ]
    pct_values = [
        value
        for value in pct_values
        if value is not None
    ]

    weekly_net = cumulative[-1] if cumulative else 0.0
    weekly_pct = pct_values[-1] if pct_values else None

    day_changes: list[float] = []
    previous = 0.0

    for value in cumulative:
        day_changes.append(value - previous)
        previous = value

    gross_gain = sum(
        value
        for value in day_changes
        if value > 0
    )

    gross_loss = abs(
        sum(
            value
            for value in day_changes
            if value < 0
        )
    )

    winning_days = sum(
        1
        for value in day_changes
        if value > 0
    )

    losing_days = sum(
        1
        for value in day_changes
        if value < 0
    )

    return (
        "📆 تقرير الأسبوع — Alpaca PAPER\n\n"
        f"💰 قيمة الحساب الآن: {money(account.get('equity'))}\n"
        f"📈 صافي الأسبوع: {money(weekly_net)}\n"
        f"📊 نسبة الأسبوع: {pct(weekly_pct, fraction=True)}\n"
        f"✅ مجموع الأيام الرابحة: {money(gross_gain)}\n"
        f"❌ مجموع الأيام الخاسرة: {money(gross_loss)}\n"
        f"🟢 أيام رابحة: {winning_days}\n"
        f"🔴 أيام خاسرة: {losing_days}"
    )


def recent_trades_text() -> str:
    try:
        orders = get_recent_filled_orders(15)
    except Exception as exc:
        return f"🧾 تعذر قراءة الصفقات:\n{exc}"

    if not orders:
        return "🧾 لا توجد أوامر منفذة حديثًا على حساب Alpaca PAPER."

    lines = [
        "🧾 آخر الصفقات المنفذة — Alpaca PAPER",
        "",
    ]

    for order in orders[-10:][::-1]:
        side = str(order.get("side") or "").upper()
        symbol = str(order.get("symbol") or "N/A").upper()
        quantity = (
            order.get("filled_qty")
            or order.get("qty")
        )
        price = order.get("filled_avg_price")
        filled_at = parse_dt(order.get("filled_at"))

        time_text = (
            filled_at.astimezone(NY_TZ).strftime(
                "%Y-%m-%d %H:%M NY"
            )
            if filled_at
            else "N/A"
        )

        icon = "🟢" if side == "BUY" else "🔴"

        lines.extend(
            [
                (
                    f"{icon} {side} {symbol} | "
                    f"{qty_text(quantity)} @ {money(price)}"
                ),
                f"   {time_text}",
            ]
        )

    return "\n".join(lines)


# ============================================================
# AUTOMATIC FILLED ORDER ALERTS
# ============================================================

def order_alert_text(
    order: dict[str, Any],
) -> str:
    side = str(order.get("side") or "").upper()
    symbol = str(order.get("symbol") or "N/A").upper()
    quantity = (
        order.get("filled_qty")
        or order.get("qty")
    )
    price = safe_float(order.get("filled_avg_price"))
    qty_number = safe_float(quantity)

    total = (
        price * qty_number
        if (
            price is not None
            and qty_number is not None
        )
        else None
    )

    filled_at = parse_dt(order.get("filled_at"))
    time_text = (
        filled_at.astimezone(NY_TZ).strftime(
            "%Y-%m-%d %H:%M:%S NY"
        )
        if filled_at
        else "N/A"
    )

    if side == "BUY":
        title = "🟢 تم شراء سهم — PAPER"
    elif side == "SELL":
        title = "🔴 تم بيع سهم — PAPER"
    else:
        title = "🔔 تم تنفيذ أمر — PAPER"

    try:
        account = get_account()
        account_tail = (
            f"\n\n💵 الكاش الآن: {money(account.get('cash'))}"
            f"\n💰 قيمة الحساب: {money(account.get('equity'))}"
        )
    except Exception:
        account_tail = ""

    return (
        f"{title}\n\n"
        f"📌 السهم: {symbol}\n"
        f"↔️ العملية: {side}\n"
        f"🔢 الكمية: {qty_text(quantity)}\n"
        f"💲 سعر التنفيذ: {money(price)}\n"
        f"💵 قيمة العملية: {money(total)}\n"
        f"🕒 الوقت: {time_text}"
        f"{account_tail}"
    )


def poll_filled_order_alerts() -> None:
    if not ORDER_ALERTS_ENABLED:
        return

    now_epoch = time.time()
    last_check = safe_float(
        controller_state.get("last_order_check_epoch")
    ) or 0.0

    if (
        now_epoch - last_check
        < ORDER_ALERT_POLL_SECONDS
    ):
        return

    controller_state["last_order_check_epoch"] = now_epoch

    try:
        orders = get_recent_filled_orders(100)
    except Exception as exc:
        print(
            f"Filled-order poll failed: {exc}",
            flush=True,
        )
        notify_error(
            "مراقبة أوامر Alpaca",
            exc,
        )
        save_state(controller_state)
        return

    ids = [
        str(order.get("id") or "").strip()
        for order in orders
        if str(order.get("id") or "").strip()
    ]

    known = set(
        str(value)
        for value in controller_state.get(
            "notified_order_ids",
            [],
        )
    )

    if not controller_state.get(
        "order_alerts_bootstrapped",
        False,
    ):
        controller_state["notified_order_ids"] = ids[-500:]
        controller_state["order_alerts_bootstrapped"] = True
        save_state(controller_state)
        print(
            f"Order alerts initialized with {len(ids)} existing fills.",
            flush=True,
        )
        return

    new_orders = [
        order
        for order in orders
        if str(order.get("id") or "").strip()
        and str(order.get("id") or "").strip()
        not in known
    ]

    for order in new_orders:
        order_id = str(order.get("id") or "").strip()

        try:
            send_message(
                order_alert_text(order)
            )
            known.add(order_id)

        except Exception as exc:
            print(
                f"Trade alert failed for {order_id}: {exc}",
                flush=True,
            )

    controller_state["notified_order_ids"] = list(known)[-500:]
    save_state(controller_state)


# ============================================================
# AUTOMATIC DAILY / WEEKLY REPORTS
# ============================================================

def maybe_send_scheduled_reports() -> None:
    now_ny = datetime.now(NY_TZ)

    # Daily report: weekdays after 16:10 New York.
    if AUTO_DAILY_REPORT and now_ny.weekday() < 5:
        daily_key = now_ny.strftime("%Y-%m-%d")

        if (
            (now_ny.hour, now_ny.minute) >= (16, 10)
            and controller_state.get(
                "last_daily_report_date"
            ) != daily_key
        ):
            try:
                send_message(
                    daily_report_text()
                )
                controller_state[
                    "last_daily_report_date"
                ] = daily_key
                save_state(controller_state)
            except Exception as exc:
                print(
                    f"Daily report send failed: {exc}",
                    flush=True,
                )
                notify_error(
                    "التقرير اليومي",
                    exc,
                )

    # Weekly report: Friday after 16:20 New York.
    if AUTO_WEEKLY_REPORT and now_ny.weekday() == 4:
        iso_year, iso_week, _ = now_ny.isocalendar()
        weekly_key = f"{iso_year}-W{iso_week:02d}"

        if (
            (now_ny.hour, now_ny.minute) >= (16, 20)
            and controller_state.get(
                "last_weekly_report_key"
            ) != weekly_key
        ):
            try:
                send_message(
                    weekly_report_text()
                )
                controller_state[
                    "last_weekly_report_key"
                ] = weekly_key
                save_state(controller_state)
            except Exception as exc:
                print(
                    f"Weekly report send failed: {exc}",
                    flush=True,
                )
                notify_error(
                    "التقرير الأسبوعي",
                    exc,
                )


# ============================================================
# LEARNING ENGINE CONTROL
# ============================================================

def learning_status_text() -> str:
    try:
        from intelligence.learning_engine import (
            get_learning_engine,
        )

        status = get_learning_engine().status()
    except Exception as exc:
        return f"🧠 تعذر قراءة حالة التعلم:\n{exc}"

    active = bool(status.get("active"))
    enabled = bool(status.get("enabled"))
    eligible = int(status.get("eligible_trades") or 0)
    minimum = int(status.get("minimum_trades") or 0)
    max_adjustment = status.get("max_adjustment_pct")

    weights = status.get("weights") or []
    changed = [
        item
        for item in weights
        if abs(
            float(item.get("multiplier") or 1.0)
            - 1.0
        ) >= 0.0025
    ]

    lines = [
        "🧠 JALWE Learning Engine V1",
        "",
        f"الحالة: {'🟢 نشط' if active else '🟡 ينتظر بيانات'}",
        f"التعلم مفعّل: {'نعم' if enabled else 'لا'}",
        f"الصفقات المؤهلة: {eligible}/{minimum}",
        f"أقصى تعديل للعوامل: ±{max_adjustment}%",
        f"العوامل المعدلة حاليًا: {len(changed)}",
    ]

    if changed:
        lines.extend(["", "📚 أهم الأوزان المتعلمة:"])

        for item in changed[:8]:
            multiplier = float(
                item.get("multiplier") or 1.0
            )
            delta = (
                multiplier - 1.0
            ) * 100.0
            lines.append(
                f"• {item.get('factor')}: "
                f"{delta:+.1f}% "
                f"(عينات {item.get('samples')})"
            )

    if not active:
        lines.extend(
            [
                "",
                "لن يغيّر أوزان التحليل حتى يجمع "
                f"{minimum} صفقة PAPER مغلقة ومؤهلة.",
            ]
        )

    return "\n".join(lines)


def run_learning_now_text() -> str:
    try:
        from intelligence.learning_engine import (
            get_learning_engine,
        )

        result = (
            get_learning_engine()
            .run_learning_cycle()
        )
    except Exception as exc:
        notify_error(
            "Learning Engine",
            exc,
        )
        return f"🎓 فشل تشغيل دورة التعلم:\n{exc}"

    active = bool(result.get("learning_active"))
    eligible = int(result.get("eligible_trades") or 0)
    minimum = int(result.get("minimum_trades") or 0)
    changed = int(result.get("changed_factors") or 0)

    return (
        "🎓 اكتملت دورة التعلم\n\n"
        f"الحالة: {'🟢 تعلم فعلي' if active else '🟡 جمع بيانات'}\n"
        f"الصفقات المؤهلة: {eligible}/{minimum}\n"
        f"العوامل التي تغيرت: {changed}\n"
        "🔒 التعديلات محكومة بحدود أمان ولا تغيّر الكود."
    )


def maybe_run_learning_cycle() -> None:
    if not AUTO_LEARNING:
        return

    now_epoch = time.time()
    last_epoch = safe_float(
        controller_state.get(
            "last_learning_epoch"
        )
    ) or 0.0

    if (
        now_epoch - last_epoch
        < LEARNING_INTERVAL_SECONDS
    ):
        return

    controller_state[
        "last_learning_epoch"
    ] = now_epoch
    save_state(controller_state)

    try:
        from intelligence.learning_engine import (
            get_learning_engine,
        )

        result = (
            get_learning_engine()
            .run_learning_cycle()
        )

        print(
            "Learning cycle | "
            f"active={result.get('learning_active')} "
            f"eligible={result.get('eligible_trades')} "
            f"changed={result.get('changed_factors')}",
            flush=True,
        )

    except Exception as exc:
        print(
            f"Learning cycle failed: {exc}",
            flush=True,
        )
        notify_error(
            "Learning Engine",
            exc,
        )


# ============================================================
# MANAGED CHILD PROCESSES
# ============================================================

class ManagedProcess:
    def __init__(self, name: str, script: Path) -> None:
        self.name = name
        self.script = script
        self.process: Optional[subprocess.Popen] = None
        self.desired_running = False

    def is_running(self) -> bool:
        return bool(
            self.process is not None
            and self.process.poll() is None
        )

    def start(self) -> str:
        self.desired_running = True

        if self.is_running():
            return (
                f"{self.name}: يعمل بالفعل "
                f"(PID {self.process.pid})"
            )

        if not self.script.exists():
            return (
                f"{self.name}: الملف غير موجود: "
                f"{self.script.name}"
            )

        self.process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(self.script),
            ],
            cwd=str(BASE_DIR),
            env=os.environ.copy(),
            start_new_session=True,
        )

        return (
            f"{self.name}: تم التشغيل "
            f"(PID {self.process.pid})"
        )

    def stop(self) -> str:
        self.desired_running = False

        if not self.is_running():
            self.process = None
            return f"{self.name}: متوقف بالفعل"

        assert self.process is not None
        pid = self.process.pid

        try:
            os.killpg(
                os.getpgid(pid),
                signal.SIGTERM,
            )
            self.process.wait(timeout=10)
        except Exception:
            try:
                os.killpg(
                    os.getpgid(pid),
                    signal.SIGKILL,
                )
            except Exception:
                pass

        self.process = None
        return f"{self.name}: تم الإيقاف"

    def restart_if_needed(self) -> Optional[str]:
        if (
            AUTO_RESTART
            and self.desired_running
            and not self.is_running()
        ):
            return self.start()

        return None

    def status(self) -> str:
        if self.is_running():
            assert self.process is not None
            return (
                f"🟢 {self.name}: يعمل "
                f"(PID {self.process.pid})"
            )

        return f"🔴 {self.name}: متوقف"


apex = ManagedProcess("APEX", APEX_SCRIPT)
jalwe = ManagedProcess("JALWE Watcher", JALWE_SCRIPT)


def start_all() -> str:
    a = apex.start()
    time.sleep(1)
    j = jalwe.start()
    return f"🟢 تشغيل النظام\n\n{a}\n{j}"


def stop_all() -> str:
    a = apex.stop()
    j = jalwe.stop()
    return f"🛑 إيقاف النظام\n\n{a}\n{j}"


def restart_all() -> str:
    stop_all()
    time.sleep(2)
    return start_all()


def pause_new_entries() -> str:
    set_new_entries_allowed(
        False,
        updated_by="TELEGRAM",
        reason="USER_PAUSE",
    )

    return (
        "⏸ تم منع الصفقات الجديدة.\n\n"
        "✅ الصفقات المفتوحة ستستمر تحت المراقبة "
        "والأهداف/الستوب تبقى فعالة."
    )


def resume_new_entries() -> str:
    set_new_entries_allowed(
        True,
        updated_by="TELEGRAM",
        reason="USER_RESUME",
    )

    return (
        "▶️ تم السماح بالصفقات الجديدة.\n\n"
        "JALWE سيعود للتنفيذ على Alpaca PAPER "
        "فقط عندما تمر جميع بوابات القرار والمخاطر."
    )


def emergency_close_prompt() -> str:
    return (
        "🚨 إغلاق صفقات JALWE على PAPER\n\n"
        "هذا سيطلب من JALWE إغلاق جميع الصفقات "
        "التي يديرها حاليًا على Alpaca PAPER.\n"
        "لن يفتح صفقات جديدة أثناء التنفيذ.\n\n"
        "إذا كنت متأكدًا اضغط:\n"
        "✅ تأكيد إغلاق PAPER"
    )


def confirm_emergency_close() -> str:
    set_new_entries_allowed(
        False,
        updated_by="TELEGRAM",
        reason="EMERGENCY_CLOSE",
    )

    controls = request_emergency_close(
        requested_by="TELEGRAM",
    )

    request_id = str(
        controls.get(
            "emergency_close_request_id",
            "",
        )
    )

    return (
        "🚨 تم إرسال أمر الإغلاق الطارئ إلى JALWE.\n\n"
        "⏸ تم منع الصفقات الجديدة تلقائيًا.\n"
        "سيغلق JALWE الصفقات المدارة على PAPER "
        "ويحدّث قاعدة البيانات بعد تأكيد التنفيذ من Alpaca.\n"
        f"Request: {request_id}"
    )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)

    if raw is None:
        return bool(default)

    return str(raw).strip().lower() in {
        "1", "true", "yes", "on"
    }


def status_text() -> str:
    paper_auto = (
        _env_bool(
            "JALWE_AUTO_PAPER_EXECUTION",
            _env_bool(
                "AUTO_PAPER_EXECUTION",
                False,
            ),
        )
        and
        _env_bool(
            "JALWE_BROKER_SUBMISSION",
            _env_bool(
                "BROKER_SUBMISSION_ENABLED",
                False,
            ),
        )
        and
        _env_bool(
            "JALWE_PAPER_TRADING",
            True,
        )
    )

    return (
        "📊 حالة JALWE + APEX\n\n"
        f"{apex.status()}\n"
        f"{jalwe.status()}\n\n"
        f"🔔 تنبيهات تنفيذ الصفقات: "
        f"{'مفعلة' if ORDER_ALERTS_ENABLED else 'معطلة'}\n"
        f"⚠️ تنبيهات أخطاء النظام: "
        f"{'مفعلة' if ERROR_ALERTS_ENABLED else 'معطلة'}\n"
        f"🧠 التعلم الذاتي: "
        f"{'مفعّل' if AUTO_LEARNING else 'معطّل'}\n"
        f"📄 تنفيذ JALWE على Alpaca PAPER: "
        f"{'مفعّل' if paper_auto else 'معطّل'}\n"
        f"🚦 الصفقات الجديدة: "
        f"{'مسموحة' if load_runtime_controls().get('allow_new_entries', True) else 'موقوفة'}\n"
        f"🚨 طلب إغلاق طارئ: "
        f"{'نعم' if load_runtime_controls().get('emergency_close_requested', False) else 'لا'}\n"
        "🔒 التداول الحقيقي LIVE: معطل"
    )


def bridge_text() -> str:
    try:
        from intelligence.external_research_bridge import (
            get_external_research_bridge,
        )

        health = (
            get_external_research_bridge()
            .health_check()
        )

        return (
            "📡 حالة الربط\n\n"
            f"OK: {health.get('ok')}\n"
            f"Backend: {health.get('backend')}\n"
            f"Reports: {health.get('latest_research_count')}\n"
            "Execution authority: "
            f"{health.get('execution_authority', False)}"
        )
    except Exception as exc:
        return f"📡 فشل فحص الربط:\n{exc}"


def help_text() -> str:
    return (
        "ℹ️ أوامر السيرفر\n\n"
        "/run - تشغيل الكل (APEX + JALWE)\n"
        "/stop - إيقاف الكل (APEX + JALWE)\n"
        "/restart - إعادة تشغيلهما\n"
        "/status - حالة النظام\n"
        "/portfolio - الرصيد والأسهم المفتوحة\n"
        "/today - ربح/خسارة اليوم\n"
        "/week - تقرير الأسبوع\n"
        "/trades - آخر الأوامر المنفذة\n"
        "/learning - حالة التعلم الذاتي\n"
        "/learn_now - تشغيل دورة تعلم الآن\n"
        "/pause_entries - منع صفقات جديدة مع استمرار إدارة المفتوحة\n"
        "/resume_entries - السماح بصفقات جديدة\n"
        "/emergency_close - عرض تأكيد الإغلاق الطارئ\n"
        "/confirm_emergency_close - تأكيد إغلاق صفقات JALWE PAPER\n"
        "/apex_on /apex_off\n"
        "/jalwe_on /jalwe_off\n"
        "/bridge - فحص الربط\n"
        "/help - عرض الأوامر\n\n"
        "🔔 سيرسل البوت تلقائيًا تنبيهًا عند كل "
        "BUY/SELL منفذ على Alpaca PAPER.\n"
        "📅 ويرسل تقريرًا يوميًا بعد إغلاق السوق، "
        "وتقريرًا أسبوعيًا يوم الجمعة.\n"
        "⚠️ وإذا توقف APEX أو JALWE بشكل غير متوقع "
        "أو فشل اتصال مهم، يرسل تنبيه خطأ على تيليجرام."
    )


def handle(text: str) -> str:
    cmd = str(text or "").strip()
    low = cmd.lower()

    if cmd == BTN_START or low == "/run":
        return start_all()

    if cmd == BTN_STOP or low == "/stop":
        return stop_all()

    if cmd == BTN_RESTART or low == "/restart":
        return restart_all()

    if cmd == BTN_STATUS or low == "/status":
        return status_text()

    if cmd == BTN_PORTFOLIO or low == "/portfolio":
        return portfolio_text()

    if cmd == BTN_TODAY or low == "/today":
        return daily_report_text()

    if cmd == BTN_WEEK or low == "/week":
        return weekly_report_text()

    if cmd == BTN_TRADES or low == "/trades":
        return recent_trades_text()

    if cmd == BTN_LEARNING or low == "/learning":
        return learning_status_text()

    if cmd == BTN_LEARN_NOW or low == "/learn_now":
        return run_learning_now_text()

    if cmd == BTN_PAUSE_ENTRIES or low == "/pause_entries":
        return pause_new_entries()

    if cmd == BTN_RESUME_ENTRIES or low == "/resume_entries":
        return resume_new_entries()

    if cmd == BTN_EMERGENCY_CLOSE or low == "/emergency_close":
        return emergency_close_prompt()

    if (
        cmd == BTN_CONFIRM_EMERGENCY_CLOSE
        or low == "/confirm_emergency_close"
    ):
        return confirm_emergency_close()

    if cmd == BTN_APEX_ON or low == "/apex_on":
        return apex.start()

    if cmd == BTN_APEX_OFF or low == "/apex_off":
        return apex.stop()

    if cmd == BTN_JALWE_ON or low == "/jalwe_on":
        return jalwe.start()

    if cmd == BTN_JALWE_OFF or low == "/jalwe_off":
        return jalwe.stop()

    if cmd == BTN_BRIDGE or low == "/bridge":
        return bridge_text()

    if cmd == BTN_HELP or low in {"/help", "/start"}:
        return help_text()

    return "الأمر غير معروف. استخدم /help"


def monitor_children() -> None:
    for managed in (apex, jalwe):
        if (
            managed.desired_running
            and managed.process is not None
            and managed.process.poll() is not None
        ):
            exit_code = managed.process.returncode

            notify_error(
                f"{managed.name} توقف بشكل غير متوقع",
                f"Exit code: {exit_code}",
                force=True,
            )

        message = managed.restart_if_needed()

        if message:
            print(message, flush=True)

            if AUTO_RESTART:
                try:
                    send_message(
                        "♻️ إعادة تشغيل تلقائية\n\n"
                        f"{message}"
                    )
                except Exception:
                    pass


def shutdown(*_: Any) -> None:
    print("Controller shutting down...", flush=True)
    apex.stop()
    jalwe.stop()
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is required"
        )

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is required"
        )

    if not alpaca_ready():
        raise RuntimeError(
            "Alpaca PAPER API configuration is required."
        )

    # getUpdates and Telegram webhooks are mutually exclusive.
    # Clear any stale webhook left by an older deployment/setup
    # before this Railway controller starts long-polling.
    try:
        webhook_info = tg_call(
            "getWebhookInfo",
            {},
            timeout=15,
        )

        webhook_url = str(
            (
                webhook_info.get("result", {})
                or {}
            ).get("url", "")
            or ""
        ).strip()

        if webhook_url:
            tg_call(
                "deleteWebhook",
                {
                    "drop_pending_updates": "false",
                },
                timeout=15,
            )

            print(
                "Telegram stale webhook removed; "
                "getUpdates long-polling enabled.",
                flush=True,
            )

    except Exception as exc:
        print(
            f"Telegram webhook cleanup warning: {exc}",
            flush=True,
        )

    me = tg_call(
        "getMe",
        {},
        timeout=15,
    )
    username = (
        me.get("result", {})
        .get("username", "UNKNOWN")
    )

    print(
        "JALWE Railway Controller started | "
        f"Telegram @{username}",
        flush=True,
    )

    if AUTOSTART:
        print(
            start_all(),
            flush=True,
        )

    try:
        send_message(
            "🎛 JALWE + APEX على Railway جاهز.\n\n"
            + status_text()
        )
    except Exception as exc:
        print(
            f"Startup Telegram message failed: {exc}",
            flush=True,
        )

    offset = 0
    telegram_conflict_started_at: Optional[float] = None
    telegram_conflict_alert_after_seconds = max(
        30,
        int(
            os.getenv(
                "JALWE_TELEGRAM_CONFLICT_ALERT_AFTER_SECONDS",
                "90",
            )
        ),
    )

    while True:
        monitor_children()
        poll_filled_order_alerts()
        maybe_send_scheduled_reports()
        maybe_run_learning_cycle()

        try:
            updates = tg_call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 15,
                    "allowed_updates": json.dumps(
                        ["message"]
                    ),
                },
                timeout=25,
            ).get("result", [])

            # A successful poll means any deployment-overlap
            # conflict has cleared.
            telegram_conflict_started_at = None

            for update in updates:
                update_id = int(
                    update.get("update_id", 0)
                )
                offset = max(
                    offset,
                    update_id + 1,
                )

                message = (
                    update.get("message")
                    or {}
                )
                chat_id = str(
                    (
                        message.get("chat")
                        or {}
                    ).get("id", "")
                )

                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                message_text = message.get("text")

                if not message_text:
                    continue

                reply = handle(
                    message_text
                )
                send_message(reply)

        except Exception as exc:
            error_text = str(exc)

            if (
                "Telegram getUpdates: Conflict:"
                in error_text
            ):
                now_epoch = time.time()

                if telegram_conflict_started_at is None:
                    telegram_conflict_started_at = now_epoch

                conflict_age = (
                    now_epoch
                    - telegram_conflict_started_at
                )

                print(
                    "Telegram getUpdates conflict "
                    f"({conflict_age:.0f}s): {error_text}",
                    flush=True,
                )

                # Railway can briefly overlap old/new containers
                # during a deployment. Do not alarm the user for
                # one transient conflict; alert only if it persists.
                if (
                    conflict_age
                    >= telegram_conflict_alert_after_seconds
                ):
                    notify_error(
                        "وحدة تحكم Telegram",
                        (
                            f"{error_text} | "
                            f"مستمر منذ {conflict_age:.0f} ثانية"
                        ),
                    )

                time.sleep(5)
                continue

            telegram_conflict_started_at = None

            print(
                f"Telegram controller error: {exc}",
                flush=True,
            )
            notify_error(
                "وحدة تحكم Telegram",
                exc,
            )
            time.sleep(5)


if __name__ == "__main__":
    main()
