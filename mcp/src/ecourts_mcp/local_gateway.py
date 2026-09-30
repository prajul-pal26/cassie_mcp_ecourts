"""On-demand local lifecycle for the bundled eCourts FastAPI gateway.

The MCP process is intentionally small and starts with the client.  The much
heavier gateway is cloned, installed, and launched only when a lookup tool is
actually called.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

DEFAULT_REPOSITORY = "https://github.com/prajul-pal26/cassie_mcp_ecourts.git"
DEFAULT_PORT = 9021


class LocalGatewayError(RuntimeError):
    """The local gateway could not be installed or started."""


def ensure_gateway() -> str:
    """Return a healthy local gateway URL, installing it on first lookup."""
    configured = os.environ.get("ECOURTS_GATEWAY_URL", "").strip()
    if configured and not _is_local_url(configured):
        return configured.rstrip("/")

    port = int(os.environ.get("CASSIE_ECOURTS_PORT", str(DEFAULT_PORT)))
    base_url = configured.rstrip("/") if configured else f"http://127.0.0.1:{port}"
    if _healthy(base_url):
        return base_url

    root = _install_root()
    repository = os.environ.get("CASSIE_ECOURTS_REPOSITORY", DEFAULT_REPOSITORY)
    checkout = root / "repository"
    changed = _checkout_or_update(checkout, repository, root)
    python = _gateway_python(checkout)
    if changed or not python.exists():
        if changed:
            _stop_gateway(root)
        _install_gateway(checkout, python)
    _start_gateway(checkout, python, base_url, root)
    _wait_until_healthy(base_url)
    return base_url


def update_gateway() -> dict[str, str | bool]:
    """Force a safe fast-forward update for the private local checkout."""
    root = _install_root()
    checkout = root / "repository"
    repository = os.environ.get("CASSIE_ECOURTS_REPOSITORY", DEFAULT_REPOSITORY)
    changed = _checkout_or_update(checkout, repository, root, force=True)
    return {"updated": changed, "message": "Local update check completed."}


def _install_root() -> Path:
    configured = os.environ.get("CASSIE_ECOURTS_HOME", "").strip()
    if configured:
        root = Path(configured).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Cassie eCourts"
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Cassie eCourts"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "cassie-ecourts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _checkout_or_update(checkout: Path, repository: str, root: Path, force: bool = False) -> bool:
    if not checkout.exists():
        _run(["git", "clone", "--depth", "1", repository, str(checkout)], "download the gateway source")
        _write_update_time(root)
        return True
    if not (checkout / ".git").is_dir():
        raise LocalGatewayError(f"The local install folder is not a Git checkout: {checkout}")
    if not force and not _update_due(root):
        return False
    before = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"], "inspect the local version").strip()
    _run(["git", "-C", str(checkout), "pull", "--ff-only"], "check for gateway updates")
    after = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"], "inspect the updated version").strip()
    _write_update_time(root)
    return before != after


def _gateway_python(checkout: Path) -> Path:
    venv = checkout / "gateway" / ".venv"
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _install_gateway(checkout: Path, python: Path) -> None:
    gateway_dir = checkout / "gateway"
    _run([sys.executable, "-m", "venv", str(gateway_dir / ".venv")], "create the private gateway environment")
    _run([str(python), "-m", "pip", "install", "--upgrade", "pip"], "prepare the gateway environment")
    _run([str(python), "-m", "pip", "install", "-r", "requirements.txt"], "install gateway dependencies", cwd=gateway_dir)


def _start_gateway(checkout: Path, python: Path, base_url: str, root: Path) -> None:
    if _healthy(base_url):
        return
    gateway_dir = checkout / "gateway"
    port = base_url.rsplit(":", 1)[-1]
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    log = open(root / "gateway.log", "ab")
    environment = os.environ.copy()
    environment.update({
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_PORT": port,
        "ECOURTS_DATA_DIR": str(data_dir),
        "LOG_LEVEL": environment.get("LOG_LEVEL", "INFO"),
    })
    process = subprocess.Popen(
        [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", port, "--workers", "1"],
        cwd=gateway_dir,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (root / "gateway.pid").write_text(str(process.pid), encoding="utf-8")


def _stop_gateway(root: Path) -> None:
    """Stop only the process previously started from this private state folder."""
    pid_file = root / "gateway.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    finally:
        pid_file.unlink(missing_ok=True)


def _wait_until_healthy(base_url: str) -> None:
    deadline = time.monotonic() + float(os.environ.get("CASSIE_ECOURTS_START_TIMEOUT_SECONDS", "90"))
    while time.monotonic() < deadline:
        if _healthy(base_url):
            return
        time.sleep(0.5)
    raise LocalGatewayError("The local eCourts gateway did not become ready. Check the local gateway log.")


def _healthy(base_url: str) -> bool:
    try:
        with urlopen(f"{base_url.rstrip('/')}/health", timeout=2) as response:
            return 200 <= response.status < 300
    except (URLError, OSError, ValueError):
        return False


def _update_due(root: Path) -> bool:
    try:
        last = json.loads((root / "update.json").read_text(encoding="utf-8"))["checked_at"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return True
    interval = float(os.environ.get("CASSIE_ECOURTS_UPDATE_INTERVAL_SECONDS", str(24 * 60 * 60)))
    return time.time() - float(last) >= interval


def _write_update_time(root: Path) -> None:
    (root / "update.json").write_text(json.dumps({"checked_at": time.time()}), encoding="utf-8")


def _is_local_url(url: str) -> bool:
    return "127.0.0.1" in url or "localhost" in url


def _run(command: list[str], action: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)
    except FileNotFoundError as error:
        raise LocalGatewayError(f"Cannot {action}: required command '{command[0]}' is not installed.") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "unknown error").strip()[-500:]
        raise LocalGatewayError(f"Could not {action}: {detail}") from error
    return result.stdout
