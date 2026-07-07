# xray_manager.py — Xray-core lifecycle manager
"""
Xray-core integration module for Spider Panel.

Handles:
- Downloading and extracting Xray-core
- Generating server config from inbound settings
- Starting / stopping / restarting Xray-core as a subprocess
- Auto-watching inbound changes and regenerating config
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Spider-Gateway.XrayMgr")

# ── Constants ────────────────────────────────────────────────────────────────
XRAY_DOWNLOAD_URL = os.environ.get(
    "XRAY_DOWNLOAD_URL",
    "https://github.com/XTLS/Xray-core/releases/download/v26.3.27/Xray-linux-64.zip",
)
XRAY_DIR = Path(os.environ.get("XRAY_DIR", "/data/xray-core"))
XRAY_BIN = XRAY_DIR / "xray"
XRAY_CONFIG_PATH = XRAY_DIR / "config.json"
XRAY_LOG_PATH = XRAY_DIR / "xray.log"
XRAY_PID_PATH = XRAY_DIR / "xray.pid"

# ── Global state ──────────────────────────────────────────────────────────────
_xray_process: Optional[subprocess.Popen] = None
_xray_running = False
_xray_lock = asyncio.Lock()
_install_lock = asyncio.Lock()
_installed = False


# ── Installation ─────────────────────────────────────────────────────────────
async def is_installed() -> bool:
    """Check whether Xray-core binary exists and is executable."""
    return XRAY_BIN.exists() and os.access(str(XRAY_BIN), os.X_OK)


async def get_local_version() -> Optional[str]:
    """Get installed Xray version string."""
    if not await is_installed():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(XRAY_BIN), "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        line = stdout.decode("utf-8", errors="ignore").split("\n")[0].strip()
        # Typical output: "Xray 26.3.27 (Xray, Penetrates Everything.) ..."
        return line.split(" ")[1] if " " in line else line
    except Exception as exc:
        logger.warning(f"Could not read Xray version: {exc}")
        return None


async def download_xray() -> bool:
    """Download Xray-core zip archive. Returns True on success."""
    import httpx

    XRAY_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = XRAY_DIR / "xray.zip"

    logger.info(f"Downloading Xray-core from {XRAY_DOWNLOAD_URL} ...")
    try:
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            async with client.stream("GET", XRAY_DOWNLOAD_URL) as resp:
                if resp.status_code != 200:
                    logger.error(f"Xray download failed: HTTP {resp.status_code}")
                    return False
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(zip_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded / total * 100
                            if downloaded % (5 * 1024 * 1024) < 1024 * 1024:
                                logger.info(f"Xray download: {pct:.0f}% ({downloaded // 1024 // 1024} MB / {total // 1024 // 1024} MB)")
        logger.info(f"Xray-core downloaded: {downloaded} bytes")
        return True
    except Exception as exc:
        logger.error(f"Xray download error: {exc}")
        if zip_path.exists():
            zip_path.unlink()
        return False


async def extract_xray() -> bool:
    """Extract xray binary from the downloaded zip."""
    zip_path = XRAY_DIR / "xray.zip"
    if not zip_path.exists():
        logger.error("Xray zip not found")
        return False

    logger.info("Extracting Xray-core ...")
    try:
        # Use Python's zipfile — fast and no external deps
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Find the xray binary in the archive
            names = zf.namelist()
            xray_name = None
            for n in names:
                base = os.path.basename(n).lower()
                if base in ("xray", "xray.exe"):
                    xray_name = n
                    break
            if not xray_name:
                logger.error(f"xray binary not found in archive: {names[:10]}")
                return False

            # Extract
            zf.extract(xray_name, XRAY_DIR)
            extracted = XRAY_DIR / xray_name

        # Move binary to expected location if needed
        if extracted != XRAY_BIN:
            shutil.move(str(extracted), str(XRAY_BIN))

        # Make executable
        XRAY_BIN.chmod(0o755)

        # Clean up zip
        zip_path.unlink()
        logger.info(f"Xray-core extracted to {XRAY_BIN}")

        # Verify
        if not await is_installed():
            logger.error("Extracted binary is not executable")
            return False

        ver = await get_local_version()
        logger.info(f"Xray-core version: {ver}")
        return True

    except Exception as exc:
        logger.error(f"Xray extraction error: {exc}")
        return False


async def install_xray(force: bool = False) -> bool:
    """Install or reinstall Xray-core. Idempotent unless force=True."""
    global _installed

    async with _install_lock:
        if _installed and not force:
            return True
        if await is_installed() and not force:
            _installed = True
            ver = await get_local_version()
            logger.info(f"Xray-core already installed: {ver}")
            return True

        if force and XRAY_BIN.exists():
            XRAY_BIN.unlink()

        ok = await download_xray()
        if not ok:
            return False
        ok = await extract_xray()
        if ok:
            _installed = True
        return ok


# ── Config generation ────────────────────────────────────────────────────────
async def generate_and_write_config() -> Optional[dict]:
    """Generate Xray server config from panel inbounds and write to disk."""
    from main import generate_xray_server_config

    try:
        config = generate_xray_server_config()  # all inbounds
    except Exception as exc:
        logger.error(f"Failed to generate Xray config: {exc}")
        return None

    # Write to file
    try:
        XRAY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info(f"Xray config written to {XRAY_CONFIG_PATH} ({len(config.get('inbounds', []))} inbounds)")
    except Exception as exc:
        logger.error(f"Failed to write Xray config: {exc}")
        return None

    return config


# ── Process management ───────────────────────────────────────────────────────
async def is_running() -> bool:
    """Check whether Xray-core process is running."""
    global _xray_process, _xray_running

    if _xray_process is None:
        return False
    # Check if process is still alive
    poll = _xray_process.poll()
    if poll is not None:
        _xray_running = False
        logger.info(f"Xray-core exited with code {poll}")
        return False
    return _xray_running


async def start_xray() -> bool:
    """Start Xray-core with the generated config."""
    global _xray_process, _xray_running

    async with _xray_lock:
        if _xray_running and await is_running():
            logger.info("Xray-core is already running")
            return True

        if not await is_installed():
            logger.warning("Xray-core not installed, attempting install...")
            ok = await install_xray()
            if not ok:
                logger.error("Xray-core installation failed")
                return False

        # Generate fresh config
        config = await generate_and_write_config()
        if not config:
            logger.error("Failed to generate Xray config")
            return False

        if not config.get("inbounds"):
            logger.warning("No inbounds configured — Xray would have nothing to serve, skipping start")
            return False

        # Start process
        cmd = [
            str(XRAY_BIN),
            "run",
            "-config", str(XRAY_CONFIG_PATH),
        ]
        logger.info(f"Starting Xray-core: {' '.join(cmd)}")

        try:
            with open(XRAY_LOG_PATH, "a", encoding="utf-8") as log_f:
                log_f.write(f"\n─── Xray started at {time.strftime('%Y-%m-%d %H:%M:%S')} ───\n")
                _xray_process = subprocess.Popen(
                    cmd,
                    encoding="utf-8", errors="replace",
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=str(XRAY_DIR),
                    preexec_fn=os.setsid if sys.platform != "win32" else None,
                )
            # Write PID
            with open(XRAY_PID_PATH, "w", encoding="utf-8") as pf:
                pf.write(str(_xray_process.pid))

            # Brief wait to check if it starts successfully
            await asyncio.sleep(2)
            if _xray_process.poll() is not None:
                # Died immediately — read last few log lines
                try:
                    with open(XRAY_LOG_PATH, "r", encoding="utf-8", errors="ignore") as lf:
                        lines = lf.readlines()
                        tail = "".join(lines[-20:])
                    logger.error(f"Xray-core died immediately. Last log lines:\n{tail}")
                except Exception:
                    logger.error(f"Xray-core died immediately with code {_xray_process.poll()}")
                _xray_running = False
                return False

            _xray_running = True
            logger.info(f"Xray-core started (PID={_xray_process.pid})")
            return True

        except Exception as exc:
            logger.error(f"Failed to start Xray-core: {exc}")
            _xray_running = False
            return False


async def stop_xray() -> bool:
    """Gracefully stop Xray-core."""
    global _xray_process, _xray_running

    async with _xray_lock:
        if not _xray_running or _xray_process is None:
            _xray_running = False
            return True

        logger.info("Stopping Xray-core ...")
        try:
            if sys.platform == "win32":
                _xray_process.terminate()
            else:
                os.killpg(os.getpgid(_xray_process.pid), signal.SIGTERM)

            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, _xray_process.wait),
                    timeout=15,
                )
            except asyncio.TimeoutError:
                logger.warning("Xray-core did not stop gracefully, force-killing")
                if sys.platform == "win32":
                    _xray_process.kill()
                else:
                    os.killpg(os.getpgid(_xray_process.pid), signal.SIGKILL)

            _xray_running = False
            _xray_process = None
            if XRAY_PID_PATH.exists():
                XRAY_PID_PATH.unlink()
            logger.info("Xray-core stopped")
            return True

        except Exception as exc:
            logger.error(f"Error stopping Xray-core: {exc}")
            _xray_running = False
            _xray_process = None
            return False


async def restart_xray() -> bool:
    """Restart Xray-core."""
    await stop_xray()
    await asyncio.sleep(1)
    return await start_xray()


async def ensure_xray_running() -> bool:
    """Check if Xray is running; start it if not. Call periodically."""
    if await is_running():
        return True
    # Only auto-start if there are Reality inbounds
    from main import INBOUNDS
    has_reality = any(
        ib.get("protocol") == "reality" or ib.get("security") == "reality"
        for ib in INBOUNDS.values()
    )
    if has_reality:
        logger.info("Reality inbounds detected, auto-starting Xray-core")
        return await start_xray()
    return True


# ── Health check loop ────────────────────────────────────────────────────────
_watcher_task = None


async def _xray_watcher_loop():
    """Background task: monitor Xray, restart on crash, watch for config changes, sync traffic."""
    last_config_hash = ""
    tick = 0

    while True:
        try:
            await asyncio.sleep(15)
            tick += 1

            # Check if we need to be running (any Reality inbounds?)
            from main import INBOUNDS
            has_reality = any(
                ib.get("protocol") == "reality" or ib.get("security") == "reality"
                for ib in INBOUNDS.values()
            )

            if not has_reality:
                if await is_running():
                    logger.info("No Reality inbounds — stopping Xray")
                    await stop_xray()
                continue

            # Check config hash — regenerate if inbound config changed
            try:
                config = await generate_and_write_config()
                if config:
                    config_str = json.dumps(config, sort_keys=True, ensure_ascii=False)
                    config_hash = hashlib.sha256(config_str.encode()).hexdigest()
                    if config_hash != last_config_hash:
                        if await is_running():
                            logger.info("Inbound config changed — restarting Xray")
                            await restart_xray()
                        last_config_hash = config_hash
            except Exception:
                pass

            # Ensure Xray is running
            if not await is_running():
                await start_xray()

            # If running, check periodically if process died unexpectedly
            if _xray_process and _xray_process.poll() is not None:
                logger.warning(f"Xray-core died unexpectedly (exit code {_xray_process.poll()}) — restarting")
                await start_xray()

            # Sync real traffic stats every 4 ticks (~60s)
            if tick % 4 == 0 and await is_running():
                try:
                    await sync_traffic_to_panel()
                except Exception as exc:
                    logger.debug(f"Traffic sync skipped: {exc}")

        except Exception as exc:
            logger.error(f"Xray watcher error: {exc}")


def start_watcher():
    """Start the background Xray watcher task."""
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        _watcher_task = asyncio.create_task(_xray_watcher_loop())
        logger.info("Xray watcher started")


async def stop_watcher():
    """Stop the background watcher task."""
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
        _watcher_task = None


# ── Status API helpers ───────────────────────────────────────────────────────
async def get_status() -> dict:
    """Return Xray-core status info."""
    return {
        "installed": await is_installed(),
        "version": await get_local_version(),
        "running": await is_running(),
        "pid": _xray_process.pid if _xray_process else None,
        "config_path": str(XRAY_CONFIG_PATH),
        "log_path": str(XRAY_LOG_PATH),
    }


# ── Real traffic stats via Xray API ────────────────────────────────────
async def query_traffic_stats() -> dict[str, dict]:
    """Query Xray-core stats API for per-user upload/download traffic.

    Returns dict keyed by user config_uuid (email) with:
        uplink: total bytes uploaded by user
        downlink: total bytes downloaded by user

    Requires Xray to be running with stats service enabled and API
    inbound on 127.0.0.1:10085.
    """
    if not await is_running():
        return {}

    try:
        # xray api statsquery --server=127.0.0.1:10085
        proc = await asyncio.create_subprocess_exec(
            str(XRAY_BIN), "api", "statsquery",
            "--server=127.0.0.1:10085",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            logger.warning(f"Xray stats query failed: {stderr.decode()}")
            return {}

        raw = stdout.decode("utf-8", errors="ignore")
        stats = json.loads(raw) if raw.strip() else {}

        # Parse: stat[].name = "user>>>UUID>>>traffic>>>uplink" / "...downlink"
        result: dict[str, dict] = {}
        for item in stats.get("stat", []):
            name = item.get("name", "")
            value = int(item.get("value", "0"))
            parts = name.split(">>>")
            if len(parts) >= 5 and parts[0] == "user":
                email = parts[1]
                direction = parts[3]  # "uplink" or "downlink"
                if email not in result:
                    result[email] = {"uplink": 0, "downlink": 0}
                if direction == "uplink":
                    result[email]["uplink"] = value
                elif direction == "downlink":
                    result[email]["downlink"] = value

        return result

    except asyncio.TimeoutError:
        logger.warning("Xray stats query timed out")
        return {}
    except json.JSONDecodeError:
        logger.warning(f"Xray stats: invalid JSON")
        return {}
    except Exception as exc:
        logger.error(f"Xray stats query error: {exc}")
        return {}


async def sync_traffic_to_panel():
    """Query Xray traffic stats and sync them to panel user records."""
    stats = await query_traffic_stats()
    if not stats:
        return

    from main import USERS, USERS_LOCK, LINKS, LINKS_LOCK, save_state

    async with USERS_LOCK:
        for uid, u in USERS.items():
            cuuid = u.get("config_uuid") or uid
            if cuuid in stats:
                s = stats[cuuid]
                total = s["uplink"] + s["downlink"]
                if total > u.get("traffic_used_bytes", 0):
                    u["traffic_used_bytes"] = total

    async with LINKS_LOCK:
        for lid, link in LINKS.items():
            if lid in stats:
                s = stats[lid]
                total = s["uplink"] + s["downlink"]
                if total > link.get("used_bytes", 0):
                    link["used_bytes"] = total

    asyncio.create_task(save_state())
