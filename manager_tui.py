"""render-service-manager TUI — local dashboard for the Render manager.

A Textual app that lets you interact with the manager from the terminal:
  - Dashboard: live overview of all services (loaded, enabled, last tick)
  - Tick: run /tick and see the human-readable summary
  - Routes: test any route and see status codes
  - Fetch: force-update scripts from GitHub
  - Status: per-service health + config

Usage:
    .venv\\Scripts\\python.exe manager_tui.py
    .venv\\Scripts\\python.exe manager_tui.py --url https://your-service.onrender.com
    .\\run_manager_tui.ps1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets._tabbed_content import ContentTab

# ── Config ──────────────────────────────────────────────────────────────

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

DEFAULT_URL = os.environ.get("MANAGER_URL", "https://render-multi-service-manager.onrender.com")
DEFAULT_TOKEN = os.environ.get("MANAGER_AUTH_TOKEN", "")
DEFAULT_T2G_TOKEN = os.environ.get("T2G_AUTH_TOKEN", "")


# ── API client ──────────────────────────────────────────────────────────

class ManagerAPI:
    """Thin HTTP client for the render-service-manager."""

    def __init__(self, base_url: str, token: str, t2g_token: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token
        self.t2g_token = t2g_token

    def _headers(self, use_t2g: bool = False) -> dict:
        tok = self.t2g_token if use_t2g else self.token
        return {"X-Auth-Token": tok} if tok else {}

    def get(self, path: str, use_t2g: bool = False, timeout: float = 120) -> httpx.Response:
        url = f"{self.base}{path}"
        return httpx.get(url, headers=self._headers(use_t2g), timeout=timeout)

    def post(self, path: str, use_t2g: bool = False, timeout: float = 120) -> httpx.Response:
        url = f"{self.base}{path}"
        return httpx.post(url, headers=self._headers(use_t2g), timeout=timeout)

    # ── convenience ──

    def health(self) -> dict:
        r = self.get("/health", timeout=30)
        return {"status_code": r.status_code, **(r.json() if r.status_code == 200 else {})}

    def tick(self) -> dict:
        r = self.get("/tick")
        return {"status_code": r.status_code, **(r.json() if r.status_code == 200 else {"error": r.text[:300]})}

    def fetch(self) -> dict:
        r = self.get("/fetch", timeout=120)
        return {"status_code": r.status_code, **(r.json() if r.status_code == 200 else {"error": r.text[:300]})}

    def status(self) -> dict:
        r = self.get("/status", timeout=30)
        return {"status_code": r.status_code, **(r.json() if r.status_code == 200 else {"error": r.text[:300]})}

    def services(self) -> dict:
        r = self.get("/services", timeout=30)
        return {"status_code": r.status_code, **(r.json() if r.status_code == 200 else {"error": r.text[:300]})}

    def service_action(self, name: str, action: str, use_t2g: bool = False) -> dict:
        r = self.get(f"/services/{name}/{action}", use_t2g=use_t2g, timeout=60)
        return {"status_code": r.status_code, **(r.json() if r.status_code == 200 else {"error": r.text[:300]})}

    def test_route(self, method: str, path: str, use_t2g: bool = False) -> dict:
        if method.upper() == "POST":
            r = self.post(path, use_t2g=use_t2g, timeout=60)
        else:
            r = self.get(path, use_t2g=use_t2g, timeout=60)
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]
        return {"status_code": r.status_code, "body": body}


# ── TUI ─────────────────────────────────────────────────────────────────


CSS = """
Screen {
    background: $surface;
}

#dashboard-content {
    padding: 1 2;
}

.info-box {
    border: round $primary;
    padding: 1 2;
    margin: 0 0 1 0;
    background: $surface;
}

.info-box-label {
    color: $text-dim;
    text-style: bold;
}

.info-box-value {
    color: $text;
}

.metric {
    color: $accent;
    text-style: bold;
}

.error-text {
    color: $danger;
}

.ok-text {
    color: $success;
}

.skip-text {
    color: $warning;
}

#route-input {
    margin: 1 0;
}

#route-result {
    border: round $primary;
    padding: 1;
    height: 1fr;
    overflow: auto;
}

#tick-result, #fetch-result {
    border: round $primary;
    padding: 1;
    height: 1fr;
    overflow: auto;
}

#status-table {
    height: 1fr;
}

DataTable > .datatable--header {
    background: $surface2;
    color: $text;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: $primary 20%;
}
"""


class ManagerTUI(App):
    """render-service-manager interactive dashboard."""

    TITLE = "render-service-manager TUI"
    CSS = CSS

    BINDINGS = [
        Binding("t", "tick", "Tick"),
        Binding("f", "fetch", "Fetch"),
        Binding("s", "status", "Status"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    url: reactive[str] = reactive(DEFAULT_URL)
    token: reactive[str] = reactive(DEFAULT_TOKEN)
    t2g_token: reactive[str] = reactive(DEFAULT_T2G_TOKEN)

    def __init__(self, url: str = DEFAULT_URL, token: str = DEFAULT_TOKEN, t2g_token: str = DEFAULT_T2G_TOKEN):
        super().__init__()
        self.url = url
        self.token = token
        self.t2g_token = t2g_token
        self.api: ManagerAPI = ManagerAPI(url, token, t2g_token)

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Dashboard", id="tab-dashboard"):
                with VerticalScroll(id="dashboard-content"):
                    yield Static(id="dash-info", markup=True)
                    yield Static(id="dash-services", markup=True)
                    yield Static(id="dash-last-tick", markup=True)
            with TabPane("Tick", id="tab-tick"):
                with Container():
                    yield Static(
                        "[dim]Press 't' or click 'Run Tick' to trigger a global tick.[/dim]\n",
                        id="tick-hint",
                    )
                    yield Static(id="tick-result", markup=True)
            with TabPane("Fetch", id="tab-fetch"):
                with Container():
                    yield Static(
                        "[dim]Force-update scripts from GitHub. If content changed, the manager restarts.[/dim]\n",
                        id="fetch-hint",
                    )
                    yield Static(id="fetch-result", markup=True)
            with TabPane("Status", id="tab-status"):
                with Container():
                    yield DataTable(id="status-table")
            with TabPane("Routes", id="tab-routes"):
                with Container():
                    yield Static(
                        "[dim]Test any route: enter a path (e.g. /t2g/status) and press Enter.[/dim]"
                    )
                    yield Input(
                        placeholder="/t2g/status  (or /credit/api/data, /services/committer/tick ...)",
                        id="route-input",
                    )
                    yield Static(id="route-result", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_dashboard()

    # ── actions ──

    def action_tick(self) -> None:
        self.run_tick()

    def action_fetch(self) -> None:
        self.run_fetch()

    def action_status(self) -> None:
        self.run_status()

    def action_refresh(self) -> None:
        self.refresh_dashboard()

    # ── workers ──

    @work
    async def refresh_dashboard(self) -> None:
        info = self.query_one("#dash-info", Static)
        svcs = self.query_one("#dash-services", Static)
        last = self.query_one("#dash-last-tick", Static)
        info.update("[dim]Loading...[/dim]")
        svcs.update("")
        last.update("")
        try:
            health = self.api.health()
            status = self.api.status()
            services_data = self.api.services()
        except Exception as exc:
            info.update(f"[error-text]Connection error: {exc}[/error-text]")
            return

        # info box
        ok = health.get("status") == "ok"
        health_icon = "[ok-text]● OK[/ok-text]" if ok else "[error-text]● DOWN[/error-text]"
        info_txt = (
            f"  Service: [metric]render-service-manager[/metric]  "
            f"Health: {health_icon}  "
            f"URL: [dim]{self.url}[/dim]\n"
            f"  Auth: {'ON' if self.token else 'OFF'}  "
            f"Config error: {status.get('config_error', 'none') or 'none'}  "
            f"Daily fetch: {status.get('daily_fetch', '?')}"
        )
        info.update(info_txt)

        # services
        svcs_raw = services_data.get("services", {})
        if not svcs_raw:
            svcs.update("[dim]No services in manifest.[/dim]")
        else:
            lines = ["  Service                 Loaded  Enabled  Error"]
            lines.append("  " + "─" * 60)
            for name, s in svcs_raw.items():
                dn = s.get("display_name", name)
                loaded = s.get("loaded", False)
                enabled = s.get("enabled", False)
                err = s.get("error") or ""
                l_icon = "[ok-text]✓[/ok-text]" if loaded else "[error-text]✗[/error-text]"
                e_icon = "[ok-text]on[/ok-text]" if enabled else "[dim]off[/dim]"
                err_txt = f"[error-text]{err[:40]}[/error-text]" if err else ""
                lines.append(f"  {dn:<24} {l_icon}       {e_icon}      {err_txt}")
            svcs.update("\n".join(lines))

        # last tick
        lt = status.get("last_tick")
        if lt:
            ts = lt.get("ts", "?")
            svcs_lt = lt.get("services", {})
            lines = [f"  Last tick: [dim]{ts}[/dim]"]
            for name, r in svcs_lt.items():
                if r.get("ok"):
                    lines.append(f"    {name}: [ok-text]OK[/ok-text] ({r.get('ms', '?')}ms)")
                elif r.get("skipped"):
                    lines.append(f"    {name}: [skip-text]SKIP[/skip-text] ({r.get('reason', '')})")
                else:
                    e = r.get("error", "?")
                    lines.append(f"    {name}: [error_text]ERR[/error_text] {e[:50]}")
            last.update("\n".join(lines))
        else:
            last.update("  [dim]No ticks yet.[/dim]")

    @work
    async def run_tick(self) -> None:
        result = self.query_one("#tick-result", Static)
        result.update("[dim]Triggering /tick (may take 30-90s on cold start)...[/dim]")
        try:
            r = self.api.tick()
        except Exception as exc:
            result.update(f"[error-text]Error: {exc}[/error-text]")
            return
        summary = r.get("summary", "(no summary)")
        ok = r.get("ok", False)
        ok_all = r.get("ok_all", False)
        icon = "[ok-text]✓ ALL OK[/ok-text]" if ok_all else ("[warning]~ PARTIAL[/warning]" if ok else "[error-text]✗ ERROR[/error-text]")
        lines = [f"  {icon}\n"]
        lines.append(f"[dim]{summary}[/dim]\n")
        # per-service details
        svcs = r.get("services", {})
        for name, s in svcs.items():
            if s.get("skipped"):
                lines.append(f"  {name}: [skip_text]SKIPPED[/skip_text] — {s.get('reason', '')}")
            elif s.get("ok"):
                data = s.get("data", {})
                hint = ""
                if isinstance(data, dict):
                    if data.get("status") == "sleeping":
                        hint = "sleeping"
                    elif "cluster_reachable" in data:
                        hint = f"cluster {'reachable' if data['cluster_reachable'] else 'UNREACHABLE'}"
                    elif isinstance(data.get("entry"), dict):
                        b = data["entry"].get("balance")
                        if b is not None:
                            hint = f"balance ${b}"
                lines.append(f"  {name}: [ok_text]OK[/ok_text] ({s.get('ms', '?')}ms){f' — {hint}' if hint else ''}")
            else:
                e = s.get("error", "?")
                lines.append(f"  {name}: [error_text]ERROR[/error_text] — {e[:80]}")
        result.update("\n".join(lines))
        # also refresh dashboard
        self.refresh_dashboard()

    @work
    async def run_fetch(self) -> None:
        result = self.query_one("#fetch-result", Static)
        result.update("[dim]Fetching scripts from GitHub (may take 10-30s)...[/dim]")
        try:
            r = self.api.fetch()
        except Exception as exc:
            result.update(f"[error-text]Error: {exc}[/error-text]")
            return
        fetch = r.get("fetch", {})
        ok = r.get("ok", False)
        changed = fetch.get("changed", [])
        errors = fetch.get("errors", [])
        fetched = fetch.get("fetched", 0)
        icon = "[ok-text]✓[/ok-text]" if ok else "[error-text]✗[/error_text]"
        lines = [f"  {icon} Fetch complete: {fetched} files"]
        if changed:
            lines.append(f"  [warning]Changed: {', '.join(changed)}[/warning]")
            lines.append("  [dim](manager will restart to reload)[/dim]")
        if errors:
            lines.append(f"  [error_text]Errors: {', '.join(errors)}[/error_text]")
        if not changed and not errors:
            lines.append("  [dim]No changes — all scripts up to date.[/dim]")
        result.update("\n".join(lines))

    @work
    async def run_status(self) -> None:
        table = self.query_one("#status-table", DataTable)
        table.clear(columns=True)
        table.add_column("Service")
        table.add_column("Loaded", width=8)
        table.add_column("Enabled", width=8)
        table.add_column("Error", width=40)
        table.add_column("Status detail", width=50)
        try:
            r = self.api.status()
        except Exception as exc:
            table.add_row("ERROR", "", "", str(exc)[:50], "")
            return
        svcs = r.get("services", {})
        for name, s in svcs.items():
            loaded = "✓" if s.get("loaded") else "✗"
            enabled = "on" if s.get("enabled") else "off"
            err = (s.get("error") or "")[:40]
            st = s.get("status", {})
            if isinstance(st, dict):
                if "error" in st:
                    detail = f"err: {str(st['error'])[:45]}"
                elif "cluster_reachable" in st:
                    detail = f"cluster: {'OK' if st['cluster_reachable'] else 'DOWN'}"
                elif "balance" in st:
                    detail = f"balance: ${st['balance']}"
                elif "pending_entries" in st:
                    detail = f"backup: {st['pending_entries']} pending"
                else:
                    detail = json.dumps(st)[:50]
            else:
                detail = str(st)[:50]
            table.add_row(name, loaded, enabled, err, detail)

    # ── route tester ──

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "route-input":
            return
        path = event.value.strip()
        if not path:
            return
        result = self.query_one("#route-result", Static)
        result.update(f"[dim]Testing {path} ...[/dim]")
        self.test_route(path)

    @work
    async def test_route(self, path: str) -> None:
        # decide token: t2g routes use the t2g token
        use_t2g = path.startswith("/t2g")
        result = self.query_one("#route-result", Static)
        try:
            r = self.api.test_route("GET", path, use_t2g=use_t2g)
        except Exception as exc:
            result.update(f"[error-text]Error: {exc}[/error-text]")
            return
        code = r["status_code"]
        body = r["body"]
        if isinstance(body, dict):
            # pretty print, truncate
            body_str = json.dumps(body, indent=2)
            if len(body_str) > 2000:
                body_str = body_str[:2000] + "\n... (truncated)"
        else:
            body_str = str(body)[:2000]
        icon = "[ok_text]200[/ok_text]" if code == 200 else f"[error_text]{code}[/error_text]"
        lines = [f"  {icon}  {path}"]
        lines.append("")
        lines.append(f"[dim]{body_str}[/dim]")
        result.update("\n".join(lines))


# ── entry point ──

def main():
    parser = argparse.ArgumentParser(description="render-service-manager TUI")
    parser.add_argument("--url", default=DEFAULT_URL, help="Manager URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="MANAGER_AUTH_TOKEN")
    parser.add_argument("--t2g-token", default=DEFAULT_T2G_TOKEN, help="T2G_AUTH_TOKEN")
    args = parser.parse_args()
    app = ManagerTUI(url=args.url, token=args.token, t2g_token=args.t2g_token)
    app.run()


if __name__ == "__main__":
    main()
