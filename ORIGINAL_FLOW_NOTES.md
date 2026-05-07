# Original-flow bridge behavior

This build changes the HBP->IPSC path to behave closer to the older
IPSC_Bridge/HB_Bridge stack:

- HBP VOICE_HEAD frames are accepted and stored, but IPSC is not keyed until a
  real voice burst arrives when `hbp_start_on_voice = true`.
- At actual voice start, the bridge sends repeated IPSC VOICE_HEAD packets before
  voice, using `hbp_to_ipsc_header_repeats = 3` by default.
- HBP-originated IPSC frames are paced with `hbp_to_ipsc_frame_interval_ms = 60`
  so buffered HBLink traffic is not dumped at the IPSC master in a burst.
- Inbound IPSC packets mark the timeslot busy. HBP->IPSC packets are dropped
  during `busy_holdoff_ms` if that slot is busy, matching the old IPSC_Bridge
  politeness behavior.
- `reflect_suppression` now defaults to `false`. The broad src/tg/timeslot
  suppression added during debug could block valid reverse-direction calls when
  the same radio ID talks back quickly from another repeater.

Recommended defaults:

```toml
[ipsc]
busy_slot_guard = true
busy_holdoff_ms = 100
reflect_suppression = false
reflect_window = 8

[hbp]
hbp_start_on_voice = true
hbp_to_ipsc_header_repeats = 3
hbp_to_ipsc_pacing = true
hbp_to_ipsc_frame_interval_ms = 60
hbp_to_ipsc_queue_limit = 512
```

If a specific upstream master definitely echoes this peer's own traffic and no
other filtering exists, `reflect_suppression` can still be enabled, but it is now
only applied while the HBP-originated call is active. Once that call ends, a new
IPSC VOICE_HEAD with the same source/talkgroup/timeslot is treated as a valid
reverse-direction call.


## Direction call lock

This build includes active-call direction locking. See `CALL_LOCK_NOTES.md` for configuration and expected log messages.
