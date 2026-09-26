# Phase 1: Northflank shadow briefing setup

This is an observation-only feature in the existing Market Intelligence service. Do not add it to New Orayan and do not give this service exchange trading credentials.

## Safe deployment

1. Keep the existing persistent volume mounted at `/data` and keep `MI_DATA_DIR=/data`.
2. Build from the repository root `Dockerfile`. Do not set a Northflank CMD override; the image already runs `orayan-market-intel run`.
3. Deploy first with `MI_GEMINI_ENABLED=false`. Confirm `orayan-market-intel status` shows advancing event and observation timestamps.
4. Separately confirm that the Google account/project has Gemini API access, the fixed stable `gemini-3.8-flash` model is visible to that API project, billing/free-tier eligibility is understood, and quota is nonzero. A consumer Gemini Pro subscription is separate and does not by itself provision Gemini API quota. Do this check in Google AI Studio; do not test eligibility by enabling the Northflank worker first.
5. In the Market Intelligence service's Northflank **Secrets** or secret-backed environment settings, create `GEMINI_API_KEY`. Enter the key there only. Do not paste it into chat, commit it, bake it into the image, or place it in `config.json`.
6. Add ordinary environment variables `MI_GEMINI_ENABLED=true`, `GEMINI_MODEL=gemini-3.8-flash`, `MI_GEMINI_INTERVAL_SECONDS=1800`, and `MI_GEMINI_EVENT_TRIGGER=true`. Keep the default limits in `.env.example` initially. Routine briefings use the model's default-quality `medium` thinking level; verified-event briefings use `high`. Neither path forces `low`.
7. Redeploy only the Market Intelligence service. Do not change its volume and do not deploy or restart New Orayan.

## Checks

Run in the Market Intelligence service shell:

```text
orayan-market-intel status
orayan-market-intel briefings --limit 5
```

Healthy collection is shown by recent `latest_event_at_utc` and `latest_observation_at_utc` values plus source health. A briefing may legitimately be `ABSTAIN_STALE_INPUT`, `DISABLED_NO_KEY`, `BILLING_DISABLED`, `QUOTA_429`, `TIMEOUT`, `NETWORK_ERROR`, `MALFORMED_RESPONSE`, or `CIRCUIT_OPEN`; these states are durable and do not stop collection.

## 24–48 hour shadow validation

- Keep all outputs observation-only and review briefing evidence references against their stored input snapshot.
- Confirm collection cadence and source health remain stable with Gemini enabled.
- Confirm no more than one API briefing request appears in any 30-minute window and memory remains flat. A single request may have one bounded retry for a retryable transport or 5xx failure; a hard attempt timeout is not retried because its worker may still be finishing.
- Confirm failures do not delay source collection and restarts do not duplicate the same snapshot.
- Treat configured RSS as partial coverage; absence of news is not proof that no relevant news occurred.
- Treat FOMC meeting dates as official only when sourced from the official calendar. Do not present stored 14:00/14:30 clock assumptions as officially confirmed times.
- After 24–48 hours, keep, adjust, or disable the observer based on evidence quality, quota usage, latency, and error rate. Do not feed briefings into trading or automatic rule updates during Phase 1.

Current API references: Google AI model availability (`https://ai.google.dev/gemini-api/docs/models`), structured output (`https://ai.google.dev/gemini-api/docs/generate-content/structured-output`), billing (`https://ai.google.dev/gemini-api/docs/billing`), and rate limits (`https://ai.google.dev/gemini-api/docs/rate-limits`). Recheck them before provisioning because eligibility and quotas can change.

## Quality, token, and billing controls

- `maxOutputTokens` includes both hidden thinking and answer tokens. The default is therefore 8192, not 2048. The service records `thoughtsTokenCount`, `candidatesTokenCount`, `totalTokenCount`, `finish_reason`, latency, selected thinking level, and the output ceiling without storing raw truncated text.
- `GEMINI_ROUTINE_THINKING_LEVEL` defaults to `medium`; `GEMINI_EVENT_THINKING_LEVEL` defaults to `high`. Supported values for this fixed model are `low`, `medium`, and `high`. Invalid values fail closed before any API request. Set both to `high` if every briefing should receive maximum reasoning depth.
- The request deadline is 30 seconds and the total deadline is 45 seconds. Retries remain capped at two attempts and the circuit breaker remains active.
- Keep `MI_GEMINI_ENABLED=false` until the intended API project's plan, active model quota, and billing account are verified. A consumer Gemini Pro subscription is separate from Gemini API billing.

### Verify the claimed Google credit without exposing the key

1. In Google AI Studio, open the API keys/projects page and identify the project that owns the deployed key by project name only. Confirm its Plan shows Paid and that `gemini-3.8-flash` is available.
2. Open AI Studio Rate limits for that same project and confirm non-zero RPM, TPM, and RPD for `gemini-3.8-flash`.
3. In Google Cloud Console, switch to that exact project, then open Billing > Overview to confirm the linked billing account. Open Billing > Credits to inspect the credit name, remaining amount, eligible services, and expiration. Do not assume a $300 welcome credit covers Gemini API; eligibility depends on when and how it was granted.
4. Open Billing > Budgets & alerts and create a small monthly budget with alerts (for example 50%, 90%, and 100%). Budgets alert; they do not automatically hard-stop usage.
5. Keep cost estimates external to stored briefings unless the current rate is explicitly configured. Token diagnostics remain authoritative; rates can change.
