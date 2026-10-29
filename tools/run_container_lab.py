"""Trusted Docker harness: offline, disposable, no host mounts or credentials."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = """FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1
COPY requirements.txt /opt/lab-requirements.txt
RUN pip install --no-cache-dir -r /opt/lab-requirements.txt
COPY source /app
WORKDIR /app
USER 65532:65532
CMD ["python", "-B", "tools/verify_container.py"]
"""


def call(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    if not shutil.which("docker"):
        raise SystemExit("Docker required. No unsafe local fallback is allowed; use Offline laboratory CI.")
    with tempfile.TemporaryDirectory(prefix="lab-context-") as tmp:
        context = Path(tmp)
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if any((ROOT / Path(*rel.parts[:i])).is_symlink() for i in range(1, len(rel.parts)+1)):
                continue
            if any(p.startswith(".") or p in {"data", "logs", "venv", "env", "__pycache__", "node_modules"} for p in rel.parts):
                continue
            dest = context / "source" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        # Only reviewed dependencies are installed; never build the repository's Dockerfile.
        shutil.copyfile(ROOT / "tools/lab-requirements.txt", context / "requirements.txt")
        (context / "Dockerfile").write_text(DOCKERFILE)
        iid = context / "image-id"
        call(["docker", "build", "--iidfile", str(iid), str(context)], timeout=900)
        image = iid.read_text().strip()
        container = None
        try:
            container = call(["docker", "create", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--user", "65532:65532", "--pids-limit", "128", "--memory", "2g", "--cpus", "2",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
                "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp", image], capture_output=True).stdout.strip()
            state = json.loads(call(["docker", "inspect", container], capture_output=True).stdout)[0]
            host = state["HostConfig"]
            assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
            assert not host.get("Binds") and not host.get("Privileged")
            assert host["CapDrop"] == ["ALL"] and host["PidsLimit"] == 128
            assert host["Memory"] == 2 * 1024**3 and host["NanoCpus"] == 2_000_000_000
            assert "no-new-privileges:true" in host["SecurityOpt"]
            assert state["Config"]["User"] == "65532:65532"
            assert all(m["Type"] == "tmpfs" for m in state.get("Mounts", []))
            print("CONTAINER_CONFIG_OK: no network/mounts, read-only, non-root, resource limits", flush=True)
            call(["docker", "start", "--attach", container], timeout=600)
            code = int(call(["docker", "inspect", "--format", "{{.State.ExitCode}}", container], capture_output=True).stdout)
            return code
        finally:
            if container:
                subprocess.run(["docker", "rm", "--force", container], check=False)
            subprocess.run(["docker", "image", "rm", image], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
