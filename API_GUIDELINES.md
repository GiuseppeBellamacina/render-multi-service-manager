# API Guidelines — conventions for uniform service APIs

This document defines the **contract** a service must satisfy to be managed by `render-service-manager`. The two example services in `services.yaml.example` (`ticker`, `sampler`) follow it; any service should satisfy it in full — section 11 condenses it into a checklist.

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
- **Self-contained**: it may load additional fetched files (helper scripts, data files), but everything it needs must be listed in the manifest's `source.files`.
- **Pip dependencies** declared in the manifest's `dependencies` AND present in the manager's `pyproject.toml` (the manager checks and warns, but the import still fails if they are truly missing).
- **No infinite loops, threads or internal timers**: scheduling is the manager's job. The service receives time from the manager; it does not create its own.

## 3. Uniform actions

Actions are the module functions the manager can invoke as `GET|POST /services/{name}/{action}` and inside the global `/tick`.

- **`tick`** (mandatory unless `schedule.mode: disabled`):
  - Semantics: ONE unit of the service's work (make a call, advance a job, record a sample — whatever the service does).
  - **Idempotent**: two ticks in a row must cause no harm (the manager holds a lock anyway, but the service must not rely on it).
  - **Light**: below its `timeout_s` (default 60 s) — internal I/O with explicit timeouts.
  - Never crash: a problem becomes a structured error (see section 4), not an exception that kills the process.
- **`status`** (recommended): read-only, no heavy I/O — ideally served from a local cache rather than re-fetching remote state. It is what the manager's `GET /status` shows at a glance.
- **Signatures**: zero required positional arguments. A legacy `tick(request)` with an unused parameter is **tolerated** through the manager's introspection but must not be replicated.
- The manifest's **`sync`** flag must reflect reality (`async def` → `sync: false`). The manager auto-corrects in both directions, but the manifest is documentation: a wrong flag produces misleading reports.

## 4. Response format and normalization

The manager normalizes EVERY action into:

```json
{"ok": true|false, "ms": <duration>, "data": {...}}        // success
{"ok": false, "ms": <duration>, "error": "<detail>"}      // failure
```

For this to work without surprises:

- Ticks return **dict/JSONResponse**: the manager decodes `JSONResponse` automatically, but a plain dict is preferred.
- Errors: either `raise HTTPException(detail=...)` (the manager extracts `detail`) or return `{"error": "..."}`. **Never** `{"status": "ok", "error": ...}`: an error nested inside a success is the easiest way to lie to the report (e.g. a failed upload counted as a completed step).
- No action returns plain text.
- `skipped` with a `reason` is a legitimate, useful answer ("sleeping", "interval not elapsed", "disabled") — keep it distinct from an error.

## 5. Error handling and timeouts

- **Every I/O operation has an explicit timeout.** Case study: an action calling `requests.get/put` WITHOUT a timeout leaves threads hanging forever and reports false successes. The manager caps actions with an outer `timeout_s`, but the cap does not kill the underlying thread: the service's internal timeout is the only real remedy.
- Check HTTP status codes explicitly: `>= 400` → `{"error": ...}`.
- Exceptions at **import** time (missing env, broken module): the service ends up `loaded: false` with the message visible — never good, never fatal for the others.
- A failing service must NOT block the other services' tick: the manager guarantees this by isolating every action; the service cooperates by not calling `os._exit` and not leaking memory per tick.

## 6. Security

- **Token**: `X-Auth-Token` header only, never in the query string (it would end up in server and proxy logs). Compare with `secrets.compare_digest` (constant time).
- **Never log** tokens, keys or sensitive content.
- **Private keys**: written at runtime to files with 0600 permissions (e.g. in a `setup[]` function), never committed to the repo.
- If the service has its **own internal auth** (every route requires its own token), declare `api.auth: service` in the manifest: the manager's token stays with the manager, the service's token with the service. Existing clients of the original service keep working unchanged against the mounted prefix.

## 7. Environment variables

- **Service prefix**: give each service's variables its own prefix (e.g. `TICKER_*`, `SAMPLER_*`). Zero collisions between services.
- `env.required` in the manifest: the manager **validates before importing** — a Render dashboard missing `SAMPLER_API_KEY` produces "missing environment variables: SAMPLER_API_KEY", not a cryptic traceback.
- `env.optional` with defaults: the manager applies `setdefault`, so a new default lives in the manifest, not in N dashboards.
- **Consistent** fail-fast: if the service validates env vars at import, the names in the manifest must match exactly — a mismatch (e.g. `*_URL` vs `*_REST_URL` variants of the same variable) is a classic silent failure.

## 8. Scheduling

- `every_tick`: for logic that decides on its own ("I sleep outside quiet hours", "I advance only if there is work", "I sample every tick").
- `interval_minutes` + `interval_minutes: N`: for sparse sampling — the manager tracks the last-run timestamp **per service**.
- A tick **consumes its slot even on error** (no retry storms: a down service does not generate a burst of attempts; the problem shows up in the tick report and logs).
- `disabled`: the service is loaded and manually invokable via `/services/{name}/tick`, but does not join the global tick.

## 9. Mount compatibility and original APIs

- The service is mounted 1:1 on its `api.mount` (`/ticker`, `/sampler`): its routes work EXACTLY as before, under a prefix. Use **relative routes** (`app.get("/status")`, never absolute redirects).
- Keep the documentation of the original routes in `api.endpoints` of the manifest: it is what `GET /services/{name}` shows — keep it up to date.
- If the service has a **browser dashboard** that cannot send headers (e.g. `/api/data`), declare it in `api.public_paths`: it is the deliberate exception to the manager token, not an implicit hole.

## 10. Update contract (fetch)

- **The source repo is the truth.** The daily fetch brings script changes; if content changed → clean restart → fresh code.
- `fallback/` is the **last-known-good** (local-only, gitignored): keep it updated when something critical changes, so a boot with GitHub down starts from the last known good version.
- A **new pip dependency** in a script → add it to the manager's `pyproject.toml`, run `uv lock`, and redeploy: the fetch does NOT install packages.
- **Breaking contract changes** (renaming `tick`, changing signatures, changing `entry`) → update `services.yaml` AT THE SAME TIME as the push, otherwise the service stays `loaded: false` until the manifest catches up.

## 11. Checklist for a new service

| Step | Check |
|---|---|
| 1. Manifest block | Copy an example from `services.yaml.example` and adapt it: entry, source, api, actions, schedule |
| 2. Structure | One import-safe entry file; every extra file listed in `source.files` (section 2) |
| 3. Actions | `tick` mandatory (unless `schedule.mode: disabled`), `status` recommended, honest `sync` flags (section 3) |
| 4. Responses | Always the normalized JSON; errors structured, never nested in a success (section 4) |
| 5. Robustness | Explicit timeout on every I/O; idempotent, light tick (section 5) |
| 6. Security | `X-Auth-Token` only; `api.auth` / `api.public_paths` declared deliberately (sections 6 and 9) |
| 7. Env vars | Service prefix; required ones in `env.required`; defaults in `env.optional` (section 7) |
| 8. Schedule | `every_tick` / `interval_minutes` / `disabled`, declared in the manifest (section 8) |
| 9. Dependencies | Add them to the manager's `pyproject.toml` and run `uv lock` (section 10) |
| 10. Fallback | Optionally seed a local `fallback/` with the first known-good copies |

For a NEW service: copy a block from `services.yaml.example`, follow sections 1-9, add its dependencies to the manager's `pyproject.toml` (`uv lock`) and, optionally, its files to a local `fallback/`. The rest (import, mount, schedule, actions, reporting) is done by the manager.