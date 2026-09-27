# New Orayan Market Intelligence

An independent, observation-only service for one spare 512 MB instance. It is not imported by New Orayan, has no trading credentials, and cannot block or authorize orders. Linkage is one-way: exported candidate births may be imported later for offline joins.

## What it collects

- Scheduled macro events from the official BLS calendar (CPI, PPI, Employment Situation/NFP), the official BEA release schedule (Personal Income and Outlays/PCE), and the Federal Reserve FOMC calendar. Dates are interpreted in `America/New_York` and stored in UTC. Every retrieval creates or preserves a schedule revision.
- Federal Reserve speeches and monetary-policy releases from configured official RSS feeds. `publisher_time_utc` is retained, but causality uses the later local `first_seen_at_utc` / `available_to_system_at_utc`.
- Bybit public linear-ticker context: BTC/ETH funding, open interest, mark/index basis, 24-hour return, ETH/BTC relative price, and market breadth. No private API key is used.
- Optional FRED series (`DGS10`, `DTWEXBGS`, `SP500`, `VIXCLS`) when a user supplies a FRED API key. These are low-frequency context, not executable quotes.
- Optional trusted RSS sources for verified exchange incidents, maintenance, listings, protocol upgrades, or other unscheduled events. Only configured publishers are treated as trusted. Token-unlock sources are intentionally not preconfigured because licensing, quotas, and timestamp semantics vary; add one only after reviewing those terms.
- Optional Gemini observer. It is bounded, deadline-limited, circuit-broken, JSON-only, and always saved as `authoritative=false`. A Gemini Pro web subscription is not assumed to provide API access.

## Phase 1 shadow briefings

Gemini remains off by default (`MI_GEMINI_ENABLED=false`) and therefore costs nothing. When explicitly enabled, the existing observer runs only inside this service after a collection cycle. It creates at most two briefing records per 30 minutes, uses a queue of at most two items, has a hard request deadline and at most one retry, and opens a circuit breaker after repeated failures. It never imports trading code, credentials, positions, signals, or orders.

Each request uses the fixed stable `gemini-3.8-flash` model, prompt `MARKET_BRIEFING_PROMPT_V3`, and validated JSON schema `MARKET_BRIEFING_V1`. Inputs contain timestamped official calendar rows, configured verified news, source health, and the latest Bybit breadth/BTC/ETH observations. Missing or stale core observations cause a local abstention without an API request. FOMC meeting dates may be official while decision/press clock times remain explicitly labelled assumptions.

Briefing requests, completions, errors, prompt/model/schema versions, input snapshots and watermarks, latency, token usage when returned, and availability timestamps are stored in the same SQLite database and hourly `briefings-*.jsonl` export. API, quota, timeout, malformed-response, stale-input, disabled, and circuit states are also visible in `source_health`. No briefing is authoritative, connected to New Orayan, or used for automatic rule learning.

The service does not claim complete market-wide liquidation history. Public Bybit ticker context is included; a liquidation-shock adapter should be added only from a licensed, verifiable feed that provides receipt timestamps. Missing data remains `NOT_AVAILABLE` rather than zero.

## Storage and memory safety

SQLite uses WAL and `synchronous=NORMAL` on the service’s own persistent volume. Each stored event/observation is also appended to an hourly JSONL file. Manifest generation reads fixed file sizes in 64 KiB chunks and records row counts and SHA-256 hashes. HTTP responses, concurrency, the Gemini queue, and response bytes are bounded. A source failure updates `source_health` and does not stop other adapters.

Key causal timestamps:

- `publisher_time_utc`: what the publisher says.
- `first_seen_at_utc`: first local retrieval.
- `observed_at_utc`: retrieval/measurement time.
- `available_to_system_at_utc`: earliest defensible time the service could have used it.

Never substitute publisher time for system availability in backtests.

## Local setup

1. Copy `.env.example` values into the instance environment; do not put credentials in files.
2. Copy `config.example.json` to `config.json` and enable only reviewed sources.
3. Run `python -m pip install .`.
4. Initialize/check storage with `orayan-market-intel migrate`.
5. Test one collection cycle with `orayan-market-intel once`.
6. Run continuously with `orayan-market-intel run`.

Read-only diagnostics:

```text
orayan-market-intel status
orayan-market-intel briefings --limit 5
```

For a container, mount a persistent volume at `/data`, set `MI_DATA_DIR=/data`, and build the included Dockerfile. This package has not been deployed or started on Northflank.

For Northflank Phase 1 setup, see `docs/PHASE1_GEMINI.md`. Keep the included Dockerfile command unchanged and never put a Gemini key in source control, a config file, or chat.

## Optional phone export UI

The service can expose a small, read-only download page from the same process. It is disabled by default and has no shell, trading controls, mutations, or unauthenticated status details. It uses a password-style token form, short-lived in-memory sessions, `HttpOnly Secure SameSite=Strict` cookies, CSRF checks, constant-time credential comparison, throttling, HTTPS enforcement, bounded request threads, and one export at a time. There is no client JavaScript.

To enable it only after reviewing the security trade-off, set runtime variables:

```text
MI_EXPORT_UI_ENABLED=true
MI_EXPORT_UI_PORT=8080
MI_EXPORT_TOKEN=<a randomly generated secret of at least 32 bytes>
```

Keep `MI_EXPORT_TOKEN` in a Northflank secret group/runtime secret, never in Git. Northflank must route an HTTPS HTTP port to container port `8080`; the server itself binds to `0.0.0.0`. `/healthz` returns only `ok` and no service metadata. Browser downloads use a secure session; scripts can send `Authorization: Bearer <token>` to the same POST download route.

Exports are prepared under `/tmp`, removed after the request, and never duplicated on `/data`. “Everything” bundles contain an online SQLite backup plus sanitized hourly JSONL. Date-limited bundles contain matching hourly JSONL; table-specific downloads contain sanitized events, observations, or briefings JSONL. Every ZIP contains row counts, source health without error details, a source watermark, migration hashes, per-file SHA-256 hashes, and an export manifest. A full database bundle is refused if credential-like content is detected.

Optional safety limits (defaults shown):

```text
MI_EXPORT_REQUIRE_HTTPS=true
MI_EXPORT_SESSION_SECONDS=3600
MI_EXPORT_REQUEST_TIMEOUT_SECONDS=120
MI_EXPORT_MAX_INPUT_MIB=192
MI_EXPORT_MAX_ZIP_MIB=192
MI_EXPORT_TMP_QUOTA_MIB=384
MI_EXPORT_MAX_HTTP_THREADS=8
```

See `docs/EXPORT_UI_NORTHFLANK.md` for phone-friendly rollout, backup, rollback, and first-24-hour checks. Enabling any public endpoint adds attack surface; use private/VPC ingress with an authenticated access layer when available.

## Offline shadow linkage

Export New Orayan prospective JSONL, copy it to this separate instance, then run:

```text
orayan-market-intel import-candidates /imports/orayan2_prospective_compact_v4_all.jsonl
```

The importer accepts only `candidate_birth` rows and deduplicates by `candidate_id`. There is no callback, shared database, synchronous request, or runtime dependency on this service.

## Source notes

- BLS calendar: `https://www.bls.gov/schedule/news_release/bls.ics` (official and revision-aware).
- BEA release schedule: `https://www.bea.gov/news/schedule` (official page; HTML changes surface as source-health failures).
- Federal Reserve FOMC calendar: `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm`. The meeting date is official; 14:00/14:30 New York times are explicitly marked as standard-time assumptions and must be reviewed if Fed publication conventions change.
- Federal Reserve RSS: official feed URLs in `config.example.json`.
- Bybit V5 public tickers: no authentication; exchange availability and rate limits apply.
- FRED API: optional key, source-native update frequency, and FRED terms/quotas apply.

## Tests

Run `python -m unittest discover -s tests -v`. Tests cover schedule revisions, no-lookahead availability, restart dedupe, one-way candidate import, JSONL manifests/hashes, BLS timezone conversion, RSS causality, all disabled/no-key/success/malformed/quota/timeout/stale/network/restart/bounded Gemini paths without paid API requests, and export authentication, cookies/headers, CSRF, throttling, concurrent SQLite writes, concurrent JSONL appends, ZIP integrity, hashes, path safety, credential audits, failure isolation, MIME/disposition, and bounded-memory large files.

