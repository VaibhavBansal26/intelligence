# Architecture

Intelligence is a standalone Python service. It reads two independent Node products (Funnels and Conversations)
only through the Analytics API contract, lands the data locally, trains small models per customer account, and
serves rankings and recommendations. The apps share no code with it; the contract is the only coupling.

## System context

```mermaid
flowchart LR
    owner([Business owner / analyst]) -->|"/v1 API (API key or JWT)"| intel[Intelligence service]
    operator([Platform operator]) -->|"/admin API (admin key)"| intel
    intel -->|"Bearer fa_* : exports, rollups"| funnels[(Funnels app)]
    intel -->|"Bearer ca_* : exports, rollups"| conv[(Conversations app)]
    intel -.->|"optional, aggregates only"| llm[Anthropic API]
    prom[Prometheus] -->|/metrics| intel
```

## Containers and modules

```mermaid
flowchart TB
    subgraph svc[Intelligence process]
        api["app/api<br/>FastAPI routers, auth, RBAC, ETag, idempotency"]
        services["app/services<br/>accounts, analytics, insights"]
        ingest["app/ingest<br/>sync, job queue and worker"]
        conn["app/connectors<br/>HTTP client, tolerant record models"]
        feat["app/features<br/>contact profiles, lead/room features, stats"]
        models["app/models<br/>funnel rank, GBM, priority, anomaly, segments, registry, drift"]
        llm["app/llm<br/>gateway, narrator, NumberGuard, ask, rerank"]
        db["app/db<br/>SQLAlchemy models, Alembic"]
    end
    api --> services
    services --> feat --> db
    services --> models
    services --> llm
    api --> ingest --> conn
    ingest --> db
    models --> files[(joblib artifacts)]
    db --> store[(SQLite or PostgreSQL)]
```

`app/deps.py` is the composition root (`build_container`); nothing keeps module-level mutable state. The HTTP
client factory, LLM client, clock and DNS resolver are injected, which is how the tests run fully offline.

## Ingestion and training sequence

```mermaid
sequenceDiagram
    participant U as Client
    participant A as /v1 API
    participant Q as jobs table
    participant W as Worker
    participant S as Source app
    participant D as DB
    participant M as Registry
    U->>A: POST /v1/sync (Idempotency-Key)
    A->>Q: enqueue sync job (202)
    W->>Q: claim job (atomic UPDATE)
    loop per resource (events, leads, rooms, ...)
        W->>S: GET /export/... ?from=watermark&cursor=c
        S-->>W: NDJSON page + X-Next-Cursor
        W->>D: insert-or-ignore / upsert chunk, save cursor
    end
    W->>D: watermark, data_version + 1
    U->>A: POST /v1/models/train
    A->>Q: enqueue train job
    W->>D: build point-in-time features
    W->>W: time-aware split, fit, calibrate, metrics, PSI reference
    W->>M: register version (joblib + sha256 + card)
    M->>M: auto-promote policy
    U->>A: GET /v1/leads/scores (ETag from data_version)
```

## Data model

```mermaid
erDiagram
    accounts ||--o{ api_keys : has
    accounts ||--o{ sources : has
    accounts ||--o{ jobs : runs
    accounts ||--o{ model_versions : owns
    accounts ||--o{ anomaly_alerts : raises
    sources ||--o{ sync_state : tracks
    sources ||--o{ events : lands
    sources ||--o{ leads : lands
    sources ||--o{ funnels : snapshots
    funnels ||--o{ funnel_steps : contains
    funnels ||--o{ funnel_structure : describes
    sources ||--o{ rooms : lands
    rooms ||--o{ messages_meta : has
    sources ||--o{ rollups : lands
    accounts {
        string id PK
        int data_version
        json settings
    }
    sources {
        string id PK
        string kind
        text api_key_enc
        text pepper_enc
        string pepper_fp
    }
    events {
        string source_id PK
        string ext_id PK
        int ts
        string contact_key
        string visitor_key
    }
    leads {
        string source_id PK
        string ext_id PK
        string contact_key
        int first_order_at
    }
    rooms {
        string source_id PK
        string ext_id PK
        string contact_key
        float first_response_s
    }
    model_versions {
        string id PK
        string model_type
        int version
        string status
        json metrics
    }
```

`contact_key` is the join column between the Funnels and Conversations tables. There is no foreign key: it is an
HMAC value produced independently by each app from the shared pepper.

## Deployment

```mermaid
flowchart LR
    lb[TLS terminating proxy] --> app1["intelligence container<br/>uvicorn, non-root, WORKER_ENABLED=true"]
    app1 --> pg[(PostgreSQL)]
    app1 --> vol[(model volume)]
    app1 -->|egress allow-list| srcs[Funnels and Conversations]
    app1 -->|egress| anth[Anthropic]
    prom[Prometheus] --> app1
```

One instance runs the worker (the job queue is a DB table; a second worker is safe because claims are atomic
`UPDATE ... WHERE status='queued'`, but rate limiting is per instance). SQLite plus a local volume is enough for a
single node. The `JobQueue` protocol in `app/ingest/jobs.py` is the seam for Celery/Redis; that adapter is not built.
