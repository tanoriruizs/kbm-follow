from __future__ import annotations

import re

_DEVICE_ID_RE = re.compile(r"\s*([0-9a-fA-F]{1,4})\s*:\s*([0-9a-fA-F]{1,4})\s*")


def normalize(text: str) -> str:
    # Cada sistema nombra distinto al mismo aparato: Windows me reportaba
    # "MX_Master_3S" y la Mac "MX Master 3S". Comparamos solo letras y números.
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def matches_name(info: dict, needle: str) -> bool:
    text = f"{info.get('product_string') or ''} {info.get('manufacturer_string') or ''}"
    return normalize(needle) in normalize(text)


def parse_device_id(value: str) -> tuple[int, int]:
    match = _DEVICE_ID_RE.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid VID:PID '{value}' (example: 05AC:024F)")
    return int(match.group(1), 16), int(match.group(2), 16)


def format_device_id(vendor_id: int, product_id: int) -> str:
    return f"{vendor_id:04X}:{product_id:04X}"
