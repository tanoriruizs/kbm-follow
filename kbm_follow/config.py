"""Configuración (un JSON por compu) y las rutas donde vive en cada sistema."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import KbmError
from .matching import parse_device_id

APP_NAME = "kbm-follow"
CONFIG_VERSION = 1
MIN_SECRET_LENGTH = 16
# Casi todos los dispositivos multi-host tienen 3 canales; dejamos margen por si acaso.
MAX_CHANNEL = 6


def _app_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def default_config_path() -> Path:
    return _app_dir() / "config.json"


def default_log_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME
    return base / f"{APP_NAME}.log"


def generate_secret() -> str:
    return secrets.token_urlsafe(24)


def _valid_channel(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_CHANNEL


def _check_keys(section: str, raw: Mapping[str, Any], allowed: set, required: set) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise KbmError(f"Unknown keys in '{section}': {', '.join(unknown)}")
    missing = sorted(required - set(raw))
    if missing:
        raise KbmError(f"Missing keys in '{section}': {', '.join(missing)}")


@dataclass
class LeaderConfig:
    # Parte del nombre del líder, p. ej. "F75". Se usa en macOS/Linux (y en Windows
    # solo si falta windows_id).
    match: str
    # Solo Windows: VID:PID del líder por Bluetooth. Con esto se detecta sin abrir
    # ningún dispositivo (ver leader.py). `kbm-follow devices` te lo muestra.
    windows_id: Optional[str] = None

    @property
    def windows_id_tuple(self) -> Optional[Tuple[int, int]]:
        return parse_device_id(self.windows_id) if self.windows_id else None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"match": self.match}
        if self.windows_id:
            data["windows_id"] = self.windows_id
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> LeaderConfig:
        if not isinstance(raw, dict):
            raise KbmError("'leader' must be an object")
        _check_keys("leader", raw, {"match", "windows_id"}, {"match"})
        leader = cls(**raw)
        if not isinstance(leader.match, str) or not leader.match.strip():
            raise KbmError("'leader.match' must be a non-empty string")
        if leader.windows_id is not None:
            try:
                parse_device_id(leader.windows_id)
            except (ValueError, TypeError) as exc:
                raise KbmError(f"'leader.windows_id': {exc}") from exc
        return leader


@dataclass
class FollowerConfig:
    driver: str
    # Las opciones de cada driver van "sueltas" en el JSON, junto a "driver"; aquí
    # las juntamos para pasárselas tal cual.
    options: Dict[str, Any] = field(default_factory=dict)
    # Por si un dispositivo numera sus canales distinto: {canal de la compu: canal del dispositivo}.
    channel_map: Dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"driver": self.driver, **self.options}
        if self.channel_map:
            data["channel_map"] = {str(k): v for k, v in sorted(self.channel_map.items())}
        return data

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> FollowerConfig:
        section = f"followers[{index}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("driver"), str):
            raise KbmError(f"'{section}' must be an object with a 'driver' string")
        options = {k: v for k, v in raw.items() if k not in ("driver", "channel_map")}
        raw_map = raw.get("channel_map", {})
        if not isinstance(raw_map, dict):
            raise KbmError(f"'{section}.channel_map' must be an object")
        channel_map: Dict[int, int] = {}
        for key, value in raw_map.items():
            try:
                source = int(key)
            except ValueError:
                source = 0
            if not (_valid_channel(source) and _valid_channel(value)):
                raise KbmError(f"'{section}.channel_map' entries must map 1-{MAX_CHANNEL} to 1-{MAX_CHANNEL}")
            channel_map[source] = value
        return cls(raw["driver"], options, channel_map)


@dataclass
class Config:
    # El canal que ocupa esta compu en el teclado y en el mouse (en mi caso Windows = 1, Mac = 2).
    channel: int
    # Tiene que ser idéntico en todas las compus; con él se firman los avisos.
    secret: str
    leader: LeaderConfig
    followers: List[FollowerConfig]
    name: str = field(default_factory=socket.gethostname)
    port: int = 47809
    broadcast: bool = True
    peers: List[str] = field(default_factory=list)
    poll_interval_s: float = 0.5
    debounce_polls: int = 2
    # Cuánto esperamos a que el líder se vaya de aquí después de que otra compu
    # avisó que llegó allá. Windows a veces tarda un par de segundos en soltarlo.
    pair_window_s: float = 8.0
    version: int = CONFIG_VERSION

    _SCALARS = ("channel", "secret", "name", "port", "broadcast", "peers", "poll_interval_s",
                "debounce_polls", "pair_window_s", "version")

    def validate(self) -> None:
        errors = []
        if self.version != CONFIG_VERSION:
            errors.append(f"unsupported config version {self.version} (expected {CONFIG_VERSION})")
        if not _valid_channel(self.channel):
            errors.append(f"channel must be an integer 1-{MAX_CHANNEL}")
        if not isinstance(self.secret, str) or len(self.secret) < MIN_SECRET_LENGTH:
            errors.append(f"secret must be at least {MIN_SECRET_LENGTH} characters")
        if not isinstance(self.name, str) or not self.name:
            errors.append("name must be a non-empty string")
        if not (isinstance(self.port, int) and 1024 <= self.port <= 65535):
            errors.append("port must be between 1024 and 65535")
        if not isinstance(self.peers, list) or not all(isinstance(p, str) and p for p in self.peers):
            errors.append("peers must be a list of IP addresses or host names")
        elif not self.broadcast and not self.peers:
            errors.append("enable broadcast or set at least one peer")
        if not isinstance(self.poll_interval_s, (int, float)) or self.poll_interval_s <= 0:
            errors.append("poll_interval_s must be positive")
        if not isinstance(self.pair_window_s, (int, float)) or self.pair_window_s <= 0:
            errors.append("pair_window_s must be positive")
        if not (isinstance(self.debounce_polls, int) and self.debounce_polls >= 1):
            errors.append("debounce_polls must be an integer >= 1")
        if not self.followers:
            errors.append("add at least one follower")
        if errors:
            raise KbmError("Invalid configuration: " + "; ".join(errors))

    def to_dict(self) -> Dict[str, Any]:
        data = {key: getattr(self, key) for key in self._SCALARS}
        data["leader"] = self.leader.to_dict()
        data["followers"] = [follower.to_dict() for follower in self.followers]
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> Config:
        if not isinstance(raw, dict):
            raise KbmError("The configuration must be a JSON object")
        _check_keys("config", raw, set(cls._SCALARS) | {"leader", "followers"},
                    {"channel", "secret", "leader", "followers"})
        followers_raw = raw["followers"]
        if not isinstance(followers_raw, list):
            raise KbmError("'followers' must be a list")
        scalars = {key: raw[key] for key in cls._SCALARS if key in raw}
        config = cls(leader=LeaderConfig.from_dict(raw["leader"]),
                     followers=[FollowerConfig.from_dict(f, i) for i, f in enumerate(followers_raw)],
                     **scalars)
        config.validate()
        return config

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KbmError(f"No configuration at {path}. Create it with: kbm-follow init --help") from exc
        except (OSError, ValueError) as exc:
            raise KbmError(f"Could not read {path}: {exc}") from exc
        try:
            return cls.from_dict(raw)
        except KbmError as exc:
            raise KbmError(f"{path}: {exc}") from exc

    def save(self, path: Path) -> None:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Escribimos a un temporal y luego lo renombramos: si algo falla a medias,
        # la configuración anterior queda intacta.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)  # el archivo trae el secreto: que solo lo lea tu usuario
        os.replace(tmp, path)
