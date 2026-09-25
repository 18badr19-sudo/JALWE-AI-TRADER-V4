from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


BASE_DIR = Path(__file__).resolve().parent
APEX_SCRIPT = BASE_DIR / "apex_research_loop.py"
JALWE_SCRIPT = BASE_DIR / "jalwe_research_watcher.py"

TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    or os.getenv("TELEGRAM_TOKEN", "").strip()
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

AUTOSTART = os.getenv("JALWE_AUTOSTART", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
AUTO_RESTART = os.getenv("JALWE_AUTO_RESTART", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

BTN_START = "🟢 تشغيل JALWE + APEX"
BTN_STOP = "🛑 إيقاف JALWE + APEX"
BTN_RESTART = "♻️ إعادة تشغيل النظام"
BTN_STATUS = "📊 حالة النظام"
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
        [{"text": BTN_APEX_ON}, {"text": BTN_APEX_OFF}],
        [{"text": BTN_JALWE_ON}, {"text": BTN_JALWE_OFF}],
        [{"text": BTN_BRIDGE}, {"text": BTN_HELP}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


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

    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")

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
            "reply_markup": json.dumps(KEYBOARD, ensure_ascii=False),
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )


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
            return f"{self.name}: يعمل بالفعل (PID {self.process.pid})"

        if not self.script.exists():
            return f"{self.name}: الملف غير موجود: {self.script.name}"

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

        return f"{self.name}: تم التشغيل (PID {self.process.pid})"

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
            return f"🟢 {self.name}: يعمل (PID {self.process.pid})"

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


def status_text() -> str:
    return (
        "📊 حالة JALWE + APEX\n\n"
        f"{apex.status()}\n"
        f"{jalwe.status()}\n\n"
        "🔒 تنفيذ أوامر التداول من Watcher: معطل"
    )


def bridge_text() -> str:
    try:
        from intelligence.external_research_bridge import (
            get_external_research_bridge,
        )

        health = get_external_research_bridge().health_check()

        return (
            "📡 حالة الربط\n\n"
            f"OK: {health.get('ok')}\n"
            f"Backend: {health.get('backend')}\n"
            f"Reports: {health.get('latest_research_count')}\n"
            f"Execution authority: {health.get('execution_authority', False)}"
        )
    except Exception as exc:
        return f"📡 فشل فحص الربط:\n{exc}"


def help_text() -> str:
    return (
        "ℹ️ أوامر السيرفر\n\n"
        "/run - تشغيل APEX وJALWE\n"
        "/stop - إيقاف APEX وJALWE\n"
        "/restart - إعادة تشغيلهما\n"
        "/status - حالة النظام\n"
        "/apex_on /apex_off\n"
        "/jalwe_on /jalwe_off\n"
        "/bridge - فحص الربط\n"
        "/help - عرض الأوامر"
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
        message = managed.restart_if_needed()

        if message:
            print(message, flush=True)


def shutdown(*_: Any) -> None:
    print("Controller shutting down...", flush=True)
    apex.stop()
    jalwe.stop()
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is required")

    me = tg_call("getMe", {}, timeout=15)
    username = me.get("result", {}).get("username", "UNKNOWN")

    print(
        f"JALWE Railway Controller started | Telegram @{username}",
        flush=True,
    )

    if AUTOSTART:
        print(start_all(), flush=True)

    try:
        send_message(
            "🎛 JALWE + APEX على Railway جاهز.\n\n"
            + status_text()
        )
    except Exception as exc:
        print(f"Startup Telegram message failed: {exc}", flush=True)

    offset = 0

    while True:
        monitor_children()

        try:
            updates = tg_call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=35,
            ).get("result", [])

            for update in updates:
                update_id = int(update.get("update_id", 0))
                offset = max(offset, update_id + 1)

                message = update.get("message") or {}
                chat_id = str(
                    (message.get("chat") or {}).get("id", "")
                )

                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                text = message.get("text")
                if not text:
                    continue

                reply = handle(text)
                send_message(reply)

        except Exception as exc:
            print(f"Telegram controller error: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
