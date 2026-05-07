# Direction call lock

This build adds a short active-call direction lock for PEER mode.

The lock is per timeslot and talkgroup by default.  When one side owns a call,
for example HBP/MMDVM -> IPSC, an inverse-direction IPSC -> HBP call on the same
talkgroup is treated as a reflection or collision and is dropped until the active
call ends.  This prevents a reflected upstream IPSC stream from stealing the slot
while the HBP-originated stream is still being transmitted.

This is intentionally different from broad reflected-call suppression.  It does
not suppress every later call with the same source/talkgroup/timeslot for several
seconds.  Once the active call and the short hang interval have cleared, a real
reverse-direction reply is allowed.

Recommended defaults:

```toml
[ipsc]
call_lock = true
call_lock_hang_ms = 250
call_lock_same_tg_only = true
reflect_suppression = false
```

Expected log line when the lock is doing its job:

```text
Call lock holding HBP call on ts=1 tg=27501; dropping inverse IPSC packet src=3159110 tg=27501 start
```

If a site needs stricter RF-like arbitration across the entire timeslot, set:

```toml
call_lock_same_tg_only = false
```
