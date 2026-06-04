"""One-shot: load categories.yaml and print summary. Tables themselves don't
need pre-seeding — categories are attached to markets/wallets as they arrive."""

from polybot.logging import get_logger
from polybot.yaml_config import categories_cfg

log = get_logger(__name__)


def main() -> None:
    cfg = categories_cfg.get().get("categories", {})
    enabled = [c for c, v in cfg.items() if v.get("enabled")]
    log.info("seed_categories", enabled=enabled, total=len(cfg))


if __name__ == "__main__":
    main()
