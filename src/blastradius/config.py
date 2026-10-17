"""Configuration for BlastRadius.

Everything is read from environment variables, with a local ``.env`` file as a
convenience for development. Nothing here contains a secret: the password is
generated into ``.env`` (git-ignored) by the setup script, and every value falls
back to a safe default so the app can explain what is missing instead of
crashing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# The project root is two levels up from ``src/blastradius/config.py``.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load .env once, without letting it override variables that are already set
# (so a real environment variable always wins over the local file).
load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_GRAPH = "blastradius"


@dataclass(frozen=True)
class Config:
    """Connection settings for the FalkorDB server and the graph we use."""

    host: str = "localhost"
    port: int = 6379
    password: str | None = None
    graph: str = DEFAULT_GRAPH

    @classmethod
    def from_env(cls) -> Config:
        """Build a config from ``FALKORDB_*`` environment variables."""
        host = os.getenv("FALKORDB_HOST", "localhost").strip() or "localhost"

        raw_port = os.getenv("FALKORDB_PORT", "6379").strip() or "6379"
        try:
            port = int(raw_port)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(
                f"FALKORDB_PORT must be a number, got {raw_port!r}"
            ) from exc

        password = os.getenv("FALKORDB_PASSWORD") or None
        graph = os.getenv("FALKORDB_GRAPH", DEFAULT_GRAPH).strip() or DEFAULT_GRAPH

        return cls(host=host, port=port, password=password, graph=graph)

    def with_graph(self, graph: str) -> Config:
        """Return a copy of this config that points at a different graph."""
        return replace(self, graph=graph)

    @property
    def is_configured(self) -> bool:
        """True when we have enough information to attempt a connection."""
        return bool(self.host) and self.port > 0

    def describe(self) -> str:
        """Human-readable target, with the password masked."""
        auth = "with password" if self.password else "no password"
        return f"redis://{self.host}:{self.port} ({auth}), graph={self.graph!r}"


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide cached configuration."""
    return Config.from_env()
