"""Check actual kernel isolation before importing any bot/test module."""
import errno
import os
from pathlib import Path
import socket
import subprocess
import sys

assert os.geteuid() == 65532
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
assert int(status["CapEff"].strip(), 16) == 0
assert status["NoNewPrivs"].strip() == "1"
assert set(os.listdir("/sys/class/net")) == {"lo"}
assert not Path("/var/run/docker.sock").exists()
assert not Path("/app/.env").exists()
for key in os.environ:
    assert not any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "DATABASE_URL", "DSN")), key
try:
    Path("/app/container-write-check").write_text("must fail")
except OSError as exc:
    assert exc.errno in {errno.EROFS, errno.EACCES}
else:
    raise AssertionError("Root filesystem writable")
with socket.socket() as sock:
    sock.settimeout(1)
    try:
        sock.connect(("1.1.1.1", 443))
    except OSError:
        pass
    else:
        raise AssertionError("External network reachable")
Path("/tmp/lab-write-check").write_text("temporary only")
print("CONTAINER_RUNTIME_OK: kernel network isolation, read-only root, no capabilities/secrets", flush=True)
raise SystemExit(subprocess.run([sys.executable, "-B", "tools/run_local_lab.py"]).returncode)
