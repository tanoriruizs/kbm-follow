"""
El cerebro de kbm-follow.

La idea que hizo que todo funcionara: no reaccionar cuando el teclado SE VA, sino
cuando OTRA compu confirma que LLEGÓ. En las pruebas mi teclado se dormía, se
desconectaba de Windows y el mouse se iba solito a la Mac. Esperando la llegada
confirmada eso ya no pasa.

Un cambio típico (estás en la compu 1 y presionas la tecla del canal 2):
  - la compu 2 ve llegar al líder y avisa: leader_arrived(channel=2)
  - la compu 1, que tiene a los seguidores, los manda al canal 2
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Callable, List, Optional, Sequence, Tuple

from .config import Config
from .drivers import Follower, SwitchResult, create_follower
from .errors import KbmError
from .leader import Debouncer, LeaderDetector, make_leader_detector
from .net import KIND_LEADER_ARRIVED, KIND_PING, Message, PeerBus

log = logging.getLogger(__name__)

_SWITCH_ATTEMPTS = 2
_SWITCH_RETRY_DELAY_S = 0.3
_RECEIVE_TIMEOUT_S = 0.5


class Coordinator:
    """Decide cuándo mover a los seguidores.

    No toca red ni dispositivos (todo entra por callbacks), así se puede probar
    con un reloj falso sin hardware.
    """

    def __init__(self, my_channel: int, pair_window_s: float,
                 switch: Callable[[int], None], announce: Callable[[bool], None],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._my_channel = my_channel
        self._pair_window_s = pair_window_s
        self._switch = switch
        self._announce = announce
        self._clock = clock
        self._leader_here: Optional[bool] = None
        # Aviso que llegó antes de que el líder se fuera de aquí: (canal destino, cuándo).
        self._pending: Optional[Tuple[int, float]] = None
        # Dónde se vio al líder por última vez, según nosotros o según los avisos.
        self._last_seen_channel: Optional[int] = None

    def leader_changed(self, present: bool) -> None:
        previous, self._leader_here = self._leader_here, present
        if previous is None:
            # Al arrancar solo tomamos nota. Si anunciáramos aquí, reiniciar una compu
            # podría jalar el mouse sin que nadie haya presionado nada.
            log.info("Initial leader state: %s", "here" if present else "not here")
            if present:
                self._last_seen_channel = self._my_channel
            return
        if present:
            # Si la última vez que supimos de él ya estaba aquí, no viene de otra compu:
            # se durmió y despertó. Lo avisamos marcado como reconexión.
            reconnect = self._last_seen_channel == self._my_channel
            self._last_seen_channel = self._my_channel
            self._pending = None
            log.info("Leader CONNECTED here (%s); notifying peers",
                     "reconnect" if reconnect else "arrived from another computer")
            self._announce(reconnect)
            return
        log.info("Leader DISCONNECTED from here")
        channel = self._take_pending()
        if channel is not None:
            self._switch(channel)

    def remote_arrival(self, channel: int, sender: str, reconnect: bool = False) -> None:
        if channel == self._my_channel:
            log.warning("'%s' announced channel %d, which is this computer's channel: "
                        "check 'channel' in both configs", sender, channel)
            return
        self._last_seen_channel = channel
        if reconnect:
            log.info("Leader reconnected to '%s' (woke up); followers stay put", sender)
            return
        if self._leader_here:
            # El aviso puede llegar antes de que notemos la salida (Windows tarda un poco
            # en soltar el teclado). Lo guardamos y lo usamos cuando se vaya.
            log.info("Leader arrived at '%s'; waiting for it to leave this computer", sender)
            self._pending = (channel, self._clock())
            return
        log.info("Leader arrived at '%s' (channel %d)", sender, channel)
        self._switch(channel)

    def _take_pending(self) -> Optional[int]:
        pending, self._pending = self._pending, None
        if pending is None:
            return None
        channel, since = pending
        if self._clock() - since > self._pair_window_s:
            log.info("Arrival notice too old; ignoring it")
            return None
        return channel


def switch_followers(followers: Sequence[Follower], channel: int) -> None:
    """Cambia a cada seguidor de canal. Si uno falla se reintenta una vez, y nunca
    bloquea a los demás."""
    for follower in followers:
        for attempt in range(1, _SWITCH_ATTEMPTS + 1):
            try:
                result = follower.switch_to(channel)
            except (KbmError, OSError) as exc:
                log.warning("%s: attempt %d failed: %s", follower.description, attempt, exc)
                time.sleep(_SWITCH_RETRY_DELAY_S)
                continue
            target = follower.device_channel(channel)
            if result is SwitchResult.SWITCHED:
                log.info("%s: sent to channel %d", follower.description, target)
            elif result is SwitchResult.ALREADY_THERE:
                log.info("%s: already on channel %d", follower.description, target)
            else:
                log.info("%s: not connected to this computer; skipping", follower.description)
            break
        else:
            log.error("%s: could not switch to channel %d", follower.description, channel)


class Agent:
    """Junta todo: revisa al líder, escucha la red y mueve a los seguidores.

    Hay un hilo que solo recibe avisos y los deja en una cola; todas las decisiones
    se toman en el hilo principal, así el Coordinator nunca necesita candados.
    """

    def __init__(self, config: Config, detector: Optional[LeaderDetector] = None,
                 bus: Optional[PeerBus] = None, followers: Optional[List[Follower]] = None) -> None:
        self._config = config
        self._detector = detector or make_leader_detector(config.leader.match,
                                                          config.leader.windows_id_tuple)
        self._bus = bus or PeerBus(config.secret, config.port, config.peers, config.broadcast)
        self._followers = followers if followers is not None else [
            create_follower(f.driver, f.options, f.channel_map) for f in config.followers
        ]
        self._sender_id = uuid.uuid4().hex
        self._debouncer = Debouncer(config.debounce_polls)
        self._inbox: queue.Queue[Message] = queue.Queue()
        self._stop = threading.Event()
        self.coordinator = Coordinator(config.channel, config.pair_window_s,
                                       switch=self._switch, announce=self._announce)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._bus.open()
        receiver = threading.Thread(target=self._receive_loop, name="kbm-receiver", daemon=True)
        receiver.start()
        log.info("kbm-follow running: '%s', channel %d, leader via %s, followers: %s",
                 self._config.name, self._config.channel, self._detector.description,
                 ", ".join(f.description for f in self._followers))
        try:
            while not self._stop.is_set():
                self._poll_leader()
                self._drain_inbox(self._config.poll_interval_s)
        finally:
            self._stop.set()
            receiver.join(timeout=2 * _RECEIVE_TIMEOUT_S)
            self._bus.close()
            log.info("kbm-follow stopped")

    def _poll_leader(self) -> None:
        try:
            present = self._detector.present()
        except (KbmError, OSError) as exc:
            log.warning("Could not check the leader: %s", exc)
            return
        change = self._debouncer.feed(present)
        if change is not None:
            self.coordinator.leader_changed(change[1])

    def _drain_inbox(self, budget_s: float) -> None:
        # En vez de dormir entre revisiones, esperamos mensajes: así un aviso se
        # atiende al instante y no hasta la siguiente vuelta.
        deadline = time.monotonic() + budget_s
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                message = self._inbox.get(timeout=remaining)
            except queue.Empty:
                return
            self._handle(message)

    def _handle(self, message: Message) -> None:
        if message.kind == KIND_PING:
            log.info("Ping received from '%s' (channel %d): the network works",
                     message.sender_name, message.channel)
        elif message.kind == KIND_LEADER_ARRIVED:
            self.coordinator.remote_arrival(message.channel, message.sender_name, message.reconnect)

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._bus.receive(_RECEIVE_TIMEOUT_S)
            except OSError as exc:  # p. ej. la red todavía no vuelve tras suspender la compu
                log.warning("Network error while receiving: %s", exc)
                self._stop.wait(1.0)
                continue
            except KbmError:
                return  # cerraron el socket: estamos apagando
            # El broadcast también nos llega a nosotros mismos; esos se ignoran.
            if message is not None and message.sender_id != self._sender_id:
                self._inbox.put(message)

    def _announce(self, reconnect: bool) -> None:
        self._bus.send(Message.new(KIND_LEADER_ARRIVED, self._sender_id, self._config.name,
                                   self._config.channel, reconnect=reconnect))

    def _switch(self, channel: int) -> None:
        switch_followers(self._followers, channel)


def send_ping(config: Config) -> None:
    bus = PeerBus(config.secret, config.port, config.peers, config.broadcast)
    bus.send(Message.new(KIND_PING, uuid.uuid4().hex, config.name, config.channel))
