"""Read-only disk monitoring; alert state stays in RAM even when disk is full."""
import os
import threading
import time


def disk_snapshot(path):
    s = os.statvfs(path)
    total = s.f_blocks * s.f_frsize
    free = max(0, s.f_bavail * s.f_frsize)
    if total <= 0:
        raise OSError("Filesystem reported zero capacity")
    return 100 * (total - free) / total, free


def storage_space_text(path):
    try:
        used, free = disk_snapshot(path)
        return f"💾 المساحة المستخدمة: {used:.1f}% | المتاح: {free / 1048576:.0f} MiB"
    except OSError:
        return "⚠️ تعذّر قياس مساحة التخزين"


class DiskMonitor:
    def __init__(self, path, send, enabled=lambda: True, clock=time.monotonic):
        self.path, self.send, self.enabled, self.clock = path, send, enabled, clock
        self.level = 0
        self.last_sent = float('-inf')

    def check(self):
        used, free = disk_snapshot(self.path)
        level = sum(used >= boundary for boundary in (80, 90, 95))
        # Hysteresis prevents alternating recovery/warning at the 80% boundary.
        if level == 0 and used >= 75 and self.level:
            return
        now = self.clock()
        if not self.enabled():
            return
        if level:
            if level <= self.level and now - self.last_sent < 21600:
                return
            message = (f"⚠️ تنبيه مساحة JALWE — مستوى {level}/3\n"
                       f"المستخدم: {used:.1f}% | المتاح: {free / 1048576:.0f} MiB\n"
                       "يلزم مراجعة التخزين قبل امتلائه. لم تُحذف أي بيانات تلقائيًا.")
        elif self.level:
            message = f"✅ تحسّنت مساحة JALWE: المستخدم {used:.1f}%"
        else:
            return
        if self.send(message):
            self.level, self.last_sent = level, now

    def run(self):
        while True:
            try:
                self.check()
            except Exception as exc:
                # No DB writes and no secrets from transport exceptions.
                print(f"Disk monitor check failed: {type(exc).__name__}", flush=True)
            time.sleep(60)

    def start(self):
        thread = threading.Thread(target=self.run, name="disk-monitor", daemon=True)
        thread.start()
        return thread
