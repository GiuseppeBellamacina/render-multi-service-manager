"""Script fetch for the services - entirely driven by services.yaml.

For each service the manifest declares `source` (repo, branch, mode) and
the list of files (`repo_path` -> `dest`). Two download modes:

- "raw": public repo -> raw.githubusercontent.com, no token needed.
- "api": private repo -> Contents API with Accept: application/vnd.github.raw
  and Bearer GITHUB_TOKEN.

The sha256 comparison ensures a file is rewritten ONLY when its content has
actually changed: the manager uses the hash to decide whether to restart
the process and reload the code. Fetches run in parallel (ThreadPoolExecutor)
to keep the boot far below the external cron's timeout.
"""

from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

_log = logging.getLogger("manager.sources")

_RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
_API_URL = "https://api.github.com/repos/{repo}/contents/{path}"


def specs_from_config(config: dict) -> list[dict]:
    """Expand services.yaml into the flat list of files to download."""
    specs: list[dict] = []
    for svc_name, svc in (config.get("services") or {}).items():
        src = svc.get("source") or {}
        for f in src.get("files") or []:
            specs.append(
                {
                    "service": svc_name,
                    "name": f["dest"],  # relative destination under services/
                    "mode": src.get("mode", "raw"),
                    "repo": src.get("repo", ""),
                    "branch": src.get("branch", "main"),
                    "path": f["repo_path"],
                }
            )
    return specs


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_file(spec: dict, token: str) -> bytes:
    """Download a single file. Raise on any HTTP/network error."""
    headers: dict[str, str] = {}
    if spec["mode"] == "raw":
        url = _RAW_URL.format(
            repo=spec["repo"], branch=spec["branch"], path=spec["path"]
        )
    else:
        url = _API_URL.format(repo=spec["repo"], path=spec["path"])
        url += f"?ref={spec['branch']}"
        headers["Accept"] = "application/vnd.github.raw"
        if token:
            headers["Authorization"] = f"Bearer {token}"

    r = requests.get(url, headers=headers, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} ({spec['repo']}/{spec['path']})")
    return r.content


def _fetch_and_store(spec: dict, services_dir: Path, token: str) -> str | None:
    """Download a file and write it ONLY if different. Returns the name when
    updated, None when identical. Raise on errors (handled by the caller)."""
    content = fetch_file(spec, token)
    dest = services_dir / spec["name"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and _sha(dest.read_bytes()) == _sha(content):
        return None
    dest.write_bytes(content)
    return spec["name"]


def fetch_all(services_dir: Path, config: dict) -> dict:
    """Download ALL the manifest's files, in parallel. Never raise:
    an unreachable file ends up in errors and the existing copy
    (fallback or previous fetch) keeps being used."""
    token = os.environ.get("GITHUB_TOKEN", "")
    specs = specs_from_config(config)
    changed: list[str] = []
    errors: list[str] = []
    ok = 0

    if not specs:
        return {"changed": [], "errors": [], "fetched": 0}

    workers = min(8, max(1, len(specs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_and_store, spec, services_dir, token): spec
            for spec in specs
        }
        for fut, spec in futures.items():
            try:
                updated = fut.result()
                if updated:
                    changed.append(updated)
                    _log.info("fetch [%s] %s: UPDATED", spec["service"], spec["name"])
                ok += 1
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                errors.append(f"{spec['name']}: {exc}")
                _log.warning(
                    "fetch [%s] %s failed: %s", spec["service"], spec["name"], exc
                )

    return {"changed": changed, "errors": errors, "fetched": ok}


def ensure_local_copies(services_dir: Path, fallback_dir: Path, config: dict) -> None:
    """Seed missing files from the fallback copies (last-known-good).

    Lets the manager start the services even when GitHub is unreachable:
    the fetch (boot/daily/retry) will update them as soon as possible.
    When the fallback is missing too -> just a warning: the fetch will
    still be able to create the file on its first success.
    """
    for spec in specs_from_config(config):
        dest = services_dir / spec["name"]
        if dest.is_file():
            continue
        fb = fallback_dir / spec["name"]
        if fb.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(fb.read_bytes())
            _log.info("seed [%s] %s from fallback", spec["service"], spec["name"])
        else:
            _log.warning(
                "neither fetch nor fallback for %s (service %s)",
                spec["name"],
                spec["service"],
            )