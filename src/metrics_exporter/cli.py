"""``metrics-exporter serve`` runs the HTTP server; ``metrics-exporter scrape`` prints one scrape and exits."""

from __future__ import annotations

import argparse
import logging
import sys

from .dockerapi import DockerCollector
from .exporter import Exporter
from .server import ExporterServer
from .simulated import FleetCollector


def build_exporter(args) -> Exporter:
    collectors = {}
    if not args.no_sim:
        collectors["fleet"] = FleetCollector(seed=args.seed)
    if args.docker:
        collectors["docker"] = DockerCollector(args.docker_socket, timeout=args.docker_timeout, max_containers=args.max_containers)
    if not collectors:
        raise SystemExit("nothing to export: enable --docker or drop --no-sim")
    return Exporter(collectors)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="metrics-exporter", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-sim", action="store_true", help="do not export the simulated fleet")
    common.add_argument("--seed", type=int, default=7, help="seed for the simulated fleet (default 7)")
    common.add_argument("--docker", action="store_true", help="also export Docker container inventory")
    common.add_argument("--docker-socket", default="/var/run/docker.sock")
    common.add_argument("--docker-timeout", type=float, default=2.0, help="seconds (default 2)")
    common.add_argument("--max-containers", type=int, default=500, help="cap on per-container series (default 500)")
    s = sub.add_parser("serve", parents=[common], help="run the HTTP server")
    s.add_argument("--bind", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    s.add_argument("--port", type=int, default=9877)
    sub.add_parser("scrape", parents=[common], help="print one scrape to stdout")
    return p


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    exporter = build_exporter(a)
    if a.cmd == "scrape":
        out.write(exporter.scrape())
        return 0
    server = ExporterServer((a.bind, a.port), exporter)
    logging.getLogger("metrics_exporter").info("listening on http://%s:%d/metrics", *server.server_address[:2])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
