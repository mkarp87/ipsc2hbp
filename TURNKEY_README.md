# ipsc2hbp PEER turnkey build

This zip is a complete runtime copy of `ipsc2hbp` with PEER mode added while preserving the original MASTER mode.

## Default runtime config

The included `ipsc2hbp.toml` is already set up from the supplied Arapahoe-style parameters:

- IPSC mode: `PEER`
- IPSC local bind: `[ipsc] bind_ip` / `bind_port`
- IPSC peer ID: `[ipsc] ipsc_peer_id`
- IPSC upstream master: `[ipsc_upstream] master_ip` / `master_port`
- IPSC auth: `[ipsc] auth_enabled` / `auth_key`
- HBP upstream: `[hbp] master_ip` / `master_port`
- HBP repeater ID: `[hbp] hbp_repeater_id`
- HBP connection behavior: `[hbp] hbp_mode`

`arapahoe.toml`, `ipsc2hbp.toml`, `ipsc2hbp.toml.sample`, and `ipsc2hbp.peer.toml.sample` are the same PEER-mode configuration in this build.

The original local IPSC-master behavior is preserved in `ipsc2hbp.master.toml.sample` with:

```toml
[ipsc]
mode = "MASTER"
```

## Quick start

From the unzipped directory:

```bash
./setup_venv.sh
./run.sh
```

Or run manually:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python ipsc2hbp.py -c ipsc2hbp.toml
```

For raw packet logging during the first hardware test:

```bash
venv/bin/python ipsc2hbp.py -c ipsc2hbp.toml --wire
```

## Systemd install

After testing manually:

```bash
sudo ./install_systemd.sh
sudo systemctl status ipsc2hbp
journalctl -u ipsc2hbp -f
```

The installer writes a unit using the current directory as `WorkingDirectory` and the current user unless a username is passed:

```bash
sudo ./install_systemd.sh radio
```

## PEER-mode behavior

In PEER mode, ipsc2hbp:

1. Binds the local IPSC UDP socket.
2. Sends IPSC master registration to `[ipsc_upstream] master_ip:master_port`.
3. Uses the existing IPSC HMAC-SHA1 auth mechanism when `auth_enabled = true`.
4. Requests the full IPSC peer-list after upstream registration.
5. Maintains upstream master and listed-peer keepalives.
6. Translates IPSC group voice to HBP and HBP DMRD group voice back to IPSC.
7. Keeps the existing HBP `TRACKING` and `PERSISTENT` modes.

`keepalive_interval` and `max_missed` control PEER-mode loss detection. With `keepalive_interval = 5` and `max_missed = 3`, an unanswered upstream master is considered expired after about 15 seconds.

## Files of interest

- `ipsc2hbp.py` - application entrypoint
- `config.py` - TOML config loader and validation
- `ipsc/protocol.py` - MASTER and PEER IPSC protocol stacks
- `hbp/protocol.py` - HBP client stack
- `translate/translator.py` - IPSC/HBP voice translation
- `ipsc2hbp.toml` - default runtime config
- `ipsc2hbp.master.toml.sample` - old MASTER mode sample

## Debug/original-flow behavior

This build includes `ORIGINAL_FLOW_NOTES.md`. The HBP->IPSC path now waits for real voice before keying IPSC, sends repeated IPSC headers, paces HBP-originated frames at 60 ms by default, and uses IPSC slot-busy arbitration instead of broad reverse-direction suppression.


## Direction call lock

This build includes active-call direction locking. See `CALL_LOCK_NOTES.md` for configuration and expected log messages.
