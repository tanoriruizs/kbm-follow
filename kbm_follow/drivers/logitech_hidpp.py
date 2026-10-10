"""
Driver para dispositivos Logitech con Easy-Switch (MX Master, MX Anywhere, MX Keys...).

Usa la función 0x1814 (ChangeHost) del protocolo HID++ 2.0. Logitech no lo publica;
lo que se sabe es gracias a la ingeniería inversa del proyecto Solaar.

Así se ve un reporte largo HID++ (20 bytes):
  [0x11] [índice de dispositivo] [índice de función] [función << 4 | software id] [parámetros...]
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, ClassVar, Iterator, Mapping, Optional, Sequence

import hid

from ..errors import KbmError
from ..matching import matches_name
from . import FollowerDriver, SwitchResult

log = logging.getLogger(__name__)

LOGITECH_VID = 0x046D
HIDPP_USAGE_PAGE = 0xFF43
HIDPP_USAGE = 0x0202

_REPORT_ID_LONG = 0x11
_REPORT_LEN_LONG = 20
# 0xFF es "el dispositivo mismo" (Bluetooth o USB directo). Algunos firmwares
# responden en 0x00, así que probamos los dos.
_DEVICE_INDEX_CANDIDATES = (0xFF, 0x00)
_ROOT_FEATURE_INDEX = 0x00
_ERROR_FEATURE_INDEX = 0xFF
_SOFTWARE_ID = 0x0A  # cualquier valor 1-15; sirve para reconocer nuestras respuestas
_FEATURE_CHANGE_HOST = 0x1814
_FN_ROOT_GET_FEATURE = 0
_FN_CHANGE_HOST_GET_INFO = 0
_FN_CHANGE_HOST_SET_CURRENT = 1
_READ_CHUNK = 64
_DEFAULT_TIMEOUT_S = 2.0

_HIDPP_ERRORS = {
    1: "UNKNOWN", 2: "INVALID_ARGUMENT", 3: "OUT_OF_RANGE", 4: "HW_ERROR",
    5: "LOGITECH_INTERNAL", 6: "INVALID_FEATURE_INDEX", 7: "INVALID_FUNCTION_ID",
    8: "BUSY", 9: "UNSUPPORTED",
}


class HidppError(KbmError):
    def __init__(self, code: int) -> None:
        super().__init__(f"Device returned HID++ error {code} ({_HIDPP_ERRORS.get(code, '?')})")
        self.code = code


@dataclass(frozen=True)
class HostInfo:
    host_count: int
    current_host: int  # empieza en 1, como viene impreso en el mouse


class HidppDevice:
    """Conversación HID++ 2.0 (reportes largos) sobre la interfaz del dispositivo."""

    def __init__(self, path: bytes, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._path = path
        self._timeout_s = timeout_s
        self._dev: hid.device | None = None
        self.device_index = _DEVICE_INDEX_CANDIDATES[0]

    def __enter__(self) -> HidppDevice:
        dev = hid.device()
        try:
            dev.open_path(self._path)
        except OSError as exc:
            raise KbmError(f"Could not open the HID++ interface ({exc})") from exc
        self._dev = dev
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    @staticmethod
    def build_report(device_index: int, feature_index: int, function: int,
                     params: Sequence[int] = ()) -> bytes:
        if len(params) > _REPORT_LEN_LONG - 4:
            raise ValueError("Too many parameters for a long report")
        header = [_REPORT_ID_LONG, device_index, feature_index, (function << 4) | _SOFTWARE_ID]
        return bytes(header + list(params)).ljust(_REPORT_LEN_LONG, b"\x00")

    def _write(self, report: bytes) -> None:
        if self._dev is None:
            raise KbmError("HID++ device is not open")
        written = self._dev.write(report)
        if written != len(report):
            raise KbmError(f"Incomplete HID write ({written}/{len(report)} bytes)")

    def _responses(self) -> Iterator[bytes]:
        if self._dev is None:
            raise KbmError("HID++ device is not open")
        deadline = time.monotonic() + self._timeout_s
        while (remaining := deadline - time.monotonic()) > 0:
            data = self._dev.read(_READ_CHUNK, max(1, int(remaining * 1000)))
            if data:
                yield bytes(data)

    def request(self, feature_index: int, function: int, params: Sequence[int] = ()) -> bytes:
        func_sw = (function << 4) | _SOFTWARE_ID
        self._write(self.build_report(self.device_index, feature_index, function, params))
        for resp in self._responses():
            if len(resp) < 5 or resp[0] != _REPORT_ID_LONG:
                continue
            if resp[2] == _ERROR_FEATURE_INDEX and resp[3] == feature_index and resp[4] == func_sw:
                raise HidppError(resp[5] if len(resp) > 5 else 0)
            if resp[2] == feature_index and resp[3] == func_sw:
                return resp[4:]
        raise KbmError("No response from device (timeout)")

    def notify(self, feature_index: int, function: int, params: Sequence[int] = ()) -> None:
        self._write(self.build_report(self.device_index, feature_index, function, params))

    def feature_index(self, feature_id: int) -> int:
        # Cada modelo acomoda sus funciones en un índice distinto (ChangeHost está en
        # 0x0A en unos y en 0x09 en otros), así que se lo preguntamos a la función raíz.
        last_error: Exception | None = None
        for device_index in _DEVICE_INDEX_CANDIDATES:
            self.device_index = device_index
            try:
                params = self.request(_ROOT_FEATURE_INDEX, _FN_ROOT_GET_FEATURE,
                                      [feature_id >> 8, feature_id & 0xFF])
            except KbmError as exc:
                last_error = exc
                continue
            if params[0] == 0:
                raise KbmError(f"Device does not support feature 0x{feature_id:04X}")
            return params[0]
        raise KbmError(f"Could not query feature 0x{feature_id:04X}: {last_error}")


class ChangeHost:
    """Función 0x1814: leer y cambiar el canal Easy-Switch."""

    def __init__(self, device: HidppDevice) -> None:
        self._device = device
        self._index = device.feature_index(_FEATURE_CHANGE_HOST)

    def info(self) -> HostInfo:
        params = self._device.request(self._index, _FN_CHANGE_HOST_GET_INFO)
        return HostInfo(host_count=params[0], current_host=params[1] + 1)

    def switch_to(self, channel: int) -> SwitchResult:
        info = self.info()
        if not 1 <= channel <= info.host_count:
            raise KbmError(f"Channel {channel} out of range (1-{info.host_count})")
        if channel == info.current_host:
            return SwitchResult.ALREADY_THERE
        # Aquí no hay respuesta que esperar: si funcionó, el mouse ya se fue de esta compu.
        self._device.notify(self._index, _FN_CHANGE_HOST_SET_CURRENT, [channel - 1])
        return SwitchResult.SWITCHED


def find_hidpp_interfaces(name: str) -> list[dict]:
    return [
        info for info in hid.enumerate(LOGITECH_VID, 0)
        if info.get("usage_page") == HIDPP_USAGE_PAGE
        and info.get("usage") == HIDPP_USAGE
        and matches_name(info, name)
    ]


class LogitechHidppDriver(FollowerDriver):
    """Opciones: `match` (obligatoria), parte del nombre del dispositivo, p. ej. "MX Master 3S"."""

    OPTIONS: ClassVar[Mapping[str, type]] = {"match": str}
    REQUIRED_OPTIONS: ClassVar[frozenset] = frozenset({"match"})

    def __init__(self, options: Mapping[str, Any]) -> None:
        super().__init__(options)
        self._match: str = self.options["match"]

    @property
    def description(self) -> str:
        return f"Logitech '{self._match}'"

    # El dispositivo se abre en cada cambio y se cierra enseguida: va y viene entre
    # compus, así que un handle guardado quedaría inválido muy pronto.
    def _open(self) -> Optional[HidppDevice]:
        interfaces = find_hidpp_interfaces(self._match)
        return HidppDevice(interfaces[0]["path"]) if interfaces else None

    def switch_to(self, channel: int) -> SwitchResult:
        device = self._open()
        if device is None:
            return SwitchResult.NOT_HERE
        with device:
            return ChangeHost(device).switch_to(channel)

    def current_channel(self) -> Optional[int]:
        device = self._open()
        if device is None:
            return None
        with device:
            return ChangeHost(device).info().current_host
