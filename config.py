"""Configuration loader for ipsc2hbp.

Supports the original IPSC MASTER mode and the new IPSC PEER mode.  The
field names consumed by hbp.protocol and translate.translator are preserved.
"""

from __future__ import annotations

import logging
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_HBP_MODES = {"TRACKING", "PERSISTENT"}
_VALID_IPSC_MODES = {"MASTER", "PEER"}
_VALID_PACKET_FLOWS = {"PACKET_TRANSLATOR", "LEGACY_AMBE"}


@dataclass(frozen=True)
class Config:
    # [global]
    log_level: str

    # [ipsc] common
    ipsc_mode: str
    ipsc_bind_ip: str
    ipsc_bind_port: int
    ipsc_master_id: int
    ipsc_peer_id: int
    ipsc_source_id: int
    allowed_peer_ip: str
    auth_enabled: bool
    auth_key: bytes
    keepalive_watchdog: int

    # [ipsc] / [ipsc_upstream] peer mode
    ipsc_upstream_master_ip: str
    ipsc_upstream_master_port: int
    ipsc_upstream_master_id: int
    ipsc_peer_alive_interval: int
    ipsc_max_missed: int
    ipsc_peer_list_allow_unknown: bool
    ipsc_peer_list_prune: bool
    ipsc_reflect_suppression: bool
    ipsc_reflect_window: int
    ipsc_busy_slot_guard: bool
    ipsc_busy_holdoff_ms: int
    ipsc_call_lock: bool
    ipsc_call_lock_hang_ms: int
    ipsc_call_lock_same_tg_only: bool

    # [compat]
    compat_packet_flow: str

    # [hbp]
    hbp_master_ip: str
    hbp_master_port: int
    hbp_repeater_id: int
    hbp_passphrase: bytes
    hbp_mode: str
    hbp_to_ipsc_pacing: bool
    hbp_to_ipsc_frame_interval_ms: int
    hbp_to_ipsc_queue_limit: int
    hbp_start_on_voice: bool
    hbp_to_ipsc_header_repeats: int

    # RPTC / RPTO fields used by hbp.protocol
    options: str
    callsign: str
    rx_freq: str
    tx_freq: str
    tx_power: str
    colorcode: str
    latitude: str
    longitude: str
    height: str
    location: str
    description: str
    url: str
    software_id: str
    package_id: str


def load(path: str | Path) -> Config:
    """Load and validate a TOML config file."""
    try:
        with Path(path).open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise FileNotFoundError(f"Config file not found: {path}")
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Config file parse error: {exc}")

    errors: list[str] = []

    def required_section(name: str) -> dict[str, Any]:
        value = raw.get(name)
        if value is None:
            errors.append(f"[{name}] section: required")
            return {}
        if not isinstance(value, dict):
            errors.append(f"[{name}] section: must be a table")
            return {}
        return value

    def optional_section(name: str) -> dict[str, Any]:
        value = raw.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            errors.append(f"[{name}] section: must be a table")
            return {}
        return value

    global_s = required_section("global")
    ipsc_s = required_section("ipsc")
    ipsc_upstream_s = optional_section("ipsc_upstream")
    compat_s = optional_section("compat")
    hbp_s = required_section("hbp")

    def get_str(
        sec: dict[str, Any],
        section_name: str,
        key: str,
        required: bool = True,
        default: str = "",
        choices: set[str] | None = None,
        upper: bool = False,
    ) -> str:
        val = sec.get(key)
        if val is None:
            if required:
                errors.append(f"[{section_name}] {key}: required")
            return default.upper() if upper else default
        if not isinstance(val, str):
            errors.append(f"[{section_name}] {key}: must be a string, got {type(val).__name__}")
            return default.upper() if upper else default
        result = val.strip()
        if upper or choices is not None:
            result = result.upper()
        if choices is not None and result not in choices:
            errors.append(f"[{section_name}] {key}: must be one of {sorted(choices)}, got {val!r}")
            return default.upper() if upper else default
        return result

    def get_int(
        sec: dict[str, Any],
        section_name: str,
        key: str,
        required: bool = True,
        default: int = 0,
        min_val: int | None = None,
        max_val: int | None = None,
    ) -> int:
        val = sec.get(key)
        if val is None:
            if required:
                errors.append(f"[{section_name}] {key}: required")
            value = default
        elif not isinstance(val, int) or isinstance(val, bool):
            errors.append(f"[{section_name}] {key}: must be an integer, got {type(val).__name__}")
            value = default
        else:
            value = val
        if min_val is not None and value < min_val:
            errors.append(f"[{section_name}] {key}: must be >= {min_val}, got {value}")
        if max_val is not None and value > max_val:
            errors.append(f"[{section_name}] {key}: must be <= {max_val}, got {value}")
        return value

    def get_bool(
        sec: dict[str, Any],
        section_name: str,
        key: str,
        required: bool = True,
        default: bool = False,
    ) -> bool:
        val = sec.get(key)
        if val is None:
            if required:
                errors.append(f"[{section_name}] {key}: required")
            return default
        if not isinstance(val, bool):
            errors.append(f"[{section_name}] {key}: must be true or false, got {type(val).__name__}")
            return default
        return val

    def first_str(
        primary: tuple[dict[str, Any], str, str],
        fallback: tuple[dict[str, Any], str, str] | None,
        *,
        required: bool,
        default: str = "",
    ) -> str:
        sec, sec_name, key = primary
        if key in sec:
            return get_str(sec, sec_name, key, required=required, default=default)
        if fallback is not None:
            fsec, fsec_name, fkey = fallback
            if fkey in fsec:
                return get_str(fsec, fsec_name, fkey, required=required, default=default)
        if required:
            errors.append(f"[{sec_name}] {key}: required")
        return default

    def first_int(
        primary: tuple[dict[str, Any], str, str],
        fallback: tuple[dict[str, Any], str, str] | None,
        *,
        required: bool,
        default: int = 0,
        min_val: int | None = None,
        max_val: int | None = None,
    ) -> int:
        sec, sec_name, key = primary
        if key in sec:
            return get_int(sec, sec_name, key, required=required, default=default, min_val=min_val, max_val=max_val)
        if fallback is not None:
            fsec, fsec_name, fkey = fallback
            if fkey in fsec:
                return get_int(fsec, fsec_name, fkey, required=required, default=default, min_val=min_val, max_val=max_val)
        if required:
            errors.append(f"[{sec_name}] {key}: required")
        return default

    # [global]
    log_level = get_str(global_s, "global", "log_level", required=False, default="INFO", choices=_VALID_LOG_LEVELS)

    # [ipsc]
    ipsc_mode = get_str(ipsc_s, "ipsc", "mode", required=False, default="MASTER", choices=_VALID_IPSC_MODES)
    ipsc_bind_ip = get_str(ipsc_s, "ipsc", "bind_ip")
    ipsc_bind_port = get_int(ipsc_s, "ipsc", "bind_port", min_val=1, max_val=65535)

    ipsc_master_id = get_int(
        ipsc_s,
        "ipsc",
        "ipsc_master_id",
        required=(ipsc_mode == "MASTER"),
        default=0,
        min_val=0,
        max_val=0xFFFFFFFF,
    )
    ipsc_peer_id = get_int(
        ipsc_s,
        "ipsc",
        "ipsc_peer_id",
        required=(ipsc_mode == "PEER"),
        default=0,
        min_val=0,
        max_val=0xFFFFFFFF,
    )

    if ipsc_mode == "MASTER" and ipsc_master_id == 0:
        errors.append("[ipsc] ipsc_master_id: must be non-zero in MASTER mode")
    if ipsc_mode == "PEER" and ipsc_peer_id == 0:
        errors.append("[ipsc] ipsc_peer_id: must be non-zero in PEER mode")

    allowed_peer_ip = get_str(ipsc_s, "ipsc", "allowed_peer_ip", required=False, default="")
    if ipsc_mode == "MASTER" and allowed_peer_ip:
        try:
            socket.inet_aton(allowed_peer_ip)
        except OSError:
            errors.append(f"[ipsc] allowed_peer_ip: not a valid IPv4 address: {allowed_peer_ip!r}")

    auth_enabled = get_bool(ipsc_s, "ipsc", "auth_enabled", required=False, default=False)
    auth_key = b"\x00" * 20
    if auth_enabled:
        raw_key = get_str(ipsc_s, "ipsc", "auth_key", required=True).strip()
        if len(raw_key) > 40:
            errors.append("[ipsc] auth_key: must be at most 40 hex characters")
        else:
            try:
                auth_key = bytes.fromhex(raw_key.zfill(40))
            except ValueError as exc:
                errors.append(f"[ipsc] auth_key: not valid hex: {exc}")
    else:
        get_str(ipsc_s, "ipsc", "auth_key", required=False, default="")

    keepalive_watchdog = get_int(ipsc_s, "ipsc", "keepalive_watchdog", required=False, default=60, min_val=5)

    upstream_ip = first_str(
        (ipsc_upstream_s, "ipsc_upstream", "master_ip"),
        (ipsc_s, "ipsc", "upstream_master_ip"),
        required=(ipsc_mode == "PEER"),
        default="",
    )
    upstream_port = first_int(
        (ipsc_upstream_s, "ipsc_upstream", "master_port"),
        (ipsc_s, "ipsc", "upstream_master_port"),
        required=(ipsc_mode == "PEER"),
        default=0,
        min_val=0,
        max_val=65535,
    )
    upstream_id = first_int(
        (ipsc_upstream_s, "ipsc_upstream", "master_id"),
        (ipsc_s, "ipsc", "upstream_master_id"),
        required=False,
        default=0,
        min_val=0,
        max_val=0xFFFFFFFF,
    )

    if ipsc_mode == "PEER":
        try:
            socket.inet_aton(upstream_ip)
        except OSError:
            errors.append(f"[ipsc_upstream] master_ip: not a valid IPv4 address: {upstream_ip!r}")
        if upstream_port == 0:
            errors.append("[ipsc_upstream] master_port: must be non-zero in PEER mode")

    peer_alive_interval = first_int(
        (ipsc_s, "ipsc", "keepalive_interval"),
        (ipsc_s, "ipsc", "peer_alive_interval"),
        required=False,
        default=5,
        min_val=1,
        max_val=300,
    )
    ipsc_max_missed = get_int(ipsc_s, "ipsc", "max_missed", required=False, default=3, min_val=1, max_val=1000)
    peer_list_allow_unknown = get_bool(
        ipsc_s,
        "ipsc",
        "peer_list_allow_unknown",
        required=False,
        default=(ipsc_mode == "PEER"),
    )
    peer_list_prune = get_bool(ipsc_s, "ipsc", "peer_list_prune", required=False, default=True)
    reflect_suppression = get_bool(
        ipsc_s,
        "ipsc",
        "reflect_suppression",
        required=False,
        default=False,
    )
    reflect_window = get_int(
        ipsc_s,
        "ipsc",
        "reflect_window",
        required=False,
        default=8,
        min_val=0,
        max_val=60,
    )
    ipsc_busy_slot_guard = get_bool(
        ipsc_s,
        "ipsc",
        "busy_slot_guard",
        required=False,
        default=True,
    )
    ipsc_busy_holdoff_ms = get_int(
        ipsc_s,
        "ipsc",
        "busy_holdoff_ms",
        required=False,
        default=100,
        min_val=0,
        max_val=5000,
    )
    ipsc_call_lock = get_bool(
        ipsc_s,
        "ipsc",
        "call_lock",
        required=False,
        default=True,
    )
    ipsc_call_lock_hang_ms = get_int(
        ipsc_s,
        "ipsc",
        "call_lock_hang_ms",
        required=False,
        default=250,
        min_val=0,
        max_val=5000,
    )
    ipsc_call_lock_same_tg_only = get_bool(
        ipsc_s,
        "ipsc",
        "call_lock_same_tg_only",
        required=False,
        default=True,
    )

    # [compat]
    compat_packet_flow = get_str(
        compat_s,
        "compat",
        "packet_flow",
        required=False,
        default="PACKET_TRANSLATOR",
        choices=_VALID_PACKET_FLOWS,
    )

    # [hbp]
    hbp_master_ip = get_str(hbp_s, "hbp", "master_ip")
    hbp_master_port = get_int(hbp_s, "hbp", "master_port", min_val=1, max_val=65535)
    hbp_mode = get_str(hbp_s, "hbp", "hbp_mode", required=False, default="TRACKING", choices=_VALID_HBP_MODES)
    hbp_repeater_id = get_int(hbp_s, "hbp", "hbp_repeater_id", required=False, default=0, min_val=0, max_val=0xFFFFFFFF)
    hbp_to_ipsc_pacing = get_bool(
        hbp_s,
        "hbp",
        "hbp_to_ipsc_pacing",
        required=False,
        default=True,
    )
    hbp_to_ipsc_frame_interval_ms = get_int(
        hbp_s,
        "hbp",
        "hbp_to_ipsc_frame_interval_ms",
        required=False,
        default=60,
        min_val=10,
        max_val=250,
    )
    hbp_to_ipsc_queue_limit = get_int(
        hbp_s,
        "hbp",
        "hbp_to_ipsc_queue_limit",
        required=False,
        default=512,
        min_val=10,
        max_val=5000,
    )
    hbp_start_on_voice = get_bool(
        hbp_s,
        "hbp",
        "hbp_start_on_voice",
        required=False,
        default=True,
    )
    hbp_to_ipsc_header_repeats = get_int(
        hbp_s,
        "hbp",
        "hbp_to_ipsc_header_repeats",
        required=False,
        default=3,
        min_val=1,
        max_val=5,
    )
    raw_passphrase = get_str(hbp_s, "hbp", "passphrase")
    hbp_passphrase = raw_passphrase.encode()

    options = get_str(hbp_s, "hbp", "options", required=False, default="")
    callsign = get_str(hbp_s, "hbp", "callsign", required=False, default="NOCALL")
    rx_freq = get_str(hbp_s, "hbp", "rx_freq", required=False, default="000000000")
    tx_freq = get_str(hbp_s, "hbp", "tx_freq", required=False, default="000000000")
    tx_power = get_str(hbp_s, "hbp", "tx_power", required=False, default="00")
    colorcode = get_str(hbp_s, "hbp", "colorcode", required=False, default="01")
    latitude = get_str(hbp_s, "hbp", "latitude", required=False, default="00.0000 ")
    longitude = get_str(hbp_s, "hbp", "longitude", required=False, default="000.0000 ")
    height = get_str(hbp_s, "hbp", "height", required=False, default="000")
    location = get_str(hbp_s, "hbp", "location", required=False, default="")
    description = get_str(hbp_s, "hbp", "description", required=False, default="")
    url = get_str(hbp_s, "hbp", "url", required=False, default="")
    software_id = get_str(hbp_s, "hbp", "software_id", required=False, default="ipsc2hbp")
    package_id = get_str(hbp_s, "hbp", "package_id", required=False, default="1.0.0")

    resolved_repeater_id = hbp_repeater_id if hbp_repeater_id else ipsc_peer_id
    if not resolved_repeater_id:
        errors.append(
            "At least one of [ipsc] ipsc_peer_id or [hbp] hbp_repeater_id must be set; "
            "HBP requires a radio ID to connect with"
        )

    # Source ID for IPSC user traffic generated by this process. This preserves
    # old MASTER behavior and makes PEER mode transmit using the local peer ID.
    ipsc_source_id = ipsc_master_id if ipsc_mode == "MASTER" else ipsc_peer_id

    if errors:
        raise ValueError("Configuration errors:\n" + "\n".join(f"  {e}" for e in errors))

    return Config(
        log_level=log_level,
        ipsc_mode=ipsc_mode,
        ipsc_bind_ip=ipsc_bind_ip,
        ipsc_bind_port=ipsc_bind_port,
        ipsc_master_id=ipsc_master_id,
        ipsc_peer_id=ipsc_peer_id,
        ipsc_source_id=ipsc_source_id,
        allowed_peer_ip=allowed_peer_ip,
        auth_enabled=auth_enabled,
        auth_key=auth_key,
        keepalive_watchdog=keepalive_watchdog,
        ipsc_upstream_master_ip=upstream_ip,
        ipsc_upstream_master_port=upstream_port,
        ipsc_upstream_master_id=upstream_id,
        ipsc_peer_alive_interval=peer_alive_interval,
        ipsc_max_missed=ipsc_max_missed,
        ipsc_peer_list_allow_unknown=peer_list_allow_unknown,
        ipsc_peer_list_prune=peer_list_prune,
        ipsc_reflect_suppression=reflect_suppression,
        ipsc_reflect_window=reflect_window,
        ipsc_busy_slot_guard=ipsc_busy_slot_guard,
        ipsc_busy_holdoff_ms=ipsc_busy_holdoff_ms,
        ipsc_call_lock=ipsc_call_lock,
        ipsc_call_lock_hang_ms=ipsc_call_lock_hang_ms,
        ipsc_call_lock_same_tg_only=ipsc_call_lock_same_tg_only,
        compat_packet_flow=compat_packet_flow,
        hbp_master_ip=hbp_master_ip,
        hbp_master_port=hbp_master_port,
        hbp_repeater_id=resolved_repeater_id,
        hbp_passphrase=hbp_passphrase,
        hbp_mode=hbp_mode,
        hbp_to_ipsc_pacing=hbp_to_ipsc_pacing,
        hbp_to_ipsc_frame_interval_ms=hbp_to_ipsc_frame_interval_ms,
        hbp_to_ipsc_queue_limit=hbp_to_ipsc_queue_limit,
        hbp_start_on_voice=hbp_start_on_voice,
        hbp_to_ipsc_header_repeats=hbp_to_ipsc_header_repeats,
        options=options,
        callsign=callsign,
        rx_freq=rx_freq,
        tx_freq=tx_freq,
        tx_power=tx_power,
        colorcode=colorcode,
        latitude=latitude,
        longitude=longitude,
        height=height,
        location=location,
        description=description,
        url=url,
        software_id=software_id,
        package_id=package_id,
    )


load_config = load
