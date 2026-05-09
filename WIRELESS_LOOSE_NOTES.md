# Wireless loose legacy flow

This build is for marginal IPSC paths where audio drops but registration stays up.

It changes `LEGACY_AMBE` behavior to be closer to the old IPSC_Bridge/HB_Bridge
architecture:

* IPSC stream-byte changes at VOICE_HEAD reset metadata instead of being dropped.
* IPSC voice bursts are accepted while a call is active even if the stream byte changes.
* HBP voice bursts are accepted while a call is active even if the HBP stream ID changes.
* HBP terminators from stale streams are ignored so they cannot kill the active call.
* Call-lock rejected-stream memory was shortened from a 10 second minimum to roughly
  `call_lock_hang_ms + 1 second`.

Recommended profile for high-latency / wireless backhaul sites:

```toml
[compat]
packet_flow = "LEGACY_AMBE"

[ipsc]
reflect_suppression = false
reflect_window = 0

# Avoid self-drop when the upstream IPSC master echoes packets back while this
# bridge is still transmitting HBP -> IPSC.
busy_slot_guard = false
busy_holdoff_ms = 0

# Keep a short same-TG active-call lock so inverse-direction echoes do not become
# new calls while the current call is still active.
call_lock = true
call_lock_hang_ms = 100
call_lock_same_tg_only = true

[hbp]
hbp_mode = "PERSISTENT"
hbp_start_on_voice = false
hbp_to_ipsc_header_repeats = 3
hbp_to_ipsc_pacing = true
hbp_to_ipsc_frame_interval_ms = 60
hbp_to_ipsc_queue_limit = 64
```

Run production tests without `--wire` and with `log_level = "INFO"` or lower.
