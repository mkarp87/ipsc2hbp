# Installation from the turnkey zip

This build is intended to run directly from the unzipped directory.

## Requirements

- Python 3.11 or newer
- Python venv support (`python3-venv` on Debian/Ubuntu)
- Network reachability to the IPSC upstream master and HBP master

## 1. Unzip

Example location:

```bash
cd /opt/NC4ES-Bridges
unzip ipsc2hbp-peer-turnkey.zip
cd ipsc2hbp-peer
```

## 2. Create the virtual environment

```bash
./setup_venv.sh
```

This creates `venv/` and installs `requirements.txt`.

## 3. Review the config

The default runtime config is `ipsc2hbp.toml`. It uses the PEER-mode structure:

```toml
[ipsc]
mode = "PEER"

[ipsc_upstream]
master_ip = "..."
master_port = 55002
```

The original MASTER mode is still available by starting from `ipsc2hbp.master.toml.sample`.

## 4. Test manually

```bash
./run.sh
```

For first-time hardware testing, raw packet logging is often useful:

```bash
venv/bin/python ipsc2hbp.py -c ipsc2hbp.toml --wire
```

## 5. Install as a service

```bash
sudo ./install_systemd.sh
sudo systemctl status ipsc2hbp
journalctl -u ipsc2hbp -f
```

To force a specific service user:

```bash
sudo ./install_systemd.sh radio
```

## Updating config

Edit `ipsc2hbp.toml`, then restart:

```bash
sudo systemctl restart ipsc2hbp
```
