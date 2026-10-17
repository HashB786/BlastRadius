"""Deterministic synthetic dataset for BlastRadius.

The topology is hand-authored so the demo has meaningful multi-hop blast
radiuses, while the numeric attributes (call volume, SLOs, incident durations)
come from a fixed-seed PRNG. That combination means:

* the shape of every blast radius is the same on every machine, and
* every number is still "generated" data rather than a hand-typed constant.

Nothing in this module talks to a database; ``seed.py`` turns these records
into real Cypher writes. Keeping them pure makes the dataset testable without
a running FalkorDB server.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SEED = 1337

# --------------------------------------------------------------------------- #
# Teams and people
# --------------------------------------------------------------------------- #

TEAMS: list[dict] = [
    {
        "id": "team-platform",
        "name": "Core Platform",
        "cost_center": "CC-1001",
        "slack_channel": "#platform-oncall",
        "timezone": "UTC",
    },
    {
        "id": "team-checkout",
        "name": "Checkout",
        "cost_center": "CC-1002",
        "slack_channel": "#checkout-oncall",
        "timezone": "Europe/Berlin",
    },
    {
        "id": "team-payments",
        "name": "Payments Platform",
        "cost_center": "CC-1003",
        "slack_channel": "#payments-oncall",
        "timezone": "UTC",
    },
    {
        "id": "team-catalog",
        "name": "Catalog & Search",
        "cost_center": "CC-1004",
        "slack_channel": "#catalog-oncall",
        "timezone": "America/New_York",
    },
    {
        "id": "team-identity",
        "name": "Identity & Access",
        "cost_center": "CC-1005",
        "slack_channel": "#identity-oncall",
        "timezone": "UTC",
    },
    {
        "id": "team-notify",
        "name": "Notifications",
        "cost_center": "CC-1006",
        "slack_channel": "#notify-oncall",
        "timezone": "Asia/Kolkata",
    },
    {
        "id": "team-data",
        "name": "Data Platform",
        "cost_center": "CC-1007",
        "slack_channel": "#data-oncall",
        "timezone": "UTC",
    },
    {
        "id": "team-support",
        "name": "Support Tooling",
        "cost_center": "CC-1008",
        "slack_channel": "#support-tools",
        "timezone": "America/Los_Angeles",
    },
]

ENGINEERS: list[dict] = [
    {"id": "eng-001", "name": "Alina Weiss", "role": "Staff SRE", "team": "team-platform"},
    {"id": "eng-002", "name": "Ravi Menon", "role": "Senior Backend Engineer", "team": "team-platform"},
    {"id": "eng-003", "name": "Jonah Kim", "role": "Backend Engineer", "team": "team-platform"},
    {"id": "eng-004", "name": "Marta Silva", "role": "Engineering Manager", "team": "team-checkout"},
    {"id": "eng-005", "name": "Tom Becker", "role": "Senior Backend Engineer", "team": "team-checkout"},
    {"id": "eng-006", "name": "Priya Nair", "role": "Backend Engineer", "team": "team-checkout"},
    {"id": "eng-007", "name": "Daniel Okafor", "role": "Staff Engineer", "team": "team-payments"},
    {"id": "eng-008", "name": "Sofia Rossi", "role": "Senior Backend Engineer", "team": "team-payments"},
    {"id": "eng-009", "name": "Hugo Lindqvist", "role": "Site Reliability Engineer", "team": "team-payments"},
    {"id": "eng-010", "name": "Chen Wei", "role": "Senior Search Engineer", "team": "team-catalog"},
    {"id": "eng-011", "name": "Nadia Haddad", "role": "Backend Engineer", "team": "team-catalog"},
    {"id": "eng-012", "name": "Lucas Meyer", "role": "Security Engineer", "team": "team-identity"},
    {"id": "eng-013", "name": "Aisha Bello", "role": "Backend Engineer", "team": "team-identity"},
    {"id": "eng-014", "name": "Erik Johansson", "role": "Senior Backend Engineer", "team": "team-notify"},
    {"id": "eng-015", "name": "Meera Iyer", "role": "Data Engineer", "team": "team-data"},
    {"id": "eng-016", "name": "Oscar Duarte", "role": "Analytics Engineer", "team": "team-data"},
    {"id": "eng-017", "name": "Hannah Cole", "role": "Full-stack Engineer", "team": "team-support"},
    {"id": "eng-018", "name": "Viktor Petrov", "role": "Site Reliability Engineer", "team": "team-platform"},
]

# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
# (id, display name, owning team, tier, primary language, repo, summary)
_SERVICE_ROWS: list[tuple[str, str, str, int, str, str, str]] = [
    ("api-gateway", "API Gateway", "team-platform", 1, "Go", "svc/api-gateway",
     "Public edge: authentication, routing and rate limiting for all client traffic."),
    ("web-bff", "Web Backend-for-Frontend", "team-platform", 1, "TypeScript", "svc/web-bff",
     "Aggregates page data for the storefront web client."),
    ("rate-limiter", "Rate Limiter", "team-platform", 2, "Go", "svc/rate-limiter",
     "Token-bucket limiter shared by the edge and internal callers."),
    ("service-registry", "Service Registry", "team-platform", 2, "Go", "svc/service-registry",
     "Service discovery catalogue used by every internal RPC client."),
    ("config-service", "Config Service", "team-platform", 2, "Rust", "svc/config-service",
     "Versioned runtime configuration served to all services."),
    ("feature-flags", "Feature Flag Service", "team-platform", 2, "Python", "svc/feature-flags",
     "Evaluates rollout flags for web, checkout and support flows."),
    ("secrets-broker", "Secrets Broker", "team-platform", 2, "Rust", "svc/secrets-broker",
     "Issues short-lived credentials to services."),
    ("audit-log", "Audit Log", "team-platform", 2, "Java", "svc/audit-log",
     "Append-only audit trail for money movement and access changes."),

    ("checkout-service", "Checkout Service", "team-checkout", 1, "Java", "svc/checkout",
     "Owns the checkout state machine from cart to order confirmation."),
    ("cart-service", "Cart Service", "team-checkout", 1, "TypeScript", "svc/cart",
     "Shopping cart read/write API."),
    ("pricing-service", "Pricing Service", "team-checkout", 1, "Java", "svc/pricing",
     "Price books, promotions and quote calculation."),
    ("order-service", "Order Service", "team-checkout", 1, "Java", "svc/orders",
     "Order lifecycle, fulfilment handoff and customer order history."),
    ("tax-service", "Tax Service", "team-checkout", 2, "Python", "svc/tax",
     "Jurisdiction-aware tax calculation."),
    ("shipping-service", "Shipping Service", "team-checkout", 2, "Java", "svc/shipping",
     "Carrier selection, rates and delivery estimates."),
    ("loyalty-service", "Loyalty Service", "team-checkout", 3, "Python", "svc/loyalty",
     "Points accrual and redemption for members."),

    ("payment-service", "Payment Service", "team-payments", 1, "Go", "svc/payments",
     "Authorises and captures card payments."),
    ("fraud-service", "Fraud Scoring Service", "team-payments", 1, "Python", "svc/fraud-scoring",
     "Real-time risk scoring for every authorisation attempt."),
    ("ledger-service", "Ledger Service", "team-payments", 1, "Java", "svc/ledger",
     "Double-entry ledger and balance reconciliation."),
    ("invoice-service", "Invoice Service", "team-payments", 2, "Java", "svc/invoices",
     "Generates and delivers invoices for business customers."),
    ("refunds-service", "Refunds Service", "team-payments", 2, "Go", "svc/refunds",
     "Refund approval workflow and money movement."),
    ("currency-service", "Currency Service", "team-payments", 2, "Go", "svc/currency",
     "FX rates and currency conversion."),

    ("catalog-service", "Catalog Service", "team-catalog", 1, "TypeScript", "svc/catalog",
     "Product catalogue source of truth."),
    ("search-service", "Search Service", "team-catalog", 1, "Java", "svc/search",
     "Product search and filtering."),
    ("inventory-service", "Inventory Service", "team-catalog", 1, "Go", "svc/inventory",
     "Stock levels and reservations per warehouse."),
    ("media-service", "Media Service", "team-catalog", 3, "TypeScript", "svc/media",
     "Product image resizing and delivery."),
    ("recommendation-service", "Recommendation Service", "team-catalog", 2, "Python", "svc/recommendations",
     "Related-product and 'customers also bought' suggestions."),

    ("auth-service", "Auth Service", "team-identity", 1, "Go", "svc/auth",
     "Token issuance, validation and OAuth flows."),
    ("session-service", "Session Service", "team-identity", 1, "Go", "svc/sessions",
     "Session storage and revocation."),
    ("user-profile-service", "User Profile Service", "team-identity", 2, "TypeScript", "svc/user-profile",
     "Customer profile, preferences and address book."),
    ("permission-service", "Permission Service", "team-identity", 2, "Python", "svc/permissions",
     "Role and permission checks for staff tooling."),

    ("notification-service", "Notification Service", "team-notify", 2, "TypeScript", "svc/notifications",
     "Orchestrates transactional email, SMS and push."),
    ("email-worker", "Email Worker", "team-notify", 2, "Python", "svc/email-worker",
     "Renders and sends transactional email."),
    ("sms-worker", "SMS Worker", "team-notify", 3, "Python", "svc/sms-worker",
     "Sends SMS via the upstream aggregator."),
    ("webhook-dispatcher", "Webhook Dispatcher", "team-notify", 3, "Go", "svc/webhook-dispatcher",
     "Delivers merchant webhooks with retries."),

    ("analytics-ingest", "Analytics Ingest", "team-data", 2, "Python", "svc/analytics-ingest",
     "Collects behavioural events from the storefront."),
    ("etl-orchestrator", "ETL Orchestrator", "team-data", 2, "Python", "svc/etl-orchestrator",
     "Schedules nightly warehouse loads."),
    ("warehouse-proxy", "Warehouse Proxy", "team-data", 2, "Go", "svc/warehouse-proxy",
     "Query gateway in front of the analytics warehouse."),
    ("reporting-api", "Reporting API", "team-data", 3, "Python", "svc/reporting-api",
     "Internal dashboards for finance and operations."),

    ("support-portal", "Support Portal", "team-support", 3, "TypeScript", "svc/support-portal",
     "Agent-facing console for order and refund actions."),
    ("ticket-router", "Ticket Router", "team-support", 3, "Python", "svc/ticket-router",
     "Routes inbound tickets to the right queue."),
]

# (source, target, protocol, criticality, comment)
_DEPENDENCY_ROWS: list[tuple[str, str, str, str]] = [
    ("api-gateway", "web-bff", "HTTP/2", "critical"),
    ("api-gateway", "auth-service", "gRPC", "critical"),
    ("api-gateway", "rate-limiter", "gRPC", "critical"),
    ("api-gateway", "service-registry", "gRPC", "high"),
    ("web-bff", "checkout-service", "gRPC", "critical"),
    ("web-bff", "cart-service", "gRPC", "critical"),
    ("web-bff", "catalog-service", "gRPC", "critical"),
    ("web-bff", "user-profile-service", "gRPC", "high"),
    ("web-bff", "recommendation-service", "HTTP", "low"),
    ("web-bff", "feature-flags", "HTTP", "high"),
    ("checkout-service", "cart-service", "gRPC", "critical"),
    ("checkout-service", "pricing-service", "gRPC", "critical"),
    ("checkout-service", "payment-service", "gRPC", "critical"),
    ("checkout-service", "inventory-service", "gRPC", "critical"),
    ("checkout-service", "tax-service", "gRPC", "high"),
    ("checkout-service", "shipping-service", "gRPC", "high"),
    ("checkout-service", "order-service", "gRPC", "critical"),
    ("checkout-service", "feature-flags", "HTTP", "high"),
    ("cart-service", "catalog-service", "gRPC", "high"),
    ("cart-service", "pricing-service", "gRPC", "high"),
    ("cart-service", "inventory-service", "gRPC", "high"),
    ("pricing-service", "catalog-service", "gRPC", "high"),
    ("pricing-service", "currency-service", "gRPC", "high"),
    ("pricing-service", "feature-flags", "HTTP", "low"),
    ("payment-service", "fraud-service", "gRPC", "critical"),
    ("payment-service", "ledger-service", "gRPC", "critical"),
    ("payment-service", "currency-service", "gRPC", "high"),
    ("payment-service", "secrets-broker", "gRPC", "critical"),
    ("fraud-service", "user-profile-service", "gRPC", "high"),
    ("fraud-service", "audit-log", "Kafka", "medium"),
    ("fraud-service", "feature-flags", "HTTP", "low"),
    ("ledger-service", "audit-log", "Kafka", "high"),
    ("ledger-service", "currency-service", "gRPC", "medium"),
    ("invoice-service", "ledger-service", "gRPC", "critical"),
    ("invoice-service", "tax-service", "gRPC", "high"),
    ("invoice-service", "email-worker", "AMQP", "medium"),
    ("refunds-service", "payment-service", "gRPC", "critical"),
    ("refunds-service", "ledger-service", "gRPC", "critical"),
    ("refunds-service", "audit-log", "Kafka", "high"),
    ("order-service", "inventory-service", "gRPC", "critical"),
    ("order-service", "shipping-service", "gRPC", "high"),
    ("order-service", "notification-service", "AMQP", "medium"),
    ("order-service", "audit-log", "Kafka", "high"),
    ("inventory-service", "catalog-service", "gRPC", "high"),
    ("inventory-service", "warehouse-proxy", "gRPC", "high"),
    ("catalog-service", "media-service", "HTTP", "low"),
    ("catalog-service", "config-service", "gRPC", "critical"),
    ("search-service", "catalog-service", "gRPC", "critical"),
    ("recommendation-service", "catalog-service", "gRPC", "medium"),
    ("recommendation-service", "analytics-ingest", "Kafka", "low"),
    ("notification-service", "email-worker", "AMQP", "high"),
    ("notification-service", "sms-worker", "AMQP", "medium"),
    ("notification-service", "user-profile-service", "gRPC", "medium"),
    ("email-worker", "config-service", "gRPC", "high"),
    ("sms-worker", "config-service", "gRPC", "high"),
    ("webhook-dispatcher", "config-service", "gRPC", "medium"),
    ("webhook-dispatcher", "audit-log", "Kafka", "low"),
    ("auth-service", "session-service", "gRPC", "critical"),
    ("auth-service", "user-profile-service", "gRPC", "high"),
    ("auth-service", "permission-service", "gRPC", "high"),
    ("auth-service", "secrets-broker", "gRPC", "critical"),
    ("session-service", "config-service", "gRPC", "high"),
    ("permission-service", "user-profile-service", "gRPC", "medium"),
    ("permission-service", "config-service", "gRPC", "high"),
    ("user-profile-service", "audit-log", "Kafka", "medium"),
    ("user-profile-service", "config-service", "gRPC", "high"),
    ("analytics-ingest", "audit-log", "Kafka", "low"),
    ("etl-orchestrator", "analytics-ingest", "Kafka", "high"),
    ("etl-orchestrator", "warehouse-proxy", "gRPC", "high"),
    ("etl-orchestrator", "audit-log", "Kafka", "medium"),
    ("reporting-api", "warehouse-proxy", "gRPC", "high"),
    ("reporting-api", "feature-flags", "HTTP", "low"),
    ("warehouse-proxy", "config-service", "gRPC", "high"),
    ("feature-flags", "config-service", "gRPC", "critical"),
    ("service-registry", "config-service", "gRPC", "high"),
    ("rate-limiter", "config-service", "gRPC", "critical"),
    ("audit-log", "config-service", "gRPC", "high"),
    ("support-portal", "ticket-router", "HTTP", "medium"),
    ("support-portal", "user-profile-service", "gRPC", "high"),
    ("support-portal", "refunds-service", "gRPC", "critical"),
    ("support-portal", "feature-flags", "HTTP", "low"),
    ("ticket-router", "notification-service", "AMQP", "medium"),
    ("ticket-router", "user-profile-service", "gRPC", "medium"),
    ("shipping-service", "inventory-service", "gRPC", "high"),
    ("shipping-service", "config-service", "gRPC", "high"),
    ("tax-service", "config-service", "gRPC", "high"),
    ("currency-service", "config-service", "gRPC", "high"),
    ("loyalty-service", "pricing-service", "gRPC", "medium"),
    ("loyalty-service", "user-profile-service", "gRPC", "medium"),
    ("loyalty-service", "notification-service", "AMQP", "low"),
]


DATASTORES: list[dict] = [
    {"id": "pg-orders", "name": "orders-primary", "engine": "PostgreSQL 16", "region": "eu-west-1", "managed": True},
    {"id": "pg-payments", "name": "payments-primary", "engine": "PostgreSQL 16", "region": "eu-west-1", "managed": True},
    {"id": "pg-catalog", "name": "catalog-primary", "engine": "PostgreSQL 15", "region": "us-east-1", "managed": True},
    {"id": "redis-cart", "name": "cart-cache", "engine": "Redis 7", "region": "eu-west-1", "managed": True},
    {"id": "redis-sessions", "name": "session-store", "engine": "Redis 7", "region": "eu-west-1", "managed": True},
    {"id": "warehouse", "name": "analytics-warehouse", "engine": "ClickHouse 24", "region": "us-east-1", "managed": True},
]

TOPICS: list[dict] = [
    {"id": "orders.events", "name": "orders.events", "broker": "Kafka", "partitions": 12},
    {"id": "payments.events", "name": "payments.events", "broker": "Kafka", "partitions": 12},
    {"id": "analytics.raw", "name": "analytics.raw", "broker": "Kafka", "partitions": 24},
    {"id": "notifications.send", "name": "notifications.send", "broker": "RabbitMQ", "partitions": 4},
]

# (service, datastore, access mode)
_DATASTORE_ACCESS: list[tuple[str, str, str]] = [
    ("order-service", "pg-orders", "READ_WRITE"),
    ("checkout-service", "pg-orders", "READ_WRITE"),
    ("payment-service", "pg-payments", "READ_WRITE"),
    ("ledger-service", "pg-payments", "READ_WRITE"),
    ("refunds-service", "pg-payments", "READ_WRITE"),
    ("invoice-service", "pg-payments", "READ"),
    ("catalog-service", "pg-catalog", "READ_WRITE"),
    ("search-service", "pg-catalog", "READ"),
    ("inventory-service", "pg-catalog", "READ"),
    ("cart-service", "redis-cart", "READ_WRITE"),
    ("checkout-service", "redis-cart", "READ"),
    ("session-service", "redis-sessions", "READ_WRITE"),
    ("auth-service", "redis-sessions", "READ"),
    ("reporting-api", "warehouse", "READ"),
    ("warehouse-proxy", "warehouse", "READ"),
    ("etl-orchestrator", "warehouse", "READ_WRITE"),
]

# (service, topic, direction)
_TOPIC_LINKS: list[tuple[str, str, str]] = [
    ("order-service", "orders.events", "PUBLISHES_TO"),
    ("checkout-service", "orders.events", "PUBLISHES_TO"),
    ("notification-service", "orders.events", "SUBSCRIBES_TO"),
    ("analytics-ingest", "orders.events", "SUBSCRIBES_TO"),
    ("payment-service", "payments.events", "PUBLISHES_TO"),
    ("ledger-service", "payments.events", "SUBSCRIBES_TO"),
    ("refunds-service", "payments.events", "PUBLISHES_TO"),
    ("analytics-ingest", "analytics.raw", "SUBSCRIBES_TO"),
    ("recommendation-service", "analytics.raw", "PUBLISHES_TO"),
    ("etl-orchestrator", "analytics.raw", "SUBSCRIBES_TO"),
    ("notification-service", "notifications.send", "PUBLISHES_TO"),
    ("email-worker", "notifications.send", "SUBSCRIBES_TO"),
    ("webhook-dispatcher", "notifications.send", "SUBSCRIBES_TO"),
]

# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #
# (id, title, severity, root cause service, days ago, duration minutes, summary,
#  [affected services in addition to the root cause])
_INCIDENT_ROWS: list[tuple[str, str, str, str, int, int, str, list[str]]] = [
    (
        "INC-2041",
        "Checkout latency spike after cart cache eviction",
        "SEV1",
        "redis-cart",
        9,
        74,
        "A mis-sized eviction policy pushed cart reads to the catalog primary, "
        "saturating it and stalling checkout for 74 minutes.",
        ["cart-service", "checkout-service", "web-bff", "catalog-service"],
    ),
    (
        "INC-2038",
        "Card authorisations declined due to fraud service timeout",
        "SEV1",
        "fraud-service",
        17,
        42,
        "Fraud scoring exceeded the payment-service timeout budget; authorisations "
        "failed closed until the timeouts were relaxed.",
        ["payment-service", "checkout-service", "api-gateway"],
    ),
    (
        "INC-2033",
        "Config reload loop degraded internal RPC",
        "SEV2",
        "config-service",
        24,
        28,
        "A malformed configuration rollout caused repeated reloads in every client, "
        "raising p99 latency for most internal calls.",
        ["rate-limiter", "feature-flags", "audit-log", "session-service"],
    ),
    (
        "INC-2029",
        "Duplicate ledger entries after retry storm",
        "SEV1",
        "ledger-service",
        31,
        96,
        "Retries without idempotency keys produced duplicate postings; finance "
        "reconciled the ledger manually.",
        ["payment-service", "invoice-service", "refunds-service"],
    ),
    (
        "INC-2024",
        "Session store failover logged out all users",
        "SEV1",
        "redis-sessions",
        45,
        19,
        "A failover to a cold replica invalidated every session, forcing mass "
        "re-authentication.",
        ["session-service", "auth-service", "api-gateway", "web-bff"],
    ),
    (
        "INC-2019",
        "Search results empty after index mapping change",
        "SEV2",
        "catalog-service",
        52,
        65,
        "An incompatible mapping change rejected documents, leaving search with an "
        "empty index until a rollback completed.",
        ["search-service", "web-bff"],
    ),
    (
        "INC-2015",
        "Email backlog delayed order confirmations",
        "SEV3",
        "email-worker",
        60,
        130,
        "A credential rotation broke the SMTP connection; the queue drained once "
        "the worker restarted with the new secret.",
        ["notification-service", "order-service"],
    ),
    (
        "INC-2008",
        "Warehouse proxy connection pool exhaustion",
        "SEV2",
        "warehouse-proxy",
        73,
        51,
        "Nightly ETL held connections open, starving reporting queries for almost "
        "an hour.",
        ["etl-orchestrator", "reporting-api", "inventory-service"],
    ),
    (
        "INC-2002",
        "Rate limiter false positives blocked mobile traffic",
        "SEV2",
        "rate-limiter",
        88,
        23,
        "A shared bucket key collapsed per-device limits into one global bucket, "
        "throttling legitimate mobile clients.",
        ["api-gateway"],
    ),
    (
        "INC-1997",
        "Inventory reservation drift oversold flash-sale SKUs",
        "SEV1",
        "inventory-service",
        101,
        140,
        "Reservations were not released after abandoned checkouts, so stock counts "
        "drifted and oversold the flash-sale SKUs.",
        ["cart-service", "checkout-service", "order-service", "shipping-service"],
    ),
]

DEPLOY_TARGETS: list[dict] = [
    {"id": "prod-eu", "name": "Production EU", "region": "eu-west-1", "provider": "AWS"},
    {"id": "prod-us", "name": "Production US", "region": "us-east-1", "provider": "AWS"},
]



@dataclass(frozen=True)
class Dataset:
    """The complete synthetic graph, ready to be written to FalkorDB."""

    services: list[dict]
    teams: list[dict]
    engineers: list[dict]
    dependencies: list[dict]
    datastores: list[dict]
    datastore_access: list[dict]
    topics: list[dict]
    topic_links: list[dict]
    incidents: list[dict]
    regions: list[dict] = field(default_factory=list)

    def service_ids(self) -> list[str]:
        return [s["id"] for s in self.services]

    def counts(self) -> dict[str, int]:
        return {
            "services": len(self.services),
            "teams": len(self.teams),
            "engineers": len(self.engineers),
            "dependencies": len(self.dependencies),
            "datastores": len(self.datastores),
            "topics": len(self.topics),
            "incidents": len(self.incidents),
        }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_dataset() -> Dataset:
    """Build the dataset deterministically (same output on every run)."""
    rng = random.Random(SEED)

    services: list[dict] = []
    for sid, name, team, tier, language, repo, summary in _SERVICE_ROWS:
        services.append(
            {
                "id": sid,
                "name": name,
                "team_id": team,
                "tier": tier,
                "language": language,
                "repo": repo,
                "summary": summary,
                "slo_target": round(rng.uniform(99.0, 99.99), 2),
                "error_budget_minutes": rng.choice([21, 43, 87, 216, 432]),
            }
        )

    dependencies: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for src, dst, protocol, criticality in _DEPENDENCY_ROWS:
        if (src, dst) in seen:
            continue
        seen.add((src, dst))
        dependencies.append(
            {
                "source": src,
                "target": dst,
                "protocol": protocol,
                "criticality": criticality,
                "calls_per_min": rng.randint(120, 48000),
            }
        )

    incidents: list[dict] = []
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for inc_id, title, severity, root_cause, days_ago, duration, summary, affected in _INCIDENT_ROWS:
        started = now - timedelta(days=days_ago, hours=rng.randint(0, 23))
        incidents.append(
            {
                "id": inc_id,
                "title": title,
                "severity": severity,
                "status": "resolved",
                "root_cause": root_cause,
                "started_at": _iso(started),
                "resolved_at": _iso(started + timedelta(minutes=duration)),
                "duration_minutes": duration,
                "summary": summary,
                "affected": [root_cause, *[a for a in affected if a != root_cause]],
                "detected_by": rng.choice(["alert", "customer report", "synthetic probe"]),
            }
        )

    # Spread services across two production regions deterministically.
    for index, service in enumerate(services):
        service["region"] = DEPLOY_TARGETS[index % len(DEPLOY_TARGETS)]["id"]

    return Dataset(
        services=services,
        teams=[dict(t) for t in TEAMS],
        engineers=[dict(e) for e in ENGINEERS],
        dependencies=dependencies,
        datastores=[dict(d) for d in DATASTORES],
        datastore_access=[
            {"service": s, "datastore": d, "mode": m} for s, d, m in _DATASTORE_ACCESS
        ],
        topics=[dict(t) for t in TOPICS],
        topic_links=[
            {"service": s, "topic": t, "direction": d} for s, t, d in _TOPIC_LINKS
        ],
        incidents=incidents,
        regions=[dict(r) for r in DEPLOY_TARGETS],
    )
