"""Command line interface.

    fitbit discover     catalog the export
    fitbit ingest       parse into the silver layer
    fitbit transform    build gold/daily_facts
    fitbit report       render the HTML report
    fitbit all          run the four in order
    fitbit sync-auth     one-time Google Health API OAuth authorization
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import discover, ingest, report, transform
from .config import load_config
from .sync import auth as sync_auth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fitbit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", help="path to config yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("discover", "ingest", "transform", "all"):
        sub.add_parser(name)

    p_report = sub.add_parser("report")
    p_report.add_argument("-o", "--out", type=Path, default=None)

    sub.add_parser("sync-auth")

    args = parser.parse_args(argv)

    if args.command == "sync-auth":
        client = sync_auth.load_oauth_client()
        sync_auth.authorize(client)
        return 0

    cfg = load_config(args.config)

    if args.command == "discover":
        discover.run(cfg)
    elif args.command == "ingest":
        ingest.run(cfg)
    elif args.command == "transform":
        transform.run(cfg)
    elif args.command == "report":
        report.run(cfg, args.out)
    elif args.command == "all":
        catalog = discover.run(cfg)
        ingest.run(cfg, catalog)
        transform.run(cfg)
        report.run(cfg)

    return 0


if __name__ == "__main__":
    sys.exit(main())
