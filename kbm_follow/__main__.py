"""Línea de comandos: `kbm-follow <comando>` (o `python -m kbm_follow <comando>`)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__
from .config import (MAX_CHANNEL, Config, FollowerConfig, LeaderConfig, default_config_path,
                     default_log_path, generate_secret)
from .errors import KbmError
from .matching import format_device_id

log = logging.getLogger("kbm_follow")

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUPS = 3


def _setup_logging(verbose: bool, log_file: Optional[Path]) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    # Con pythonw.exe (la tarea programada de Windows, sin ventana) no hay stderr.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=_LOG_MAX_BYTES,
                                           backupCount=_LOG_BACKUPS, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def _build_followers(config: Config) -> list:
    from .drivers import create_follower

    return [create_follower(f.driver, f.options, f.channel_map) for f in config.followers]


# --- Commands -------------------------------------------------------------------
# En Windows buscamos el VID:PID del líder al hacer `init`, para que nadie tenga que
# averiguarlo a mano. Ojo: por cable o dongle el mismo teclado tiene otro VID:PID
# (el mío: 0C45:800A por USB y 05AC:024F por Bluetooth), por eso pedimos Bluetooth.
def _resolve_windows_id(leader_match: str, explicit: Optional[str]) -> Optional[str]:
    from .leader import IS_WINDOWS, lookup_device_ids

    if explicit or not IS_WINDOWS:
        return explicit
    ids = sorted(lookup_device_ids(leader_match))
    if len(ids) == 1:
        detected = format_device_id(*ids[0])
        print(f"Detected leader VID:PID {detected}")
        return detected
    if not ids:
        print(f"Warning: no connected device matches '{leader_match}'. Connect the leader over "
              "Bluetooth and run init again, or pass --leader-windows-id.")
    else:
        print("Warning: several devices match the leader: "
              f"{', '.join(format_device_id(*i) for i in ids)}. Pass --leader-windows-id with the "
              "Bluetooth one (see `kbm-follow devices`).")
    return None


def _parse_follower(value: str) -> FollowerConfig:
    driver, sep, match = value.partition(":")
    if not sep or not driver.strip() or not match.strip():
        raise argparse.ArgumentTypeError('expected DRIVER:NAME, e.g. "logitech-hidpp:MX Master 3S"')
    return FollowerConfig(driver.strip(), {"match": match.strip()})


def cmd_init(args: argparse.Namespace) -> None:
    from .drivers import create_follower

    path: Path = args.config
    if path.exists() and not args.force:
        raise KbmError(f"{path} already exists. Use --force to overwrite it.")
    # Probamos crear cada seguidor antes de guardar: mejor enterarse ahora de un
    # driver mal escrito que cuando el agente ya esté corriendo en segundo plano.
    for follower in args.follower:
        create_follower(follower.driver, follower.options, follower.channel_map)
    config = Config(
        channel=args.channel,
        secret=args.secret or generate_secret(),
        leader=LeaderConfig(args.leader, _resolve_windows_id(args.leader, args.leader_windows_id)),
        followers=list(args.follower),
    )
    if args.name:
        config.name = args.name
    config.save(path)
    print(f"Configuration saved to {path} (this computer uses channel {config.channel}).")
    if not args.secret:
        print("\nOn every OTHER computer run the same command with its own --channel and:")
        print(f"  --secret {config.secret}")


def cmd_status(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    print(f"Config:    {args.config}")
    print(f"Log:       {default_log_path()}")
    print(f"Computer:  {config.name} (channel {config.channel}, UDP port {config.port})")
    print(f"Leader:    '{config.leader.match}'"
          + (f" (Windows VID:PID {config.leader.windows_id})" if config.leader.windows_id else ""))
    for follower in config.followers:
        print(f"Follower:  {follower.driver} {follower.options}"
              + (f" channel_map={follower.channel_map}" if follower.channel_map else ""))
    targets = (["broadcast"] if config.broadcast else []) + config.peers
    print(f"Targets:   {', '.join(targets)}")


def cmd_run(args: argparse.Namespace) -> None:
    from .agent import Agent

    agent = Agent(Config.load(args.config))

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("Signal %d received; stopping", signum)
        agent.stop()

    # launchd (macOS) detiene el agente con SIGTERM; así cerramos el socket con calma.
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop()


def cmd_ping(args: argparse.Namespace) -> None:
    from .agent import send_ping

    send_ping(Config.load(args.config))
    print("Ping sent. The other computer's log should show 'Ping received'.")


def cmd_devices(args: argparse.Namespace) -> None:
    import hid

    from .drivers.logitech_hidpp import HIDPP_USAGE_PAGE, LOGITECH_VID

    groups: Dict[Tuple[str, int, int], List[str]] = {}
    for info in hid.enumerate(0, 0):
        name = info.get("product_string") or ""
        if not name and not args.all:
            continue
        key = (name or "?", info["vendor_id"], info["product_id"])
        tag = f"{info.get('usage_page', 0):04X}:{info.get('usage', 0):04X}"
        if info["vendor_id"] == LOGITECH_VID and info.get("usage_page") == HIDPP_USAGE_PAGE:
            tag += "*"
        groups.setdefault(key, [])
        if tag not in groups[key]:
            groups[key].append(tag)
    print(f"{'NAME':<32} {'VID:PID':<10} USAGES (* = Logitech HID++, usable by logitech-hidpp)")
    for (name, vid, pid), usages in sorted(groups.items()):
        print(f"{name[:32]:<32} {format_device_id(vid, pid):<10} {' '.join(usages)}")


def cmd_drivers(_args: argparse.Namespace) -> None:
    from .drivers import available_drivers

    for name, target in available_drivers().items():
        print(f"{name:<20} {target}")


def cmd_followers(args: argparse.Namespace) -> None:
    for follower in _build_followers(Config.load(args.config)):
        try:
            channel = follower.driver.current_channel()
            state = f"channel {channel}" if channel else "not here (or channel unknown)"
        except (KbmError, OSError) as exc:
            state = f"error: {exc}"
        print(f"{follower.description:<40} {state}")


def cmd_switch(args: argparse.Namespace) -> None:
    from .agent import switch_followers

    followers = _build_followers(Config.load(args.config))
    if args.follower is not None:
        if not 1 <= args.follower <= len(followers):
            raise KbmError(f"--follower must be between 1 and {len(followers)}")
        followers = [followers[args.follower - 1]]
    switch_followers(followers, args.to)


# --- CLI --------------------------------------------------------------------------
def _channel(value: str) -> int:
    try:
        channel = int(value)
    except ValueError:
        channel = 0
    if not 1 <= channel <= MAX_CHANNEL:
        raise argparse.ArgumentTypeError(f"channel must be 1-{MAX_CHANNEL}")
    return channel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kbm-follow",
        description="Make multi-device peripherals follow your leader device between computers.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--config", type=Path, default=default_config_path(),
                        help=f"config file (default: {default_config_path()})")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("init", help="create this computer's configuration")
    p.add_argument("--channel", type=_channel, required=True,
                   help="channel this computer uses on the leader and followers")
    p.add_argument("--leader", required=True, metavar="NAME",
                   help='text contained in the leader device name, e.g. "F75"')
    p.add_argument("--leader-windows-id", metavar="VID:PID",
                   help="Windows only; auto-detected when the leader is connected")
    p.add_argument("--follower", type=_parse_follower, action="append", required=True,
                   metavar="DRIVER:NAME", help='repeatable, e.g. "logitech-hidpp:MX Master 3S"')
    p.add_argument("--secret", help="shared secret (omit on the first computer to generate one)")
    p.add_argument("--name", help="name shown in logs (default: host name)")
    p.add_argument("--force", action="store_true", help="overwrite an existing configuration")
    p.set_defaults(func=cmd_init)

    sub.add_parser("status", help="show configuration and paths").set_defaults(func=cmd_status)
    sub.add_parser("run", help="run the agent in the foreground").set_defaults(func=cmd_run)
    sub.add_parser("ping", help="test the network with the other computers").set_defaults(func=cmd_ping)

    p = sub.add_parser("devices", help="list connected HID devices (names and VID:PID)")
    p.add_argument("--all", action="store_true", help="include devices without a name")
    p.set_defaults(func=cmd_devices)

    sub.add_parser("drivers", help="list available follower drivers").set_defaults(func=cmd_drivers)
    sub.add_parser("followers", help="show followers and their current channel").set_defaults(
        func=cmd_followers)

    p = sub.add_parser("switch", help="switch followers to a channel (manual test)")
    p.add_argument("--to", type=_channel, required=True, help="computer channel")
    p.add_argument("--follower", type=int, metavar="N", help="only the N-th follower (1-based)")
    p.set_defaults(func=cmd_switch)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, default_log_path() if args.command == "run" else None)
    try:
        args.func(args)
    except KbmError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 - corriendo en segundo plano, que al menos quede en el registro
        log.exception("Unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
