# API Change Guardian — Architecture

> Matches PRD v2 and the specs in [`../specs/`](../specs/README.md).
> Diagrams are [Mermaid](https://mermaid.js.org/), rendered natively on GitHub.

---

## 1. System overview

One trigger starts everything: a Dependabot or Renovate PR that bumps a watched SDK.

```text
Bump PR webhook -> App service -> queue -> Worker:
  discovery -> extraction -> scan -> fix -> sandbox -> draft fix PR / comment / nothing
```

```mermaid
flowchart LR
    subgraph GitHub
        BUMP["Bump PR<br/>(Dependabot / Renovate)"]
        REPO["Repository<br/>bump branch head"]
        REL["SDK releases<br/>(openai, stripe)"]
        OUT["Draft fix PR<br/>or comment"]
    end

    subgraph GCP["API Change Guardian on GCP"]
        APP["App service<br/>Cloud Run · checks signature"]
        Q["Pub/Sub<br/>one message per run"]
        subgraph VM["Worker (Compute Engine VM)"]
            D["1 Discovery<br/>notes + griffe diff"] --> E["2 Extractor<br/>schema + checks"] --> S["3 Scanner<br/>libcst call sites"]
            S --> F["4 Fixer<br/>codemods · AST guard"] --> V["5 Sandbox runner<br/>Docker · L1 to L3"] --> W["6 PR writer"]
        end
        SQL[("Cloud SQL")]
        GCS[("Cloud Storage")]
        SM["Secret Manager"]
        VAI["Vertex AI"]
    end

    PYPI["PyPI"]

    BUMP -->|webhook| APP -->|publish| Q --> D
    REPO -->|clone| S
    REL -->|release notes| D
    W -->|open PR| OUT
    VM --> SQL
    VM --> GCS
    VM --> SM
    VM --> VAI
    VM --> PYPI
```

**Components:** webhook service (Cloud Run), job queue (Pub/Sub), and one worker
(Compute Engine VM) that runs change discovery, the LLM extractor, the libcst scanner,
the fixer, the sandbox runner and the PR writer.

The webhook service only checks the signature and publishes a message. The worker is
the only part that reaches the database, storage, the LLM, the SDK release pages and
PyPI.

---

## 2. Layered view

```mermaid
flowchart TB
    subgraph Edge["Edge"]
        WEBHOOK["Bump PR webhooks"]
        GHAPI["GitHub REST API"]
        CLI["guardian CLI"]
    end
    subgraph App["Application"]
        SVC["App service<br/>(FastAPI)"]
        PIPE["pipeline.run<br/>(one function, six stages)"]
    end
    subgraph Tools["Analysis tools"]
        LLM["LLM client<br/>(structured output, no tools)"]
        GRIFFE["griffe API differ"]
        LIBCST["libcst<br/>scanner + codemods"]
        SBX["Docker sandbox"]
    end
    subgraph Interfaces["Swappable interfaces"]
        DB[("Database<br/>SQLite / Cloud SQL")]
        QUEUE["Job queue<br/>in-process / Pub/Sub"]
        BLOB[("Blob store<br/>local dir / Cloud Storage")]
    end

    WEBHOOK --> SVC --> QUEUE --> PIPE
    CLI --> PIPE
    PIPE --> GHAPI
    PIPE --> LLM
    PIPE --> GRIFFE
    PIPE --> LIBCST
    PIPE --> SBX
    PIPE --> DB
    PIPE --> BLOB
```

The three interfaces at the bottom are why the same code runs on a laptop and on GCP.

---

## 3. End-to-end sequence

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant APP as App service
    participant Q as Pub/Sub
    participant WK as Worker
    participant LLM as Vertex AI
    participant SB as Docker sandbox

    GH->>APP: pull_request opened / synchronize (bot author)
    APP->>APP: Verify signature
    APP->>Q: Publish run request
    Q->>WK: Deliver message
    WK->>GH: Read dependency-file diff
    WK->>WK: Old → new version of a watched package
    alt Not an upgrade of a watched package
        WK->>Q: Ack (run ends silently)
    else Upgrade
        WK->>GH: Release notes for (old, new] (cached, revisions)
        WK->>WK: griffe diff of old vs new
        WK->>LLM: Extract change records (once per note, cached)
        WK->>WK: Evidence check · symbol-exists check · confidence
        WK->>GH: Clone bump branch head
        WK->>WK: libcst scan for call sites
        alt No findings
            WK->>Q: Ack (no output)
        else Findings
            WK->>WK: Codemod, or LLM diff + AST guard
            WK->>SB: Install (PyPI only), then run offline
            SB-->>WK: Level reached (L1 / L2 / L3) or failure
            alt L1 or higher (within 2 retries)
                WK->>GH: Branch guardian/fix-… + draft PR based on bump branch
            else Not verified, or report-only changes
                WK->>GH: One comment on the bump PR
            end
            WK->>Q: Ack
        end
    end
```

---

## 4. Data model

```mermaid
erDiagram
    RELEASE_NOTES ||--o{ CHANGE_RECORDS : "yields"
    RUNS ||--o{ FINDINGS : "produces"
    CHANGE_RECORDS ||--o{ FINDINGS : "matched by"
    RUNS }o--|| PULL_REQUESTS : "delivers to"
    RUNS ||--o{ LLM_CALLS : "costs"

    RELEASE_NOTES {
        string provider
        string package
        string version
        int revision
        string content_hash
        string raw_text_ref
    }
    CHANGE_RECORDS {
        string change_type
        string qualified_symbol
        string affected_parameter
        string replacement
        bool fixable
        string evidence_quote
        string migration_hint
        string source
    }
    RUNS {
        string repo
        string package
        string from_version
        string to_version
        string head_sha
        string outcome
        string verification_level
    }
    FINDINGS {
        string file
        int line
        string matched_symbol
        string match_tier
        string confidence
        string fix_status
    }
    PULL_REQUESTS {
        string repo
        string package
        string to_version
        int bump_pr_number
        int fix_pr_number
        string state
        bool closed_remembered
    }
    LLM_CALLS {
        string purpose
        string model
        json input
        json output
        int tokens
        float latency_ms
        float cost
    }
```

- `release_notes` is keyed on (package, version, revision). An edited note becomes a
  new revision, never a second record.
- `runs` is keyed on (repo, package, to_version, head_sha), which makes queue
  redelivery safe.
- `pull_requests` is keyed on (repo, package, to_version), which is how a closed fix
  PR is remembered.

---

## 5. Trust boundaries and safety design

```mermaid
flowchart LR
    subgraph Untrusted["Untrusted input"]
        NOTE["Release notes"]
        CODE["Repo code and comments"]
        TOUT["Test output (retries)"]
        HOOK["Webhook payloads"]
    end
    subgraph Controls["Controls"]
        SIG["Signature check"]
        SCHEMA["No tools · schema-only output"]
        EVID["Evidence-quote check<br/>symbol-exists check"]
        GUARD["AST guard on LLM diffs"]
    end
    subgraph Sandbox["Docker sandbox"]
        INSTALL["Install: PyPI only, no secrets"]
        RUN["Run: no network, no secrets"]
    end
    subgraph Human["Human in the loop"]
        DRAFT["Draft PR on its own branch"]
        MERGE["Human merges"]
    end

    HOOK --> SIG
    NOTE --> SCHEMA --> EVID
    CODE --> SCHEMA
    TOUT --> SCHEMA
    SCHEMA --> GUARD
    CODE --> INSTALL --> RUN
    GUARD --> RUN
    RUN --> DRAFT --> MERGE
```

| Threat | Control |
|---|---|
| Injected text steers extraction | No tools; schema-only output; evidence-quote and symbol-exists checks |
| Injected text steers the fixer into writing harmful code | AST guard: edits confined to matched statements and SDK imports; no new non-SDK imports; `.py` files with findings only |
| Fix branch runs in the repo's own CI, which may hold secrets | Same AST guard; no Workflows permission; comment-only mode pushes nothing |
| Repo code attacks the host during install or tests | Docker, non-root, no secrets, PyPI-only network for install, no network for run, CPU and time limits |
| Sandbox reads the VM's service-account token from the metadata server | Firewall rule blocks 169.254.169.254 from the Docker networks; minimum roles on the worker's service account |
| Leaked or over-broad cloud credentials | Separate service accounts for the webhook service and the worker; secrets only in Secret Manager |
| Forged or flooding webhooks | Signature check on every request; Cloud Run max instances capped |

Residual risk: the AST guard narrows what an LLM diff can do but does not prove it
harmless. Repos with sensitive CI secrets should use comment-only mode.

---

## 6. Deployment view

### On GCP

```text
                     GitHub
        webhook (HTTPS, signed)      REST API (installation token)
              │                              ▲
              ▼                              │
   ┌──────────────────┐   publish   ┌─────────────────────────────────────────┐
   │ Cloud Run        │────────────►│ Pub/Sub topic  ──►  pull subscription    │
   │ App service      │             │                    (+ dead-letter topic) │
   └──────────────────┘             └───────────────────┬─────────────────────┘
                                                        │ pull
                                                        ▼
   ┌────────────────────────── Compute Engine VM ──────────────────────────────┐
   │  worker (systemd)  ──►  pipeline.run                                      │
   │        │                                                                  │
   │        └──►  Docker sandbox:  install net (PyPI-only proxy) │ run (no net) │
   │              firewall: sandbox networks ✕ 169.254.169.254                 │
   └───────┬──────────────┬──────────────┬──────────────┬─────────────────────┘
           ▼              ▼              ▼              ▼
       Cloud SQL    Cloud Storage  Secret Manager   Vertex AI
```

- The worker is a VM because the sandbox must start containers with the network off,
  and Cloud Run cannot start containers from inside a container.
- One run at a time on the VM. The VM can be stopped between demos; Cloud Run scales
  to zero.

### Local

```text
guardian CLI ──► pipeline.run ──► SQLite · local blob directory · local Docker sandbox
```

No GitHub App and no cloud account are needed for local runs. Details →
[`../specs/09-gcp-deployment.md`](../specs/09-gcp-deployment.md)
