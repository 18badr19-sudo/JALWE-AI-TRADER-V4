"""Run reviewed mock-only tests in a disposable source copy. Never starts the bot."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

SUITE = ['test_completed_bars', 'test_feature_freshness', 'test_breakout_bar_integrity', 'test_risk_input_integrity', 'test_entry_submission_phases', 'test_exit_submission_phases', 'test_restart_protective_stop_safety', 'test_target_touch_management', 'test_trade_chain_and_feature_integrity', 'test_paper_safety']
ROOT = Path(__file__).resolve().parents[1]
GUARD = r'''import socket, sys

def deny(*args, **kwargs):
    raise PermissionError("LAB: network and child processes are disabled")

socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.socket.sendto = deny
socket.create_connection = deny
socket.getaddrinfo = deny

def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty"}:
        deny()
sys.addaudithook(audit)
'''


def main():
    if len(sys.argv) != 1:
        raise SystemExit("Run without arguments; edit the reviewed SUITE to add mock-only tests.")
    with tempfile.TemporaryDirectory(prefix="jalwe-lab-") as directory:
        work = Path(directory)
        source = work / "source"
        # Never copy .env, databases, credentials, logs, .git or runtime state.
        for path in ROOT.rglob("*.py"):
            relative = path.relative_to(ROOT)
            if path.is_symlink() or any(p.startswith(".") or p in {"data", "logs", "venv", "env", "__pycache__", "node_modules"} for p in relative.parts):
                continue
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        guard = work / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
        # Retain only interpreter package locations, never parent credentials/PYTHONPATH.
        packages = [p for p in sys.path if p and (p.endswith("site-packages") or p.endswith("dist-packages"))]
        env = {
            "PATH": os.defpath, "HOME": str(work), "TMPDIR": str(work),
            "PYTHONPATH": os.pathsep.join([str(guard), str(source), str(source / "tests"), *packages]),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
            "ALPACA_API_KEY": "lab-dummy", "ALPACA_SECRET_KEY": "lab-dummy",
            "APCA_API_KEY_ID": "lab-dummy", "APCA_API_SECRET_KEY": "lab-dummy",
            "ALPACA_BASE_URL": "http://127.0.0.1:9", "APCA_API_BASE_URL": "http://127.0.0.1:9",
            "PAPER_TRADING": "true", "ALLOW_LIVE_TRADING": "false",
            "JALWE_DATABASE_PATH": str(work / "lab.db"),
            "JALWE_CONTROLLER_DATA_DIR": str(work / "state"),
            "JALWE_DECISION_REPLAY_ENABLED": "false",
        }
        check = r'''import socket, subprocess, sys, os
assert 'sitecustomize' in sys.modules
assert not os.environ.get('DATABASE_URL')
assert not os.environ.get('TELEGRAM_BOT_TOKEN')
assert os.environ['ALLOW_LIVE_TRADING'] == 'false'
for action in [lambda: socket.create_connection(('127.0.0.1', 9)), lambda: socket.getaddrinfo('example.invalid', 443), lambda: subprocess.run([sys.executable, '-c', 'pass']), lambda: os.system('true')]:
    try:
        action()
    except PermissionError:
        pass
    else:
        raise AssertionError('Isolation guard failed')
print('LAB_GUARDS_OK: sockets, DNS, subprocess, shell, clean credentials')
'''
        subprocess.run([sys.executable, "-c", check], cwd=source, env=env, check=True)
        print("LAB: disposable source and SQLite; mocks only; no bot/controller started", flush=True)
        return subprocess.run([sys.executable, "-m", "unittest", *SUITE, "-v"], cwd=source, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
