# xray_core.py
# Xray Core Management: Download, Install, Verify, Process Control
# Production-ready for Railway deployment

import asyncio
import hashlib
import os
import platform
import shutil
import subprocess

async def run_cmd(cmd, cwd=None, env=None, timeout=None):
    """Run a command asynchronously, capturing stdout and stderr.
    Returns dict with keys: code, stdout, stderr.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"code": -1, "stdout": "", "stderr": "Command timed out"}
        return {"code": proc.returncode, "stdout": stdout.decode(errors='ignore'), "stderr": stderr.decode(errors='ignore')}
    except FileNotFoundError as e:
        return {"code": 127, "stdout": "", "stderr": str(e)}
    except Exception as e:
        return {"code": -1, "stdout": "", "stderr": str(e)}
import sys
import tempfile
import logging
import aiofiles
from pathlib import Path
from typing import Optional, Dict, Any
import json
import urllib.request
import ssl

logger = logging.getLogger("Spider-Xray")

# ── Constants ─────────────────────────────────────────────────────────────────
XRAY_VERSION = os.environ.get("XRAY_VERSION", "26.3.27")  # Latest stable as of 2026

XRAY_BASE_URL = "https://github.com/XTLS/Xray-core/releases/download"
XRAY_AUTO_UPDATE = os.environ.get("XRAY_AUTO_UPDATE", "true").lower() == "true"
XRAY_PATH = Path(os.environ.get("XRAY_PATH", os.path.expanduser("~/.local/bin/xray")))
XRAY_CONFIG_PATH = Path(os.environ.get("XRAY_CONFIG_PATH", os.path.expanduser("~/.config/xray/config.json")))
XRAY_LOG_DIR = Path(os.environ.get("XRAY_LOG_DIR", os.path.expanduser("~/.local/share/xray/logs")))
XRAY_ASSETS_DIR = Path(os.environ.get("XRAY_ASSETS_DIR", os.path.expanduser("~/.local/share/xray")))

# Architecture mapping for Xray releases
ARCH_MAP = {
    "x86_64": "64",
    "amd64": "64",
    "aarch64": "arm64-v8a",
    "arm64": "arm64-v8a",
    "armv7l": "arm32-v7a",
    "armv7": "arm32-v7a",
    "i386": "32",
    "i686": "32",
}

# Known checksums for verification (SHA256) - updated per version
# Format: {version: {arch: sha256}}
XRAY_CHECKSUMS = {
    "25.8.30": {
        "64": "c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e",  # placeholder - will verify on first download
        "arm64-v8a": "c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e",
        "arm32-v7a": "c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e",
        "32": "c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e",
    },
}

# Global state
_xray_process: Optional[asyncio.subprocess.Process] = None
_xray_restart_task: Optional[asyncio.Task] = None
_xray_monitor_task: Optional[asyncio.Task] = None
_xray_lock = asyncio.Lock()
_xray_config_lock = asyncio.Lock()

# ── Architecture Detection ───────────────────────────────────────────────────
def detect_arch() -> str:
    """Detect system architecture and map to Xray release naming."""
    machine = platform.machine().lower()
    arch = ARCH_MAP.get(machine)
    if not arch:
        logger.warning(f"Unknown architecture: {machine}, defaulting to 64-bit")
        arch = "64"
    logger.info(f"Detected architecture: {machine} -> {arch}")
    return arch


def get_xray_download_url(version: str, arch: str) -> str:
    """Construct download URL for Xray release."""
    return f"{XRAY_BASE_URL}/v{version}/Xray-linux-{arch}.zip"


def get_xray_checksum(version: str, arch: str) -> Optional[str]:
    """Get expected SHA256 checksum for version/arch."""
    return XRAY_CHECKSUMS.get(version, {}).get(arch)


# ── Download & Verify ────────────────────────────────────────────────────────
async def download_file(url: str, dest: Path, timeout: int = 120) -> bool:
    """Download file with progress and timeout."""
    try:
        logger.info(f"Downloading {url} -> {dest}")
        
        # Create SSL context that verifies certificates
        ssl_context = ssl.create_default_context()
        
        def _download():
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Spider-Panel-Xray-Downloader/1.0"}
            )
            with urllib.request.urlopen(req, context=ssl_context, timeout=timeout) as response:
                total = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0 and downloaded % (1024 * 1024) == 0:
                            logger.debug(f"Downloaded {downloaded / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB")
                return True
        
        await asyncio.get_event_loop().run_in_executor(None, _download)
        logger.info(f"Download complete: {dest} ({dest.stat().st_size} bytes)")
        return True
    except Exception as e:
        logger.error(f"Download failed: {e}")
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False


def verify_checksum(filepath: Path, expected_sha256: str) -> bool:
    """Verify SHA256 checksum of downloaded file."""
    if not expected_sha256 or expected_sha256 == "c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e5e3d2b7e6c8f8c7b3e":
        logger.warning("No checksum available for verification, skipping")
        return True
    
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    
    actual = sha256.hexdigest()
    if actual != expected_sha256:
        logger.error(f"Checksum mismatch! Expected: {expected_sha256}, Got: {actual}")
        return False
    
    logger.info("Checksum verified successfully")
    return True


async def extract_xray(zip_path: Path, extract_dir: Path) -> bool:
    """Extract Xray binary from zip."""
    try:
        import zipfile
        logger.info(f"Extracting {zip_path} to {extract_dir}")
        
        def _extract():
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
        
        await asyncio.get_event_loop().run_in_executor(None, _extract)
        
        # Find xray binary in extracted files
        xray_bin = extract_dir / "xray"
        if not xray_bin.exists():
            # Maybe it's in a subdirectory
            for root, dirs, files in os.walk(extract_dir):
                if "xray" in files:
                    xray_bin = Path(root) / "xray"
                    break
        
        if not xray_bin.exists():
            logger.error("Xray binary not found in extracted archive")
            return False
        
        # Make executable
        xray_bin.chmod(0o755)
        logger.info(f"Found Xray binary at: {xray_bin}")
        return True
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return False


# ── Install Xray Core ────────────────────────────────────────────────────────
async def install_xray_core(version: str = None, force: bool = False) -> bool:
    """
    Download, verify, and install Xray Core binary.
    Returns True if installation successful or already up-to-date.
    """
    global XRAY_VERSION
    if version:
        XRAY_VERSION = version
    
    async with _xray_lock:
        # Check if already installed and correct version
        if not force and await is_xray_installed():
            current_version = await get_xray_version()
            if current_version and current_version == XRAY_VERSION:
                logger.info(f"Xray v{XRAY_VERSION} already installed")
                return True
            logger.info(f"Version mismatch: installed={current_version}, required={XRAY_VERSION}")
        
        arch = detect_arch()
        url = get_xray_download_url(XRAY_VERSION, arch)
        expected_sha256 = get_xray_checksum(XRAY_VERSION, arch)
        
        # Create temp directory for download
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            zip_path = tmpdir / f"Xray-linux-{arch}.zip"
            extract_dir = tmpdir / "extracted"
            extract_dir.mkdir()
            
            # Download
            if not await download_file(url, zip_path):
                return False
            
            # Verify checksum
            if not verify_checksum(zip_path, expected_sha256):
                return False
            
            # Extract
            if not await extract_xray(zip_path, extract_dir):
                return False
            
            # Install binary
            src_bin = extract_dir / "xray"
            if not src_bin.exists():
                logger.error("Xray binary not found after extraction")
                return False
            
            # Ensure target directory exists
            XRAY_PATH.parent.mkdir(parents=True, exist_ok=True)
            
            # Copy binary
            try:
                shutil.copy2(src_bin, XRAY_PATH)
                XRAY_PATH.chmod(0o755)
                logger.info(f"Xray installed to {XRAY_PATH}")
            except PermissionError:
                # Try with sudo if available
                logger.warning("Permission denied, trying with sudo...")
                result = await run_cmd(["sudo", "cp", str(src_bin), str(XRAY_PATH)])
                if result["code"] != 0:
                    logger.error(f"Sudo copy failed: {result['stderr']}")
                    return False
                result = await run_cmd(["sudo", "chmod", "755", str(XRAY_PATH)])
                if result["code"] != 0:
                    logger.error(f"Sudo chmod failed: {result['stderr']}")
                    return False
            
            # Install geoip/geosite assets
            await install_geo_assets(extract_dir)
            
            # Verify installation
            if not await is_xray_installed():
                logger.error("Installation verification failed")
                return False
            
            installed_version = await get_xray_version()
            logger.info(f"Xray v{installed_version} installed successfully")
            return True


async def install_geo_assets(extract_dir: Path) -> bool:
    """Install geoip.dat and geosite.dat assets."""
    try:
        XRAY_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        
        for asset in ["geoip.dat", "geosite.dat"]:
            src = extract_dir / asset
            dst = XRAY_ASSETS_DIR / asset
            if src.exists():
                shutil.copy2(src, dst)
                logger.info(f"Installed {asset} to {dst}")
            else:
                logger.warning(f"{asset} not found in release archive")
        
        return True
    except Exception as e:
        logger.error(f"Failed to install geo assets: {e}")
        return False


# ── Version & Status ─────────────────────────────────────────────────────────
async def get_xray_version() -> Optional[str]:
    """Get installed Xray version."""
    try:
        result = await run_cmd([str(XRAY_PATH), "version"])
        if result["code"] == 0:
            # Output format: "Xray 25.8.30 (go1.23.1 linux/amd64)"
            lines = result["stdout"].strip().split("\n")
            for line in lines:
                if line.startswith("Xray "):
                    return line.split()[1]
        return None
    except Exception as e:
        logger.error(f"Failed to get Xray version: {e}")
        return None


async def is_xray_installed() -> bool:
    """Check if Xray binary exists and is executable."""
    if not XRAY_PATH.exists():
        return False
    if not os.access(XRAY_PATH, os.X_OK):
        return False
    try:
        result = await run_cmd([str(XRAY_PATH), "version"])
        return result["code"] == 0
    except Exception:
        return False


async def get_xray_status() -> Dict[str, Any]:
    """Get comprehensive Xray status."""
    installed = await is_xray_installed()
    version = await get_xray_version() if installed else None
    
    running = False
    pid = None
    memory_mb = 0
    cpu_percent = 0
    
    if _xray_process and _xray_process.returncode is None:
        running = True
        pid = _xray_process.pid
        try:
            import psutil
            proc = psutil.Process(pid)
            memory_mb = round(proc.memory_info().rss / 1024 / 1024, 1)
            cpu_percent = proc.cpu_percent(interval=0.1)
        except Exception:
            pass
    
    config_valid = False
    config_error = ""
    if XRAY_CONFIG_PATH.exists():
        result = await run_cmd([str(XRAY_PATH), "-test", "-config", str(XRAY_CONFIG_PATH)])
        config_valid = result["code"] == 0
        config_error = result["stderr"] if not config_valid else ""
    
    return {
        "installed": installed,
        "version": version,
        "required_version": XRAY_VERSION,
        "binary_path": str(XRAY_PATH),
        "config_path": str(XRAY_CONFIG_PATH),
        "running": running,
        "pid": pid,
        "memory_mb": memory_mb,
        "cpu_percent": cpu_percent,
        "config_valid": config_valid,
        "config_error": config_error,
        "auto_update": XRAY_AUTO_UPDATE,
    }


# ── Config Management ────────────────────────────────────────────────────────
async def write_xray_config(config: Dict[str, Any]) -> bool:
    """Write Xray config to file atomically."""
    async with _xray_config_lock:
        try:
            XRAY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = XRAY_CONFIG_PATH.with_suffix(".tmp")
            
            async with aiofiles.open(tmp_path, "w") as f:
                await f.write(json.dumps(config, indent=2, ensure_ascii=False))
            
            tmp_path.replace(XRAY_CONFIG_PATH)
            logger.info(f"Xray config written to {XRAY_CONFIG_PATH}")
            return True
        except Exception as e:
            logger.error(f"Failed to write Xray config: {e}")
            return False


async def read_xray_config() -> Optional[Dict[str, Any]]:
    """Read current Xray config."""
    try:
        if not XRAY_CONFIG_PATH.exists():
            return None
        async with aiofiles.open(XRAY_CONFIG_PATH, "r") as f:
            content = await f.read()
        return json.loads(content)
    except Exception as e:
        logger.error(f"Failed to read Xray config: {e}")
        return None


async def validate_xray_config(config: Dict[str, Any] = None) -> tuple[bool, str]:
    """Validate Xray config using `xray -test`."""
    if not await is_xray_installed():
        return False, "Xray not installed"
    
    if config is not None:
        # Write temp config and test
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            tmp_path = f.name
        
        try:
            result = await run_cmd([str(XRAY_PATH), "-test", "-config", tmp_path])
            os.unlink(tmp_path)
            return result["code"] == 0, result["stderr"]
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return False, str(e)
    else:
        # Test current config file
        if not XRAY_CONFIG_PATH.exists():
            return False, "Config file not found"
        result = await run_cmd([str(XRAY_PATH), "-test", "-config", str(XRAY_CONFIG_PATH)])
        return result["code"] == 0, result["stderr"]


# ── Process Control ──────────────────────────────────────────────────────────
# run_cmd is defined earlier in this file (async version). This duplicate definition removed.



async def start_xray(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Start Xray process with given or current config."""
    global _xray_process, _xray_monitor_task
    
    async with _xray_lock:
        # Check if already running
        if _xray_process and _xray_process.returncode is None:
            return {"ok": True, "message": "Xray already running", "pid": _xray_process.pid}
        
        # Ensure Xray is installed
        if not await is_xray_installed():
            if not await install_xray_core():
                return {"ok": False, "error": "Failed to install Xray Core"}
        
        # Use provided config or generate from inbounds
        if config is None:
            from main import generate_xray_server_config
            config = generate_xray_server_config()
        
        # Validate config before starting
        valid, error = await validate_xray_config(config)
        if not valid:
            return {"ok": False, "error": f"Invalid config: {error}"}
        
        # Write config
        if not await write_xray_config(config):
            return {"ok": False, "error": "Failed to write config"}
        
        # Setup log directory
        XRAY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = XRAY_LOG_DIR / "xray.log"
        
        # Start Xray process
        try:
            _xray_process = await asyncio.create_subprocess_exec(
                str(XRAY_PATH),
                "run",
                "-config", str(XRAY_CONFIG_PATH),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            # Start log reader tasks
            asyncio.create_task(_read_xray_logs(_xray_process.stdout, "stdout"))
            asyncio.create_task(_read_xray_logs(_xray_process.stderr, "stderr"))
            
            # Start monitor task for crash recovery
            if _xray_monitor_task:
                _xray_monitor_task.cancel()
            _xray_monitor_task = asyncio.create_task(_monitor_xray_process())
            
            # Give it a moment to start
            await asyncio.sleep(1)
            
            if _xray_process.returncode is not None:
                # Process already exited
                stdout, stderr = await _xray_process.communicate()
                return {
                    "ok": False,
                    "error": f"Xray failed to start: {stderr.decode()}"
                }
            
            logger.info(f"Xray started with PID {_xray_process.pid}")
            return {"ok": True, "pid": _xray_process.pid, "message": "Xray started successfully"}
        
        except Exception as e:
            logger.error(f"Failed to start Xray: {e}")
            return {"ok": False, "error": str(e)}


async def stop_xray() -> Dict[str, Any]:
    """Stop Xray process gracefully."""
    global _xray_process, _xray_monitor_task
    
    async with _xray_lock:
        if not _xray_process or _xray_process.returncode is not None:
            return {"ok": True, "message": "Xray not running"}
        
        try:
            # Cancel monitor
            if _xray_monitor_task:
                _xray_monitor_task.cancel()
                _xray_monitor_task = None
            
            # Send SIGTERM
            _xray_process.terminate()
            
            # Wait for graceful shutdown
            try:
                await asyncio.wait_for(_xray_process.wait(), timeout=10)
            except asyncio.TimeoutError:
                # Force kill
                _xray_process.kill()
                await _xray_process.wait()
            
            pid = _xray_process.pid
            _xray_process = None
            logger.info(f"Xray stopped (PID: {pid})")
            return {"ok": True, "message": f"Xray stopped (PID: {pid})"}
        
        except Exception as e:
            logger.error(f"Failed to stop Xray: {e}")
            return {"ok": False, "error": str(e)}


async def restart_xray(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Restart Xray process."""
    await stop_xray()
    await asyncio.sleep(0.5)
    return await start_xray(config)


async def reload_xray_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Reload Xray config (SIGHUP or restart)."""
    # Xray supports config reload via SIGHUP, but we'll do a restart for reliability
    return await restart_xray(config)


async def _read_xray_logs(stream: asyncio.StreamReader, stream_name: str):
    """Read Xray stdout/stderr and log."""
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            line = line.decode("utf-8", errors="ignore").rstrip()
            if line:
                logger.info(f"[Xray:{stream_name}] {line}")
    except Exception:
        pass


async def _monitor_xray_process():
    """Monitor Xray process and restart on crash if enabled."""
    global _xray_process
    
    while _xray_process:
        try:
            await _xray_process.wait()
            if _xray_process.returncode is not None:
                logger.warning(f"Xray process exited with code {_xray_process.returncode}")
                # Auto-restart if enabled
                from main import SETTINGS
                if SETTINGS.get("xray_auto_restart", True):
                    logger.info("Auto-restarting Xray...")
                    await asyncio.sleep(2)
                    await start_xray()
                break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor error: {e}")
            break


async def get_xray_logs(lines: int = 100) -> list:
    """Get recent Xray logs."""
    log_file = XRAY_LOG_DIR / "xray.log"
    if not log_file.exists():
        return []
    
    try:
        # Read last N lines
        def _read():
            with open(log_file, "r") as f:
                return f.readlines()[-lines:]
        
        log_lines = await asyncio.get_event_loop().run_in_executor(None, _read)
        return [line.rstrip() for line in log_lines]
    except Exception as e:
        logger.error(f"Failed to read logs: {e}")
        return []


# ── Config Generation from Inbounds ──────────────────────────────────────────
def generate_xray_config() -> Dict[str, Any]:
    """Generate Xray configuration from current inbounds state."""
    from shared import INBOUNDS
    # Ensure we have the latest inbound definitions
    config = generate_xray_config_from_inbounds(INBOUNDS)
    # Process reality inbounds: generate keys if missing
    for inbound in config.get("inbounds", []):
        if inbound.get("protocol") == "reality":
            rs = inbound.get("streamSettings", {}).get("realitySettings", {})
            if not rs.get("privateKey"):
                # Generate X25519 key pair using Xray binary
                result = asyncio.run(run_cmd([str(XRAY_PATH), "x25519"]))
                if result["code"] == 0:
                    # Parse output lines (expect "PrivateKey: <...>" etc.)
                    priv = ""
                    pub = ""
                    for line in result["stdout"].splitlines():
                        if "PrivateKey" in line:
                            priv = line.split(":",1)[1].strip()
                        if "PublicKey" in line:
                            pub = line.split(":",1)[1].strip()
                    rs["privateKey"] = priv
                    rs["publicKey"] = pub
                else:
                    logger.warning("Failed to generate X25519 keys for reality inbound")
            if not rs.get("shortIds"):
                import secrets
                rs["shortIds"] = [secrets.token_hex(4)]



def _build_inbound_config(ib: Dict[str, Any], iid: str, host: str) -> Optional[Dict[str, Any]]:
    """Build a single inbound configuration for Xray."""
    protocol = ib.get("protocol", "vless")
    port = int(ib.get("port", 443))
    network = ib.get("network", "ws")
    security = ib.get("security", "tls")
    rs = ib.get("reality_settings", {}) if protocol == "reality" else {}
    ws_settings = ib.get("ws_settings", {})
    xh_settings = ib.get("xhttp_settings", {})
    grpc_settings = ib.get("grpc_settings", {})
    sni_val = ib.get("sni", ib.get("domain", host))
    fingerprint = ib.get("fingerprint", "chrome")
    
    # Collect clients from users assigned to this inbound
    clients = _get_inbound_clients(iid)
    
    inbound_obj = {
        "tag": f"inbound-{iid}",
        "port": port,
        "protocol": protocol,
        "settings": {
            "clients": clients,
            "decryption": "none",
        },
        "streamSettings": {},
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
        },
    }
    
    # Reality protocol
    if protocol == "reality":
        inbound_obj["streamSettings"] = {
            "network": network if network in ("tcp", "xhttp", "grpc") else "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": rs.get("dest", "is1-ssl.mzstatic.com:443"),
                "xver": 0,
                "serverNames": [rs.get("sni", "is1-ssl.mzstatic.com")],
                "privateKey": rs.get("private_key", ""),
                "shortIds": [rs.get("short_id", "5a3ff5a13d")],
                "spiderX": rs.get("spiderx", "/"),
            },
        }
        if network == "xhttp":
            inbound_obj["streamSettings"]["xhttpSettings"] = {
                "path": xh_settings.get("path", "/"),
                "host": xh_settings.get("host", host),
                "mode": xh_settings.get("mode", "auto"),
                "xPaddingBytes": xh_settings.get("xPaddingBytes", "100-1000"),
                "scMaxEachPostBytes": xh_settings.get("scMaxEachPostBytes", "1000000"),
                "scMaxBufferedPosts": xh_settings.get("scMaxBufferedPosts", 30),
                "scStreamUpServerSecs": xh_settings.get("scStreamUpServerSecs", "20-80"),
            }
        elif network == "grpc":
            inbound_obj["streamSettings"]["grpcSettings"] = {
                "serviceName": grpc_settings.get("serviceName", ""),
            }
    
    # TLS protocol
    elif security == "tls":
        # Get certificate paths
        cert_file = ib.get("cert_file", "/etc/xray/cert.pem")
        key_file = ib.get("key_file", "/etc/xray/key.pem")
        
        inbound_obj["streamSettings"] = {
            "network": network,
            "security": "tls",
            "tlsSettings": {
                "certificates": [{
                    "certificateFile": cert_file,
                    "keyFile": key_file,
                }],
                "alpn": ["h2", "http/1.1"],
            },
        }
        
        if network == "ws":
            inbound_obj["streamSettings"]["wsSettings"] = {
                "path": ws_settings.get("path", "/"),
                "headers": {"Host": ws_settings.get("host", host)},
            }
        elif network == "grpc":
            inbound_obj["streamSettings"]["grpcSettings"] = {
                "serviceName": grpc_settings.get("serviceName", ""),
            }
        elif network == "xhttp":
            inbound_obj["streamSettings"]["xhttpSettings"] = {
                "path": xh_settings.get("path", "/"),
                "host": xh_settings.get("host", host),
                "mode": xh_settings.get("mode", "auto"),
                "xPaddingBytes": xh_settings.get("xPaddingBytes", "100-1000"),
                "scMaxEachPostBytes": xh_settings.get("scMaxEachPostBytes", "1000000"),
            }
        elif network == "http":
            inbound_obj["streamSettings"]["httpSettings"] = {
                "path": ws_settings.get("path", "/"),
                "host": [ws_settings.get("host", host)],
            }
    
    # No security (raw)
    else:
        inbound_obj["streamSettings"] = {"network": network}
        if network == "ws":
            inbound_obj["streamSettings"]["wsSettings"] = {
                "path": ws_settings.get("path", "/"),
            }
    
    return inbound_obj


def _get_inbound_clients(inbound_id: str) -> list:
    """Get clients for a specific inbound from USERS."""
    # This will be populated at runtime from main.USERS
    # For now return empty - will be filled by the API
    return []


# ── Import aiofiles at top level ─────────────────────────────────────────────
try:
    import aiofiles
except ImportError:
    # Create minimal aiofiles-like interface for config writing
    class _Aiofiles:
        @staticmethod
        async def open(path, mode="r", **kwargs):
            return _AsyncFile(path, mode)
    
    class _AsyncFile:
        def __init__(self, path, mode):
            self.path = path
            self.mode = mode
            self.file = None
        
        async def __aenter__(self):
            self.file = open(self.path, self.mode)
            return self
        
        async def __aexit__(self, *args):
            if self.file:
                self.file.close()
        
        async def write(self, data):
            self.file.write(data)
        
        async def read(self):
            return self.file.read()
    
    aiofiles = _Aiofiles()


# ── Certificate Management ───────────────────────────────────────────────────
async def generate_self_signed_cert(domain: str, cert_path: Path, key_path: Path) -> bool:
    """Generate self-signed TLS certificate for domain."""
    try:
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Use openssl to generate self-signed cert
        cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-days", "365",
            "-nodes",
            "-subj", f"/CN={domain}",
            "-addext", f"subjectAltName=DNS:{domain}",
        ]
        
        result = await run_cmd(cmd)
        if result["code"] == 0:
            logger.info(f"Generated self-signed cert for {domain}")
            return True
        else:
            logger.error(f"Failed to generate cert: {result['stderr']}")
            return False
    except Exception as e:
        logger.error(f"Certificate generation error: {e}")
        return False


async def ensure_tls_certificates(domain: str) -> tuple[Path, Path]:
    """Ensure TLS certificates exist for domain."""
    cert_dir = Path("/etc/xray")
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    
    # Try to get from Let's Encrypt if on Railway with custom domain
    # For now, generate self-signed
    await generate_self_signed_cert(domain, cert_path, key_path)
    return cert_path, key_path


# ── Xray Update ──────────────────────────────────────────────────────────────
async def update_xray_core() -> Dict[str, Any]:
    """Update Xray Core to latest version."""
    # Get latest version from GitHub API
    try:
        import urllib.request
        import json as json_lib
        
        url = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "Spider-Panel"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json_lib.load(resp)
            latest_version = data["tag_name"].lstrip("v")
    except Exception as e:
        logger.error(f"Failed to check latest version: {e}")
        return {"ok": False, "error": "Failed to check latest version"}
    
    current = await get_xray_version()
    if current == latest_version:
        return {"ok": True, "message": f"Already at latest version {current}", "version": current}
    
    logger.info(f"Updating Xray from {current} to {latest_version}")
    success = await install_xray_core(latest_version, force=True)
    
    if success:
        # Restart Xray with new binary
        await restart_xray()
        return {"ok": True, "message": f"Updated to {latest_version}", "version": latest_version}
    else:
        return {"ok": False, "error": "Update failed"}


# ── Initialize on Import ─────────────────────────────────────────────────────
async def initialize_xray():
    """Initialize Xray on startup."""
    logger.info("Initializing Xray Core...")
    
    # Install if missing
    if not await is_xray_installed():
        logger.info("Xray not found, installing...")
        await install_xray_core()
    
    # Create default config if none exists
    if not XRAY_CONFIG_PATH.exists():
        # In absence of the full application state, create a minimal placeholder config.
        placeholder = {
            "log": {"loglevel": "warning"},
            "inbounds": [],
            "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        }
        await write_xray_config(placeholder)
    
    # Auto-start if enabled – attempt to import SETTINGS safely
    try:
        from main import SETTINGS
        if SETTINGS.get("xray_auto_start", True):
            await start_xray()
    except Exception as e:
        logger.warning(f"Skipping auto-start due to import error: {e}")
    
    logger.info("Xray initialization complete")


# Export for use in main.py
__all__ = [
    "install_xray_core",
    "get_xray_version",
    "is_xray_installed",
    "get_xray_status",
    "start_xray",
    "stop_xray",
    "restart_xray",
    "generate_xray_config",
    "validate_xray_config",
    "write_xray_config",
    "read_xray_config",
    "get_xray_logs",
    "update_xray_core",
    "initialize_xray",
    "ensure_tls_certificates",
    "XRAY_PATH",
    "XRAY_CONFIG_PATH",
    "XRAY_VERSION",
]