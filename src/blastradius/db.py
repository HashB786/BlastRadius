"""Connection layer for the real FalkorDB server.

There is no in-memory fallback and no mock: if the server is unreachable the
app says so. Every function here talks to ``redis://host:port`` using the
official ``falkordb`` Python client.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from falkordb import FalkorDB

from .config import Config, get_config


class DatabaseUnavailable(RuntimeError):
    """Raised when the FalkorDB server cannot be reached or refuses a query."""


@dataclass
class HealthReport:
    """What a live round-trip to the server actually told us."""

    reachable: bool
    graph: str
    latency_ms: float | None = None
    server_version: str | None = None
    node_count: int | None = None
    edge_count: int | None = None
    error: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _format_falkor_version(value: Any) -> str:
    """FalkorDB reports an int like 42201 meaning 4.22.1."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    major, rest = divmod(number, 10000)
    minor, patch = divmod(rest, 100)
    return f"{major}.{minor}.{patch}"


def _node_to_dict(node: Any) -> dict[str, Any]:
    """Convert a client Node object into plain Python data."""
    props = dict(getattr(node, "properties", {}) or {})
    labels = list(getattr(node, "labels", []) or [])
    props["_labels"] = labels
    props["_id"] = getattr(node, "id", None)
    return props


def _edge_to_dict(edge: Any) -> dict[str, Any]:
    """Convert a client Edge object into plain Python data."""
    props = dict(getattr(edge, "properties", {}) or {})
    props["_type"] = getattr(edge, "relation", None)
    return props


def path_nodes(path: Any) -> list[Any]:
    """Nodes of a path, whether the client exposes them as a method or property."""
    value = getattr(path, "nodes", None)
    if callable(value):
        value = value()
    return list(value or [])


def path_edges(path: Any) -> list[Any]:
    """Edges of a path, whether the client exposes them as a method or property."""
    value = getattr(path, "edges", None)
    if callable(value):
        value = value()
    return list(value or [])


def path_to_dict(path: Any) -> dict[str, Any]:
    """Turn a FalkorDB Path into ``{"nodes": [...], "edges": [...]}``.

    Keeping this conversion in one place means the UI and the tests agree on
    the shape of "the real relationship path that supports this conclusion".
    """
    nodes = [_node_to_dict(n) for n in path_nodes(path)]
    edges = [_edge_to_dict(e) for e in path_edges(path)]
    return {"nodes": nodes, "edges": edges}


def path_to_arrow(path: Any, sep: str = " -> ") -> str:
    """Render a path as ``API Gateway -> Cart Service -> Cart Cache``."""
    nodes = path_nodes(path)
    names = []
    for node in nodes:
        props = getattr(node, "properties", {}) or {}
        names.append(str(props.get("name") or props.get("id") or "?"))
    return sep.join(names)


class BlastRadiusDB:
    """Thin, explicit wrapper around one FalkorDB graph."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self._client: FalkorDB | None = None

    # -- connection ------------------------------------------------------- #

    def connect(self) -> FalkorDB:
        """Create (and cache) the client. Raises DatabaseUnavailable on failure."""
        if self._client is not None:
            return self._client
        try:
            client = FalkorDB(
                host=self.config.host,
                port=self.config.port,
                password=self.config.password,
            )
            client.connection.ping()
        except Exception as exc:  # connection errors come from redis-py
            raise DatabaseUnavailable(
                f"Cannot reach FalkorDB at {self.config.host}:{self.config.port} "
                f"({type(exc).__name__}: {exc}). "
                "Is the container running? See README.md → Running FalkorDB."
            ) from exc
        self._client = client
        return client

    @property
    def graph(self):
        return self.connect().select_graph(self.config.graph)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.connection.close()
            finally:
                self._client = None

    # -- querying --------------------------------------------------------- #

    def run(self, cypher: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        """Run one Cypher statement and return its raw result rows."""
        try:
            result = self.graph.query(cypher, params or {})
        except DatabaseUnavailable:
            raise
        except Exception as exc:
            raise DatabaseUnavailable(f"Cypher query failed: {exc}") from exc
        return list(result.result_set)

    def run_many(self, statements: Iterable[tuple[str, dict[str, Any] | None]]) -> None:
        for cypher, params in statements:
            self.run(cypher, params)

    def scalar(self, cypher: str, params: dict[str, Any] | None = None) -> Any:
        rows = self.run(cypher, params)
        if not rows or not rows[0]:
            return None
        return rows[0][0]

    def server_version(self) -> str | None:
        """Best-effort Redis/FalkorDB version string; None if unavailable."""
        client = self.connect()
        try:
            info = client.connection.info("server")
        except Exception:
            return None
        redis_version = info.get("redis_version")
        falkor = None
        try:
            module_info = client.connection.execute_command("MODULE", "LIST")
            for module in module_info:
                if isinstance(module, dict):
                    fields = dict(module)
                else:
                    fields = {
                        module[i].decode()
                        if isinstance(module[i], bytes)
                        else str(module[i]): module[i + 1]
                        for i in range(0, len(module) - 1, 2)
                    }
                name = fields.get("name")
                if isinstance(name, bytes):
                    name = name.decode()
                if name and (
                    "falkor" in str(name).lower() or str(name).lower() == "graph"
                ):
                    falkor = _format_falkor_version(fields.get("ver"))
        except Exception:
            falkor = None
        if falkor and redis_version:
            return f"FalkorDB {falkor} / Redis {redis_version}"
        return f"Redis {redis_version}" if redis_version else None

    def health(self) -> HealthReport:
        """Live round-trip: ping, read the server version, count nodes/edges."""
        started = time.perf_counter()
        try:
            self.scalar("RETURN 1")
            elapsed = (time.perf_counter() - started) * 1000
            version = self.server_version()
            node_count = self.scalar("MATCH (n) RETURN count(n)")
            edge_count = self.scalar("MATCH ()-[r]->() RETURN count(r)")
        except DatabaseUnavailable as exc:
            return HealthReport(
                reachable=False, graph=self.config.graph, error=str(exc)
            )
        return HealthReport(
            reachable=True,
            graph=self.config.graph,
            latency_ms=round(elapsed, 2),
            server_version=version,
            node_count=int(node_count or 0),
            edge_count=int(edge_count or 0),
        )

    # -- schema / lifecycle ----------------------------------------------- #

    def clear_graph(self) -> None:
        """Delete every node and relationship in *this* graph only."""
        self.run("MATCH (n) DETACH DELETE n")

    def labels(self) -> list[str]:
        rows = self.run("CALL db.labels()")
        return [str(row[0]) for row in rows]

    def relationship_types(self) -> list[str]:
        rows = self.run("CALL db.relationshipTypes()")
        return [str(row[0]) for row in rows]


def as_dicts(rows: Sequence[Sequence[Any]], columns: Sequence[str]) -> list[dict[str, Any]]:
    """Zip raw rows with explicit column names.

    FalkorDB returns positional rows; naming the columns at the call site keeps
    the mapping obvious and independent of client metadata.
    """
    return [dict(zip(columns, row)) for row in rows]


_DEFAULT_DB: BlastRadiusDB | None = None


def get_db(config: Config | None = None) -> BlastRadiusDB:
    """Process-wide default connection (cheap to create, reused by the UI)."""
    global _DEFAULT_DB
    if config is not None:
        return BlastRadiusDB(config)
    if _DEFAULT_DB is None:
        _DEFAULT_DB = BlastRadiusDB()
    return _DEFAULT_DB
