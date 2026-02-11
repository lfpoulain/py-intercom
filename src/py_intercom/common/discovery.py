from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from loguru import logger

from .constants import (
    AUDIO_UDP_PORT,
    CONTROL_PORT_OFFSET,
    DISCOVERY_BEACON_INTERVAL_S,
    DISCOVERY_EXPIRY_S,
    DISCOVERY_PORT_OFFSET,
)

_BEACON_TYPE = "py-intercom-beacon"
_BEACON_VERSION = 1


@dataclass(frozen=True)
class DiscoveredServer:
    ip: str
    server_name: str
    audio_port: int
    control_port: int
    version: int
    last_seen: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Server-side beacon sender
# ---------------------------------------------------------------------------

class DiscoveryBeacon:
    """Periodically broadcasts a JSON beacon via UDP broadcast."""

    def __init__(
        self,
        server_name: str = "py-intercom",
        audio_port: int = AUDIO_UDP_PORT,
        *,
        bind_ip: str = "",
        interval_s: float = DISCOVERY_BEACON_INTERVAL_S,
    ) -> None:
        self._server_name = server_name
        self._audio_port = audio_port
        self._control_port = audio_port + CONTROL_PORT_OFFSET
        self._discovery_port = audio_port + DISCOVERY_PORT_OFFSET
        self._bind_ip = bind_ip
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="discovery-beacon")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _build_payload(self) -> bytes:
        data = {
            "type": _BEACON_TYPE,
            "server_name": self._server_name,
            "audio_port": self._audio_port,
            "control_port": self._control_port,
            "version": _BEACON_VERSION,
        }
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _run(self) -> None:
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.5)
            payload = self._build_payload()
            logger.debug("discovery beacon started on port {}", self._discovery_port)
            while not self._stop.is_set():
                try:
                    sock.sendto(payload, ("255.255.255.255", self._discovery_port))
                except Exception as e:
                    logger.debug("beacon send error: {}", e)
                self._stop.wait(self._interval_s)
        except Exception as e:
            logger.error("discovery beacon thread error: {}", e)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            logger.debug("discovery beacon stopped")


# ---------------------------------------------------------------------------
# Client-side discovery listener
# ---------------------------------------------------------------------------

class DiscoveryListener:
    """Listens for server beacons and maintains a list of discovered servers."""

    def __init__(
        self,
        audio_port: int = AUDIO_UDP_PORT,
        *,
        expiry_s: float = DISCOVERY_EXPIRY_S,
        on_update: Optional[Callable[[Dict[str, DiscoveredServer]], None]] = None,
    ) -> None:
        self._discovery_port = audio_port + DISCOVERY_PORT_OFFSET
        self._expiry_s = expiry_s
        self._on_update = on_update
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._servers: Dict[str, DiscoveredServer] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_servers(self) -> Dict[str, DiscoveredServer]:
        with self._lock:
            self._purge_expired()
            return dict(self._servers)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="discovery-listener")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            self._servers.clear()

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._servers.items() if (now - v.last_seen) > self._expiry_s]
        for k in expired:
            del self._servers[k]

    def _run(self) -> None:
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            sock.bind(("", self._discovery_port))
            sock.settimeout(1.0)
            logger.debug("discovery listener started on port {}", self._discovery_port)
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    self._check_expiry()
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    continue
                self._handle_beacon(data, addr[0])
        except Exception as e:
            logger.error("discovery listener thread error: {}", e)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            logger.debug("discovery listener stopped")

    def _handle_beacon(self, data: bytes, sender_ip: str) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            return
        if not isinstance(msg, dict) or msg.get("type") != _BEACON_TYPE:
            return

        server_name = str(msg.get("server_name", ""))
        audio_port = int(msg.get("audio_port", AUDIO_UDP_PORT))
        control_port = int(msg.get("control_port", audio_port + CONTROL_PORT_OFFSET))
        version = int(msg.get("version", 0))

        key = f"{sender_ip}:{audio_port}"
        entry = DiscoveredServer(
            ip=sender_ip,
            server_name=server_name,
            audio_port=audio_port,
            control_port=control_port,
            version=version,
            last_seen=time.monotonic(),
        )

        changed = False
        with self._lock:
            old = self._servers.get(key)
            if old is None or old.server_name != server_name:
                changed = True
            self._servers[key] = entry
            self._purge_expired()

        if changed and self._on_update is not None:
            try:
                self._on_update(self.get_servers())
            except Exception as e:
                logger.debug("discovery on_update callback error: {}", e)

    def _check_expiry(self) -> None:
        changed = False
        with self._lock:
            before = len(self._servers)
            self._purge_expired()
            if len(self._servers) != before:
                changed = True

        if changed and self._on_update is not None:
            try:
                self._on_update(self.get_servers())
            except Exception as e:
                logger.debug("discovery on_update callback error: {}", e)
