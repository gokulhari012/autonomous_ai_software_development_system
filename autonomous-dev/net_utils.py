"""The laptop's address on the local network (Telegram /getip and the staging "Remote URL")."""

from __future__ import annotations

import ipaddress
import socket

import psutil


def _usable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def _default_route_address() -> str | None:
    """Address of the interface used for outgoing traffic. A UDP connect sends no packet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # reserved documentation address
            address = sock.getsockname()[0]
    except OSError:
        return None
    return address if _usable(address) else None


def lan_addresses() -> list[tuple[str, str]]:
    """``(ip, adapter name)`` for every active network adapter, the most likely Wi-Fi/LAN one first."""
    found: dict[str, str] = {}
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        for item in addresses:
            if item.family == socket.AF_INET and _usable(item.address):
                found.setdefault(item.address, name)
    primary = _default_route_address()
    if primary:
        found.setdefault(primary, "default network")
    return sorted(found.items(), key=lambda kv: (kv[0] != primary, not ipaddress.ip_address(kv[0]).is_private))


def lan_ip() -> str | None:
    addresses = lan_addresses()
    return addresses[0][0] if addresses else None
