"""
Drivers de seguidores: dispositivos a los que se les puede ordenar cambiar de canal.

Cada marca habla su propio idioma, así que cada una necesita su driver. El de
Logitech viene incluido; los demás pueden vivir en paquetes aparte y registrarse
con un entry point, sin tocar este repositorio:

    # pyproject.toml del paquete del driver
    [project.entry-points."kbm_follow.drivers"]
    acme-mouse = "kbm_follow_acme:AcmeMouseDriver"
"""

from __future__ import annotations

import abc
import enum
import importlib
import logging
import sys
from typing import Any, ClassVar, Dict, Mapping, Optional, Type

from ..errors import KbmError

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "kbm_follow.drivers"

_BUILTIN_DRIVERS: Dict[str, str] = {
    "logitech-hidpp": "kbm_follow.drivers.logitech_hidpp:LogitechHidppDriver",
}


class SwitchResult(enum.Enum):
    SWITCHED = "switched"            # se mandó la orden; el dispositivo se va de esta compu
    ALREADY_THERE = "already_there"
    NOT_HERE = "not_here"            # no está conectado aquí (lo normal en las otras compus)


class FollowerDriver(abc.ABC):
    """Base para los drivers.

    Cada driver declara qué opciones acepta en OPTIONS ({nombre: tipo}) y cuáles
    son obligatorias en REQUIRED_OPTIONS. La validación la hace esta clase, para
    que un error en el config.json se vea al arrancar y no a mitad de un cambio.
    """

    OPTIONS: ClassVar[Mapping[str, type]] = {}
    REQUIRED_OPTIONS: ClassVar[frozenset] = frozenset()

    def __init__(self, options: Mapping[str, Any]) -> None:
        unknown = sorted(set(options) - set(self.OPTIONS))
        if unknown:
            raise KbmError(f"{type(self).__name__}: unknown options: {', '.join(unknown)}")
        missing = sorted(self.REQUIRED_OPTIONS - set(options))
        if missing:
            raise KbmError(f"{type(self).__name__}: missing options: {', '.join(missing)}")
        for key, value in options.items():
            expected = self.OPTIONS[key]
            # bool hereda de int en Python; sin esto `"canal": true` pasaría como número.
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise KbmError(f"{type(self).__name__}: option '{key}' must be {expected.__name__}")
        self.options: Dict[str, Any] = dict(options)

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Nombre corto para los registros, p. ej. "Logitech 'MX Master 3S'"."""

    @abc.abstractmethod
    def switch_to(self, channel: int) -> SwitchResult:
        """Pide al dispositivo cambiarse a `channel` (empieza en 1).

        Si el dispositivo no está en esta compu, devuelve NOT_HERE en vez de fallar:
        en un cambio normal eso le pasa a todas las compus menos a una. Para errores
        de verdad lanza KbmError u OSError; el agente reintenta una vez.
        """

    def current_channel(self) -> Optional[int]:
        """Canal actual, si el dispositivo está aquí y el driver sabe leerlo."""
        return None


class Follower:
    """Un driver ya configurado, más su channel_map (canal de la compu -> canal del dispositivo)."""

    def __init__(self, driver: FollowerDriver, channel_map: Optional[Mapping[int, int]] = None) -> None:
        self.driver = driver
        self._channel_map = dict(channel_map or {})

    @property
    def description(self) -> str:
        return self.driver.description

    def device_channel(self, computer_channel: int) -> int:
        return self._channel_map.get(computer_channel, computer_channel)

    def switch_to(self, computer_channel: int) -> SwitchResult:
        return self.driver.switch_to(self.device_channel(computer_channel))


def _entry_point_drivers() -> Dict[str, str]:
    from importlib.metadata import entry_points

    found = entry_points()
    if sys.version_info >= (3, 10):
        group = found.select(group=ENTRY_POINT_GROUP)
    else:  # pragma: no cover - en 3.9 entry_points() devuelve un dict de listas
        group = found.get(ENTRY_POINT_GROUP, [])
    return {ep.name: ep.value for ep in group}


def available_drivers() -> Dict[str, str]:
    """{nombre: "modulo:Clase"}. Si un plugin usa el nombre de uno incluido, gana el incluido."""
    drivers = _entry_point_drivers()
    drivers.update(_BUILTIN_DRIVERS)
    return dict(sorted(drivers.items()))


def load_driver_class(name: str) -> Type[FollowerDriver]:
    target = available_drivers().get(name)
    if target is None:
        raise KbmError(f"Unknown driver '{name}'. Available: {', '.join(available_drivers())}")
    module_name, _, attr = target.partition(":")
    try:
        driver_class = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise KbmError(f"Could not load driver '{name}' ({target}): {exc}") from exc
    if not (isinstance(driver_class, type) and issubclass(driver_class, FollowerDriver)):
        raise KbmError(f"Driver '{name}' ({target}) is not a FollowerDriver subclass")
    return driver_class


def create_follower(driver_name: str, options: Mapping[str, Any],
                    channel_map: Optional[Mapping[int, int]] = None) -> Follower:
    return Follower(load_driver_class(driver_name)(options), channel_map)
