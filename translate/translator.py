"""
CallTranslator — bidirectional IPSC ↔ HBP translation.

Outbound (IPSC → HBP): ipsc_voice_received()
  VOICE_HEAD:       Build DMRD VOICE_LC_HEAD frame from BPTC-encoded LC.
  SLOT1/SLOT2_VOICE: Convert 3×49-bit AMBE from IPSC to 3×72-bit, assemble
                    264-bit DMR voice frame with proper EMBED/SYNC field.
  VOICE_TERM:       Build DMRD VOICE_LC_TERM frame from BPTC-encoded LC.

Inbound (HBP → IPSC): hbp_voice_received()
  VOICE_HEAD/TERM:  Reconstruct IPSC header/terminator packet with LC payload.
  VOICE/VOICESYNC:  Extract 3×72-bit AMBE from DMRD, convert to 3×49-bit,
                    pack into 19-byte IPSC AMBE format, send burst-type-specific payload.

AMBE layout in IPSC SLOT_VOICE (bytes 33–51, 19 bytes = 152 bits):
  bits[0:49]    = AMBE frame a (49 bits)
  bit[49]       = bad data bit for frame a (1 = AMBE decode error; always 0 on transmit)
  bits[50:99]   = AMBE frame b (49 bits)
  bit[99]       = bad data bit for frame b (always 0 on transmit)
  bits[100:149] = AMBE frame c (49 bits)
  bit[149]      = bad data bit for frame c (always 0 on transmit)
  bits[150:152] = pad (0)

DMR voice frame layout (264 bits = 33 bytes):
  frame_a[72] | frame_b_first[36] | EMBED[48] | frame_b_second[36] | frame_c[72]
  (frame b straddles the EMBED/SYNC field; reassembled by concatenating the two halves)

Superframe mapping (6-frame cycle, position resets on each VOICE_HEAD):
  position 0   → HBPF_FRAMETYPE_VOICESYNC, EMBED = BS_VOICE_SYNC
  position 1   → HBPF_FRAMETYPE_VOICE | 0, EMBED = BURST_B + EMB_LC[1]
  position 2   → HBPF_FRAMETYPE_VOICE | 1, EMBED = BURST_C + EMB_LC[2]
  position 3   → HBPF_FRAMETYPE_VOICE | 2, EMBED = BURST_D + EMB_LC[3]
  position 4   → HBPF_FRAMETYPE_VOICE | 3, EMBED = BURST_E + EMB_LC[4]
  position 5   → HBPF_FRAMETYPE_VOICE | 4, EMBED = BURST_F + NULL_EMB_LC
"""

import asyncio
import logging
import os
import struct
from collections import deque
from random import randint
from time import time

from bitarray import bitarray

from dmr_utils3 import bptc
from dmr_utils3.ambe_utils import convert49BitTo72BitAMBE, convert72BitTo49BitAMBE
from dmr_utils3.const import EMB, SLOT_TYPE, BS_VOICE_SYNC, BS_DATA_SYNC, LC_OPT

from config import Config
from ipsc.const import (
    GROUP_VOICE,
    VOICE_HEAD, VOICE_TERM, SLOT1_VOICE, SLOT2_VOICE,
    TS_CALL_MSK, END_MSK,
    GV_SRC_SUB_OFF, GV_DST_GROUP_OFF, GV_IPSC_SEQ_OFF,
)
from hbp.const import (
    HBPF_DMRD,
    HBPF_TGID_TS2,
    HBPF_FRAMETYPE_VOICE, HBPF_FRAMETYPE_VOICESYNC, HBPF_FRAMETYPE_DATASYNC,
    HBPF_FRAMETYPE_MASK, HBPF_DTYPE_MASK,
    HBPF_SLT_VHEAD, HBPF_SLT_VTERM,
    DMRD_LEN,
    DMRD_SRC_OFF, DMRD_DST_OFF,
    DMRD_FLAGS_OFF, DMRD_PAYLOAD_OFF,
)

log = logging.getLogger(__name__)

# 32-bit zero bitarray for voice frame F (null embedded LC)
_NULL_EMB_LC = bitarray(32, endian='big')
_NULL_EMB_LC.setall(0)

_EMB_BURST_NAMES = ('BURST_B', 'BURST_C', 'BURST_D', 'BURST_E', 'BURST_F')


def _ambe49_to_72(ba49: bitarray) -> bitarray:
    """Convert 49-bit raw AMBE to 72-bit interleaved, returning bitarray(72)."""
    raw = convert49BitTo72BitAMBE(ba49)
    out = bitarray(endian='big')
    out.frombytes(bytes(raw))
    return out


def _extract_ambe_from_dmrd(payload_33: bytes) -> bytes:
    """
    Extract 3×72-bit AMBE from a 33-byte DMR voice frame payload, convert each
    to 49-bit raw AMBE, and pack into the 19-byte IPSC AMBE format.
    """
    burst = bitarray(endian='big')
    burst.frombytes(payload_33)
    # DMR voice frame: AMBE1[0:72] | AMBE2_half1[72:108] | EMB[108:156] | AMBE2_half2[156:192] | AMBE3[192:264]
    a1_72 = burst[0:72]
    a2_72 = burst[72:108] + burst[156:192]
    a3_72 = burst[192:264]

    a1_49 = convert72BitTo49BitAMBE(a1_72)
    a2_49 = convert72BitTo49BitAMBE(a2_72)
    a3_49 = convert72BitTo49BitAMBE(a3_72)

    # IPSC 19-byte (152-bit) AMBE: [a1(49)] [0] [a2(49)] [0] [a3(49)] [000]
    ipsc_bits = bitarray(152, endian='big')
    ipsc_bits.setall(0)
    ipsc_bits[0:49]   = a1_49
    ipsc_bits[50:99]  = a2_49
    ipsc_bits[100:149] = a3_49
    return ipsc_bits.tobytes()


def _build_ipsc_voice_payload(lc: bytes, burst_type: int) -> bytes:
    """
    Build the MOTOTRBO-format payload bytes (after the burst_type byte) for
    VOICE_HEAD or VOICE_TERM.  Both produce 23-byte payloads → 54-byte total
    GROUP_VOICE packet (confirmed from voice_packets.txt and wire captures).

    Byte layout:
      byte 0:    RSSI_THRESH_PARITY     (0x80, confirmed from wire captures)
      bytes 1–2: length_to_follow       (10 words = (54-34)/2)
      byte 3:    RSSI status            (0x80)
      byte 4:    slot type / sync       (0x0a)
      bytes 5–6: data size in bits      (0x0060 = 96)
      bytes 7–15: LC word (9 bytes)
      bytes 16–18: RS(12,9) FEC (3 bytes, mask differs HEAD vs TERM)
      bytes 19–22: type indicator + zeros (0x00, 0x11 or 0x12, 0x00, 0x00)
    """
    if burst_type == VOICE_HEAD:
        fec      = bptc.rs129.lc_header_encode(lc[:9])
        type_tag = b'\x11'
    else:  # VOICE_TERM
        fec      = bptc.rs129.lc_terminator_encode(lc[:9])
        type_tag = b'\x12'
    return (
        b'\x80'                    # RSSI_THRESH_PARITY — 0x80 confirmed from wire captures
        + struct.pack('>H', 10)    # length_to_follow = 10 words
        + b'\x80'                  # RSSI status
        + b'\x0a'                  # slot type / sync
        + struct.pack('>H', 0x60)  # data size = 96 bits
        + lc[:9]                   # full LC word (FLCO + FID + opts + dst + src)
        + fec                      # RS(12,9) parity: 3 bytes
        + b'\x00' + type_tag + b'\x00\x00'   # 4 bytes: 0x00, 0x11/0x12, 0x00, 0x00
    )


class CallTranslator:
    """
    Wires IPSCProtocol and HBPClient together.

    Instantiate first, then pass to both protocol objects, then call
    set_protocols() so the translator can reach back into each stack.
    """

    def __init__(self, cfg: Config):
        self._cfg           = cfg
        self._ipsc          = None
        self._hbp           = None
        self._repeater_id_b = cfg.hbp_repeater_id.to_bytes(4, 'big')
        ipsc_source_id     = getattr(cfg, 'ipsc_source_id', cfg.ipsc_master_id)
        self._master_id_b   = ipsc_source_id.to_bytes(4, 'big')

        # Outbound call state (IPSC → HBP) — keyed by timeslot (1 or 2)
        self._out_stream_id = {1: None, 2: None}  # 4 random bytes, new per call
        self._out_seq       = 0                   # DMRD sequence byte, wraps at 256 (shared)
        self._out_frame_pos = {1: 0, 2: 0}        # superframe position (0–5, cycles)
        self._out_lc        = {1: None, 2: None}  # 9-byte LC for embedded LC generation
        self._out_emb_lc    = {1: None, 2: None}  # dict {1–4: bitarray(32)} embedded LC

        # Inbound call state (HBP → IPSC) — keyed by timeslot (1 or 2)
        self._in_lc         = {1: None, 2: None}  # 9-byte LC decoded from HBP VOICE_HEAD
        self._in_emb_lc     = {1: None, 2: None}  # dict {1–4: bitarray(32)} from bptc.encode_emblc
        self._in_stream_id  = {1: 0, 2: 0}        # byte 5: call stream ID, constant per call
        self._in_stream_ctr = 0                   # increments once per call (shared across TS)
        self._in_hbp_stream = {1: None, 2: None}  # 4-byte HBP stream currently mapped to IPSC
        self._in_src_sub    = {1: None, 2: None}  # 3-byte source currently mapped to IPSC
        self._in_dst_group  = {1: None, 2: None}  # 3-byte destination currently mapped to IPSC
        self._in_last_head  = {1: 0.0, 2: 0.0}   # last HBP VOICE_HEAD time for duplicate debounce
        self._in_rtp_seq    = {1: 0, 2: 0}        # RTP sequence in GV header
        self._in_rtp_ts     = {1: 0, 2: 0}        # RTP timestamp; increments 480/frame

        # Original IPSC_Bridge-style slot arbitration.  Inbound IPSC traffic marks
        # the timeslot busy; HBP-to-IPSC traffic is not transmitted while the slot
        # is busy.  This avoids using broad src/tg suppression for normal
        # direction changes.
        self._ipsc_busy_last = {1: 0.0, 2: 0.0}

        # Per-timeslot call-direction lock.  This prevents a reflected or
        # colliding opposite-direction call on the same talkgroup from stealing
        # the slot while the current call is still active.  It is intentionally
        # short-lived and active-call based, unlike the older broad reflection
        # suppression window.
        self._call_lock_owner = {1: None, 2: None}       # 'IPSC' or 'HBP'
        self._call_lock_src   = {1: None, 2: None}       # 3-byte src_sub
        self._call_lock_tg    = {1: None, 2: None}       # 3-byte dst_group
        self._call_lock_until = {1: 0.0, 2: 0.0}         # post-call hang expiry
        self._call_lock_blocked_ipsc = {1: {}, 2: {}}    # IPSC stream byte -> expiry
        self._call_lock_blocked_hbp  = {1: {}, 2: {}}    # HBP stream bytes -> expiry

        # HBP-to-IPSC pacing.  HBLink can deliver buffered DMRD frames in a burst,
        # but MotoTRBO/IPSC expects approximately real-time 60 ms voice cadence.
        self._ipsc_tx_queue = {1: deque(), 2: deque()}
        self._ipsc_tx_handle = {1: None, 2: None}
        self._in_started = {1: False, 2: False}  # True after an IPSC VHEAD has actually been sent

        # Reflected-call suppression for IPSC PEER mode.  Disabled by default in
        # this build because the original IPSC_Bridge did not suppress by
        # src/tg/ts; it relied on slot-busy arbitration instead.
        self._reflect_recent = {1: [], 2: []}     # [(src_sub, dst_group, expires_at), ...]
        self._reflect_ignore = {1: {}, 2: {}}     # IPSC stream byte -> expires_at

        # Last-packet timestamps for hung-call detection (seconds since epoch)
        self._out_last_pkt  = {1: 0.0, 2: 0.0}
        self._in_last_pkt   = {1: 0.0, 2: 0.0}

        # Call metadata learned from the IPSC peer and echoed back inbound
        self._peer_call_type = b'\x02'               # group voice (Motorola default)
        self._peer_call_ctrl = b'\x00\x00\x43\xe2'  # Motorola repeater default

    def set_protocols(self, ipsc_proto, hbp_client):
        self._ipsc = ipsc_proto
        self._hbp  = hbp_client

    # ------------------------------------------------------------------
    # IPSC callbacks
    # ------------------------------------------------------------------

    def peer_registered(self, peer_id: bytes, host: str, port: int):
        log.info('IPSC peer registered: id=%d  %s:%d',
                 int.from_bytes(peer_id, 'big'), host, port)
        if self._cfg.hbp_mode == 'TRACKING':
            self._hbp.activate()

    def peer_lost(self):
        log.warning('IPSC peer lost')
        self._out_stream_id  = {1: None, 2: None}
        self._out_lc         = {1: None, 2: None}
        self._out_emb_lc     = {1: None, 2: None}
        self._out_last_pkt   = {1: 0.0, 2: 0.0}
        self._in_lc          = {1: None, 2: None}
        self._in_emb_lc      = {1: None, 2: None}
        self._in_stream_id   = {1: 0, 2: 0}
        self._in_hbp_stream  = {1: None, 2: None}
        self._in_src_sub     = {1: None, 2: None}
        self._in_dst_group   = {1: None, 2: None}
        self._in_last_head   = {1: 0.0, 2: 0.0}
        self._in_last_pkt    = {1: 0.0, 2: 0.0}
        self._in_started     = {1: False, 2: False}
        self._clear_ipsc_tx_queues()
        self._reflect_recent = {1: [], 2: []}
        self._reflect_ignore = {1: {}, 2: {}}
        self._clear_call_locks()
        self._peer_call_type = b'\x02'
        self._peer_call_ctrl = b'\x00\x00\x43\xe2'
        if self._cfg.hbp_mode == 'TRACKING':
            self._hbp.deactivate()

    def ipsc_voice_received(self, data: bytes, ts: int, burst_type: int):
        if not self._hbp.is_connected():
            return
        self._out_last_pkt[ts] = time()

        src_sub   = data[GV_SRC_SUB_OFF   : GV_SRC_SUB_OFF   + 3]
        dst_group = data[GV_DST_GROUP_OFF  : GV_DST_GROUP_OFF + 3]
        flags     = HBPF_TGID_TS2 if ts == 2 else 0x00
        ipsc_stream = data[GV_IPSC_SEQ_OFF] if len(data) > GV_IPSC_SEQ_OFF else None

        if self._call_lock_blocks(ts, 'IPSC', src_sub, dst_group, ipsc_stream, burst_type == VOICE_HEAD, burst_type == VOICE_TERM):
            return

        if self._should_suppress_ipsc_reflection(data, ts, burst_type, src_sub, dst_group):
            return

        self._ipsc_busy_last[ts] = time()

        # Learn call metadata so we echo the same values back inbound
        if len(data) >= 17:
            self._peer_call_type = data[12:13]
            self._peer_call_ctrl = data[13:17]

        if burst_type == VOICE_HEAD:
            if self._out_stream_id[ts] is None:
                self._out_stream_id[ts] = os.urandom(4)
                log.info('IPSC call start: src=%d  tg=%d  ts=%d  stream=%s',
                         int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'),
                         ts, self._out_stream_id[ts].hex())
            else:
                # Motorola radios fire VOICE_HEAD twice at call start — once on LC
                # detection, once confirmed. MMDVMHost absorbs this at the driver
                # layer; we see it raw over IPSC. Reuse the existing stream_id so
                # HBlink doesn't flag stream contention.
                log.debug('Duplicate VOICE_HEAD ts=%d — keeping stream=%s',
                          ts, self._out_stream_id[ts].hex())
            self._out_frame_pos[ts] = 0
            lc = LC_OPT + dst_group + src_sub
            self._out_lc[ts]     = lc
            self._out_emb_lc[ts] = bptc.encode_emblc(lc)
            full_lc = bptc.encode_header_lc(lc)
            frame_bits = (
                full_lc[0:98]
                + SLOT_TYPE['VOICE_LC_HEAD'][:10]
                + BS_DATA_SYNC
                + SLOT_TYPE['VOICE_LC_HEAD'][-10:]
                + full_lc[98:]
            )
            payload_33 = frame_bits.tobytes()
            flags |= HBPF_FRAMETYPE_DATASYNC | HBPF_SLT_VHEAD

        elif burst_type == VOICE_TERM:
            if self._out_stream_id[ts] is None:
                return
            lc = self._out_lc[ts] if self._out_lc[ts] else LC_OPT + dst_group + src_sub
            full_lc = bptc.encode_terminator_lc(lc)
            frame_bits = (
                full_lc[0:98]
                + SLOT_TYPE['VOICE_LC_TERM'][:10]
                + BS_DATA_SYNC
                + SLOT_TYPE['VOICE_LC_TERM'][-10:]
                + full_lc[98:]
            )
            payload_33 = frame_bits.tobytes()
            flags |= HBPF_FRAMETYPE_DATASYNC | HBPF_SLT_VTERM

        else:  # SLOT1_VOICE or SLOT2_VOICE
            if self._out_stream_id[ts] is None:
                # Late entry: IPSC Burst E (byte 32 == 0x16) carries dst_group and
                # src_sub in the header, giving us enough to reconstruct the LC word
                # and resume forwarding mid-stream. All other burst types lack
                # unambiguous position information so we keep waiting.
                if data[32] != 0x16:
                    return
                lc = LC_OPT + dst_group + src_sub
                self._out_stream_id[ts] = os.urandom(4)
                self._out_lc[ts]        = lc
                self._out_emb_lc[ts]    = bptc.encode_emblc(lc)
                self._out_frame_pos[ts] = 4  # Burst E is superframe position 4
                log.info('IPSC late entry: ts=%d src=%d tg=%d — LC from Burst E, stream=%s',
                         ts, int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'),
                         self._out_stream_id[ts].hex())
            if len(data) < 52:
                log.warning('SLOT_VOICE too short for AMBE: %d bytes', len(data))
                return

            # Extract 3×49-bit AMBE from IPSC bytes 33–51
            raw_ba = bitarray(endian='big')
            raw_ba.frombytes(data[33:52])
            a1_72 = _ambe49_to_72(raw_ba[0:49])
            a2_72 = _ambe49_to_72(raw_ba[50:99])
            a3_72 = _ambe49_to_72(raw_ba[100:149])

            pos   = self._out_frame_pos[ts] % 6
            embed = self._build_embed(pos, self._out_emb_lc[ts])
            frame_bits = a1_72 + a2_72[:36] + embed + a2_72[36:] + a3_72
            payload_33 = frame_bits.tobytes()
            flags |= HBPF_FRAMETYPE_VOICESYNC if pos == 0 else (HBPF_FRAMETYPE_VOICE | pos)
            self._out_frame_pos[ts] += 1

        dmrd = (
            HBPF_DMRD
            + bytes([self._out_seq])
            + src_sub
            + dst_group
            + self._repeater_id_b
            + bytes([flags])
            + self._out_stream_id[ts]
            + payload_33
            + b'\x00\x00'   # BER + RSSI (synthesised, no RF measurement)
        )
        self._out_seq = (self._out_seq + 1) & 0xFF
        self._hbp.send_dmrd(dmrd)
        log.debug('→ HBP DMRD  burst=0x%02x  ts=%d  flags=0x%02x', burst_type, ts, flags)

        if burst_type == VOICE_TERM:
            log.info('IPSC call end:   src=%d  tg=%d  ts=%d',
                     int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'), ts)
            self._out_stream_id[ts] = None
            self._out_lc[ts]        = None
            self._out_emb_lc[ts]    = None
            self._release_call_lock(ts, 'IPSC', src_sub, dst_group)

    def _build_embed(self, pos: int, emb_lc) -> bitarray:
        """Build the 48-bit EMBED field for superframe position 0–5."""
        if pos == 0:
            return BS_VOICE_SYNC
        name    = _EMB_BURST_NAMES[pos - 1]
        lc_bits = emb_lc.get(pos, _NULL_EMB_LC) if emb_lc and pos <= 4 else _NULL_EMB_LC
        return EMB[name][:8] + lc_bits + EMB[name][-8:]

    # ------------------------------------------------------------------
    # HBP callbacks
    # ------------------------------------------------------------------

    def hbp_connected(self):
        log.info('HBP connected')

    def hbp_disconnected(self):
        log.warning('HBP disconnected')
        self._out_stream_id = {1: None, 2: None}
        self._out_lc        = {1: None, 2: None}
        self._out_emb_lc    = {1: None, 2: None}
        self._out_last_pkt  = {1: 0.0, 2: 0.0}
        self._in_lc         = {1: None, 2: None}
        self._in_emb_lc     = {1: None, 2: None}
        self._in_stream_id  = {1: 0, 2: 0}
        self._in_hbp_stream = {1: None, 2: None}
        self._in_src_sub    = {1: None, 2: None}
        self._in_dst_group  = {1: None, 2: None}
        self._in_last_head  = {1: 0.0, 2: 0.0}
        self._in_last_pkt   = {1: 0.0, 2: 0.0}
        self._in_started    = {1: False, 2: False}
        self._clear_ipsc_tx_queues()
        self._reflect_recent = {1: [], 2: []}
        self._reflect_ignore = {1: {}, 2: {}}

    def hbp_voice_received(self, dmrd: bytes):
        """Inbound HBP -> IPSC.

        This path now follows the older IPSC_Bridge/AMBE_IPSC behavior more
        closely:

        * a new HBP call allocates one IPSC stream byte for the whole call;
        * IPSC RTP sequence starts at a random value per call;
        * the RF side is not keyed on an isolated HBP VOICE_HEAD.  We wait for
          the first voice frame, then send repeated IPSC VOICE_HEAD packets and
          continue with voice at a paced cadence;
        * broad reverse-direction suppression is avoided; slot-busy arbitration
          decides whether HBP-originated traffic may transmit.
        """
        if not self._ipsc.is_peer_registered():
            return
        if len(dmrd) < DMRD_LEN:
            return

        src_sub     = dmrd[DMRD_SRC_OFF  : DMRD_SRC_OFF  + 3]
        dst_group   = dmrd[DMRD_DST_OFF  : DMRD_DST_OFF  + 3]
        flags       = dmrd[DMRD_FLAGS_OFF]
        hbp_stream  = dmrd[16:20]
        payload_33  = dmrd[DMRD_PAYLOAD_OFF : DMRD_PAYLOAD_OFF + 33]

        ts         = 2 if (flags & HBPF_TGID_TS2) else 1
        now        = time()
        self._in_last_pkt[ts] = now
        frame_type = flags & HBPF_FRAMETYPE_MASK
        dtype      = flags & HBPF_DTYPE_MASK
        call_info  = TS_CALL_MSK if ts == 2 else 0x00
        slot_burst = SLOT2_VOICE if ts == 2 else SLOT1_VOICE

        is_head = frame_type == HBPF_FRAMETYPE_DATASYNC and dtype == HBPF_SLT_VHEAD
        is_term = frame_type == HBPF_FRAMETYPE_DATASYNC and dtype == HBPF_SLT_VTERM
        # A voice burst without an active HBP call is a late-entry start.
        is_hbp_start = is_head or (not is_term and self._in_lc[ts] is None)

        if self._call_lock_blocks(ts, 'HBP', src_sub, dst_group, hbp_stream, is_hbp_start, is_term):
            return

        if is_head:
            lc = self._decode_hbp_voice_lc(payload_33)
            same_hbp_stream = self._in_hbp_stream[ts] == hbp_stream
            same_call_tuple = (
                self._in_lc[ts] is not None
                and self._in_src_sub[ts] == src_sub
                and self._in_dst_group[ts] == dst_group
            )
            duplicate_head = bool(
                self._in_lc[ts] is not None
                and (same_hbp_stream or (same_call_tuple and (now - self._in_last_head[ts]) <= 1.5))
            )

            if duplicate_head:
                self._in_lc[ts] = lc
                self._in_emb_lc[ts] = bptc.encode_emblc(lc)
                self._in_last_head[ts] = now
                log.debug('Duplicate HBP VOICE_HEAD ts=%d hbp_stream=%s src=%d tg=%d - keeping IPSC stream_id=0x%02x',
                          ts, hbp_stream.hex(), int.from_bytes(src_sub, 'big'),
                          int.from_bytes(dst_group, 'big'), self._in_stream_id[ts])
                return

            if self._in_hbp_stream[ts] is not None and self._in_lc[ts] is not None and self._in_started[ts]:
                log.warning('HBP VOICE_HEAD interrupted active call: ts=%d old_stream=%s new_stream=%s',
                            ts, self._in_hbp_stream[ts].hex(), hbp_stream.hex())
                self._reset_hbp_to_ipsc_state(ts)

            self._in_lc[ts]         = lc
            self._in_emb_lc[ts]     = bptc.encode_emblc(lc)
            self._in_hbp_stream[ts] = hbp_stream
            self._in_src_sub[ts]    = src_sub
            self._in_dst_group[ts]  = dst_group
            self._in_last_head[ts]  = now
            self._in_started[ts]    = False
            self._in_stream_ctr     = (self._in_stream_ctr + 1) & 0xFF
            self._in_stream_id[ts]  = self._in_stream_ctr
            self._in_rtp_seq[ts]    = randint(0, 0x7FFF)
            self._in_rtp_ts[ts]     = randint(0, 0xFFFFFFFF)

            log.info('HBP call start: src=%d  tg=%d  ts=%d  stream=%s',
                     int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'), ts,
                     hbp_stream.hex())

            if not getattr(self._cfg, 'hbp_start_on_voice', True):
                self._emit_hbp_ipsc_start(ts, src_sub, dst_group, call_info)
            return

        if is_term:
            if not self._in_started[ts]:
                if self._in_lc[ts] is not None:
                    log.debug('Dropping HBP header-only call on ts=%d stream=%s; no voice before terminator',
                              ts, hbp_stream.hex())
                self._release_call_lock(ts, 'HBP', src_sub, dst_group)
                self._reset_hbp_to_ipsc_state(ts)
                return

            lc = self._in_lc[ts] if self._in_lc[ts] else LC_OPT + dst_group + src_sub
            call_info |= END_MSK
            gv_payload = bytes([VOICE_TERM]) + _build_ipsc_voice_payload(lc, VOICE_TERM)
            packet = self._make_hbp_ipsc_packet(ts, src_sub, dst_group, call_info, 0x5e, gv_payload)
            self._send_hbp_to_ipsc(ts, packet, src_sub, dst_group, VOICE_TERM)
            log.info('HBP call end:   src=%d  tg=%d  ts=%d  stream=%s',
                     int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'), ts,
                     hbp_stream.hex())
            self._release_call_lock(ts, 'HBP', src_sub, dst_group)
            self._reset_hbp_to_ipsc_state(ts)
            return

        # VOICESYNC (burst A) or VOICE (bursts B-F)
        if self._in_lc[ts] is None:
            # Late entry: src_sub and dst_group are in every DMRD header so we
            # can reconstruct a valid LC word immediately from any voice burst.
            lc = LC_OPT + dst_group + src_sub
            self._in_lc[ts]         = lc
            self._in_emb_lc[ts]     = bptc.encode_emblc(lc)
            self._in_hbp_stream[ts] = hbp_stream
            self._in_src_sub[ts]    = src_sub
            self._in_dst_group[ts]  = dst_group
            self._in_last_head[ts]  = now
            self._in_started[ts]    = False
            self._in_stream_ctr     = (self._in_stream_ctr + 1) & 0xFF
            self._in_stream_id[ts]  = self._in_stream_ctr
            self._in_rtp_seq[ts]    = randint(0, 0x7FFF)
            self._in_rtp_ts[ts]     = randint(0, 0xFFFFFFFF)
            log.info('HBP late entry: ts=%d src=%d tg=%d - LC from stream, hbp_stream=%s',
                     ts, int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'),
                     hbp_stream.hex())

        if not self._in_started[ts]:
            self._emit_hbp_ipsc_start(ts, src_sub, dst_group, call_info)

        ambe_19 = _extract_ambe_from_dmrd(payload_33)
        if frame_type == HBPF_FRAMETYPE_VOICESYNC:
            # Burst A: 52 bytes total.  byte31=0x14 (len=20), byte32=0x40.
            gv_payload = bytes([slot_burst]) + b'\x14\x40' + ambe_19
        elif dtype == 4:
            # Burst E carries embedded LC fragment 4 and a copy of src/dst.
            emb_frag = (self._in_emb_lc[ts][4].tobytes()
                        if self._in_emb_lc[ts] and 4 in self._in_emb_lc[ts]
                        else _NULL_EMB_LC.tobytes())
            lc_prefix = self._in_lc[ts][0:3] if self._in_lc[ts] else b'\x00\x00\x00'
            gv_payload = (bytes([slot_burst]) + b'\x22\x16' + ambe_19
                          + emb_frag + lc_prefix + dst_group + src_sub + b'\x14')
        elif dtype >= 5:
            gv_payload = bytes([slot_burst]) + b'\x19\x06' + ambe_19 + b'\x00\x00\x00\x00\x10'
        else:
            pos      = max(dtype, 1)
            emb_frag = (self._in_emb_lc[ts][pos].tobytes()
                        if self._in_emb_lc[ts] and pos in self._in_emb_lc[ts]
                        else _NULL_EMB_LC.tobytes())
            emb_hdr  = EMB[_EMB_BURST_NAMES[pos - 1]][:8].tobytes()[0] & 0xFE
            gv_payload = bytes([slot_burst]) + b'\x19\x06' + ambe_19 + emb_frag + bytes([emb_hdr])

        packet = self._make_hbp_ipsc_packet(ts, src_sub, dst_group, call_info, 0x5d, gv_payload)
        self._send_hbp_to_ipsc(ts, packet, src_sub, dst_group, slot_burst)
        log.debug('<- IPSC GV  burst=0x%02x  ts=%d  dtype=%d', slot_burst, ts, dtype)

    def _decode_hbp_voice_lc(self, payload_33: bytes) -> bytes:
        """Decode the BPTC-encoded 9-byte LC from a HBP DMRD VOICE_HEAD frame."""
        frame_bits = bitarray(endian='big')
        frame_bits.frombytes(payload_33)
        # The 196-bit BPTC codeword is split around slot type + sync in the
        # 264-bit DMR frame: first half [0:98], second half [166:264].
        bptc_bits = frame_bits[0:98] + frame_bits[166:264]
        return bptc.decode_full_lc(bptc_bits).tobytes()

    def _emit_hbp_ipsc_start(self, ts: int, src_sub: bytes, dst_group: bytes, call_info: int) -> None:
        """Emit repeated IPSC VOICE_HEAD packets at actual voice start.

        IPSC_Bridge/AMBE_IPSC sends three HEAD frames before voice.  We do the
        same here, but only once voice has actually arrived unless configured
        otherwise.  This prevents a lone early HBP header from keying RF and
        then dropping before audio.
        """
        if self._in_started[ts]:
            return
        lc = self._in_lc[ts] if self._in_lc[ts] else LC_OPT + dst_group + src_sub
        count = max(1, min(5, getattr(self._cfg, 'hbp_to_ipsc_header_repeats', 3)))
        for idx in range(count):
            rtp_pt = 0xdd if idx == 0 else 0x5d
            gv_payload = bytes([VOICE_HEAD]) + _build_ipsc_voice_payload(lc, VOICE_HEAD)
            packet = self._make_hbp_ipsc_packet(ts, src_sub, dst_group, call_info, rtp_pt, gv_payload)
            self._send_hbp_to_ipsc(ts, packet, src_sub, dst_group, VOICE_HEAD)
        self._in_started[ts] = True

    def _make_hbp_ipsc_packet(
        self,
        ts: int,
        src_sub: bytes,
        dst_group: bytes,
        call_info: int,
        rtp_pt: int,
        gv_payload: bytes,
    ) -> bytes:
        rtp_seq_b = struct.pack('>H', self._in_rtp_seq[ts] & 0xFFFF)
        rtp_ts_b  = struct.pack('>I', self._in_rtp_ts[ts]  & 0xFFFFFFFF)
        self._in_rtp_seq[ts] = (self._in_rtp_seq[ts] + 1) & 0xFFFF
        self._in_rtp_ts[ts]  = (self._in_rtp_ts[ts] + 480) & 0xFFFFFFFF
        rtp_hdr = b'\x80' + bytes([rtp_pt]) + rtp_seq_b + rtp_ts_b + b'\x00\x00\x00\x00'
        return self._build_gv(src_sub, dst_group, call_info, rtp_hdr, gv_payload, self._in_stream_id[ts])

    def _send_hbp_to_ipsc(self, ts: int, packet: bytes, src_sub: bytes, dst_group: bytes, burst_type: int) -> None:
        """Send or queue a HBP-originated IPSC packet with optional real-time pacing."""
        if not getattr(self._cfg, 'hbp_to_ipsc_pacing', True):
            self._send_hbp_to_ipsc_now(ts, packet, src_sub, dst_group, burst_type)
            return
        queue_limit = max(10, getattr(self._cfg, 'hbp_to_ipsc_queue_limit', 512))
        q = self._ipsc_tx_queue[ts]
        if len(q) >= queue_limit:
            log.warning('Dropping HBP->IPSC packet on ts=%d: pacing queue full (%d)', ts, queue_limit)
            return
        q.append((packet, src_sub, dst_group, burst_type))
        if self._ipsc_tx_handle[ts] is None:
            self._drain_hbp_to_ipsc_queue(ts)

    def _drain_hbp_to_ipsc_queue(self, ts: int) -> None:
        self._ipsc_tx_handle[ts] = None
        q = self._ipsc_tx_queue[ts]
        if not q:
            return
        packet, src_sub, dst_group, burst_type = q.popleft()
        self._send_hbp_to_ipsc_now(ts, packet, src_sub, dst_group, burst_type)
        if q:
            loop = asyncio.get_event_loop()
            delay = max(0.010, getattr(self._cfg, 'hbp_to_ipsc_frame_interval_ms', 60) / 1000.0)
            self._ipsc_tx_handle[ts] = loop.call_later(delay, self._drain_hbp_to_ipsc_queue, ts)
        else:
            self._hbp_ipsc_queue_drained(ts)

    def _send_hbp_to_ipsc_now(self, ts: int, packet: bytes, src_sub: bytes, dst_group: bytes, burst_type: int) -> None:
        if self._is_ipsc_slot_busy(ts):
            log.info('Slot %d is busy from IPSC; dropping HBP->IPSC packet src=%d tg=%d burst=0x%02x',
                     ts, int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'), burst_type)
            return
        self._ipsc.send_to_peer(packet)
        self._remember_hbp_to_ipsc(ts, src_sub, dst_group)

    def _is_ipsc_slot_busy(self, ts: int) -> bool:
        if not getattr(self._cfg, 'ipsc_busy_slot_guard', True):
            return False
        holdoff = max(0, getattr(self._cfg, 'ipsc_busy_holdoff_ms', 100)) / 1000.0
        if holdoff <= 0:
            return False
        return (time() - self._ipsc_busy_last[ts]) < holdoff

    def _clear_ipsc_tx_queues(self) -> None:
        for ts in (1, 2):
            handle = self._ipsc_tx_handle.get(ts)
            if handle is not None:
                handle.cancel()
                self._ipsc_tx_handle[ts] = None
            self._ipsc_tx_queue[ts].clear()

    def _reset_hbp_to_ipsc_state(self, ts: int) -> None:
        self._in_lc[ts] = None
        self._in_emb_lc[ts] = None
        self._in_hbp_stream[ts] = None
        self._in_src_sub[ts] = None
        self._in_dst_group[ts] = None
        self._in_last_head[ts] = 0.0
        self._in_started[ts] = False

    def _remember_hbp_to_ipsc(self, ts: int, src_sub: bytes, dst_group: bytes) -> None:
        """Remember HBP-originated traffic so PEER mode can suppress master echoes."""
        if not getattr(self._cfg, 'ipsc_reflect_suppression', False):
            return
        window = max(0, getattr(self._cfg, 'ipsc_reflect_window', 8))
        if window <= 0:
            return
        now = time()
        expires = now + window
        entries = [e for e in self._reflect_recent[ts] if e[2] > now]
        # Collapse repeated packets from the same call into one expiry entry.
        updated = False
        for idx, (src, dst, _old_expires) in enumerate(entries):
            if src == src_sub and dst == dst_group:
                entries[idx] = (src_sub, dst_group, expires)
                updated = True
                break
        if not updated:
            entries.append((src_sub, dst_group, expires))
        self._reflect_recent[ts] = entries

    def _call_lock_blocks(
        self,
        ts: int,
        direction: str,
        src_sub: bytes,
        dst_group: bytes,
        stream_key,
        is_start: bool,
        is_end: bool,
    ) -> bool:
        """Return True when a packet should be dropped by call-direction lock.

        The lock is per timeslot.  When a call is active in one direction, a
        same-talkgroup call in the opposite direction is treated as a reflection
        or collision and is ignored until the active call ends.  This is more
        precise than suppressing every later packet with the same src/tg/ts.
        """
        if not getattr(self._cfg, 'ipsc_call_lock', True):
            return False

        now = time()
        self._prune_call_lock_blocks(ts, now)

        block_for = max(10.0, getattr(self._cfg, 'ipsc_call_lock_hang_ms', 250) / 1000.0 + 2.0)

        # If a stream was already rejected because it collided with the current
        # owner, keep dropping that stream through its terminator even if the
        # owner clears while the rejected stream is still arriving.
        if direction == 'IPSC' and stream_key is not None:
            blocked = self._call_lock_blocked_ipsc[ts]
            if stream_key in blocked:
                if is_end:
                    blocked.pop(stream_key, None)
                else:
                    blocked[stream_key] = now + block_for
                return True
        elif direction == 'HBP' and stream_key is not None:
            blocked = self._call_lock_blocked_hbp[ts]
            if stream_key in blocked:
                if is_end:
                    blocked.pop(stream_key, None)
                else:
                    blocked[stream_key] = now + block_for
                return True

        owner = self._call_lock_owner[ts]
        if owner is not None and not self._call_lock_is_active(ts, now):
            self._clear_call_lock(ts)
            owner = None

        if owner is None:
            # First packet on this slot establishes the current owner.  For HBP,
            # this is usually VOICE_HEAD; for late-entry it can be the first
            # voice burst.  For IPSC, this is normally VOICE_HEAD.
            self._acquire_call_lock(ts, direction, src_sub, dst_group, now)
            return False

        if owner == direction:
            # Same direction continues the current call.  Keep the original TG
            # for matching unless this is a fresh start after the previous owner
            # has already been cleared.
            return False

        same_tg_only = getattr(self._cfg, 'ipsc_call_lock_same_tg_only', True)
        owner_tg = self._call_lock_tg[ts]
        same_tg = (owner_tg == dst_group)
        if same_tg_only and not same_tg:
            return False

        # Opposite direction while the owner is active: block it.  If this is a
        # call start, remember its stream so the rest of that rejected call does
        # not leak through after the owner releases.
        expires = now + block_for
        if direction == 'IPSC' and stream_key is not None:
            self._call_lock_blocked_ipsc[ts][stream_key] = expires
        elif direction == 'HBP' and stream_key is not None:
            self._call_lock_blocked_hbp[ts][stream_key] = expires

        log.info(
            'Call lock holding %s call on ts=%d tg=%d; dropping inverse %s packet src=%d tg=%d%s',
            owner,
            ts,
            int.from_bytes(owner_tg or b'\x00\x00\x00', 'big'),
            direction,
            int.from_bytes(src_sub, 'big'),
            int.from_bytes(dst_group, 'big'),
            ' start' if is_start else '',
        )
        return True

    def _acquire_call_lock(self, ts: int, direction: str, src_sub: bytes, dst_group: bytes, now: float) -> None:
        if not getattr(self._cfg, 'ipsc_call_lock', True):
            return
        self._call_lock_owner[ts] = direction
        self._call_lock_src[ts] = src_sub
        self._call_lock_tg[ts] = dst_group
        self._call_lock_until[ts] = 0.0
        log.debug('Call lock acquired: owner=%s ts=%d src=%d tg=%d',
                  direction, ts, int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'))

    def _release_call_lock(self, ts: int, direction: str, src_sub: bytes | None = None, dst_group: bytes | None = None) -> None:
        if not getattr(self._cfg, 'ipsc_call_lock', True):
            return
        if self._call_lock_owner[ts] != direction:
            return
        hang = max(0, getattr(self._cfg, 'ipsc_call_lock_hang_ms', 250)) / 1000.0
        self._call_lock_until[ts] = time() + hang
        log.debug('Call lock released by %s: ts=%d hang_ms=%d', direction, ts, int(hang * 1000))

    def _call_lock_is_active(self, ts: int, now: float | None = None) -> bool:
        if now is None:
            now = time()
        owner = self._call_lock_owner[ts]
        if owner is None:
            return False
        if owner == 'IPSC' and self._out_stream_id[ts] is not None:
            return True
        if owner == 'HBP' and (self._in_lc[ts] is not None or self._in_started[ts] or bool(self._ipsc_tx_queue[ts])):
            return True
        return now <= self._call_lock_until[ts]

    def _clear_call_lock(self, ts: int) -> None:
        self._call_lock_owner[ts] = None
        self._call_lock_src[ts] = None
        self._call_lock_tg[ts] = None
        self._call_lock_until[ts] = 0.0

    def _clear_call_locks(self) -> None:
        for ts in (1, 2):
            self._clear_call_lock(ts)
            self._call_lock_blocked_ipsc[ts].clear()
            self._call_lock_blocked_hbp[ts].clear()

    def _prune_call_lock_blocks(self, ts: int, now: float) -> None:
        self._call_lock_blocked_ipsc[ts] = {
            sid: exp for sid, exp in self._call_lock_blocked_ipsc[ts].items() if exp > now
        }
        self._call_lock_blocked_hbp[ts] = {
            sid: exp for sid, exp in self._call_lock_blocked_hbp[ts].items() if exp > now
        }

    def _hbp_ipsc_queue_drained(self, ts: int) -> None:
        if self._call_lock_owner[ts] == 'HBP' and self._in_lc[ts] is None:
            hang = max(0, getattr(self._cfg, 'ipsc_call_lock_hang_ms', 250)) / 1000.0
            self._call_lock_until[ts] = time() + hang

    def _should_suppress_ipsc_reflection(
        self,
        data: bytes,
        ts: int,
        burst_type: int,
        src_sub: bytes,
        dst_group: bytes,
    ) -> bool:
        """Drop IPSC calls that are the upstream echo of our own HBP-originated call."""
        if not getattr(self._cfg, 'ipsc_reflect_suppression', False):
            return False
        if getattr(self._cfg, 'ipsc_mode', 'MASTER') != 'PEER':
            return False
        if len(data) <= GV_IPSC_SEQ_OFF:
            return False

        now = time()
        # Prune expired recent signatures.
        self._reflect_recent[ts] = [e for e in self._reflect_recent[ts] if e[2] > now]

        ipsc_stream = data[GV_IPSC_SEQ_OFF]
        window = max(1, getattr(self._cfg, 'ipsc_reflect_window', 8))
        self._reflect_ignore[ts] = {sid: exp for sid, exp in self._reflect_ignore[ts].items() if exp > now}

        if ipsc_stream in self._reflect_ignore[ts]:
            if burst_type == VOICE_TERM:
                self._reflect_ignore[ts].pop(ipsc_stream, None)
                log.info('Suppressed reflected IPSC call end: src=%d tg=%d ts=%d stream_id=0x%02x',
                         int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'), ts, ipsc_stream)
            else:
                self._reflect_ignore[ts][ipsc_stream] = now + window
            return True

        if burst_type != VOICE_HEAD:
            return False

        # Only suppress while the HBP-originated call is still active.  After it
        # ends, a new IPSC VOICE_HEAD with the same src/tg/ts is a legitimate
        # reverse-direction call and must be allowed.  This is closer to the
        # original IPSC_Bridge behavior, which used slot-busy arbitration rather
        # than long src/tg suppression windows.
        if not self._in_started[ts]:
            return False

        for seen_src, seen_dst, _expires in self._reflect_recent[ts]:
            if seen_src == src_sub and seen_dst == dst_group:
                self._reflect_ignore[ts][ipsc_stream] = now + window
                log.info('Suppressing reflected IPSC call from upstream: src=%d tg=%d ts=%d stream_id=0x%02x',
                         int.from_bytes(src_sub, 'big'), int.from_bytes(dst_group, 'big'), ts, ipsc_stream)
                return True
        return False

    def _build_gv(self, src_sub, dst_group, call_info, rtp_hdr, gv_payload, stream_id: int) -> bytes:
        """Assemble a complete GROUP_VOICE packet."""
        return (
            bytes([GROUP_VOICE])
            + self._master_id_b
            + bytes([stream_id])   # call stream ID — constant for the entire call
            + src_sub
            + dst_group
            + self._peer_call_type
            + self._peer_call_ctrl
            + bytes([call_info])
            + rtp_hdr
            + gv_payload
        )

    # ------------------------------------------------------------------
    # Watchdog support
    # ------------------------------------------------------------------

    def check_call_timeouts(self, timeout: float = 10.0):
        """
        Called by the IPSC watchdog every 5 s.  If a call stream has been active
        but silent for longer than `timeout` seconds (default 10 s — 2 watchdog
        ticks), log a warning and clear that timeslot's state so it can accept
        a new call.  This handles the case where VOICE_TERM is never received
        (RF dropout, firmware bug, lost packet).
        """
        now = time()
        for ts in (1, 2):
            if self._out_stream_id[ts] is not None:
                elapsed = now - self._out_last_pkt[ts]
                if elapsed > timeout:
                    log.warning(
                        'IPSC→HBP call timeout: ts=%d stream=%s — no voice for %.1fs, clearing',
                        ts, self._out_stream_id[ts].hex(), elapsed,
                    )
                    self._out_stream_id[ts] = None
                    self._out_lc[ts]        = None
                    self._out_emb_lc[ts]    = None
                    self._release_call_lock(ts, 'IPSC')
            if self._in_lc[ts] is not None:
                elapsed = now - self._in_last_pkt[ts]
                if elapsed > timeout:
                    log.warning(
                        'HBP→IPSC call timeout: ts=%d — no voice for %.1fs, clearing',
                        ts, elapsed,
                    )
                    self._release_call_lock(ts, 'HBP')
                    self._reset_hbp_to_ipsc_state(ts)

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    def is_hbp_connected(self) -> bool:
        return self._hbp is not None and self._hbp.is_connected()

    def is_ipsc_registered(self) -> bool:
        return self._ipsc is not None and self._ipsc.is_peer_registered()
