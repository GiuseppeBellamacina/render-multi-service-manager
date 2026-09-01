"""render-service-manager TUI — local dashboard for the Render manager.

A Textual app that lets you interact with the manager from the terminal.
Services are discovered DYNAMICALLY from the live /services endpoint —
no hardcoded names, no hardcoded routes.

Tabs:
  - Dashboard: live overview of all services (from /services)
  - Tick: run /tick and see the human-readable summary
  - Fetch: force-update scripts from GitHub
  - Status: per-service health table
  - Routes: test any route interactively (services auto-discovered)
  - Service: pick a service from a dropdown, run any of its actions

Usage:
    .venv\\Scripts\\python.exe manager_tui.py
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
    Select,
    Static,
    TabbedContent,
    TabPane,
)

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
        return httpx.get(f"{self.base}{path}", headers=self._headers(use_t2g), timeout=timeout)

    def post(self, path: str, use_t2g: bool = False, timeout: float = 120) -> httpx.Response:
        return httpx.post(f"{self.base}{path}", headers=self._headers(use_t2g), timeout=timeout)

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

    def test_route(self, method: str, path: str, use_t2g: bool = False) -> dict:
        r = self.post(path, use_t2g=use_t2g, timeout=60) if method.upper() == "POST" else self.get(path, use_t2g=use_t2g, timeout=60)
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]
        return {"status_code": r.status_code, "body": body}


# ── CSS (only valid Textual design tokens) ──────────────────────────────

CSS = """
Screen {
    background: $surface;
}

#dashboard-content {
    padding: 1 2;
}

#tick-result, #fetch-result, #route-result, #service-result {
    border: round $primary;
    padding: 1;
    height: 1fr;
    overflow: auto;
}

#status-table {
    height: 1fr;
}

#route-input, #service-select-row {
    margin: 1 0;
}

DataTable > .datatable--header {
    background: $panel;
    color: $text;
    text-style: bold;
}
"""


# ── TUI ─────────────────────────────────────────────────────────────────


class ManagerTUI(App):
    """render-service-manager interactive dashboard — fully dynamic."""

    TITLE = "render-service-manager TUI"
    CSS = CSS

    BINDINGS = [
        Binding("t", "tick", "Tick"),
        Binding("f", "fetch", "Fetch"),
        Binding("s", "status", "Status"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, url: str = DEFAULT_URL, token: str = DEFAULT_TOKEN, t2g_token: str = DEFAULT_T2G_TOKEN):
        super().__init__()
        self.url = url
        self.token = token
        self.t2g_token = t2g_token
        self.api: ManagerAPI = ManagerAPI(url, token, t2g_token)
        self._services_cache: dict = {}

    # ── compose ──

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
                        "[dim]Press 't' to trigger a global tick (may take 30-90s on cold start).[/dim]\n",
                    )
                    yield Static(id="tick-result", markup=True)
            with TabPane("Fetch", id="tab-fetch"):
                with Container():
                    yield Static(
                        "[dim]Force-update scripts from GitHub. If content changed, the manager restarts.[/dim]\n",
                    )
                    yield Static(id="fetch-result", markup=True)
            with TabPane("Status", id="tab-status"):
                with Container():
                    yield DataTable(id="status-table")
            with TabPane("Routes", id="tab-routes"):
                with Container():
                    yield Static("[dim]Enter a path (e.g. /t2g/status) and press Enter. Routes starting with /t2g use the T2G token.[/dim]")
                    yield Input(
                        placeholder="/t2g/status  /credit/api/data  /services/committer/tick ...",
                        id="route-input",
                    )
                    yield Static(id="route-result", markup=True)
            with TabPane("Service", id="tab-service"):
                with Container():
                    yield Static("[dim]Pick a service and an action — both lists are populated from the live manifest.[/dim]")
                    yield Horizontal(
                        Select([], id="service-select", allow_blank=False),
                        Select([], id="action-select", allow_blank=False),
                        id="service-select-row",
                    )
                    yield Static(id="service-result", markup=True)
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
        info_w = self.query_one("#dash-info", Static)
        svcs_w = self.query_one("#dash-services", Static)
        last_w = self.query_one("#dash-last-tick", Static)
        info_w.update("[dim]Loading...[/dim]")
        svcs_w.update("")
        last_w.update("")
        try:
            health = self.api.health()
            status = self.api.status()
            services_data = self.api.services()
        except Exception as exc:
            info_w.update(f"[red]Connection error: {exc}[/]")
            return

        self._services_cache = services_data.get("services", {})

        # populate Service tab dropdowns
        try:
            svc_select = self.query_one("#service-select", Select)
            act_select = self.query_one("#action-select", Select)
            svc_options = [(s.get("display_name", n), n) for n, s in self._services_cache.items()]
            svc_select.set_options(svc_options)
            first_svc = list(self._services_cache.keys())[0] if self._services_cache else ""
            first_actions = sorted((self._services_cache.get(first_svc, {}).get("actions") or []))
            act_select.set_options([(a, a) for a in first_actions])
        except Exception:
            pass  # dropdowns may not be mounted yet

        # info
        ok = health.get("status") == "ok"
        health_icon = "[green]OK[/]" if ok else "[red]DOWN[/]"
        ce = status.get("config_error") or "none"
        info_w.update(
            f"  Service: [bold cyan]render-service-manager[/]  Health: {health_icon}\n"
            f"  URL: [dim]{self.url}[/]\n"
            f"  Auth: {'ON' if self.token else 'OFF'}  Config error: {ce}  "
            f"Daily fetch: {status.get('daily_fetch', '?')}"
        )

        # services
        if not self._services_cache:
            svcs_w.update("[dim]No services in manifest.[/dim]")
        else:
            lines = ["  Service                       Loaded  Enabled  Error", "  " + "─" * 60]
            for name, s in self._services_cache.items():
                dn = s.get("display_name", name)
                loaded = s.get("loaded", False)
                enabled = s.get("enabled", False)
                err = s.get("error") or ""
                l_icon = "[green]✓[/]" if loaded else "[red]✗[/]"
                e_icon = "[green]on[/]" if enabled else "[dim]off[/]"
                err_txt = f"[red]{err[:40]}[/]" if err else ""
                lines.append(f"  {dn:<30} {l_icon}       {e_icon}      {err_txt}")
            svcs_w.update("\n".join(lines))

        # last tick
        lt = status.get("last_tick")
        if lt:
            ts = lt.get("ts", "?")
            svcs_lt = lt.get("services", {})
            lines = [f"  Last tick: [dim]{ts}[/]"]
            for name, r in svcs_lt.items():
                if r.get("ok"):
                    lines.append(f"    {name}: [green]OK[/] ({r.get('ms', '?')}ms)")
                elif r.get("skipped"):
                    lines.append(f"    {name}: [yellow]SKIP[/] ({r.get('reason', '')})")
                else:
                    e = r.get("error", "?")
                    lines.append(f"    {name}: [red]ERR[/] {e[:50]}")
            last_w.update("\n".join(lines))
        else:
            last_w.update("[dim]No ticks yet.[/]")

    @work
    async def run_tick(self) -> None:
        result = self.query_one("#tick-result", Static)
        result.update("[dim]Triggering /tick (may take 30-90s on cold start)...[/]")
        try:
            r = self.api.tick()
        except Exception as exc:
            result.update(f"[red]Error: {exc}[/]")
            return
        summary = r.get("summary", "(no summary)")
        ok_all = r.get("ok_all", False)
        ok = r.get("ok", False)
        icon = "[green]✓ ALL OK[/]" if ok_all else ("[yellow]~ PARTIAL[/]" if ok else "[red]✗ ERROR[/]")
        lines = [f"  {icon}\n", f"[dim]{summary}[/]\n"]
        svcs = r.get("services", {})
        for name, s in svcs.items():
            if s.get("skipped"):
                lines.append(f"  {name}: [yellow]SKIPPED[/] — {s.get('reason', '')}")
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
                lines.append(f"  {name}: [green]OK[/] ({s.get('ms', '?')}ms){f' — {hint}' if hint else ''}")
            else:
                e = s.get("error", "?")
                lines.append(f"  {name}: [red]ERROR[/] — {e[:80]}")
        result.update("\n".join(lines))
        self.refresh_dashboard()

    @work
    async def run_fetch(self) -> None:
        result = self.query_one("#fetch-result", Static)
        result.update("[dim]Fetching scripts from GitHub (10-30s)...[/]")
        try:
            r = self.api.fetch()
        except Exception as exc:
            result.update(f"[red]Error: {exc}[/]")
            return
        fetch = r.get("fetch", {})
        ok = r.get("ok", False)
        changed = fetch.get("changed", [])
        errors = fetch.get("errors", [])
        fetched = fetch.get("fetched", 0)
        icon = "[green]✓[/]" if ok else "[red]✗[/]"
        lines = [f"  {icon} Fetch complete: {fetched} files"]
        if changed:
            lines.append(f"  [yellow]Changed: {', '.join(changed)}[/]")
            lines.append("  [dim](manager will restart to reload)[/]")
        if errors:
            lines.append(f"  [red]Errors: {', '.join(errors)}[/]")
        if not changed and not errors:
            lines.append("  [dim]No changes — all scripts up to date.[/]")
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
        result.update(f"[dim]Testing {path} ...[/]")
        self._test_route(path)

    @work
    async def _test_route(self, path: str) -> None:
        use_t2g = path.startswith("/t2g")
        result = self.query_one("#route-result", Static)
        try:
            r = self.api.test_route("GET", path, use_t2g=use_t2g)
        except Exception as exc:
            result.update(f"[red]Error: {exc}[/]")
            return
        code = r["status_code"]
        body = r["body"]
        if isinstance(body, dict):
            body_str = json.dumps(body, indent=2)
            if len(body_str) > 2000:
                body_str = body_str[:2000] + "\n... (truncated)"
        else:
            body_str = str(body)[:2000]
        icon = "[green]200[/]" if code == 200 else f"[red]{code}[/]"
        result.update(f"  {icon}  {path}\n\n[dim]{body_str}[/]")

    # ── service action tab ──

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "service-select":
            svc_name = str(event.value)
            svc = self._services_cache.get(svc_name, {})
            actions = sorted(svc.get("actions") or [])
            act_select = self.query_one("#action-select", Select)
            act_select.set_options([(a, a) for a in actions])
        elif event.select.id == "action-select":
            # action selected — run it
            svc_select = self.query_one("#service-select", Select)
            svc_name = str(svc_select.value)
            action_name = str(event.value)
            if svc_name and action_name:
                self._run_service_action(svc_name, action_name)

    @work
    async def _run_service_action(self, svc_name: str, action_name: str) -> None:
        result = self.query_one("#service-result", Static)
        result.update(f"[dim]Running /services/{svc_name}/{action_name} ...[/]")
        use_t2g = svc_name == "t2g"
        try:
            r = self.api.test_route("GET", f"/services/{svc_name}/{action_name}", use_t2g=use_t2g)
        except Exception as exc:
            result.update(f"[red]Error: {exc}[/]")
            return
        code = r["status_code"]
        body = r["body"]
        if isinstance(body, dict):
            body_str = json.dumps(body, indent=2)
            if len(body_str) > 2000:
                body_str = body_str[:2000] + "\n... (truncated)"
        else:
            body_str = str(body)[:2000]
        icon = "[green]200[/]" if code == 200 else f"[red]{code}[/]"
        result.update(f"  {icon}  /services/{svc_name}/{action_name}\n\n[dim]{body_str}[/]")


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
