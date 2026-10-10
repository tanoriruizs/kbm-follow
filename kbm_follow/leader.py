"""Saber si el líder (el dispositivo que cambias a mano) está conectado a esta compu."""

from __future__ import annotations

import logging
import re
import sys
from typing import Optional, Protocol, Tuple

from .errors import KbmError
from .matching import format_device_id, matches_name

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


class LeaderDetector(Protocol):
    description: str

    def present(self) -> bool: ...


class HidapiLeaderDetector:
    # En macOS y Linux enumerar con hidapi solo lee la lista del sistema,
    # no abre los dispositivos, así que se puede hacer cada medio segundo sin problema.

    def __init__(self, name: str) -> None:
        import hid

        self._hid = hid
        self._name = name
        self.description = f"hidapi, name contains '{name}'"

    def present(self) -> bool:
        return any(matches_name(info, self._name) for info in self._hid.enumerate(0, 0))


class WindowsInterfaceLeaderDetector:
    # Historia corta: la primera versión usaba hid.enumerate() también en Windows y
    # el teclado se quedaba "en el limbo" al cambiar de canal (no escribía en ninguna
    # compu). En Windows, hidapi abre cada dispositivo para leer su nombre, y hacerlo
    # en plena reconexión Bluetooth la estorbaba. Aquí solo pedimos a cfgmgr32 la
    # lista de interfaces HID presentes y buscamos el VID/PID en la ruta. Nada se abre.

    # GUID_DEVINTERFACE_HID {4D1E55B2-F16F-11CF-88CB-001111000030}
    _GUID_DEVINTERFACE_HID = (0x4D1E55B2, 0xF16F, 0x11CF,
                              (0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))
    _CR_SUCCESS = 0x00
    _CR_BUFFER_SMALL = 0x1A
    _LIST_PRESENT = 0x0
    _MAX_ATTEMPTS = 3

    def __init__(self, vendor_id: int, product_id: int) -> None:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                        ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

        d1, d2, d3, d4 = self._GUID_DEVINTERFACE_HID
        self._ctypes = ctypes
        self._guid = GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
        cfgmgr = ctypes.WinDLL("cfgmgr32")
        self._get_size = cfgmgr.CM_Get_Device_Interface_List_SizeW
        self._get_size.argtypes = [ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(GUID),
                                   wintypes.LPCWSTR, wintypes.ULONG]
        self._get_size.restype = wintypes.ULONG
        self._get_list = cfgmgr.CM_Get_Device_Interface_ListW
        self._get_list.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                   ctypes.c_wchar_p, wintypes.ULONG, wintypes.ULONG]
        self._get_list.restype = wintypes.ULONG
        self._pattern = device_path_pattern(vendor_id, product_id)
        self.description = f"Windows HID interfaces, VID:PID={format_device_id(vendor_id, product_id)}"

    def interface_paths(self) -> list[str]:
        ctypes = self._ctypes
        size = ctypes.c_ulong(0)
        # La lista puede cambiar entre "dime el tamaño" y "dame la lista" (justo cuando
        # algo se conecta). Por eso el reintento con BUFFER_SMALL.
        for _ in range(self._MAX_ATTEMPTS):
            ret = self._get_size(ctypes.byref(size), ctypes.byref(self._guid), None, self._LIST_PRESENT)
            if ret != self._CR_SUCCESS:
                raise KbmError(f"CM_Get_Device_Interface_List_Size failed (0x{ret:X})")
            buffer = ctypes.create_unicode_buffer(size.value)
            ret = self._get_list(ctypes.byref(self._guid), None, buffer, size.value, self._LIST_PRESENT)
            if ret == self._CR_SUCCESS:
                return [path for path in buffer[:size.value].split("\x00") if path]
            if ret != self._CR_BUFFER_SMALL:
                raise KbmError(f"CM_Get_Device_Interface_List failed (0x{ret:X})")
        raise KbmError("The device list changed too quickly")

    def present(self) -> bool:
        return any(self._pattern.search(path) for path in self.interface_paths())


def device_path_pattern(vendor_id: int, product_id: int) -> re.Pattern[str]:
    # Por USB la ruta trae "vid_0c45&pid_800a"; por Bluetooth LE trae "vid&0105ac_pid&024f"
    # (ese "01" de más indica de dónde viene el VID). La expresión acepta ambas.
    return re.compile(rf"vid[_&][0-9a-f]*{vendor_id:04x}[_&]pid[_&]{product_id:04x}", re.IGNORECASE)


def make_leader_detector(name: str, windows_id: Optional[Tuple[int, int]]) -> LeaderDetector:
    if IS_WINDOWS:
        if windows_id is not None:
            return WindowsInterfaceLeaderDetector(*windows_id)
        log.warning("leader.windows_id is not set: falling back to hidapi, which opens devices "
                    "on Windows and may disturb Bluetooth reconnection. Set it for best results.")
    return HidapiLeaderDetector(name)


def lookup_device_ids(name: str) -> set[Tuple[int, int]]:
    """VID:PID de los dispositivos conectados ahora mismo cuyo nombre contiene `name`."""
    import hid

    return {(info["vendor_id"], info["product_id"]) for info in hid.enumerate(0, 0)
            if matches_name(info, name)}


class Debouncer:
    """Solo da por bueno un cambio cuando se repite N lecturas seguidas."""

    # Al cambiar de canal el teclado puede "parpadear" (aparece y desaparece un par de
    # veces en un segundo). Sin este filtro saldrían avisos falsos.

    def __init__(self, required_polls: int) -> None:
        if required_polls < 1:
            raise ValueError("required_polls must be >= 1")
        self._required = required_polls
        self._candidate: Optional[bool] = None
        self._streak = 0
        self.state: Optional[bool] = None

    def feed(self, value: bool) -> Optional[Tuple[Optional[bool], bool]]:
        """Devuelve (anterior, nuevo) cuando el estado estable cambia; si no, None."""
        self._streak = self._streak + 1 if value == self._candidate else 1
        self._candidate = value
        if self._streak >= self._required and value != self.state:
            previous, self.state = self.state, value
            return previous, value
        return None
