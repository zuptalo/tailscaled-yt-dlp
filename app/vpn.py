import asyncio
import json
import logging
import time

from app.auth import load_config

logger = logging.getLogger(__name__)

# Avoid log spam when tailscale is not installed (e.g. local Python dev on macOS).
_tailscale_missing_logged = False


def _log_tailscale_cli_missing_once() -> None:
    global _tailscale_missing_logged
    if _tailscale_missing_logged:
        return
    _tailscale_missing_logged = True
    logger.info(
        "tailscale CLI not found in PATH; VPN features are unavailable "
        "(expected when developing outside the container)."
    )


def _peer_matches_exit_node(peer: dict, exit_node: str) -> bool:
    """True if configured exit identifier matches this peer (IP, DNS name, or hostname)."""
    exit_node = (exit_node or "").strip()
    if not exit_node:
        return False
    ips = peer.get("TailscaleIPs") or []
    if exit_node in ips:
        return True
    dns = (peer.get("DNSName") or "").rstrip(".")
    if dns and exit_node.lower() == dns.lower():
        return True
    host = peer.get("HostName") or ""
    if host and exit_node.lower() == host.lower():
        return True
    return False


def _compute_exit_node_online(peers: dict, configured_exit: str) -> bool:
    configured_exit = (configured_exit or "").strip()
    if configured_exit:
        for peer in peers.values():
            if peer.get("ExitNode") and _peer_matches_exit_node(peer, configured_exit):
                return peer.get("Online", False)
        for peer in peers.values():
            if peer.get("ExitNodeOption") and _peer_matches_exit_node(peer, configured_exit):
                return peer.get("Online", False)
        return False
    for peer in peers.values():
        if peer.get("ExitNode"):
            return peer.get("Online", False)
    return False


class VPNMonitor:
    """Monitors tailscale state and keeps the mesh connection alive.

    Key concept: tailscale stays connected to the mesh at all times.
    "Disconnect" means clearing the exit node (traffic goes direct).
    "Connect" means setting an exit node (traffic routes through it via SOCKS).
    """

    def __init__(self):
        self.connected: bool = False
        self.exit_node: str | None = None
        self.exit_node_online: bool = False
        self.exit_node_active: bool = False
        self._task: asyncio.Task | None = None
        self._broadcast = None
        self._reconnect_backoff_until: float = 0.0
        self._reconnect_fail_streak: int = 0

    def set_broadcast(self, broadcast_fn):
        self._broadcast = broadcast_fn

    def is_healthy(self) -> bool:
        return self.connected and self.exit_node_online

    def status_dict(self) -> dict:
        return {
            "connected": self.connected,
            "exit_node": self.exit_node,
            "exit_node_online": self.exit_node_online,
            "exit_node_active": self.exit_node_active,
        }

    async def start(self):
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self):
        while True:
            try:
                prev = self.status_dict()
                await self._check()
                curr = self.status_dict()
                if curr != prev and self._broadcast:
                    self._broadcast("vpn_status", curr)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("VPN monitor check failed")
            await asyncio.sleep(30)

    async def _check(self):
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            data = json.loads(stdout)
        except Exception:
            self.connected = False
            self.exit_node_online = False
            self.exit_node_active = False
            return

        backend_state = data.get("BackendState", "")
        self.connected = backend_state == "Running"
        if self.connected:
            self._reconnect_fail_streak = 0
            self._reconnect_backoff_until = 0.0

        config = load_config() or {}
        configured_exit = (config.get("exit_node") or "").strip()
        self.exit_node = configured_exit or None

        peers = data.get("Peer") or {}

        # Check if any peer is actively being used as exit node (ExitNode=true)
        self.exit_node_active = any(p.get("ExitNode") for p in peers.values())

        self.exit_node_online = _compute_exit_node_online(peers, configured_exit)

        # Auto-reconnect the mesh if tailscale crashed (not a user action)
        if not self.connected:
            await self._reconnect_mesh()

    async def _reconnect_mesh(self):
        """Reconnect tailscale to the mesh if it crashed. Does NOT set an exit node."""
        now = time.time()
        if now < self._reconnect_backoff_until:
            return

        logger.warning("Tailscale mesh disconnected, attempting reconnection...")
        config = load_config()
        if not config:
            logger.error("Cannot reconnect: no config file")
            return
        if not config.get("use_vpn", True):
            return

        url = config.get("headscale_url", "")
        authkey = config.get("headscale_authkey", "")
        if not all([url, authkey]):
            logger.error("Cannot reconnect: incomplete VPN config")
            return

        # Reconnect to mesh but don't force an exit node — preserve user's choice
        success = await connect(url, authkey, exit_node=None)
        if success:
            logger.info("Tailscale mesh reconnected")
            self._reconnect_fail_streak = 0
            self._reconnect_backoff_until = 0.0
        else:
            logger.error("Tailscale mesh reconnection failed")
            delay = min(30 * (2**self._reconnect_fail_streak), 3600)
            self._reconnect_fail_streak += 1
            self._reconnect_backoff_until = now + delay

    async def connect_from_config(self):
        config = load_config()
        if not config:
            return
        if not config.get("use_vpn", True):
            logger.info("VPN disabled in config; skipping connect_from_config")
            return
        url = config.get("headscale_url", "")
        authkey = config.get("headscale_authkey", "")
        exit_node = config.get("exit_node", "")
        if not all([url, authkey]):
            return
        success = await connect(url, authkey, exit_node or None)
        if success:
            self.exit_node = exit_node
            logger.info("VPN connected from saved config (exit node: %s)", exit_node)
        else:
            logger.info("VPN not connected from saved config")


async def connect(headscale_url: str, authkey: str, exit_node: str | None = None) -> bool:
    cmd = [
        "tailscale", "up",
        "--login-server", headscale_url,
        "--auth-key", authkey,
        "--accept-routes",
        "--accept-dns=false",  # Don't let Tailscale manage DNS (prevents bootstrap issues)
        "--exit-node-allow-lan-access=false",
    ]
    if exit_node:
        cmd.extend(["--exit-node", exit_node])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            logger.error("tailscale up failed: %s", stderr.decode().strip())
        return proc.returncode == 0
    except FileNotFoundError:
        _log_tailscale_cli_missing_once()
        return False
    except Exception:
        logger.exception("tailscale up error")
        return False


async def list_exit_nodes() -> list[dict]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = json.loads(stdout)
    except FileNotFoundError:
        _log_tailscale_cli_missing_once()
        return []
    except Exception:
        logger.exception("Failed to query tailscale status")
        return []

    nodes = []
    for peer in (data.get("Peer") or {}).values():
        if peer.get("ExitNodeOption", False):
            ips = peer.get("TailscaleIPs", [])
            nodes.append({
                "name": peer.get("HostName", "unknown"),
                "dns_name": peer.get("DNSName", ""),
                "ip": ips[0] if ips else "",
                "online": peer.get("Online", False),
            })
    return nodes


async def disconnect() -> bool:
    """Clear the exit node so traffic goes direct. Tailscale stays connected to the mesh."""
    return await set_exit_node(None)


async def set_exit_node(exit_node: str | None) -> bool:
    """Change exit node without re-authenticating."""
    cmd = ["tailscale", "set"]
    if exit_node:
        cmd.extend(["--exit-node", exit_node])
    else:
        cmd.append("--exit-node=")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            logger.error("tailscale set exit-node failed: %s", stderr.decode().strip())
        return proc.returncode == 0
    except FileNotFoundError:
        _log_tailscale_cli_missing_once()
        return False
    except Exception:
        logger.exception("tailscale set exit-node error")
        return False


def has_active_exit_node_sync() -> bool:
    """Check if tailscale currently has an active exit node (sync, for worker threads)."""
    import subprocess as sp

    try:
        proc = sp.run(
            ["tailscale", "status", "--json"],
            capture_output=True, timeout=10, text=True,
        )
        if proc.returncode != 0:
            return False
        data = json.loads(proc.stdout)
        return any(p.get("ExitNode") for p in (data.get("Peer") or {}).values())
    except Exception:
        return False


def set_exit_node_sync(exit_node: str | None) -> bool:
    """Sync version for worker threads (same as set_exit_node)."""
    import subprocess as sp

    cmd = ["tailscale", "set"]
    if exit_node:
        cmd.extend(["--exit-node", exit_node])
    else:
        cmd.append("--exit-node=")
    try:
        proc = sp.run(cmd, capture_output=True, timeout=15, text=True)
        if proc.returncode != 0:
            logger.error("tailscale set exit-node failed: %s", (proc.stderr or "").strip())
        return proc.returncode == 0
    except FileNotFoundError:
        _log_tailscale_cli_missing_once()
        return False
    except Exception:
        logger.exception("tailscale set exit-node sync error")
        return False
