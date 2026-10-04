"""Run the orchestrator: `botco --config config.toml`."""

from __future__ import annotations

import argparse
import logging
import queue
from pathlib import Path

from . import xclient
from .agents import Runner
from .breakers import Breakers
from .company import Company
from .config import load
from .llm import LLM
from .store import Store
from .team import Team
from .world import World


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=Path("config.toml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The zulip library logs every long-poll at INFO.
    logging.getLogger("zulip").setLevel(logging.WARNING)

    cfg = load(args.config)
    inbox: queue.Queue = queue.Queue()
    store = Store(cfg.state_dir / "botco.sqlite3")
    team = Team(cfg, Breakers(cfg.breakers))
    world = World(cfg, store, team, xclient.make(cfg.x.dry_run, cfg.x.env_file))
    Company(world, Runner(world, LLM(cfg.llm), inbox), inbox).run()


if __name__ == "__main__":
    main()
