import asyncio
import json
import logging

from app.auth import load_config

logger = logging.getLogger(__name__)


class VPNMonitor:
    def __init__(self):
        self.connected: bool = False
        self.exit_node: str | None = None
        self.exit_node_online: bool = False
        self._task: asyncio.Task | None = None
        self._broadcast = None

    def set_broadcast(self, broadcast_fn):
        self._broadcast = broadcast_fn

    def is_healthy(self) -> bool:
        return self.connected and self.exit_node_online

    def status_dict(self) -> dict:
        return {
            "connected": self.connected,
            "exit_node": self.exit_node,
            "exit_node_online": self.exit_node_online,
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
            return

        backend_state = data.get("BackendState", "")
        self.connected = backend_state == "Running"

        peers = data.get("Peer") or {}
        self.exit_node_online = False
        for peer in peers.values():
            if peer.get("ExitNode", False):
                self.exit_node_online = peer.get("Online", False)
                break

        if not self.connected:
            await self._reconnect()

    async def _reconnect(self):
        logger.warning("VPN disconnected, attempting reconnection...")
        config = load_config()
        if not config:
            logger.error("Cannot reconnect: no config file")
            return

        url = config.get("headscale_url", "")
        authkey = config.get("headscale_authkey", "")
        exit_node = config.get("exit_node", "")
        if not all([url, authkey, exit_node]):
            logger.error("Cannot reconnect: incomplete VPN config")
            return

        success = await connect(url, authkey, exit_node)
        if success:
            logger.info("VPN reconnected successfully")
        else:
            logger.error("VPN reconnection failed")

    # --- Static methods for setup wizard and startup ---

    async def connect_from_config(self):
        config = load_config()
        if not config:
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
            logger.warning("VPN connection from saved config failed")


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
    """Disconnect from Tailscale (tailscale down)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "down",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            logger.error("tailscale down failed: %s", stderr.decode().strip())
        return proc.returncode == 0
    except Exception:
        logger.exception("tailscale down error")
        return False


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
    except Exception:
        logger.exception("tailscale set exit-node error")
        return False
