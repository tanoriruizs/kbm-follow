"""Avisos entre computadoras por UDP, firmados para que nadie más en la red los falsifique."""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import logging
import secrets
import socket
import time
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from .errors import KbmError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
KIND_LEADER_ARRIVED = "leader_arrived"
KIND_PING = "ping"
_KINDS = {KIND_LEADER_ARRIVED, KIND_PING}

MAX_CLOCK_SKEW_S = 30.0
_MAX_DATAGRAM = 2048
# UDP no garantiza la entrega, así que cada aviso se manda 3 veces con el mismo
# nonce; quien lo recibe se queda con el primero y descarta los demás.
_SEND_REPEATS = 3
_REPEAT_GAP_S = 0.05
_REPLAY_CACHE_SIZE = 1024
_BROADCAST_ADDR = "255.255.255.255"


class InvalidMessage(ValueError):
    """Datagrama descartado: firma, formato o antigüedad no válidos."""


@dataclass(frozen=True)
class Message:
    kind: str
    sender_id: str
    sender_name: str
    channel: int
    ts: float
    nonce: str
    # True cuando el líder volvió a la misma compu donde ya estaba (se durmió y despertó).
    reconnect: bool = False

    @classmethod
    def new(cls, kind: str, sender_id: str, sender_name: str, channel: int,
            reconnect: bool = False) -> Message:
        return cls(kind, sender_id, sender_name, channel, time.time(), secrets.token_hex(8),
                   reconnect)


def _sign(body: str, secret: bytes) -> str:
    return hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()


def encode(message: Message, secret: bytes) -> bytes:
    # Se firma el texto exacto del cuerpo (no el objeto) para que el receptor
    # verifique lo mismo que se envió, sin depender de cómo cada lado ordena el JSON.
    body = json.dumps({"v": PROTOCOL_VERSION, **asdict(message)}, sort_keys=True, separators=(",", ":"))
    return json.dumps({"body": body, "sig": _sign(body, secret)}).encode("utf-8")


def decode(data: bytes, secret: bytes, now: Optional[float] = None) -> Message:
    try:
        envelope = json.loads(data.decode("utf-8"))
        body, signature = envelope["body"], envelope["sig"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise InvalidMessage("malformed datagram") from exc
    if not isinstance(body, str) or not isinstance(signature, str):
        raise InvalidMessage("malformed datagram")
    # Primero la firma: nada del contenido se interpreta hasta saber que es de confianza.
    if not hmac.compare_digest(_sign(body, secret), signature):
        raise InvalidMessage("bad signature (different secret?)")

    try:
        fields = json.loads(body)
        if fields.pop("v") != PROTOCOL_VERSION:
            raise InvalidMessage("protocol version mismatch")
        message = Message(**fields)
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidMessage("invalid content") from exc

    if (message.kind not in _KINDS or not isinstance(message.channel, int)
            or not isinstance(message.reconnect, bool)):
        raise InvalidMessage("invalid kind, channel or flag")
    # Un aviso viejo reenviado por alguien más no debe mover nada.
    current = time.time() if now is None else now
    if abs(current - float(message.ts)) > MAX_CLOCK_SKEW_S:
        raise InvalidMessage("stale message or clocks out of sync")
    return message


class ReplayGuard:
    """Recuerda los nonces recientes para ignorar copias y reenvíos."""

    def __init__(self, capacity: int = _REPLAY_CACHE_SIZE) -> None:
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._capacity = capacity

    def first_time(self, nonce: str) -> bool:
        if nonce in self._seen:
            return False
        self._seen[nonce] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True


class PeerBus:
    """Manda y recibe avisos por broadcast y/o a IPs fijas."""

    def __init__(self, secret: str, port: int, peers: Iterable[str] = (), broadcast: bool = True,
                 send_port: Optional[int] = None) -> None:
        self._secret = secret.encode("utf-8")
        self._port = port
        # send_port solo existe para las pruebas, donde dos agentes viven en la misma máquina.
        self._send_port = send_port if send_port is not None else port
        self._targets = ([_BROADCAST_ADDR] if broadcast else []) + list(peers)
        if not self._targets:
            raise KbmError("No network targets: enable 'broadcast' or set 'peers'")
        self._replay = ReplayGuard()
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        # Si el puerto ya está ocupado casi siempre es otra copia del agente corriendo.
        # Lo aprovechamos como candado: así nunca hay dos agentes en la misma compu.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", self._port))
        except OSError as exc:
            sock.close()
            raise KbmError(f"Cannot use UDP port {self._port} ({exc}). "
                           "Is another kbm-follow instance running?") from exc
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def send(self, message: Message) -> None:
        data = encode(message, self._secret)
        # `kbm-follow ping` envía sin abrir el puerto (el agente ya lo tiene tomado),
        # así que en ese caso usamos un socket temporal.
        owns_socket = self._sock is None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if owns_socket else self._sock
        try:
            if owns_socket:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for attempt in range(_SEND_REPEATS):
                for host in self._targets:
                    try:
                        sock.sendto(data, (host, self._send_port))
                    except OSError as exc:
                        log.warning("Could not send to %s:%d (%s)", host, self._send_port, exc)
                if attempt < _SEND_REPEATS - 1:
                    time.sleep(_REPEAT_GAP_S)
        finally:
            if owns_socket:
                sock.close()

    def receive(self, timeout_s: float) -> Optional[Message]:
        """Espera un aviso válido y nuevo; devuelve None si se acaba el tiempo."""
        if self._sock is None:
            raise KbmError("Socket is not open")
        self._sock.settimeout(timeout_s)
        try:
            data, (address, _port) = self._sock.recvfrom(_MAX_DATAGRAM)
        except socket.timeout:
            return None
        try:
            message = decode(data, self._secret)
        except InvalidMessage as exc:
            log.debug("Dropped datagram from %s: %s", address, exc)
            return None
        if not self._replay.first_time(message.nonce):
            return None
        log.debug("Message from %s (%s): %s", message.sender_name, address, message.kind)
        return message
