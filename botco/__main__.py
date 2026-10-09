"""Run the orchestrator: `botco --config config.toml`."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import xclient
from .agents import Runner
from .breakers import Breakers
from .company import Company
from .config import load
from .llm import LLM
from .policy import Policy
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
    # Approvals an older database recorded as the CEO's go to the first
    # owner.
    owners = Policy(cfg).holders("owner") if "owner" in cfg.roles else []
    store = Store(cfg.state_dir / "botco.sqlite3", owner=owners[0] if owners else "owner")
    team = Team(cfg, Breakers(cfg.breakers))
    world = World(cfg, store, team, xclient.make(cfg.x.dry_run, cfg.x.env_file))
    llms = {name: LLM(m) for name, m in cfg.models.items()}
    Company(world, Runner(world, llms)).run()


if __name__ == "__main__":
    main()
