# HBP-to-IPSC transmit pacer fix

This build fixes a timing bug in the HBP/MMDVM-to-IPSC path.

Previously, `hbp_to_ipsc_pacing = true` only paced packets that were already
queued behind a pending timer. Packets generated in one callback, such as the
three repeated IPSC VOICE_HEAD frames followed by the first voice frame, could
still leave in the same event-loop tick because the queue briefly drained to
empty between enqueues.

On a tolerant or low-latency IPSC path this may work, but a strict/high-latency
MotoTRBO path can interpret the burst/gap pattern as a broken voice stream.

This build makes pacing a per-timeslot cooldown:

- first packet may leave immediately;
- the next packet waits `hbp_to_ipsc_frame_interval_ms`;
- the cooldown is honored even if the queue briefly becomes empty;
- the default interval remains 60 ms.

Recommended high-latency site settings:

```toml
[global]
log_level = "INFO"

[ipsc]
reflect_suppression = false
busy_slot_guard = true
busy_holdoff_ms = 100
call_lock = true
call_lock_hang_ms = 250
call_lock_same_tg_only = true

[hbp]
hbp_mode = "PERSISTENT"
hbp_start_on_voice = false
hbp_to_ipsc_header_repeats = 3
hbp_to_ipsc_pacing = true
hbp_to_ipsc_frame_interval_ms = 60
hbp_to_ipsc_queue_limit = 64
```

Do not run production audio tests with `--wire` or per-packet DEBUG logging.
