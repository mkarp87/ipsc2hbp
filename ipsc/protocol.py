"""IPSC UDP protocol stacks for ipsc2hbp.

Two operating modes are implemented here:

* IPSCProtocol       - the original local IPSC master mode.
* IPSCPeerProtocol   - new IPSC peer mode that registers to an upstream
                       Motorola repeater or c-Bridge acting as IPSC master.

The public methods used by the translator are intentionally identical:
`is_peer_registered()` and `send_to_peer(packet)`.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any

from ipsc.const import (
    AUTH_DIGEST_LEN,
    DATA_CALL_MSK,
    DE_REG_REPLY,
    DE_REG_REQ,
    GROUP_VOICE,
    GV_BURST_TYPE_OFF,
    GV_CALL_INFO_OFF,
    IPSC_VER,
    MASTER_ALIVE_REPLY,
    MASTER_ALIVE_REQ,
    MASTER_REG_REPLY,
    MASTER_REG_REQ,
    MSTR_PEER_MSK,
    PEER_ALIVE_REPLY,
    PEER_ALIVE_REQ,
    PEER_LIST_REPLY,
    PEER_LIST_REQ,
    PEER_REG_REPLY,
    PEER_REG_REQ,
    PKT_AUTH_MSK,
    TS_CALL_MSK,
    VOICE_CALL_MSK,
)

log = logging.getLogger(__name__)
wire = logging.getLogger("ipsc.wire")

# Matches the existing implementation's mode byte. This is the IPSC "mode"
# byte sent inside registration/keepalive/peer-list packets.
_OUR_MODE = b"\x6a"
_PEER_ENTRY_LEN = 11


def _id_b(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _id_i(value: bytes) -> int:
    return int.from_bytes(value, "big")


def _ts_flags(*, master_peer: bool, auth_enabled: bool) -> bytes:
    # byte 0: linking/mode. bytes 1-4: flags. The old master mode advertised
    # MSTR_PEER_MSK; PEER mode deliberately does not.
    flags_byte4 = VOICE_CALL_MSK
    if master_peer:
        flags_byte4 |= MSTR_PEER_MSK
    if auth_enabled:
        flags_byte4 |= PKT_AUTH_MSK
    return _OUR_MODE + b"\x00\x00\x00" + bytes([flags_byte4])


def _parse_group_voice_meta(data: bytes) -> tuple[int, int]:
    call_info = data[GV_CALL_INFO_OFF]
    timeslot = 2 if call_info & TS_CALL_MSK else 1
    burst_type = data[GV_BURST_TYPE_OFF]
    return timeslot, burst_type


def _safe_peer_id(data: bytes) -> int:
    if len(data) < 5:
        return 0
    return _id_i(data[1:5])


class IPSCProtocol(asyncio.DatagramProtocol):
    """Original IPSC master stack.

    A Motorola repeater connects to this process as its IPSC master. This class
    is intentionally kept source-compatible with the previous implementation.
    """

    def __init__(self, cfg: Any, translator: Any):
        self._cfg = cfg
        self._translator = translator
        self._transport: asyncio.DatagramTransport | None = None

        self._registered = False
        self._peer_id: bytes | None = None
        self._peer_ip: str | None = None
        self._peer_port: int | None = None
        self._last_ka: float = 0.0
        self._watchdog_task: asyncio.Task[None] | None = None

        self._master_id_i = cfg.ipsc_master_id
        self._master_id = _id_b(cfg.ipsc_master_id)
        self._ts_flags = _ts_flags(master_peer=True, auth_enabled=cfg.auth_enabled)
        self._allowed_peer_ip = cfg.allowed_peer_ip or None
        self._expected_peer_id = cfg.ipsc_peer_id if cfg.ipsc_peer_id else None

        self._master_reg_reply = (
            bytes([MASTER_REG_REPLY])
            + self._master_id
            + self._ts_flags
            + struct.pack(">H", 1)
            + IPSC_VER
        )
        self._master_alive_reply = bytes([MASTER_ALIVE_REPLY]) + self._master_id + self._ts_flags + IPSC_VER
        self._de_reg_reply = bytes([DE_REG_REPLY]) + self._master_id

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("sockname")
        log.info("IPSC master listening on %s:%s", sock[0], sock[1])
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def connection_lost(self, exc: Exception | None) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        host, port = addr
        now = time.monotonic()

        if self._cfg.auth_enabled:
            if not self._check_auth(data):
                log.warning("Dropping IPSC packet with invalid auth from %s:%d", host, port)
                return
            data = data[:-AUTH_DIGEST_LEN]

        if not data:
            return

        opcode = data[0]
        wire.debug("rx %s:%d opcode=0x%02x len=%d data=%s", host, port, opcode, len(data), data.hex())

        if opcode == MASTER_REG_REQ:
            self._on_reg_req(data, host, port, now)
        elif opcode == MASTER_ALIVE_REQ:
            self._on_alive_req(data, host, port, now)
        elif opcode == PEER_LIST_REQ:
            self._on_peer_list_req(data, host, port)
        elif opcode == DE_REG_REQ:
            self._on_de_reg_req(data, host, port)
        elif opcode == GROUP_VOICE:
            self._on_group_voice(data, host, port, now)
        elif opcode in {
            PEER_REG_REQ,
            PEER_REG_REPLY,
            PEER_ALIVE_REQ,
            PEER_ALIVE_REPLY,
            MASTER_REG_REPLY,
            MASTER_ALIVE_REPLY,
            PEER_LIST_REPLY,
            DE_REG_REPLY,
        }:
            log.debug("Ignoring IPSC peer-side opcode 0x%02x in MASTER mode", opcode)
        else:
            log.debug("Unhandled IPSC opcode 0x%02x from %s:%d", opcode, host, port)

    def _on_reg_req(self, data: bytes, host: str, port: int, now: float) -> None:
        if len(data) < 10:
            log.warning("Short MASTER_REG_REQ from %s:%d", host, port)
            return

        peer_id_b = data[1:5]
        peer_id_i = _id_i(peer_id_b)
        if self._allowed_peer_ip and host != self._allowed_peer_ip:
            log.warning("Rejecting IPSC peer %s from unexpected IP %s", peer_id_i, host)
            return
        if self._expected_peer_id is not None and peer_id_i != self._expected_peer_id:
            log.warning("Rejecting IPSC peer id %s, expected %s", peer_id_i, self._expected_peer_id)
            return
        if self._registered and self._peer_ip and host != self._peer_ip:
            log.warning("Rejecting IPSC registration from %s:%d; %s is already registered", host, port, self._peer_ip)
            return

        first_registration = not self._registered
        self._registered = True
        self._peer_id = peer_id_b
        self._peer_ip = host
        self._peer_port = port
        self._last_ka = now

        log.info("IPSC peer registered id=%s addr=%s:%d", peer_id_i, host, port)
        self._send(self._master_reg_reply, host, port)
        self._send_peer_list(host, port)

        if first_registration:
            self._translator.peer_registered(peer_id_b, host, port)

    def _on_alive_req(self, data: bytes, host: str, port: int, now: float) -> None:
        peer_id_b = data[1:5] if len(data) >= 5 else b"\x00\x00\x00\x00"
        if not self._registered or peer_id_b != self._peer_id:
            log.debug("MASTER_ALIVE_REQ from unregistered peer %s:%d", host, port)
            return
        self._last_ka = now
        self._send(self._master_alive_reply, host, port)

    def _on_peer_list_req(self, data: bytes, host: str, port: int) -> None:
        peer_id_b = data[1:5] if len(data) >= 5 else b"\x00\x00\x00\x00"
        if not self._registered or peer_id_b != self._peer_id:
            log.debug("PEER_LIST_REQ from unregistered peer %s:%d", host, port)
            return
        self._send_peer_list(host, port)

    def _on_de_reg_req(self, data: bytes, host: str, port: int) -> None:
        peer_id_b = data[1:5] if len(data) >= 5 else b"\x00\x00\x00\x00"
        if self._registered and peer_id_b == self._peer_id:
            log.info("IPSC peer de-registered id=%s", _id_i(peer_id_b))
            self._send(self._de_reg_reply, host, port)
            self._clear_peer()
        else:
            log.debug("DE_REG_REQ from unknown peer %s:%d", host, port)

    def _on_group_voice(self, data: bytes, host: str, port: int, now: float) -> None:
        if not self._registered or host != self._peer_ip or port != self._peer_port:
            log.debug("GROUP_VOICE from unregistered IPSC peer %s:%d", host, port)
            return
        try:
            timeslot, burst_type = _parse_group_voice_meta(data)
        except IndexError:
            log.warning("Short GROUP_VOICE from %s:%d", host, port)
            return
        self._last_ka = now
        self._translator.ipsc_voice_received(data, timeslot, burst_type)

    def _send_peer_list(self, host: str, port: int) -> None:
        if not self._peer_id or not self._peer_ip or not self._peer_port:
            return
        try:
            ip_packed = socket.inet_aton(self._peer_ip)
        except OSError:
            ip_packed = b"\x00\x00\x00\x00"
        peer_entry = self._peer_id + ip_packed + struct.pack(">H", self._peer_port) + _OUR_MODE
        pkt = bytes([PEER_LIST_REPLY]) + self._master_id + struct.pack(">H", len(peer_entry)) + peer_entry
        self._send(pkt, host, port)

    async def _watchdog_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                self._translator.check_call_timeouts()
                if self._registered and (time.monotonic() - self._last_ka) > self._cfg.keepalive_watchdog:
                    log.warning("IPSC peer keepalive watchdog expired")
                    self._clear_peer()
        except asyncio.CancelledError:
            return

    def _clear_peer(self) -> None:
        self._registered = False
        self._peer_id = None
        self._peer_ip = None
        self._peer_port = None
        self._last_ka = 0.0
        self._translator.peer_lost()

    def _check_auth(self, data: bytes) -> bool:
        if len(data) <= AUTH_DIGEST_LEN:
            return False
        payload = data[:-AUTH_DIGEST_LEN]
        supplied = data[-AUTH_DIGEST_LEN:]
        expected = hmac.new(self._cfg.auth_key, payload, "sha1").digest()[:AUTH_DIGEST_LEN]
        return hmac.compare_digest(supplied, expected)

    def _auth_suffix(self, payload: bytes) -> bytes:
        if not self._cfg.auth_enabled:
            return b""
        return hmac.new(self._cfg.auth_key, payload, "sha1").digest()[:AUTH_DIGEST_LEN]

    def _send(self, payload: bytes, host: str, port: int) -> None:
        if self._transport is None:
            return
        pkt = payload + self._auth_suffix(payload)
        wire.debug("tx %s:%d opcode=0x%02x len=%d data=%s", host, port, payload[0], len(pkt), pkt.hex())
        self._transport.sendto(pkt, (host, port))

    def send_to_peer(self, packet: bytes) -> None:
        if not self._registered or not self._peer_ip or not self._peer_port:
            log.debug("Dropping outbound IPSC packet: no registered peer")
            return
        self._send(packet, self._peer_ip, self._peer_port)

    def is_peer_registered(self) -> bool:
        return self._registered

    def get_peer_list(self) -> list[dict[str, Any]]:
        if not self._registered or not self._peer_id or not self._peer_ip or not self._peer_port:
            return []
        return [
            {
                "peer_id": _id_i(self._peer_id),
                "ip": self._peer_ip,
                "port": self._peer_port,
                "connected": True,
                "source": "local-master-peer",
            }
        ]

    def stop(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()


@dataclass
class _PeerEntry:
    peer_id: int
    ip: str
    port: int
    mode: bytes = _OUR_MODE
    connected: bool = False
    from_peer_list: bool = True
    last_rx: float = 0.0
    last_tx: float = 0.0
    outstanding: int = 0


class IPSCPeerProtocol(asyncio.DatagramProtocol):
    """IPSC peer stack.

    This process registers to an upstream IPSC master, retrieves and maintains
    the master-provided peer-list, and exchanges peer registration/keepalive
    packets with listed peers. HBP translation remains handled by the existing
    translator and HBP client.
    """

    def __init__(self, cfg: Any, translator: Any):
        self._cfg = cfg
        self._translator = translator
        self._transport: asyncio.DatagramTransport | None = None

        self._local_id_i = cfg.ipsc_peer_id
        self._local_id_b = _id_b(cfg.ipsc_peer_id)
        self._ts_flags = _ts_flags(master_peer=False, auth_enabled=cfg.auth_enabled)

        self._configured_master_ip = cfg.ipsc_upstream_master_ip
        self._master_ip = cfg.ipsc_upstream_master_ip
        self._master_port = cfg.ipsc_upstream_master_port
        self._master_id_i = cfg.ipsc_upstream_master_id
        self._master_id_b = _id_b(cfg.ipsc_upstream_master_id) if cfg.ipsc_upstream_master_id else b"\x00\x00\x00\x00"

        self._registered = False
        self._master_num_peers = 0
        self._master_mode = b"\x00"
        self._last_master_rx = 0.0
        self._master_outstanding = 0
        self._master_reg_reported = False
        self._peer_list_seen = False

        self._peers: dict[int, _PeerEntry] = {}
        self._maintenance_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

        self._master_reg_req = bytes([MASTER_REG_REQ]) + self._local_id_b + self._ts_flags + IPSC_VER
        self._master_alive_req = bytes([MASTER_ALIVE_REQ]) + self._local_id_b + self._ts_flags + IPSC_VER
        self._peer_list_req = bytes([PEER_LIST_REQ]) + self._local_id_b
        self._peer_reg_req = bytes([PEER_REG_REQ]) + self._local_id_b + IPSC_VER
        self._peer_reg_reply = bytes([PEER_REG_REPLY]) + self._local_id_b + IPSC_VER
        self._peer_alive_req = bytes([PEER_ALIVE_REQ]) + self._local_id_b + self._ts_flags
        self._peer_alive_reply = bytes([PEER_ALIVE_REPLY]) + self._local_id_b + self._ts_flags
        self._de_reg_req = bytes([DE_REG_REQ]) + self._local_id_b

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("sockname")
        log.info(
            "IPSC peer endpoint on %s:%s local_id=%s upstream=%s:%d expected_master_id=%s",
            sock[0],
            sock[1],
            self._local_id_i,
            self._configured_master_ip,
            self._master_port,
            self._master_id_i or "learn",
        )
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        self._send_to_master(self._master_reg_req, use_configured_port=True)

    def connection_lost(self, exc: Exception | None) -> None:
        self.stop()

    def stop(self) -> None:
        if self._registered:
            self._send_to_master(self._de_reg_req)
            for peer in list(self._peers.values()):
                if peer.connected:
                    self._send(self._de_reg_req, peer.ip, peer.port)
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        host, port = addr
        now = time.monotonic()

        if self._cfg.auth_enabled:
            if not self._check_auth(data):
                log.warning("Dropping IPSC packet with invalid auth from %s:%d", host, port)
                return
            data = data[:-AUTH_DIGEST_LEN]

        if not data:
            return

        opcode = data[0]
        src_id = _safe_peer_id(data)
        wire.debug("rx %s:%d opcode=0x%02x src=%s len=%d data=%s", host, port, opcode, src_id, len(data), data.hex())

        if src_id == self._local_id_i:
            return

        is_master = self._is_master_source(src_id, host, port, opcode)
        peer = None if is_master else self._accept_or_get_peer(src_id, host, port, opcode, now)
        if not is_master and peer is None and opcode not in {MASTER_REG_REPLY, MASTER_ALIVE_REPLY, PEER_LIST_REPLY}:
            log.debug("Dropping IPSC packet from unlisted peer src=%s addr=%s:%d opcode=0x%02x", src_id, host, port, opcode)
            return

        if is_master:
            self._touch_master(src_id, host, port, now)
        elif peer is not None:
            self._touch_peer(peer, host, port, now)

        if opcode == MASTER_REG_REPLY:
            if is_master:
                self._on_master_reg_reply(data, host, port, now)
            else:
                log.debug("Ignoring MASTER_REG_REPLY from non-master %s:%d", host, port)
        elif opcode == MASTER_ALIVE_REPLY:
            if is_master:
                self._on_master_alive_reply(data, host, port, now)
        elif opcode == PEER_LIST_REPLY:
            if is_master:
                self._on_peer_list_reply(data, host, port, now)
        elif opcode == PEER_REG_REQ:
            if peer is not None:
                peer.connected = True
                peer.outstanding = 0
                self._send(self._peer_reg_reply, peer.ip, peer.port)
                log.info("IPSC peer registration request accepted id=%s addr=%s:%d", peer.peer_id, peer.ip, peer.port)
        elif opcode == PEER_REG_REPLY:
            if peer is not None:
                peer.connected = True
                peer.outstanding = 0
                log.info("IPSC peer registered with us id=%s addr=%s:%d", peer.peer_id, peer.ip, peer.port)
        elif opcode == PEER_ALIVE_REQ:
            if peer is not None:
                peer.connected = True
                peer.outstanding = 0
                self._send(self._peer_alive_reply, peer.ip, peer.port)
        elif opcode == PEER_ALIVE_REPLY:
            if peer is not None:
                peer.connected = True
                peer.outstanding = 0
        elif opcode == GROUP_VOICE:
            if self._registered and (is_master or peer is not None):
                self._on_group_voice(data, host, port, now)
        elif opcode in {DE_REG_REQ, DE_REG_REPLY}:
            if is_master:
                log.warning("Upstream IPSC master de-registered or acknowledged de-registration")
                self._clear_master_registration()
            elif peer is not None:
                log.info("IPSC peer de-registered id=%s addr=%s:%d", peer.peer_id, peer.ip, peer.port)
                peer.connected = False
                peer.outstanding = 0
                if opcode == DE_REG_REQ:
                    self._send(bytes([DE_REG_REPLY]) + self._local_id_b, peer.ip, peer.port)
        elif opcode in {MASTER_REG_REQ, MASTER_ALIVE_REQ, PEER_LIST_REQ}:
            log.debug("Ignoring master-side opcode 0x%02x in PEER mode", opcode)
        else:
            log.debug("Unhandled IPSC opcode 0x%02x from %s:%d", opcode, host, port)

    def _is_master_source(self, src_id: int, host: str, port: int, opcode: int) -> bool:
        if host != self._configured_master_ip:
            return False
        if self._master_id_i and src_id not in {0, self._master_id_i}:
            return False
        if opcode in {MASTER_REG_REPLY, MASTER_ALIVE_REPLY, PEER_LIST_REPLY, GROUP_VOICE, DE_REG_REQ, DE_REG_REPLY}:
            return True
        return False

    def _touch_master(self, src_id: int, host: str, port: int, now: float) -> None:
        if src_id and self._master_id_i == 0:
            self._master_id_i = src_id
            self._master_id_b = _id_b(src_id)
            log.info("Learned upstream IPSC master id=%s from %s:%d", src_id, host, port)
        self._master_ip = host
        self._master_port = port
        self._last_master_rx = now
        self._master_outstanding = 0

    def _accept_or_get_peer(self, src_id: int, host: str, port: int, opcode: int, now: float) -> _PeerEntry | None:
        if src_id == 0:
            return None
        peer = self._peers.get(src_id)
        if peer is not None:
            return peer
        if not self._cfg.ipsc_peer_list_allow_unknown:
            return None
        if opcode not in {PEER_REG_REQ, PEER_REG_REPLY, PEER_ALIVE_REQ, PEER_ALIVE_REPLY, GROUP_VOICE, DE_REG_REQ, DE_REG_REPLY}:
            return None
        peer = _PeerEntry(peer_id=src_id, ip=host, port=port, connected=False, from_peer_list=False, last_rx=now)
        self._peers[src_id] = peer
        log.info("Accepted unlisted IPSC peer by wildcard fallback id=%s addr=%s:%d", src_id, host, port)
        return peer

    def _touch_peer(self, peer: _PeerEntry, host: str, port: int, now: float) -> None:
        if peer.ip != host or peer.port != port:
            log.info("Updating IPSC peer address id=%s %s:%d -> %s:%d", peer.peer_id, peer.ip, peer.port, host, port)
            peer.ip = host
            peer.port = port
        peer.last_rx = now

    def _on_master_reg_reply(self, data: bytes, host: str, port: int, now: float) -> None:
        if len(data) < 16:
            log.warning("Short MASTER_REG_REPLY from %s:%d", host, port)
            return
        src_id = _id_i(data[1:5])
        self._touch_master(src_id, host, port, now)

        self._master_mode = data[5:6]
        self._master_num_peers = struct.unpack(">H", data[10:12])[0]
        first_registration = not self._registered
        self._registered = True

        log.info(
            "Registered upstream IPSC master id=%s addr=%s:%d num_peers=%d mode=0x%s",
            self._master_id_i or src_id,
            host,
            port,
            self._master_num_peers,
            self._master_mode.hex(),
        )
        if first_registration and not self._master_reg_reported:
            self._master_reg_reported = True
            self._translator.peer_registered(self._local_id_b, host, port)

        # Always request the full peer-list. Some c-Bridge and repeater
        # deployments do not populate num_peers consistently, and an empty
        # PEER_LIST_REPLY is harmless.
        self._send_to_master(self._peer_list_req)

    def _on_master_alive_reply(self, data: bytes, host: str, port: int, now: float) -> None:
        self._touch_master(_safe_peer_id(data), host, port, now)
        if not self._registered:
            log.debug("MASTER_ALIVE_REPLY received before registration; requesting registration")
            self._send_to_master(self._master_reg_req)

    def _on_peer_list_reply(self, data: bytes, host: str, port: int, now: float) -> None:
        if len(data) < 7:
            log.warning("Short PEER_LIST_REPLY from %s:%d", host, port)
            return
        self._touch_master(_safe_peer_id(data), host, port, now)
        length = struct.unpack(">H", data[5:7])[0]
        payload = data[7 : 7 + length]
        if len(payload) < length:
            log.warning("Truncated PEER_LIST_REPLY: advertised=%d actual=%d", length, len(payload))
            return
        if length % _PEER_ENTRY_LEN:
            log.warning("PEER_LIST_REPLY length %d is not a multiple of %d", length, _PEER_ENTRY_LEN)

        seen: set[int] = set()
        for offset in range(0, len(payload) - (len(payload) % _PEER_ENTRY_LEN), _PEER_ENTRY_LEN):
            entry = payload[offset : offset + _PEER_ENTRY_LEN]
            peer_id = _id_i(entry[0:4])
            ip = str(ipaddress.IPv4Address(entry[4:8]))
            peer_port = struct.unpack(">H", entry[8:10])[0]
            mode = entry[10:11]

            if peer_id in {0, self._local_id_i, self._master_id_i}:
                continue
            seen.add(peer_id)
            existing = self._peers.get(peer_id)
            if existing is None:
                self._peers[peer_id] = _PeerEntry(peer_id=peer_id, ip=ip, port=peer_port, mode=mode, from_peer_list=True)
                log.info("Peer-list add id=%s addr=%s:%d mode=0x%s", peer_id, ip, peer_port, mode.hex())
            else:
                changed = existing.ip != ip or existing.port != peer_port or existing.mode != mode
                existing.ip = ip
                existing.port = peer_port
                existing.mode = mode
                existing.from_peer_list = True
                if changed:
                    log.info("Peer-list update id=%s addr=%s:%d mode=0x%s", peer_id, ip, peer_port, mode.hex())

        if self._cfg.ipsc_peer_list_prune:
            for peer_id, peer in list(self._peers.items()):
                if peer.from_peer_list and peer_id not in seen:
                    log.info("Peer-list remove id=%s addr=%s:%d", peer.peer_id, peer.ip, peer.port)
                    del self._peers[peer_id]

        self._peer_list_seen = True
        self._log_peer_list()

    def _on_group_voice(self, data: bytes, host: str, port: int, now: float) -> None:
        try:
            timeslot, burst_type = _parse_group_voice_meta(data)
        except IndexError:
            log.warning("Short GROUP_VOICE from %s:%d", host, port)
            return
        self._translator.ipsc_voice_received(data, timeslot, burst_type)

    async def _maintenance_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._cfg.ipsc_peer_alive_interval)
                if not self._registered:
                    self._send_to_master(self._master_reg_req, use_configured_port=True)
                    self._master_outstanding += 1
                    continue

                self._send_to_master(self._master_alive_req)
                self._master_outstanding += 1

                if not self._peer_list_seen:
                    self._send_to_master(self._peer_list_req)

                for peer in list(self._peers.values()):
                    if peer.peer_id == self._local_id_i:
                        continue
                    if peer.connected:
                        self._send(self._peer_alive_req, peer.ip, peer.port)
                    else:
                        self._send(self._peer_reg_req, peer.ip, peer.port)
                    peer.last_tx = time.monotonic()
                    peer.outstanding += 1
                    max_missed = max(1, self._cfg.ipsc_max_missed)
                    if peer.outstanding > max_missed:
                        if peer.connected:
                            log.warning("IPSC peer keepalive expired id=%s addr=%s:%d", peer.peer_id, peer.ip, peer.port)
                        peer.connected = False
        except asyncio.CancelledError:
            return

    async def _watchdog_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                self._translator.check_call_timeouts()
                peer_timeout = max(1, self._cfg.ipsc_peer_alive_interval) * max(1, self._cfg.ipsc_max_missed)
                if self._registered and self._last_master_rx and (time.monotonic() - self._last_master_rx) > peer_timeout:
                    log.warning("Upstream IPSC master watchdog expired after %ss", peer_timeout)
                    self._clear_master_registration()
        except asyncio.CancelledError:
            return

    def _clear_master_registration(self) -> None:
        if self._registered:
            self._registered = False
            self._master_outstanding = 0
            self._peer_list_seen = False
            self._master_reg_reported = False
            for peer in self._peers.values():
                peer.connected = False
                peer.outstanding = 0
            self._translator.peer_lost()

    def _check_auth(self, data: bytes) -> bool:
        if len(data) <= AUTH_DIGEST_LEN:
            return False
        payload = data[:-AUTH_DIGEST_LEN]
        supplied = data[-AUTH_DIGEST_LEN:]
        expected = hmac.new(self._cfg.auth_key, payload, "sha1").digest()[:AUTH_DIGEST_LEN]
        return hmac.compare_digest(supplied, expected)

    def _auth_suffix(self, payload: bytes) -> bytes:
        if not self._cfg.auth_enabled:
            return b""
        return hmac.new(self._cfg.auth_key, payload, "sha1").digest()[:AUTH_DIGEST_LEN]

    def _send(self, payload: bytes, host: str, port: int) -> None:
        if self._transport is None:
            return
        pkt = payload + self._auth_suffix(payload)
        wire.debug("tx %s:%d opcode=0x%02x len=%d data=%s", host, port, payload[0], len(pkt), pkt.hex())
        self._transport.sendto(pkt, (host, port))

    def _send_to_master(self, payload: bytes, *, use_configured_port: bool = False) -> None:
        port = self._cfg.ipsc_upstream_master_port if use_configured_port else self._master_port
        self._send(payload, self._master_ip, port)

    def send_to_peer(self, packet: bytes) -> None:
        if not self._registered:
            log.debug("Dropping outbound IPSC packet: not registered to upstream IPSC master")
            return

        # In IPSC mesh operation the peer sends user traffic to the master and to
        # connected peers. If a c-Bridge does not return a peer-list, master-only
        # forwarding still works.
        self._send_to_master(packet)
        for peer in list(self._peers.values()):
            if peer.connected:
                self._send(packet, peer.ip, peer.port)

    def is_peer_registered(self) -> bool:
        return self._registered

    def get_peer_list(self) -> list[dict[str, Any]]:
        rows = []
        if self._master_ip and self._master_port:
            rows.append(
                {
                    "peer_id": self._master_id_i,
                    "ip": self._master_ip,
                    "port": self._master_port,
                    "connected": self._registered,
                    "source": "upstream-master",
                }
            )
        for peer in sorted(self._peers.values(), key=lambda p: p.peer_id):
            rows.append(
                {
                    "peer_id": peer.peer_id,
                    "ip": peer.ip,
                    "port": peer.port,
                    "connected": peer.connected,
                    "source": "peer-list" if peer.from_peer_list else "wildcard",
                    "mode": peer.mode.hex(),
                    "last_rx": peer.last_rx,
                    "outstanding": peer.outstanding,
                }
            )
        return rows

    def _log_peer_list(self) -> None:
        if not log.isEnabledFor(logging.INFO):
            return
        peers = [p for p in self.get_peer_list() if p["source"] != "upstream-master"]
        if not peers:
            log.info("Peer-list empty; using upstream master only")
            return
        log.info("Peer-list contains %d entries", len(peers))
        for peer in peers:
            log.info(
                "  peer id=%s addr=%s:%s connected=%s source=%s mode=%s",
                peer["peer_id"],
                peer["ip"],
                peer["port"],
                peer["connected"],
                peer["source"],
                peer.get("mode", ""),
            )
