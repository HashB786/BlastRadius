# The Cypher behind BlastRadius

Every query in the application lives in `src/blastradius/queries.py` and
`src/blastradius/seed.py`. This page shows them with the reasoning behind each clause and a
real sample result from the demo graph (`FalkorDB 4.22.1`, 88 nodes, 267 edges).

Parameters are always bound (`$service_id`, `$id`, `$rows`); no user input is interpolated
into a query string. The one exception is the traversal depth, which **cannot** be a Cypher
parameter — variable-length bounds must be literals — so it is validated and clamped to
`1..6` in Python before being formatted into the statement.

---

## 1. Seeding: idempotent writes

### Indexes

```cypher
CREATE INDEX FOR (s:Service) ON (s.id)
CREATE INDEX FOR (s:Service) ON (s.name)
CREATE INDEX FOR (t:Team) ON (t.id)
CREATE INDEX FOR (e:Engineer) ON (e.id)
CREATE INDEX FOR (i:Incident) ON (i.id)
CREATE INDEX FOR (d:Datastore) ON (d.id)
CREATE INDEX FOR (t:Topic) ON (t.id)
CREATE INDEX FOR (r:Region) ON (r.id)
```

`create_indexes()` first reads `CALL db.indexes()` and skips `(label, property)` pairs that
already exist — FalkorDB raises `Attribute 'id' is already indexed` rather than ignoring a
duplicate. If a future image rejects the modern `CREATE INDEX FOR` form, the seeder retries
with the legacy `CREATE INDEX ON :Service(id)` form once, and raises if both fail.

### Nodes

```cypher
UNWIND $rows AS row
MERGE (n:Service {id: row.id})
SET n.id = row.id,
    n.name = row.name,
    n.tier = row.tier,
    n.language = row.language,
    n.repo = row.repo,
    n.summary = row.summary,
    n.slo_target = row.slo_target,
    n.error_budget_minutes = row.error_budget_minutes,
    n.region = row.region
RETURN count(n)
```

Batched 200 rows at a time. `MERGE` on the business key makes seeding idempotent: running it
twice updates properties instead of duplicating nodes.

### Relationships

```cypher
UNWIND $rows AS row
MATCH (t:Team {id: row.team_id})
MATCH (s:Service {id: row.id})
MERGE (t)-[:OWNS]->(s)
```

```cypher
UNWIND $rows AS row
MATCH (a:Service {id: row.source})
MATCH (b:Service {id: row.target})
MERGE (a)-[r:DEPENDS_ON]->(b)
SET r.protocol = row.protocol,
    r.criticality = row.criticality,
    r.calls_per_min = row.calls_per_min
```

```cypher
UNWIND $rows AS row
MATCH (i:Incident {id: row.id})
MATCH (s) WHERE s.id = row.root_cause
MERGE (i)-[:ROOT_CAUSE]->(s)
```

`MATCH (s)` without a label is intentional here: two incidents are rooted in **datastores**
(`redis-cart`, `redis-sessions`), and forcing `:Service` silently dropped those edges. The
verification step below is what caught it.

### Verification (counts read back, not assumed)

```cypher
MATCH (n:Service) RETURN count(n)
MATCH ()-[r:DEPENDS_ON]->() RETURN count(r)
```

```text
Service     40      DEPENDS_ON   90
Team         8      OWNS         40
Engineer    18      MEMBER_OF    18
Datastore    6      IMPACTED     40
Topic        4      ROOT_CAUSE   10
Incident    10      nodes_total  88
Region       2      edges_total 267
```

---

## 2. Health check

```cypher
RETURN 1
MATCH (n) RETURN count(n)
MATCH ()-[r]->() RETURN count(r)
```

`RETURN 1` proves the socket authenticates and the query engine answers; the counts tell the
user whether the graph is empty. The sidebar renders exactly these values.

Server identity comes from Redis itself:

```text
MODULE LIST -> [['name', 'graph', 'ver', 42201, 'path', '/var/lib/falkordb/bin/falkordb.so', ...]]
```

`42201` is FalkorDB's packed version (`major*10000 + minor*100 + patch`), decoded to `4.22.1`.

---

## 3. Multi-hop impact analysis (service origin)

```cypher
MATCH p = (dependent:Service)-[:DEPENDS_ON*1..4]->(origin:Service {id: $service_id})
WHERE dependent.id <> origin.id
RETURN dependent.id, dependent.name, dependent.tier, length(p) AS hops, p
ORDER BY hops ASC, dependent.name ASC
```

Read the arrow carefully: `DEPENDS_ON` points from a consumer to its dependency. A failure at
`origin` therefore travels **against** the arrow, which is why the pattern starts at
`dependent` and ends at `origin`.

* `*1..4` — between one and four hops. The upper bound is the validated depth (1–6).
* `WHERE dependent.id <> origin.id` — a service cannot be its own blast radius.
* `length(p)` — number of relationships in the returned path.
* Returning `p` is what makes the answer auditable: the client receives the nodes and edges,
  including each edge's `protocol`, `criticality` and `calls_per_min`.

FalkorDB returns one row **per path**. A service reachable by several routes appears several
times, so the Python layer keeps the shortest path per service id:

```python
for dep_id, _name, _tier, hops, path in rows:
    if dep_id not in best or hops < best[dep_id][0]:
        best[dep_id] = (int(hops), path)
```

Sample (`payment-service`, depth 3):

| Hops | Service | Tier | Path |
|---:|---|---:|---|
| 1 | Checkout Service | 1 | `Checkout Service -> Payment Service` |
| 1 | Refunds Service | 2 | `Refunds Service -> Payment Service` |
| 2 | Web Backend-for-Frontend | 1 | `Web Backend-for-Frontend -> Checkout Service -> Payment Service` |
| 2 | Support Portal | 3 | `Support Portal -> Refunds Service -> Payment Service` |
| 3 | API Gateway | 1 | `API Gateway -> Web Backend-for-Frontend -> Checkout Service -> Payment Service` |

### Teams to page

```cypher
MATCH (t:Team)-[:OWNS]->(s:Service)
WHERE s.id IN $ids
OPTIONAL MATCH (e:Engineer)-[:MEMBER_OF]->(t)
RETURN t.id, t.name, t.slack_channel,
       collect(DISTINCT s.name), collect(DISTINCT e.name)
ORDER BY t.name
```

`$ids` is the Python list of affected service ids — one round-trip instead of N.

---

## 4. Multi-hop impact analysis (datastore origin)

```cypher
MATCH p = (dependent:Service)-[:DEPENDS_ON*0..3]->(consumer:Service)
          -[:READS_FROM|WRITES_TO]->(origin:Datastore {id: $service_id})
RETURN dependent.id, dependent.name, dependent.tier, length(p) AS hops, p
ORDER BY hops ASC, dependent.name ASC
```

Why this shape:

* `*0..3` lets the direct consumer be its own starting point. A zero-length segment plus the
  store edge still produces a one-hop path, so "hops" stays intuitive.
* `-[:READS_FROM|WRITES_TO]->` keeps the real access edge as the final hop, which is what the
  UI shows and the graph view draws.
* A datastore has no `tier`, so the origin card omits it rather than inventing one.

Sample (`redis-cart`, depth 3):

| Hops | Service | Path |
|---:|---|---|
| 1 | Cart Service | `Cart Service -> cart-cache` |
| 1 | Checkout Service | `Checkout Service -> cart-cache` |
| 2 | Web Backend-for-Frontend | `Web Backend-for-Frontend -> Checkout Service -> cart-cache` |
| 3 | API Gateway | `API Gateway -> Web Backend-for-Frontend -> Checkout Service -> cart-cache` |

---

## 5. Ownership lookup

```cypher
MATCH (t:Team)-[:OWNS]->(s:Service {id: $id})
RETURN t.id, t.name, t.cost_center, t.slack_channel, t.timezone
ORDER BY t.name
```

```cypher
MATCH (t:Team)-[:OWNS]->(:Service {id: $id})
MATCH (e:Engineer)-[:MEMBER_OF]->(t)
RETURN e.id, e.name, e.role, t.name
ORDER BY e.name
```

`(t)-[:OWNS]->(:Service {id: $id})` is an anonymous node with a property filter: FalkorDB uses
the `Service.id` index, and the traversal then hops to the owning team. Sample for
`ledger-service`: `Payments Platform`, `#payments-oncall`, `CC-1003`, `UTC`, and engineers
Daniel Okafor, Hugo Lindqvist, Sofia Rossi.

Team view reverses the direction:

```cypher
MATCH (t:Team {id: $id})-[:OWNS]->(s:Service)
RETURN s.id, s.name, s.tier, s.language
ORDER BY s.tier, s.name
```

```cypher
MATCH (i:Incident)-[:IMPACTED]->(s:Service)<-[:OWNS]-(t:Team {id: $id})
RETURN DISTINCT i.id, i.title, i.severity, i.started_at
ORDER BY i.started_at DESC
```

---

## 6. Incident history

```cypher
MATCH (i:Incident)-[:ROOT_CAUSE]->(s {id: $id})
RETURN i.id AS id, i.title AS title, i.severity AS severity, i.status AS status,
       i.started_at AS started_at, i.duration_minutes AS duration_minutes,
       i.summary AS summary, 'root cause' AS role
UNION ALL
MATCH (i:Incident)-[:IMPACTED]->(s {id: $id})
RETURN i.id AS id, i.title AS title, i.severity AS severity, i.status AS status,
       i.started_at AS started_at, i.duration_minutes AS duration_minutes,
       i.summary AS summary, 'affected' AS role
```

Two lessons baked into this statement:

1. **`UNION` requires identical column names.** Without the `AS` aliases FalkorDB rejects the
   query with *"All sub queries in a UNION must have the same column names."*
2. **`(s {id: $id})` is unlabelled on purpose** so a datastore's incident history works too.

The same incident can appear in both branches (root cause and affected); Python merges by id
and keeps `role = "root cause"`, because "we caused it" is the stronger fact.

Sample (`ledger-service`): `INC-2029`, SEV1, `root cause`, 96 minutes, *Duplicate ledger
entries after retry storm*.

Sample (`redis-cart`): `INC-2041`, SEV1, `root cause`, 74 minutes, *Checkout latency spike
after cart cache eviction*.

### Affected services recorded on an incident

```cypher
UNWIND $ids AS inc_id
MATCH (i:Incident {id: inc_id})-[:IMPACTED]->(s:Service)
RETURN i.id, collect(DISTINCT s.name)
```

### Incident detail, including a live replay of its blast radius

```cypher
MATCH (i:Incident {id: $id})
OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(root)
OPTIONAL MATCH (i)-[:IMPACTED]->(s:Service)
RETURN i.id, i.title, i.severity, i.status, i.started_at, i.resolved_at,
       i.duration_minutes, i.summary, i.detected_by, root.id, root.name,
       labels(root), collect(DISTINCT s.name)
LIMIT 1
```

`labels(root)` tells the UI whether the root cause was a `Service` or a `Datastore`, so the
badge is accurate. The "downstream paths" table is **not** read from the incident node — it
is recomputed by calling the same impact query used by the Blast radius tab, so the incident
replay and the live analysis can never disagree.

---

## 7. Overview aggregates

```cypher
MATCH (s:Service) WITH count(s) AS services
MATCH (t:Team) WITH services, count(t) AS teams
MATCH (e:Engineer) WITH services, teams, count(e) AS engineers
MATCH (i:Incident) WITH services, teams, engineers, count(i) AS incidents
MATCH ()-[r:DEPENDS_ON]->()
RETURN services, teams, engineers, incidents, count(r)
```

Chained `WITH` clauses carry the running totals instead of producing a Cartesian product.

```cypher
MATCH (d:Service)-[:DEPENDS_ON]->(s:Service)
RETURN s.name, s.tier, count(d) AS dependents
ORDER BY dependents DESC, s.name
LIMIT 8
```

Current top of that ranking:

| Service | Direct dependents |
|---|---:|
| Config Service | 15 |
| Audit Log | 8 |
| User Profile Service | 8 |
| Catalog Service | 6 |
| Feature Flag Service | 6 |

```cypher
MATCH (i:Incident) RETURN i.severity, count(i) ORDER BY i.severity
MATCH (s:Service) RETURN s.tier, count(s) ORDER BY s.tier
```

---

## 8. Missing entities, honestly

```cypher
MATCH (n {id: $id}) RETURN labels(n), properties(n) LIMIT 1
```

An empty result is a fact, not a failure: `impact_analysis` returns
`missing=True` with the message *"'x' is not in the graph, so no impact can be computed.
Nothing was assumed or substituted."* Close matches come from a second query:

```cypher
MATCH (s:Service)
WHERE toLower(s.id) CONTAINS toLower($q) OR toLower(s.name) CONTAINS toLower($q)
RETURN s.id ORDER BY s.id LIMIT $limit
```

and fall back to `difflib.get_close_matches` over the service ids (in Python) when nothing
contains the typo. The CLI exits `2`; the UI shows the message and the suggestions.

---

## 9. Path handling in Python

FalkorDB returns a `falkordb.path.Path` for `RETURN p`. Its `nodes()` and `edges()` are
**methods**, not properties:

```python
def path_to_dict(path):
    nodes = [{"id": n.properties["id"], "name": n.properties["name"], ...}
             for n in path.nodes()]
    edges = [{"_type": e.relation, **e.properties} for e in path.edges()]
    return {"nodes": nodes, "edges": edges}
```

`db.path_nodes()` / `db.path_edges()` accept either callables or properties so a client
version change cannot break the UI silently. Tests assert `len(edges) == len(nodes) - 1`,
`hops == len(edges)`, and that the first node is the affected service and the last is the
origin.

---

## 10. Performance notes

Measured on the demo graph, warm connection, Windows 11 / Docker Desktop:

| Operation | Rows returned | Time |
|---|---|---:|
| `RETURN 1` health probe (warm) | 1 | ~1–5 ms |
| Service impact, depth 4 | 5 paths | ~1–3 ms |
| Config-service impact, depth 3 | 124 paths → 37 services | ~1–5 ms |
| Full overview (5 queries) | — | ~10–20 ms |

(The very first query on a new connection pays a one-off ~40 ms handshake.)

Two things keep it there: `id` indexes on every label (so entry points are lookups, not
scans), and bounded traversal depth. Depth 6 on the demo estate still returns in tens of
milliseconds; the cap exists for comprehensibility as much as cost.
