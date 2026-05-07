#!/usr/bin/env python3
"""
ipsc2hbp entry point.

Wires IPSCProtocol/IPSCPeerProtocol, HBPClient, and CallTranslator together
and runs the asyncio event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import signal
import sys

from config import Config, load as load_config

_DEFAULT_CFG = pathlib.Path(__file__).parent / "ipsc2hbp.toml"


def _setup_logging(level: str) -> None:
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(fmt))
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setLevel(getattr(logging, level))
            handler.setFormatter(logging.Formatter(fmt))


def main() -> None:
    ap = argparse.ArgumentParser(description="IPSC to HomeBrew Protocol translator")
    ap.add_argument(
        "-c",
        "--config",
        default=str(_DEFAULT_CFG),
        help="Path to TOML config file (default: ipsc2hbp.toml next to this script)",
    )
    ap.add_argument("--log-level", dest="log_level", default=None, help="Override config log level")
    ap.add_argument("--wire", action="store_true", help="Log raw IPSC/HBP hex only; silence normal output")
    args = ap.parse_args()

    try:
        cfg: Config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"Configuration error: {exc}")

    if args.wire:
        logging.getLogger().setLevel(logging.WARNING)
        wire_handler = logging.StreamHandler(sys.stderr)
        wire_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        for name in ("wire.ipsc", "ipsc.wire", "wire.hbp", "hbp.wire"):
            wl = logging.getLogger(name)
            wl.setLevel(logging.DEBUG)
            wl.addHandler(wire_handler)
            wl.propagate = False
    else:
        log_level = args.log_level.upper() if args.log_level else cfg.log_level
        _setup_logging(log_level)

    log = logging.getLogger("ipsc2hbp")
    log.info(
        "ipsc2hbp starting - IPSC mode=%s source_id=%d master_id=%d peer_id=%d HBP %s:%d mode=%s",
        cfg.ipsc_mode,
        cfg.ipsc_source_id,
        cfg.ipsc_master_id,
        cfg.ipsc_peer_id,
        cfg.hbp_master_ip,
        cfg.hbp_master_port,
        cfg.hbp_mode,
    )

    from hbp.protocol import HBPClient
    from ipsc.protocol import IPSCPeerProtocol, IPSCProtocol
    from translate.translator import CallTranslator

    translator = CallTranslator(cfg)
    ipsc_proto = IPSCPeerProtocol(cfg, translator) if cfg.ipsc_mode == "PEER" else IPSCProtocol(cfg, translator)
    hbp_client = HBPClient(cfg, translator)
    translator.set_protocols(ipsc_proto, hbp_client)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(signum: int, frame) -> None:  # noqa: ANN001 - signal handler API
        log.info("Signal %d received - shutting down", signum)
        if hasattr(ipsc_proto, "stop"):
            ipsc_proto.stop()
        hbp_client.stop()
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _shutdown)

    ipsc_coro = loop.create_datagram_endpoint(
        lambda: ipsc_proto,
        local_addr=(cfg.ipsc_bind_ip, cfg.ipsc_bind_port),
    )

    try:
        loop.run_until_complete(ipsc_coro)
        log.info("IPSC %s endpoint up - %s:%d", cfg.ipsc_mode.lower(), cfg.ipsc_bind_ip, cfg.ipsc_bind_port)
        hbp_client.start(loop)
        loop.run_forever()
    except OSError as exc:
        sys.exit(f"Failed to bind IPSC socket: {exc}")
    finally:
        if hasattr(ipsc_proto, "stop"):
            ipsc_proto.stop()
        hbp_client.stop()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        log.info("ipsc2hbp stopped")
        loop.close()


if __name__ == "__main__":
    main()
