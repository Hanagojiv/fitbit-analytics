"""Configuration loading and layer paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_CANDIDATES = ("config.local.yaml", "config.yaml", "config.example.yaml")


@dataclass
class Config:
    takeout_root: Path
    data_dir: Path = Path("./data")
    timezone: str = "UTC"
    intraday_grains: list[str] = field(default_factory=lambda: ["daily", "hourly"])
    intraday_file_limit: int | None = None

    # --- layer paths -------------------------------------------------
    @property
    def bronze(self) -> Path:
        """Parsed-but-untransformed records, one parquet file per dataset."""
        return self.data_dir / "bronze"

    @property
    def silver(self) -> Path:
        """Conformed, deduplicated, typed records at their natural grain."""
        return self.data_dir / "silver"

    @property
    def gold(self) -> Path:
        """Analysis-ready wide tables, one row per day."""
        return self.data_dir / "gold"

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.parquet"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.bronze, self.silver, self.gold):
            p.mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from an explicit path, or the first default candidate found."""
    if path is not None:
        cfg_path = Path(path)
        if not cfg_path.exists():
            raise FileNotFoundError(f"Config not found: {cfg_path}")
    else:
        cfg_path = next((Path(c) for c in DEFAULT_CONFIG_CANDIDATES if Path(c).exists()), None)
        if cfg_path is None:
            raise FileNotFoundError(
                "No config file found. Copy config.example.yaml to config.local.yaml "
                "and set takeout_root."
            )

    raw = yaml.safe_load(cfg_path.read_text()) or {}
    return Config(
        takeout_root=Path(raw["takeout_root"]).expanduser(),
        data_dir=Path(raw.get("data_dir", "./data")).expanduser(),
        timezone=raw.get("timezone", "UTC"),
        intraday_grains=raw.get("intraday_grains", ["daily", "hourly"]),
        intraday_file_limit=raw.get("intraday_file_limit"),
    )
