from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

from .constants import (
    AUDIO_UDP_PORT,
    CONTROL_PORT_OFFSET,
    DISCOVERY_BEACON_INTERVAL_S,
    DISCOVERY_EXPIRY_S,
    DISCOVERY_PORT_OFFSET,
)
from .logging import best_effort

_BEACON_TYPE = "py-intercom-beacon"
_BEACON_VERSION = 1

# Identifies this process in its beacons. A server announces itself from every
# interface, so a client reached through several of them (same PC, Hyper-V or
# VPN adapter, redundant networks) receives one beacon per interface: the id
# lets it list the server once. Restarting the server inside the process keeps
# the id, so its entry does not appear twice.
_INSTANCE_ID = uuid.uuid4().hex


@dataclass(frozen=True)
class DiscoveredServer:
    ip: str
    server_name: str
    audio_port: int
    control_port: int
    version: int
    last_seen: float = field(default_factory=time.monotonic)


def local_ipv4_addresses() -> list[str]:
    """IPv4 addresses of this host's network interfaces, loopback excluded."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        logger.debug("local IPv4 address lookup failed: {}", exc)
        return []
    return sorted({str(info[4][0]) for info in infos if not str(info[4][0]).startswith("127.")})


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
        self._thread: threading.Thread | None = None

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
            "instance": _INSTANCE_ID,
            "server_name": self._server_name,
            "audio_port": self._audio_port,
            "control_port": self._control_port,
            "version": _BEACON_VERSION,
        }
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _source_addresses(self) -> list[str]:
        """Local addresses to broadcast from: the bound one, else every interface.

        Windows sends a limited broadcast from an unbound socket through the
        best-metric interface only, so a PC with a separate intercom network
        card would never announce itself there. "" stands for an unbound
        socket, used only when no interface address is known.
        """
        bind_ip = str(self._bind_ip or "").strip()
        if bind_ip and bind_ip != "0.0.0.0":
            return [bind_ip]
        return local_ipv4_addresses() or [""]

    def _sync_sockets(self, sockets: dict[str, socket.socket]) -> None:
        """Follow interfaces that appear or disappear while the server runs."""
        wanted = set(self._source_addresses())
        for address in [address for address in sockets if address not in wanted]:
            with best_effort("close beacon socket"):
                sockets.pop(address).close()
        for address in sorted(wanted - sockets.keys()):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(0.5)
                sock.bind((address, 0))
            except OSError as exc:
                sock.close()
                logger.debug("discovery beacon cannot send from {}: {}", address or "*", exc)
                continue
            sockets[address] = sock
            logger.info("discovery beacon sending from {} to port {}", address or "*", self._discovery_port)

    def _run(self) -> None:
        sockets: dict[str, socket.socket] = {}
        payload = self._build_payload()
        target = ("255.255.255.255", self._discovery_port)
        try:
            while not self._stop.is_set():
                self._sync_sockets(sockets)
                for address, sock in sockets.items():
                    try:
                        sock.sendto(payload, target)
                    except OSError as e:
                        logger.debug("beacon send from {} failed: {}", address or "*", e)
                self._stop.wait(self._interval_s)
        except Exception as e:
            logger.error("discovery beacon thread error: {}", e)
        finally:
            for sock in sockets.values():
                with best_effort("close beacon socket"):
                    sock.close()
            logger.debug("discovery beacon stopped")


# ---------------------------------------------------------------------------
# Client-side discovery listener
# ---------------------------------------------------------------------------


class DiscoveryListener:
    """Listens for server beacons and maintains a list of discovered servers.

    A server is keyed by its instance id and audio port. While an entry is
    fresh it keeps the address it was first seen from, so the listed address
    does not flip between the server's interfaces; another address takes over
    only once that one has expired. A beacon without an instance id (older
    server) is keyed by its sender address.
    """

    def __init__(
        self,
        audio_port: int = AUDIO_UDP_PORT,
        *,
        expiry_s: float = DISCOVERY_EXPIRY_S,
        on_update: Callable[[dict[str, DiscoveredServer]], None] | None = None,
    ) -> None:
        self._discovery_port = audio_port + DISCOVERY_PORT_OFFSET
        self._expiry_s = expiry_s
        self._on_update = on_update
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._servers: dict[str, DiscoveredServer] = {}
        self._invalid_beacons = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_servers(self) -> dict[str, DiscoveredServer]:
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
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):  # not on Windows
                with best_effort("set SO_REUSEPORT"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self._discovery_port))
            sock.settimeout(1.0)
            logger.debug("discovery listener started on port {}", self._discovery_port)
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except TimeoutError:
                    self._check_expiry()
                    continue
                except OSError as e:
                    if self._stop.is_set():
                        break
                    logger.debug("discovery receive error: {}", e)
                    continue
                self._handle_beacon(data, addr[0])
        except Exception as e:
            logger.error("discovery listener thread error: {}", e)
        finally:
            if sock is not None:
                with best_effort("sock.close()"):
                    sock.close()
            logger.debug("discovery listener stopped")

    @property
    def invalid_beacons(self) -> int:
        """Malformed packets received on the discovery port since creation."""
        return self._invalid_beacons

    def _reject_beacon(self, sender_ip: str, reason: object) -> None:
        # Counted rather than logged per packet, so a misbehaving sender
        # cannot flood the log: only the first one is logged.
        self._invalid_beacons += 1
        if self._invalid_beacons == 1:
            logger.debug("ignored invalid discovery beacon from {}: {}", sender_ip, reason)

    def _handle_beacon(self, data: bytes, sender_ip: str) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, RecursionError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            self._reject_beacon(sender_ip, exc)
            return
        if not isinstance(msg, dict) or msg.get("type") != _BEACON_TYPE:
            return  # another application's traffic, not an error

        try:
            server_name = str(msg.get("server_name", "")).strip() or "py-intercom"
            audio_port = int(msg.get("audio_port", AUDIO_UDP_PORT))
            control_port = int(msg.get("control_port", audio_port + CONTROL_PORT_OFFSET))
            version = int(msg.get("version", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            self._reject_beacon(sender_ip, exc)
            return

        if not (1 <= audio_port <= 65535 and 1 <= control_port <= 65535) or version <= 0:
            self._reject_beacon(sender_ip, f"audio_port={audio_port} control_port={control_port} version={version}")
            return

        instance = msg.get("instance")
        if isinstance(instance, str) and instance.strip():
            key = f"{instance.strip()}:{audio_port}"
        else:
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
            self._purge_expired()
            old = self._servers.get(key)
            if old is not None and old.ip != sender_ip:
                return  # the same server seen through another interface
            if old is None or old.server_name != server_name:
                changed = True
            self._servers[key] = entry

        if changed:
            self._notify()

    def _check_expiry(self) -> None:
        with self._lock:
            before = len(self._servers)
            self._purge_expired()
            changed = len(self._servers) != before

        if changed:
            self._notify()

    def _notify(self) -> None:
        if self._on_update is not None:
            with best_effort("discovery on_update callback"):
                self._on_update(self.get_servers())
