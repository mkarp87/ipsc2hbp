# IPSC PEER mode for ipsc2hbp

This build adds `[ipsc].mode = "PEER"` while preserving the original `[ipsc].mode = "MASTER"` behavior.

## Modes

### MASTER

Existing behavior. A local Motorola repeater is configured with this host as its IPSC master. Use `ipsc2hbp.master.toml.sample` as the starting config.

### PEER

New behavior. ipsc2hbp registers to an upstream IPSC master, which may be a MotoTRBO repeater or c-Bridge, then continues translating IPSC group voice traffic to and from HBP. Use `ipsc2hbp.toml` or `ipsc2hbp.peer.toml.sample` as the starting config.

The HBP side is unchanged:

- `hbp_mode = "TRACKING"` activates HBP after upstream IPSC registration and deactivates when the IPSC side is lost.
- `hbp_mode = "PERSISTENT"` keeps HBP connected independently of IPSC registration.

## PEER-mode IPSC flow

The PEER stack performs the normal IPSC peer flow:

1. Send `MASTER_REG_REQ` to `[ipsc_upstream] master_ip:master_port`.
2. Accept `MASTER_REG_REPLY`, learning the upstream master ID automatically unless `[ipsc_upstream] master_id` is set.
3. Send `MASTER_ALIVE_REQ` keepalives.
4. Request the full peer-list with `PEER_LIST_REQ` after upstream registration.
5. Parse `PEER_LIST_REPLY` entries as:
   - peer ID, 4 bytes
   - IPv4 address, 4 bytes
   - UDP port, 2 bytes
   - mode byte, 1 byte
6. Exchange `PEER_REG_REQ` / `PEER_REG_REPLY` and `PEER_ALIVE_REQ` / `PEER_ALIVE_REPLY` with listed peers.
7. Send outbound IPSC voice packets to the upstream master and to connected listed peers.

If a c-Bridge or NAT setup returns an incomplete peer-list, `peer_list_allow_unknown = true` allows wildcard fallback for peer-side registration, keepalive, and voice packets. The default in PEER mode is `true`.

## Important config keys

```toml
[ipsc]
mode = "PEER"
bind_ip = "10.255.0.254"
bind_port = 55019
ipsc_peer_id = 19
ipsc_master_id = 19
allowed_peer_ip = ""
auth_enabled = true
auth_key = "...hex key..."
keepalive_interval = 5
max_missed = 3
keepalive_watchdog = 60

[ipsc_upstream]
master_ip = "10.100.10.11"
master_port = 55002
# Optional: master_id = 1234567

[hbp]
master_ip = "127.0.0.1"
master_port = 62019
hbp_repeater_id = 31000119
hbp_mode = "TRACKING"
```

Use `[ipsc_upstream] master_id = 0` or omit it to learn the upstream master ID from the first valid reply from the configured upstream IP. Set a non-zero value to enforce the expected master ID.

`keepalive_interval` and `max_missed` control PEER-mode upstream loss detection. `keepalive_watchdog` remains available for legacy MASTER mode.

## Running

```bash
./setup_venv.sh
./run.sh
```

For first hardware testing:

```bash
venv/bin/python ipsc2hbp.py -c ipsc2hbp.toml --wire
```

## Notes

- Generated IPSC voice packets use `ipsc_source_id`, which is the local master ID in MASTER mode and the local peer ID in PEER mode.
- PEER mode sends traffic to the upstream master even when the peer-list is empty. This supports c-Bridge deployments that do not expose a full mesh peer-list.
- Live hardware validation should be done against the target MotoTRBO repeater or c-Bridge with `--wire` enabled for first registration testing.


## Debug update: HBLink parrot / duplicate VHEAD handling

HBLink parrot and some HBP masters may replay multiple `VOICE_HEAD` DMRD frames for one stream. PEER mode now keeps duplicate HBP `VOICE_HEAD` frames inside the same IPSC stream ID and only marks the first one as the RTP call-start packet. This prevents the RF repeater from keying, dropping, and keying again before audio.

PEER mode also includes reflected-call suppression. If the upstream IPSC master echoes an HBP-originated call back to this peer, the bridge suppresses that reflected IPSC stream so it is not fed back into HBLink or a parrot. Configure this under `[ipsc]` with `reflect_suppression` and `reflect_window`.

## Debug/original-flow behavior

This build includes `ORIGINAL_FLOW_NOTES.md`. The HBP->IPSC path now waits for real voice before keying IPSC, sends repeated IPSC headers, paces HBP-originated frames at 60 ms by default, and uses IPSC slot-busy arbitration instead of broad reverse-direction suppression.


## Direction call lock

This build includes active-call direction locking. See `CALL_LOCK_NOTES.md` for configuration and expected log messages.
