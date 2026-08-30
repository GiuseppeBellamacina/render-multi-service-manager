# API Guidelines — conventions for uniform service APIs

This document defines the **contract** a service must satisfy to be managed by `render-service-manager`. The three current services (committer, t2g, credit) already satisfy most of it: residual differences are annotated in the roadmap (section 11). Any **new** service should satisfy it in full.

## 1. The contract at a glance

| Aspect | Rule |
|---|---|
| Structure | One entry file, self-contained, known dependencies |
| Actions | `tick` **mandatory** · `status` strongly recommended |
| Signatures | Zero required arguments · `async def` or `def` honestly declared in the manifest |
| Responses | Always JSON · structured errors · never "ok" with a nested error |
| Robustness | Timeout on every I/O · never crash the process · idempotent tick |
| Security | Token only via the `X-Auth-Token` header · never in the query string · never log secrets |
| Env vars | Service prefix · required ones in the manifest · fail-fast on import |
| Schedule | Declared in the manifest · light tick (<60 s) |
| Mount | Relative routes · CORS if there is a browser dashboard · no lifespan needed |
| Updates | The source repo is the truth · fallback = last-known-good · breaking change → update the manifest at the same time |

## 2. Structure of a manageable service

- **One entry file** (declared in `entry`), importable as a module: importing must have no side effects beyond reading env vars and building the app object.
- **Self-contained**: it may load additional fetched files (as t2g does with `cluster_helper.sh` and `src/utils/chain_monitor.py`), but everything it needs must be listed in the manifest's `source.files`.
- **Pip dependencies** declared in the manifest's `dependencies` AND present in the manager's `requirements.txt` (the manager checks and warns, but the import still fails if they are truly missing).
- **No infinite loops, threads or internal timers**: scheduling is the manager's job. The service receives time from the manager; it does not create its own.

## 3. Uniform actions

Actions are the module functions the manager can invoke as `GET|POST /services/{name}/{action}` and inside the global `/tick`.

- **`tick`** (mandatory unless `schedule.mode: disabled`):
  - Semantics: ONE unit of the service's work (commit or not, advance the chain, sample the credit).
  - **Idempotent**: two ticks in a row must cause no harm (the manager holds a lock anyway, but the service must not rely on it).
  - **Light**: below its `timeout_s` (default 60 s) — internal I/O with explicit timeouts.
  - Never crash: a problem becomes a structured error (see section 4), not an exception that kills the process.
- **`status`** (recommended): read-only, no heavy I/O (t2g does it perfectly with `_cached_status`, which only reads the SQLite cache). It is what the manager's `GET /status` shows at a glance.
- **Signatures**: zero required positional arguments. The committer's legacy `tick(request)` (unused parameter) is **tolerated** through the manager's introspection but must not be replicated.
- The manifest's **`sync`** flag must reflect reality (`async def` → `sync: false`). The manager auto-corrects in both directions, but the manifest is documentation: a wrong flag produces misleading reports.

## 4. Response format and normalization

The manager normalizes EVERY action into:

```json
{"ok": true|false, "ms": <duration>, "data": {...}}        // success
{"ok": false, "ms": <duration>, "error": "<detail>"}      // failure
```

For this to work without surprises:

- Ticks return **dict/JSONResponse**: the manager decodes `JSONResponse` automatically, but a plain dict is preferred.
- Errors: either `raise HTTPException(detail=...)` (the manager extracts `detail`) or return `{"error": "..."}`. **Never** `{"status": "ok", "error": ...}`: an error nested inside a success is the easiest way to lie to the report (real case: before the fix, the committer counted a failed PUT as a done commit).
- No action returns plain text.
- `skipped` with a `reason` is a legitimate, useful answer ("sleeping", "interval not elapsed", "disabled") — keep it distinct from an error.

## 5. Error handling and timeouts

- **Every I/O operation has an explicit timeout.** Case study: the committer's `make_github_commit` used to call `requests.get/put` WITHOUT a timeout → threads hanging forever + false successes. The manager caps actions with an outer `timeout_s`, but the cap does not kill the underlying thread: the service's internal timeout is the only real remedy.
- Check HTTP status codes explicitly: `>= 400` → `{"error": ...}`.
- Exceptions at **import** time (missing env, broken module): the service ends up `loaded: false` with the message visible — never good, never fatal for the others.
- A failing service must NOT block the other services' tick: the manager guarantees this by isolating every action; the service cooperates by not calling `os._exit` and not leaking memory per tick.

## 6. Security

- **Token**: `X-Auth-Token` header only, never in the query string (it would end up in server and proxy logs). Compare with `secrets.compare_digest` (constant time).
- **Never log** tokens, keys or sensitive content.
- **Private keys**: written at runtime to files with 0600 permissions (the `_setup_key` pattern), never committed to the repo.
- If the service has its **own internal auth** (t2g: every route requires its token), declare `api.auth: service` in the manifest: the manager's token stays with the manager, the service's token with the service. Clients like the t2g TUI keep working because the manager also accepts the frontier service's token.

## 7. Environment variables

- **Service prefix**: `T2G_*`, `GITHUB_*` (committer), `OPENROUTER_*`/`UPSTASH_*` (credit). Zero collisions between services.
- `env.required` in the manifest: the manager **validates before importing** — a Render dashboard missing `T2G_SSH_USER` produces "missing environment variables: T2G_SSH_USER", not a cryptic traceback.
- `env.optional` with defaults: the manager applies `setdefault`, so a new default lives in the manifest, not in N dashboards.
- **Consistent** fail-fast: if the service validates env vars at import (credit does), the names in the manifest must match exactly (note: the credit service reads `UPSTASH_REDIS_URL`/`UPSTASH_REDIS_TOKEN`, NOT the `*_REST_*` variants).

## 8. Scheduling

- `every_tick`: for logic that decides on its own (committer: "I sleep outside working hours"; t2g: "I advance if there is something to advance"; credit: "I sample").
- `interval_minutes` + `interval_minutes: N`: for sparse sampling — the manager tracks the last-run timestamp **per service**.
- A tick **consumes its slot even on error** (no retry storms: a down service does not generate a burst of attempts; the problem shows up in the tick report and logs).
- `disabled`: the service is loaded and manually invokable via `/services/{name}/tick`, but does not join the global tick.

## 9. Mount compatibility and original APIs

- The service is mounted 1:1 on its `api.mount` (`/t2g`, `/committer`, `/credit`): its routes work EXACTLY as before, under a prefix. Use **relative routes** (`app.get("/status")`, never absolute redirects).
- Keep the documentation of the original routes in `api.endpoints` of the manifest: it is what `GET /services/{name}` shows — keep it up to date.
- If the service has a **browser dashboard** that cannot send headers (credit: `/api/data`), declare it in `api.public_paths`: it is the deliberate exception to the manager token, not an implicit hole.

## 10. Update contract (fetch)

- **The source repo is the truth.** The daily fetch brings script changes; if content changed → clean restart → fresh code.
- `fallback/` is the **last-known-good** (local-only, gitignored): keep it updated when something critical changes (like the committer timeout fix), so a boot with GitHub down starts from the last known good version.
- A **new pip dependency** in a script → add it to the manager's `requirements.txt` and redeploy: the fetch does NOT install packages.
- **Breaking contract changes** (renaming `tick`, changing signatures, changing `entry`) → update `services.yaml` AT THE SAME TIME as the push, otherwise the service stays `loaded: false` until the manifest catches up.

## 11. Alignment roadmap for the current three

| Service | Compliance | Remaining actions |
|---|---|---|
| **committer** | Good | Push the timeout/status-check hardening of `make_github_commit` to the source repo (otherwise the daily fetch keeps restoring the unfixed version). Legacy `tick(request)` pattern: drop the parameter at the service's next refactor |
| **t2g** | Excellent (it is the model: internal auth, 0600 keys, read-only cache for status, KEY=VALUE snapshot) | None needed. `T2G_AUTH_TOKEN` is not in `required` so as not to break local deploys — on Render it is de facto mandatory |
| **credit** | Good | `main.py` reads `UPSTASH_REDIS_URL`/`UPSTASH_REDIS_TOKEN`: use THESE names (an older `.env` used the `*_REST_*` variants — a known source of confusion). No `/health` route of its own: the manager covers it globally. One day: expose a real `status` action with the current balance instead of just `backup_status` |

For a NEW service: copy an existing block in `services.yaml`, follow sections 1-9, add its dependencies to the manager's `requirements.txt` and, optionally, its files to a local `fallback/`. The rest (import, mount, schedule, actions, reporting) is done by the manager.