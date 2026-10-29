# Local test laboratory

This branch is a development/test copy. Production remains on `main`; do not connect this branch to Railway or broker credentials.

Run with a Python environment containing this repository's dependencies:

```sh
python tools/run_local_lab.py
```

The runner copies Python source into a temporary directory, supplies dummy keys, uses temporary SQLite/state, clears inherited credentials and database URLs, and runs only the reviewed mock-based test modules in `SUITE`. It never launches the controller/watcher or sends broker orders/Telegram. Tests may print simulated order messages; those are mocks. All temporary state is removed on exit. No production database or .env is copied. No paid service is required. Installing Python dependencies may require internet before running the lab.

Before importing bot code, a fail-closed self-check confirms socket/DNS, subprocess and shell calls are blocked. Python guards protect this reviewed test workload; they are NOT an OS security boundary for malicious code or native extensions. Do not run arbitrary unreviewed code here. For that use a disposable container/VM with network disabled and no credentials/mounts. No Docker runtime was available when this runner was validated.

This is an executable testing environment, not a second continuously running trading bot. PostgreSQL-specific tests skip without a separate test database; production SQL/broker behavior is not proven by mock tests. The selected suite is not the full repository suite.

## Release gate

1. Develop on this test branch; refresh deliberately from a known main SHA.
2. Review imports and add regression fixtures/mocks for the changed behavior to `SUITE`.
3. Run the lab; any failure blocks promotion. Record skipped checks explicitly.
4. Open a PR; require the existing full CI including its disposable PostgreSQL tests.
5. Compare the approved source SHA to production after deployment, then inspect runtime evidence.
6. Do not weaken entry/risk/stop gates to generate samples. PAPER only; LIVE stays disabled.

No automated merge or production deployment is configured by this lab.

## OS-isolated execution

Run `python tools/run_container_lab.py` with Docker, or use the **Offline laboratory** workflow on the lab branch. There is no fallback if Docker is unavailable. The trusted harness builds an image from reviewed dependency requirements and Python source only; it does not execute bot source during build. Dependency downloads happen during build. Tests run afterward with network `none`, a read-only root, UID 65532, no capabilities, no-new-privileges, no host bind mounts or credentials, 2 CPU/2 GiB/128 process limits, and a 256 MiB temporary filesystem. Container configuration and actual kernel restrictions are checked before the bot modules are loaded. The container is deleted afterward.

The offline test suite still skips PostgreSQL checks: separate full CI uses a disposable PostgreSQL container. Do not connect the offline container to production to remove those skips.

Trust/review this harness, its verification script, dependencies and workflow; do not let untrusted submissions replace them. No host Docker socket is mounted. This substantially strengthens process isolation but containers share the host kernel and are not a guarantee against hostile kernel exploits. Use disposable hosted runners for suspicious code. This workflow uses no Railway service or paid data API; GitHub Actions remains subject to your account's usage limits.
