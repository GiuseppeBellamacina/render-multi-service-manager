"""render-service-manager — a config-driven manager of scheduled services.

A single FastAPI process that manages N services declared in `services.yaml`.
For each one the manifest says: which files to fetch from GitHub (and how),
which environment variables it requires, its dependencies, what kind of API
it exposes and which uniform actions it supports.

The manifest is SECRET (gitignored, never published): at boot it is resolved
in this order — 1. `MANIFEST_CONTENT` env var (the whole YAML, literal \\n
allowed); 2. `MANIFEST_REPO` env var (a private GitHub repository, fetched
via the Contents API with `GITHUB_TOKEN`; optional `MANIFEST_BRANCH` /
`MANIFEST_PATH`); 3. a local `services.yaml` file (development).

Lifecycle:
1. At boot: seed files from the fallback copies (if any are present) ->
   parallel fetch from GitHub -> dynamic import -> setup (lifespan
   equivalent) -> mount the original sub-apps on their paths (`/{name}/...`).
2. `GET|POST /tick` (external cron, every 5 min): daily fetch on the first
   tick of the day + each service's tick according to its own schedule,
   with full error isolation and an anti-overlap lock.
3. `GET /fetch`: update the scripts from GitHub immediately (explicit
   request). If content changed -> clean process restart that reloads the
   fresh code (Render relaunches the container), with an anti-flapping guard.
4. `GET|POST /services/{name}/{action}`: invoke ONE service's action
   (e.g. /services/t2g/tick, /services/committer/status).
5. The sub-apps mounted at /{name}/* expose the services' ORIGINAL APIs
   (the t2g TUI points at .../t2g, the credit dashboard at .../credit).

Security: `MANAGER_AUTH_TOKEN` (optional, recommended on Render) protects
EVERY route except /health and the manifest's `public_paths`; it also
accepts `T2G_AUTH_TOKEN` so the TUI keeps working. The token travels ONLY
in the `X-Auth-Token` header (never in the query string: it would end up
in logs). No token or key is ever logged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# -- dotenv BEFORE everything: the service modules read env vars at import --
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:  # pragma: no cover
    pass

import sources
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
_log = logging.getLogger("manager")

# -- Paths and configuration -------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "services.yaml"
SERVICES_DIR = BASE_DIR / "services"
FALLBACK_DIR = BASE_DIR / "fallback"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "manager_state.json"

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute_env(value: Any, missing: set) -> Any:
    """Substitute `${VAR}` placeholders with environment variables.

    Lets a manifest contain NO personal data: real repos, owners and hosts
    live only in the env (local: .env - Render: dashboard).
    """
    if isinstance(value, str):

        def _repl(m):
            var = m.group(1)
            val = os.environ.get(var)
            if not val:
                missing.add(var)
                return m.group(0)  # stays literal: fetch errors will show it
            return val

        return _ENV_PLACEHOLDER.sub(_repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v, missing) for v in value]
    return value


def _ensure_manifest() -> None:
    """Resolve the SECRET manifest (services.yaml is gitignored).

    Priority: MANIFEST_CONTENT env var (the whole YAML, literal \\n allowed,
    normalized like T2G_SSH_KEY_CONTENT) > MANIFEST_REPO env var (a private
    GitHub repository: MANIFEST_BRANCH default "main", MANIFEST_PATH default
    "services.yaml", fetched via the Contents API with GITHUB_TOKEN) >
    MANIFEST_FILE env var (an explicit file path, e.g. /etc/secrets/
    services.yaml for a Render Secret File) > a local services.yaml file
    (development) > a Render Secret File named services.yaml (mounted under
    /etc/secrets/, linked at the app root).
    """
    content = os.environ.get("MANIFEST_CONTENT", "")
    if content.strip():
        # Render serializes multiline env vars as a single line with literal
        # \n: normalize ONLY when there are no real newlines (a value with
        # real newlines, or a literal "\n" inside a comment, stays intact).
        if "\n" not in content:
            content = content.replace("\\n", "\n")
        CONFIG_FILE.write_text(content, encoding="utf-8")
        _log.info("manifest provided via MANIFEST_CONTENT env var")
        return

    repo = os.environ.get("MANIFEST_REPO", "").strip()
    if repo:
        spec = {
            "name": "services.yaml",
            "mode": "api",
            "repo": repo,
            "branch": os.environ.get("MANIFEST_BRANCH", "main"),
            "path": os.environ.get("MANIFEST_PATH", "services.yaml"),
        }
        try:
            data = sources.fetch_file(spec, os.environ.get("GITHUB_TOKEN", ""))
            CONFIG_FILE.write_bytes(data)
            _log.info("manifest fetched from %s (%s)", repo, spec["path"])
        except Exception as exc:  # noqa: BLE001 - never block the boot
            _log.error("manifest fetch from %s failed: %s", repo, exc)
        return

    manifest_file = os.environ.get("MANIFEST_FILE", "").strip()
    if manifest_file:
        src = Path(manifest_file)
        if not src.is_file():
            _log.error("MANIFEST_FILE %s not found", manifest_file)
            # fall through: the app-root file may still be there
        elif CONFIG_FILE.is_file() and CONFIG_FILE.resolve() == src.resolve():
            _log.info("manifest: local services.yaml file (MANIFEST_FILE)")
            return
        else:
            try:
                CONFIG_FILE.write_bytes(src.read_bytes())
                _log.info("manifest loaded from MANIFEST_FILE %s", manifest_file)
            except OSError as exc:
                _log.warning("manifest copy from %s failed: %s", manifest_file, exc)
            return

    if CONFIG_FILE.is_file():
        _log.info("manifest: local services.yaml file")
        return
    # Render Secret Files: uploaded via the dashboard, mounted under
    # /etc/secrets (and normally linked at the app root too).
    secret_manifest = Path("/etc/secrets/services.yaml")
    if secret_manifest.is_file():
        CONFIG_FILE.write_bytes(secret_manifest.read_bytes())
        _log.info(
            "manifest loaded from Render secret file /etc/secrets/services.yaml"
        )
        return
    _log.error(
        "manifest not found: provide services.yaml locally, or set "
        "MANIFEST_CONTENT, or set MANIFEST_REPO, or upload it as a "
        "Render Secret File named services.yaml"
    )


def _load_config() -> dict:
    """Parse services.yaml (+ ${VAR} substitution). A broken manifest does
    NOT brick the manager: it starts with no services and /status shows the
    error."""
    if not CONFIG_FILE.is_file():
        return {
            "services": {},
            "config_error": (
                "services.yaml not found: provide it locally, or set "
                "MANIFEST_CONTENT, or set MANIFEST_REPO on Render"
            ),
        }
    try:
        import yaml

        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or not data.get("services"):
            return {"services": {}, "config_error": "services.yaml without 'services'"}
        missing: set = set()
        data = _substitute_env(data, missing)
        if missing:
            _log.warning(
                "services.yaml: unset environment variables -> "
                "placeholders stay literal (related fetches will fail): %s",
                ", ".join(sorted(missing)),
            )
        return data
    except Exception as exc:  # noqa: BLE001
        return {"services": {}, "config_error": f"parse services.yaml: {exc}"}


_ensure_manifest()
CONFIG = _load_config()
SERVICES: dict = CONFIG.get("services") or {}
CONFIG_ERROR = CONFIG.get("config_error")
if CONFIG_ERROR:
    _log.error("config services.yaml: %s", CONFIG_ERROR)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# -- The manager's own config -------------------------------------------------

MANAGER_AUTH_TOKEN = os.environ.get("MANAGER_AUTH_TOKEN", "")
T2G_AUTH_TOKEN = os.environ.get("T2G_AUTH_TOKEN", "")
SERVICES_ENABLED = {
    s.strip()
    for s in os.environ.get("SERVICES_ENABLED", ",".join(SERVICES.keys())).split(",")
    if s.strip()
}
DAILY_FETCH = os.environ.get("DAILY_FETCH", "1") not in ("", "0", "false", "False")
RESTART_ON_CHANGE = os.environ.get("RESTART_ON_CHANGE", "1") not in (
    "",
    "0",
    "false",
    "False",
)
TZ_OFFSET_H = _env_int("TIMEZONE_OFFSET", 2)  # same clock as the committer
RESTART_GUARD_S = 600  # at most one restart every 10 min (anti-flapping)

# Public paths (no manager token) derived from the manifest: for every service
# with auth "manager", its public_paths are reachable from a browser
# (e.g. the credit monitor's dashboard, as in the original exposure).
PUBLIC_PREFIXES: list[str] = []
for _n, _s in SERVICES.items():
    _api = _s.get("api") or {}
    if _api.get("auth", "manager") != "manager":
        continue
    _mount = str(_api.get("mount") or f"/{_n}").rstrip("/")
    for _p in _api.get("public_paths") or []:
        _p = str(_p)
        PUBLIC_PREFIXES.append(f"{_mount}{_p}" if _p.startswith("/") else f"{_mount}/{_p}")

# -- Local state (ephemeral on Render: best-effort, by design) ----------------


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:  # noqa: BLE001
        _log.warning("state save failed: %s", exc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_key() -> str:
    """Local day (same TZ as the committer): used for the daily fetch."""
    return (_now() + timedelta(hours=TZ_OFFSET_H)).strftime("%Y-%m-%d")


# -- Render Secret Files normalization ----------------------------------------


def _normalize_ssh_key_file() -> None:
    """When T2G_SSH_KEY_FILE points to a Render Secret File (under
    /etc/secrets), copy it to a local file with 0600 permissions and point
    the env var at the copy: ssh strictly rejects keys with open
    permissions, and we do not control the Secret File mount's mode.
    Runs BEFORE the service modules load (they read the env at import)."""
    key_file = os.environ.get("T2G_SSH_KEY_FILE", "").strip()
    if not key_file or not key_file.startswith("/etc/secrets/"):
        return
    src = Path(key_file)
    if not src.is_file():
        _log.warning(
            "T2G_SSH_KEY_FILE %s not found: the t2g service will fail its ssh",
            key_file,
        )
        return
    dest = BASE_DIR / "data" / "ssh_key"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Windows CRLF in the key would break ssh on Linux ("error in libcrypto")
    content = src.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    dest.write_bytes(content)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    os.environ["T2G_SSH_KEY_FILE"] = str(dest)
    _log.info("ssh key copied from Render secret file to %s (0600)", dest)


# -- Service loading (config-driven) ------------------------------------------

REGISTRY: dict[str, dict] = {}  # name -> {ok, error, module}


def _validate_env(name: str, envspec: dict) -> str | None:
    """Missing required vars -> a clear error message (service disabled,
    without even importing it). Optional with default -> setdefault in env."""
    # convenience: GITHUB_REPO defaults to COMMITTER_REPO (always the same repo)
    if name == "committer" and not os.environ.get("GITHUB_REPO"):
        cr = os.environ.get("COMMITTER_REPO", "")
        if cr:
            os.environ["GITHUB_REPO"] = cr
    missing = [v for v in (envspec.get("required") or []) if not os.environ.get(v)]
    if missing:
        return f"missing environment variables: {', '.join(missing)}"
    for key, default in (envspec.get("optional") or {}).items():
        if default is not None and str(default) != "" and not os.environ.get(key):
            os.environ.setdefault(key, str(default))
    return None


def _check_deps(name: str, deps: list | None) -> None:
    """Non-blocking warning when a declared dependency is not installed:
    the eventual failed import is caught anyway with its own error."""
    if not deps:
        return
    from importlib import metadata

    missing = []
    for dep in deps:
        try:
            metadata.version(dep)
        except metadata.PackageNotFoundError:
            missing.append(dep)
    if missing:
        _log.warning(
            "service '%s': dependencies not installed in the manager: %s "
            "(update requirements.txt and redeploy)",
            name,
            ", ".join(missing),
        )


def _import_module(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot create a loader for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)  # no half-initialized modules left behind
        raise
    return module


def _load_services() -> None:
    """Import the declared service modules. One failure (missing env, import
    error, failed setup) never blocks the others."""
    for name, svc in SERVICES.items():
        dest = svc.get("entry")
        if not dest:
            REGISTRY[name] = {"ok": False, "error": "config: 'entry' missing"}
            continue

        env_err = _validate_env(name, svc.get("env") or {})
        if env_err:
            REGISTRY[name] = {"ok": False, "error": env_err}
            _log.error("service '%s' disabled: %s", name, env_err)
            continue

        _check_deps(name, svc.get("dependencies"))

        path = SERVICES_DIR / dest
        if not path.is_file():
            REGISTRY[name] = {"ok": False, "error": f"script missing: services/{dest}"}
            continue

        mod_name = f"svc_{name}"
        try:
            module = _import_module(mod_name, path)
            # Setup equivalent to the original lifespans (mounted sub-apps do
            # NOT receive lifespan events from Starlette).
            for attr in svc.get("setup_dirs") or []:
                obj = module
                for part in str(attr).split("."):
                    obj = getattr(obj, part)
                Path(str(obj)).mkdir(parents=True, exist_ok=True)
            for fn in svc.get("setup") or []:
                call = getattr(module, fn, None)
                if callable(call):
                    call()
            REGISTRY[name] = {"ok": True, "error": None, "module": module}
            _log.info("service '%s' loaded (entry: %s)", name, dest)
        except Exception as exc:  # noqa: BLE001 - isolation per service
            REGISTRY[name] = {"ok": False, "error": str(exc)[:300]}
            _log.error("service '%s' FAILED to load: %s", name, exc)


def _mount_services() -> None:
    for name, svc in SERVICES.items():
        api = svc.get("api") or {}
        mount = api.get("mount")
        entry = REGISTRY.get(name, {})
        if mount and entry.get("ok") and getattr(entry["module"], "app", None) is not None:
            app.mount(mount, entry["module"].app)
            _log.info("sub-app '%s' mounted at %s", name, mount)


# -- Action execution (the uniform mechanism) ----------------------------------


def _exc_detail(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail)[:300] if detail else str(exc)[:300]


def _positional_required(target) -> int:
    """Number of positional arguments without a default: the convention is
    that actions require NO arguments; the committer's legacy tick(request)
    is tolerated (the parameter is unused -> None is passed)."""
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):  # noqa: PERF203
        return 0
    n = 0
    for p in sig.parameters.values():
        if (
            p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and p.default is inspect.Parameter.empty
        ):
            n += 1
    return n


async def _call_async(target, args):
    """Wrapper for actions: AWAITS the result when target is a coroutine
    function (async def); when the manifest declares async a sync function,
    it returns the value (auto-correction instead of tripping wait_for)."""
    result = target(*args)
    if asyncio.iscoroutine(result):
        result = await result
    return result


def _decode_result(raw):
    """FastAPI actions may return a JSONResponse: decode it into a dict for
    a uniform report."""
    if hasattr(raw, "body"):
        try:
            return json.loads(raw.body)
        except Exception:  # noqa: BLE001
            return {"raw": str(raw)[:300]}
    return raw


async def _exec_action(name: str, action_spec: dict) -> dict:
    """Run ONE action of ONE service: sync in the threadpool, async awaited
    directly, an outer timeout cap, full error isolation. ALWAYS returns a
    report {ok, ms, data|error} - never raises to the caller."""
    entry = REGISTRY.get(name, {})
    module = entry.get("module")
    t0 = time.perf_counter()
    timeout = float(action_spec.get("timeout_s", 60))
    try:
        target = getattr(module, action_spec.get("function", ""), None)
        if not callable(target):
            raise RuntimeError(
                f"function '{action_spec.get('function')}' not found in the module"
            )
        args = [None] * _positional_required(target)
        # iscoroutinefunction takes precedence over the manifest's "sync"
        # flag: an async def ALWAYS ends up awaited, whatever the config says
        if action_spec.get("sync") and not asyncio.iscoroutinefunction(target):
            raw = await asyncio.wait_for(
                run_in_threadpool(target, *args), timeout
            )
        else:
            raw = await asyncio.wait_for(_call_async(target, args), timeout)
        return {
            "ok": True,
            "ms": int((time.perf_counter() - t0) * 1000),
            "data": _decode_result(raw),
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "ms": int((time.perf_counter() - t0) * 1000),
            "error": f"timeout after {timeout:g}s",
        }
    except Exception as exc:  # noqa: BLE001 - report, never crash the tick
        return {
            "ok": False,
            "ms": int((time.perf_counter() - t0) * 1000),
            "error": _exc_detail(exc),
        }


async def _scheduled_run(name: str, state: dict) -> dict:
    """Run a service's tick inside the global /tick, according to its
    schedule: every_tick - interval_minutes - disabled."""
    if name not in SERVICES_ENABLED:
        return {"skipped": True, "reason": "disabled (SERVICES_ENABLED)"}
    entry = REGISTRY.get(name, {})
    if not entry.get("ok"):
        return {"skipped": True, "reason": entry.get("error", "service not loaded")}

    sched = SERVICES[name].get("schedule") or {}
    mode = str(sched.get("mode", "every_tick"))
    if mode == "disabled":
        return {"skipped": True, "reason": "schedule disabled (config)"}
    if mode == "interval_minutes":
        try:
            interval = max(0, int(sched.get("interval_minutes", 0) or 0))
        except (TypeError, ValueError):
            interval = 0
        if interval > 0:
            last = float(
                ((state.get("schedules") or {}).get(name) or {}).get("last_run_ts", 0)
            )
            if _now().timestamp() - last < interval * 60:
                return {
                    "skipped": True,
                    "reason": f"interval of {interval} min not elapsed yet",
                }

    tick_spec = (SERVICES[name].get("actions") or {}).get("tick")
    if not tick_spec:
        return {"skipped": True, "reason": "no action 'tick' configured"}

    out = await _exec_action(name, tick_spec)
    # the attempt consumes its time slot even on error (no retry storms)
    state.setdefault("schedules", {})[name] = {"last_run_ts": _now().timestamp()}
    return out


# -- Fetch + smart restart -----------------------------------------------------

TICK_LOCK = asyncio.Lock()  # one tick/fetch at a time (no double runs)


async def _do_fetch(state: dict) -> dict:
    """Fetch the scripts. The day is marked ONLY when there are no errors:
    a failed fetch is retried on the next ticks (self-healing).
    If something changed -> restart_pending (a defer is a POSTPONEMENT, never
    a loss: the guard decides ONLY when, not whether, to restart)."""
    fetch_info = await run_in_threadpool(sources.fetch_all, SERVICES_DIR, CONFIG)
    state["last_fetch_ts"] = _now().isoformat()
    if not fetch_info["errors"]:
        state["last_fetch_day"] = _today_key()
    if fetch_info["changed"] and RESTART_ON_CHANGE:
        state["restart_pending"] = True
    return fetch_info


async def _graceful_exit() -> None:
    """Clean restart AFTER the response has been flushed (BackgroundTask
    supports coroutines: the sleep gives the socket time to drain)."""
    _log.warning("scripts updated: restarting the process to reload them")
    await asyncio.sleep(1)
    os._exit(0)  # Render relaunches the web service automatically


def _maybe_restart(payload: dict, state: dict) -> JSONResponse:
    """When a restart is pending and the anti-flapping guard allows it,
    respond and then exit: the process restarts with the freshly fetched code."""
    if state.get("restart_pending") and RESTART_ON_CHANGE:
        now_ts = _now().timestamp()
        if now_ts - float(state.get("last_restart_ts", 0)) > RESTART_GUARD_S:
            state["restart_pending"] = False
            state["last_restart_ts"] = now_ts
            _save_state(state)
            return JSONResponse(payload, background=BackgroundTask(_graceful_exit))
    return JSONResponse(payload)


# -- Lifespan -------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # normalize a Render Secret File ssh key BEFORE the services load
    _normalize_ssh_key_file()

    # 1) seed from fallbacks (best-effort) 2) fresh fetch (best-effort)
    try:
        sources.ensure_local_copies(SERVICES_DIR, FALLBACK_DIR, CONFIG)
    except Exception as exc:  # noqa: BLE001 - never block the boot
        _log.error("seed from fallback failed: %s", exc)

    state = _load_state()
    try:
        fetch_info = await run_in_threadpool(sources.fetch_all, SERVICES_DIR, CONFIG)
        state["last_fetch_ts"] = _now().isoformat()
        if not fetch_info["errors"]:
            state["last_fetch_day"] = _today_key()
        _log.info(
            "boot fetch: %d ok, changed=%s, errors=%s",
            fetch_info["fetched"],
            fetch_info["changed"] or "-",
            fetch_info["errors"] or "-",
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("boot fetch failed: %s", exc)
    _save_state(state)

    # 3) import + mount (happen BEFORE the first request)
    _load_services()
    _mount_services()

    enabled = [n for n, e in REGISTRY.items() if e.get("ok")]
    broken = {n: e.get("error") for n, e in REGISTRY.items() if not e.get("ok")}
    _log.info(
        "service-manager ready - active services: %s (enabled via env: %s)",
        enabled or "none",
        sorted(SERVICES_ENABLED) or "none",
    )
    if broken:
        _log.warning("services with problems: %s", broken)
    yield
    _log.info("service-manager shutting down")


app = FastAPI(
    title="render-service-manager",
    description=(
        "Config-driven (services.yaml) manager of scheduled services: "
        "daily script fetch from GitHub, orchestrated ticks, uniform "
        "per-service APIs. External tick every 5 min."
    ),
    version="2.0.0",
    lifespan=lifespan,
)


# -- Global auth (optional) -----------------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """When MANAGER_AUTH_TOKEN is set, EVERY route except /health and the
    manifest's public_paths requires the X-Auth-Token header. Also accepts
    T2G_AUTH_TOKEN: the TUI keeps working pointed at /t2g.
    The token is NOT accepted in the query string (it would end up in logs)."""

    async def dispatch(self, request, call_next):
        if not MANAGER_AUTH_TOKEN:
            return await call_next(request)
        path = request.url.path
        if path == "/health" or any(
            path.startswith(p) for p in PUBLIC_PREFIXES
        ):
            return await call_next(request)
        token = request.headers.get("x-auth-token", "")
        valid = secrets.compare_digest(token, MANAGER_AUTH_TOKEN) or (
            bool(T2G_AUTH_TOKEN) and secrets.compare_digest(token, T2G_AUTH_TOKEN)
        )
        if not valid:
            return JSONResponse({"detail": "missing or invalid X-Auth-Token"}, 401)
        return await call_next(request)


app.add_middleware(AuthMiddleware)


# -- Manager endpoints -----------------------------------------------------------


@app.get("/info")
def info() -> dict:
    """Service info (the tick endpoint is at / and /tick)."""
    return {
        "service": "render-service-manager",
        "config_error": CONFIG_ERROR,
        "docs": "/docs",
        "endpoints": {
            "tick": "GET|POST / and /tick - global tick (external cron, every 5 min)",
            "fetch": "GET /fetch - update the scripts from GitHub immediately",
            "services": "GET /services - service list from the manifest",
            "service_action": "GET|POST /services/{name}/{action} - one service's action",
            "service_info": "GET /services/{name} - detail of one service",
            "status": "GET /status - manager state + last tick",
            "health": "GET /health - health check (no auth)",
        },
        "mounted": [
            f"{(s.get('api') or {}).get('mount', '')} -> {s.get('display_name', n)}"
            for n, s in SERVICES.items()
            if (s.get("api") or {}).get("mount")
        ],
        "auth": bool(MANAGER_AUTH_TOKEN),
    }


@app.get("/")
@app.post("/")
@app.get("/tick")
@app.post("/tick")
async def tick() -> JSONResponse:
    """Global tick (at / and /tick): daily fetch (on the first tick of the
    day) + the tick of ALL services per their schedule. Lock: never
    overlapping ticks."""
    if TICK_LOCK.locked():
        return JSONResponse(
            {"ok": False, "error": "previous tick still in progress"}, status_code=409
        )
    async with TICK_LOCK:
        state = _load_state()
        fetch_info: dict = {"skipped": True}
        if DAILY_FETCH and state.get("last_fetch_day") != _today_key():
            fetch_info = await _do_fetch(state)

        results: dict = {}
        for name in SERVICES:
            results[name] = await _scheduled_run(name, state)

        state["last_tick"] = {
            "ts": _now().isoformat(),
            "services": {
                n: {k: v for k, v in r.items() if k != "data"}
                for n, r in results.items()
            },
        }
        _save_state(state)

        payload = {
            "ok": all(r.get("ok") for r in results.values() if not r.get("skipped")),
            "ts": _now().isoformat(),
            "fetch": fetch_info,
            "services": results,
        }
        _log.info(
            "tick: %s",
            json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "data"}
                        for k, v in results.items()})[:800],
        )
        return _maybe_restart(payload, state)


@app.get("/fetch")
async def fetch_now() -> JSONResponse:
    """Update the scripts from GitHub immediately (explicit update request).
    If content changed -> restart after the response."""
    if TICK_LOCK.locked():
        return JSONResponse(
            {"ok": False, "error": "tick/fetch in progress, try again shortly"},
            status_code=409,
        )
    async with TICK_LOCK:
        state = _load_state()
        fetch_info = await _do_fetch(state)
        _save_state(state)
        payload = {"ok": not fetch_info["errors"], "fetch": fetch_info}
        return _maybe_restart(payload, state)


@app.get("/services")
def services_list() -> dict:
    out = {}
    for name, svc in SERVICES.items():
        api = svc.get("api") or {}
        src = svc.get("source") or {}
        out[name] = {
            "display_name": svc.get("display_name", name),
            "description": (svc.get("description") or "").strip(),
            "loaded": REGISTRY.get(name, {}).get("ok", False),
            "error": REGISTRY.get(name, {}).get("error"),
            "enabled": name in SERVICES_ENABLED,
            "source": {
                "repo": src.get("repo"),
                "branch": src.get("branch"),
                "mode": src.get("mode"),
                "files": [f.get("dest") for f in src.get("files") or []],
            },
            "api": {
                "mount": api.get("mount"),
                "auth": api.get("auth", "manager"),
                "public_paths": api.get("public_paths") or [],
                "endpoints": api.get("endpoints") or {},
            },
            "actions": sorted((svc.get("actions") or {}).keys()),
            "schedule": svc.get("schedule") or {"mode": "every_tick"},
        }
    return {"services": out, "config_error": CONFIG_ERROR}


@app.get("/services/{name}")
def service_info(name: str) -> JSONResponse:
    if name not in SERVICES:
        return JSONResponse({"detail": f"unknown service: {name}"}, 404)
    full = services_list()["services"]
    return JSONResponse(full[name])


@app.get("/services/{name}/{action}")
@app.post("/services/{name}/{action}")
async def service_action(name: str, action: str) -> JSONResponse:
    """ONE service's action, by name, from the manifest:
    e.g. GET /services/t2g/status - POST /services/committer/tick."""
    if name not in SERVICES:
        return JSONResponse({"detail": f"unknown service: {name}"}, 404)
    action_spec = (SERVICES[name].get("actions") or {}).get(action)
    if not action_spec:
        known = sorted((SERVICES[name].get("actions") or {}).keys())
        return JSONResponse(
            {"detail": f"unknown action: {action} (available: {known})"}, 404
        )
    if name not in SERVICES_ENABLED:
        return JSONResponse(
            {"skipped": True, "reason": "disabled (SERVICES_ENABLED)"}
        )
    entry = REGISTRY.get(name, {})
    if not entry.get("ok"):
        return JSONResponse(
            {"skipped": True, "reason": entry.get("error", "service not loaded")}
        )
    result = await _exec_action(name, action_spec)
    return JSONResponse(
        {"service": name, "action": action, "result": result}
    )


@app.get("/status")
async def status() -> dict:
    state = _load_state()
    services: dict = {}
    for name, svc in SERVICES.items():
        entry = REGISTRY.get(name, {})
        info = {
            "loaded": entry.get("ok", False),
            "enabled": name in SERVICES_ENABLED,
            "error": entry.get("error"),
        }
        # per-service status (when the action is configured) - read-only, isolated
        st_spec = (svc.get("actions") or {}).get("status")
        if st_spec and entry.get("ok"):
            res = await _exec_action(name, st_spec)
            info["status"] = res.get("data") if res.get("ok") else {"error": res.get("error")}
        services[name] = info
    return {
        "ts": _now().isoformat(),
        "config_error": CONFIG_ERROR,
        "daily_fetch": DAILY_FETCH,
        "services": services,
        "last_fetch_day": state.get("last_fetch_day"),
        "last_tick": state.get("last_tick"),
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_env_int("PORT", 8000))