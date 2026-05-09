# LEGACY_AMBE compatibility flow

This build adds a compatibility mode intended to behave closer to the old
`IPSC_Bridge.py` / `HB_Bridge.py` AMBE-centered architecture.

Enable it with:

```toml
[compat]
packet_flow = "LEGACY_AMBE"
```

The normal full-packet translator remains available with:

```toml
[compat]
packet_flow = "PACKET_TRANSLATOR"
```

## What LEGACY_AMBE changes

The old bridge did not try to preserve every full IPSC/HBP packet detail. It
used the received packet only to establish call metadata and extract AMBE voice,
then rebuilt outbound packets cleanly.

This compatibility mode follows that model inside the single app:

- one active stream per timeslot per direction;
- duplicate IPSC VOICE_HEAD packets are ignored instead of sent to HBP;
- IPSC -> HBP sends a controlled two-header start, similar to old HB output;
- HBP -> IPSC sends three IPSC headers, like old AMBE_IPSC;
- voice arriving without an active head is dropped rather than used for late-entry;
- stale or foreign stream packets are dropped, not merged into the active call;
- no jitter/reorder buffer is used;
- broad reflection suppression remains disabled by default;
- old-style 100 ms busy-slot arbitration is still available.

Recommended settings for high-latency IPSC sites:

```toml
[compat]
packet_flow = "LEGACY_AMBE"

[ipsc]
busy_slot_guard = true
busy_holdoff_ms = 100
reflect_suppression = false
reflect_window = 0
call_lock = false

[hbp]
hbp_mode = "PERSISTENT"
hbp_start_on_voice = false
hbp_to_ipsc_header_repeats = 3
hbp_to_ipsc_pacing = true
hbp_to_ipsc_frame_interval_ms = 60
hbp_to_ipsc_queue_limit = 64
```

## Expected logs

Duplicate/stale traffic should show as drops rather than as new calls or call
interruptions:

```text
Legacy flow: ignoring duplicate IPSC VOICE_HEAD ...
Legacy flow: dropping IPSC stale/foreign stream ...
Legacy flow: dropping HBP voice from stale/foreign stream ...
Legacy flow: dropping HBP VOICE_HEAD for foreign stream ...
```

Those logs are generally good on a long-range/high-latency path. They mean the
bridge is refusing to let late packets from an older stream corrupt the current
call.
