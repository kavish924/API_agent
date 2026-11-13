# API Change Guardian — Architecture

> Matches PRD v3 and the specs in [`../specs/`](../specs/README.md).
> Diagrams are [Mermaid](https://mermaid.js.org/), rendered natively on GitHub.

---

## 1. System overview

The trigger is the provider: a new SDK release or a changed deprecation page. A daily
poll finds it, and one run starts per installed repository.

```text
Daily poll -> watcher -> change records -> release gate -> fan-out (one run per repo):
  scan -> fix (bump · codemods · agent · AST guard) -> sandbox -> draft upgrade PR / issue / nothing
```

```mermaid
flowchart LR
    subgraph GitHub
        EV["App events<br/>install, uninstall"]
        REPO["Repository<br/>default branch head"]
        REL["SDK releases<br/>(openai, stripe)"]
        OUT["Draft upgrade PR<br/>or issue"]
    end

    subgraph GCP["API Change Guardian on GCP"]
        APP["App service<br/>Cloud Run webhooks"]
        SCH["Cloud Scheduler<br/>daily poll"]
        Q["Pub/Sub<br/>poll and run jobs"]
        subgraph VM["Worker (Compute Engine VM)"]
            W1["1 Watcher<br/>releases, API pages"] --> W2["2 Change records<br/>extract + API diff"] --> W3["3 Scanner<br/>libcst call sites"]
            W3 --> W4["4 Fixer<br/>codemods · agent · AST guard"] --> W5["5 Sandbox runner<br/>Docker · L1 to L3"] --> W6["6 PR writer"]
        end
        SQL[("Cloud SQL")]
        GCS[("Cloud Storage")]
        SM["Secret Manager"]
        VAI["Vertex AI"]
    end

    PYPI["PyPI"]
    DOCS["Provider docs<br/>deprecation pages"]

    EV -->|webhook| APP --> Q
    SCH --> Q
    Q --> W1
    REL -->|release notes| W1
    DOCS --> W1
    REPO -->|clone| W3
    W6 -->|open PR| OUT
    VM --> SQL
    VM --> GCS
    VM --> SM
    VM --> VAI
    VM --> PYPI
```

**Components:** scheduler (Cloud Scheduler), webhook service (Cloud Run), job queue
(Pub/Sub), and one worker (Compute Engine VM) that runs the watcher, change-record
builder, scanner, fixer, sandbox runner and PR writer.

The webhook service only checks the signature and publishes a job. The worker is the
only part that reaches the database, storage, the LLM, GitHub, PyPI and provider pages.

---

## 2. Layered view

```mermaid
flowchart TB
    subgraph Edge["Edge"]
        SCHED["Scheduled poll"]
        WEBHOOK["Installation webhooks"]
        GHAPI["GitHub REST API"]
        CLI["guardian CLI"]
    end
    subgraph App["Application"]
        SVC["App service<br/>(FastAPI)"]
        POLL["pipeline.poll<br/>watch · gate · fan-out"]
        RUN["pipeline.run<br/>scan · fix · verify · deliver"]
    end
    subgraph Tools["Analysis tools"]
        LLM["LLM client<br/>structured output · tool use"]
        GRIFFE["griffe API differ"]
        LIBCST["libcst<br/>scanner + codemods"]
        AGENT["Agent fixer<br/>bounded tools"]
        GUARD["AST guard"]
        SBX["Docker sandbox"]
    end
    subgraph Interfaces["Swappable interfaces"]
        DB[("Database<br/>SQLite / Cloud SQL")]
        QUEUE["Job queue<br/>in-process / Pub/Sub"]
        BLOB[("Blob store<br/>local dir / Cloud Storage")]
    end

    SCHED --> QUEUE
    WEBHOOK --> SVC --> QUEUE
    QUEUE --> POLL --> QUEUE
    QUEUE --> RUN
    CLI --> POLL
    CLI --> RUN
    POLL --> LLM
    POLL --> GRIFFE
    RUN --> LIBCST
    RUN --> AGENT --> GUARD
    AGENT --> LLM
    AGENT --> SBX
    RUN --> SBX
    RUN --> GHAPI
    POLL --> DB
    RUN --> DB
    POLL --> BLOB
```

The three interfaces at the bottom are why the same code runs on a laptop and on GCP.

---

## 3. End-to-end sequence

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Cloud Scheduler
    participant Q as Pub/Sub
    participant WK as Worker
    participant SRC as Provider sources
    participant LLM as Vertex AI
    participant GH as GitHub
    participant SB as Docker sandbox

    SCH->>Q: poll job (daily)
    Q->>WK: Deliver poll
    WK->>SRC: Conditional fetch (ETag)
    SRC-->>WK: New release note / changed page
    WK->>LLM: Extract change records (no tools, schema only)
    WK->>WK: Evidence check · API diff vs previous release
    alt No breaking or deprecating change
        WK->>Q: Ack (release gate: no runs)
    else Passes the gate
        WK->>Q: One run job per installed repo
    end

    Q->>WK: Deliver run (one repo)
    WK->>GH: Read dependency files on the default branch
    alt Package not used, not pinned, or up to date
        WK->>Q: Ack (run ends silently)
    else Pinned version is behind
        WK->>WK: Notes for (pinned, latest] · API diff · confidence
        WK->>GH: Clone default branch head
        WK->>WK: libcst scan for call sites
        alt No findings
            WK->>Q: Ack (no output)
        else Findings
            WK->>WK: Bump editor + codemods
            WK->>LLM: Agent session per remaining finding
            WK->>WK: AST guard on every proposed patch
            WK->>SB: Install (PyPI only), then run offline
            SB-->>WK: Level reached (L1 / L2 / L3) or failure
            alt L1 or higher (within 2 retries)
                WK->>GH: Branch guardian/upgrade-… + draft PR (bump + fix)
            else Not verified, or report-only changes
                WK->>GH: One issue listing the call sites
            end
            WK->>Q: Ack
        end
    end
```

---

## 4. The agent fixer loop

The fixer is the one place where a model chooses the next action. It can read, check
and propose. It cannot run commands, reach the network, or write to the repo.

```mermaid
flowchart TB
    START["Finding + change record<br/>+ enclosing function"] --> THINK{"Model picks<br/>a tool"}
    THINK -->|read_file / search_repo| REPOT["Scanned checkout<br/>(confined, secrets denied)"]
    THINK -->|lookup_api| SURF["New SDK surface<br/>(griffe)"]
    THINK -->|get_change| REC["Change record<br/>+ migration hint"]
    THINK -->|run_typecheck| SBX["Offline sandbox<br/>new errors only"]
    REPOT --> THINK
    SURF --> THINK
    REC --> THINK
    SBX --> THINK
    THINK -->|propose_patch| GUARD{"AST guard<br/>+ size cap"}
    GUARD -->|rejected: reason returned| THINK
    GUARD -->|accepted| DONE["Patch joins the run<br/>→ full verification"]
    THINK -->|cannot_fix| REPORT["Finding becomes<br/>report-only"]
    BUDGET["Budget: steps · proposals<br/>tokens · wall clock"] -.->|exceeded| REPORT
```

Every step is written to `agent_steps`, so a session can be replayed. A single-shot
mode (one LLM call, no tools) runs on the same findings for comparison in the evals.

---

## 5. Data model

```mermaid
erDiagram
    SOURCES ||--o{ SOURCE_DOCUMENTS : "publishes"
    SOURCE_DOCUMENTS ||--o{ CHANGE_RECORDS : "yields"
    REPOS ||--o{ RUNS : "has"
    RUNS ||--o{ FINDINGS : "produces"
    CHANGE_RECORDS ||--o{ FINDINGS : "matched by"
    REPOS ||--o{ PULL_REQUESTS : "receives"
    RUNS ||--o{ LLM_CALLS : "costs"
    FINDINGS ||--o{ AGENT_STEPS : "fixed through"

    REPOS {
        int installation_id
        string full_name
        bool active
        string default_branch
    }
    SOURCES {
        string kind
        string provider
        string package
        string etag
        string last_seen
    }
    SOURCE_DOCUMENTS {
        string version_or_snapshot
        int revision
        string content_hash
        string raw_text_ref
    }
    CHANGE_RECORDS {
        string layer
        string change_type
        string qualified_symbol
        string identifier
        string replacement
        bool fixable
        date shutdown_date
        string evidence_quote
        string origin
    }
    RUNS {
        string target
        string head_sha
        string from_version
        string to_version
        string outcome
        string verification_level
    }
    FINDINGS {
        string file
        int line
        string matched_symbol
        string match_tier
        string confidence
        string fix_tier
        string fix_status
    }
    PULL_REQUESTS {
        string package_or_source
        int number
        string branch
        string state
        json covered_change_ids
        json declined_change_ids
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
    AGENT_STEPS {
        int step_number
        string tool
        json arguments
        string result_summary
        int tokens
        float latency_ms
    }
```

- `source_documents` is keyed on (source, version or snapshot, revision). An edited
  note becomes a new revision, never a second record.
- `runs` is keyed on (repo, target, head SHA), which makes queue redelivery safe. A
  target looks like `openai@1.45.0` or `openai-deprecations@rev7`.
- `pull_requests` remembers the change IDs a closed PR covered, so the same changes
  are never proposed twice.

---

## 6. Trust boundaries and safety design

```mermaid
flowchart LR
    subgraph Untrusted["Untrusted input"]
        NOTE["Release notes"]
        PAGE["Provider pages"]
        CODE["Repo code and comments"]
        TOUT["Tool and test output"]
        HOOK["Webhook payloads"]
    end
    subgraph Controls["Controls"]
        SIG["Signature check"]
        SCHEMA["Extraction:<br/>no tools · schema only"]
        EVID["Evidence-quote check<br/>symbol-exists check"]
        TOOLS["Agent: fixed tools<br/>budgets · path confinement"]
        GUARD["AST guard on<br/>model-written diffs"]
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
    NOTE --> SCHEMA
    PAGE --> SCHEMA
    SCHEMA --> EVID
    CODE --> TOOLS
    TOUT --> TOOLS
    TOOLS --> GUARD
    CODE --> INSTALL --> RUN
    GUARD --> RUN
    RUN --> DRAFT --> MERGE
```

| Threat | Control |
|---|---|
| Injected text steers extraction | No tools; schema-only output; evidence-quote and symbol-exists checks |
| One poisoned release note reaches every installed repo through fan-out | Same checks; drafts only; the pause switch; fixes still pass the guard per repo |
| Injected text in a repo file steers the agent into writing harmful code | AST guard on every proposed patch: edits confined to matched statements and SDK imports; no new non-SDK imports; `.py` files with findings only |
| Agent reads secrets committed in the repo and sends them to the model | Read tools confined to the checkout; secret-looking files denied; output size caps |
| Agent runs away in a loop or runs up cost | Step, proposal, token and time budget per finding; per-run cost cap |
| Agent reaches outside the repo | No shell, network, GitHub or write tools; path checks reject escapes and symlinks |
| PR branch runs in the repo's own CI, which may hold secrets | Same AST guard; no Workflows permission; issue-only mode pushes nothing |
| Repo code attacks the host during install or tests | Docker, non-root, no secrets, PyPI-only network for install, no network for run, CPU and time limits |
| Sandbox reads the VM's service-account token from the metadata server | Firewall rule blocks 169.254.169.254 from the Docker networks; minimum roles on the worker's service account |
| Leaked or over-broad cloud credentials | Separate service accounts for the webhook service and the worker; secrets only in Secret Manager |
| Forged or flooding webhooks | Signature check on every request; Cloud Run max instances capped |

The structural defence is what a model's output is allowed to do, not what the prompt
asks of it. Residual risk: the AST guard narrows what a model-written diff can do but
does not prove it harmless. Repos with sensitive CI secrets should use issue-only mode.

---

## 7. Deployment view

### On GCP

```text
   Cloud Scheduler ── daily poll ──┐                GitHub
                                   │      webhook (signed)    REST API (installation token)
                                   │            │                       ▲
                                   ▼            ▼                       │
                           ┌──────────────────────────┐   ┌──────────────────┐
                           │ Pub/Sub topic            │◄──│ Cloud Run        │
                           │  jobs: poll · run · event│   │ App service      │
                           │  (+ dead-letter topic)   │   └──────────────────┘
                           └───────┬──────────▲───────┘
                              pull │          │ publish run jobs (fan-out)
                                   ▼          │
   ┌────────────────────────── Compute Engine VM ──────────────────────────────┐
   │  worker (systemd)  ──►  pipeline.poll / pipeline.run                      │
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
- A pause switch stops every write to GitHub without a deploy.

### Local

```text
guardian CLI ──► pipeline.poll / pipeline.run ──► SQLite · local blob directory · local Docker sandbox
```

No GitHub App and no cloud account are needed for local runs. Details →
[`../specs/09-gcp-deployment.md`](../specs/09-gcp-deployment.md)
