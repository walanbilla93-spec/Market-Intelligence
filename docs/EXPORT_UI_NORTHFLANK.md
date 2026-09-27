# Phone export UI: safe Northflank rollout

This patch does not deploy or change Northflank. The UI remains off until `MI_EXPORT_UI_ENABLED=true` is deliberately added.

## Before enabling anything

1. In Northflank, open the project, choose **Volumes**, select the volume mounted at `/data`, open **Backups**, and create a manual backup named `before-export-ui`. Wait until it succeeds. A volume backup is point-in-time and may not capture an in-flight write, so also keep one ZIP downloaded from the existing data if you have another safe method.
2. Keep the service at one replica. SQLite plus a Single Read/Write volume must not be horizontally scaled.
3. Generate a new random secret of at least 32 bytes using a trusted password manager. Do not reuse the Gemini or FRED key. Do not place it in Git, screenshots, chat, filenames, or URLs.
4. Prefer **VPC** accessibility plus your organisation's authenticated access layer when your Northflank plan and phone network support it. A project-private port alone is not reachable directly from a phone. If neither is practical, a public port is usable but adds internet attack surface even with this token gate.

Official references: [volume backups](https://northflank.com/docs/v1/application/databases-and-persistence/backup-and-clone-volumes), [persistent-volume limits](https://northflank.com/docs/v1/application/databases-and-persistence/add-a-volume), and [network accessibility choices](https://northflank.com/docs/v1/application/network/configure-ports).

## Put the patch in GitHub from a phone

1. Download and unzip the supplied patch-only ZIP on the phone.
2. In GitHub, open `walanbilla93-spec/Market-Intelligence`, create a branch such as `export-ui-review`, and upload the ZIP's files at their exact repository paths. Do not upload the patch manifest into the repository.
3. Confirm the diff does not delete or replace collection adapters, `migrations/003_briefing_diagnostics.sql`, or the Gemini V3 code. The expected baseline commit and every patched-file hash are in the supplied manifest.
4. Merge only after GitHub checks pass. If automatic deployment is enabled, pause it first so the commit does not deploy merely because it was merged.

## Northflank console settings

1. Open the existing service and leave its Docker command unchanged: `orayan-market-intel run`.
2. Add these **runtime** variables. Put `MI_EXPORT_TOKEN` in a secret group or protected runtime variable, not a build argument.

   ```text
   MI_EXPORT_UI_ENABLED=true
   MI_EXPORT_UI_PORT=8080
   MI_EXPORT_TOKEN=<new random 32+ byte secret>
   MI_EXPORT_REQUIRE_HTTPS=true
   MI_EXPORT_MAX_INPUT_MIB=192
   MI_EXPORT_MAX_ZIP_MIB=192
   MI_EXPORT_TMP_QUOTA_MIB=384
   MI_EXPORT_REQUEST_TIMEOUT_SECONDS=120
   ```

3. Open **Run → Networking**. Add or detect container port `8080`, protocol **HTTP**, and choose **VPC** if you have private ingress; otherwise choose **Public** only after accepting the risk. Northflank terminates public TLS and routes HTTPS to the container. It also redirects public HTTP to HTTPS.
4. Add an HTTP liveness/readiness check for port `8080`, path `/healthz`, expected status `200`, with a conservative initial delay. That path returns only `ok`. Do not health-check `/`, because `/` intentionally requires authentication for useful content.
5. Deploy the reviewed commit manually. Do not change the `/data` mount or `MI_DATA_DIR=/data`.

Official references: [runtime secrets](https://northflank.com/docs/v1/application/secure/inject-secrets), [ports and automatic TLS](https://northflank.com/docs/v1/application/network/configure-ports), and [health checks](https://northflank.com/docs/v1/application/observe/configure-health-checks).

## Phone verification

1. Open the generated `https://…code.run` address. Never append the token to the URL.
2. Confirm the locked page shows no row counts or source status.
3. Enter the token in the password field. Confirm the unlocked page shows counts, freshness, source-health summary, and size previews.
4. Download **Observations → Latest 24 hours** first. Open the ZIP and check `manifest.json` and its SHA-256 entries.
5. Download **Bundle → Everything** only on Wi-Fi and only if the preview fits your phone storage. It contains a consistent SQLite backup and hourly JSONL.
6. Tap **Lock** when finished. Sessions are in memory and disappear on restart; the cookie is Secure, HttpOnly, SameSite Strict, and expires after one hour by default.

## First 24 hours

- Check Northflank logs after deployment, after the first login, and after each test download. Logs should show response status only—never tokens, query strings, or form bodies.
- Watch memory, CPU, ephemeral disk, restarts, request errors, collection cadence, source health, and newest observation/event timestamps. The 512 MiB service should remain comfortably below its memory ceiling; exports use disk, not a large in-memory ZIP.
- Confirm Gemini briefings still report model `gemini-3.8-flash`, prompt `MARKET_BRIEFING_PROMPT_V3`, medium routine thinking, and `maxOutputTokens=8192`.
- Add Northflank infrastructure alerts if your plan supports them. Northflank exposes service logs/metrics and health-check replacement behavior; an incorrect health check can cause restarts, so use only `/healthz`.

Official references: [Northflank observability](https://northflank.com/docs/v1/application/observe/observability-on-northflank) and [health-check behavior](https://northflank.com/docs/v1/application/observe/configure-health-checks).

## Rollback

1. Fastest safe disable: set `MI_EXPORT_UI_ENABLED=false` and remove public/VPC accessibility from port `8080`. Redeploy. The collector continues with the original command and data.
2. If the build itself is unhealthy, deploy the previously known-good image/commit. Do not revert or delete `/data`; this patch adds no database migration.
3. Restore the pre-change volume backup only if the data itself is proven corrupt. Restore into a **new** volume first, verify it, then attach it at `/data`; do not overwrite the only copy.
4. Rotate `MI_EXPORT_TOKEN` after any suspected exposure. Existing sessions are in memory and are invalidated by restart.

The UI startup is failure-isolated: missing/short token, bind errors, or exporter failures disable or fail the optional UI without stopping the collection loop. Runtime ZIP failures clean `/tmp` and never write a bundle to `/data`.
