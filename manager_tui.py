"""render-service-manager TUI — terminal dashboard for the Render manager.

One operational screen instead of one tab per endpoint. Global actions
(tick, fetch, refresh) live in a persistent command bar and are bound to
keys that work from any view. Services, live status and the last tick are
merged into a single table with a detail pane for the selected service.

Views (keys 1 / 2 / 3, or F1 / F2 / F3 — the F-keys also work while typing,
or click the tabs):
  Operations  services, state, last tick, status detail, per-service actions
  Sources     browse the source files of each service (fetched from GitHub)
  Routes      send GET/POST to any manager or service route

Global keys (work from any view, even while typing in an input):
  ctrl+t  run a global tick            ctrl+f  fetch scripts from GitHub
  ctrl+r  refresh all data             ctrl+a  auto-refresh every 30s
  ctrl+l  show / hide the activity log T       cycle the color theme
  q       quit

View keys: 1 / 2 / 3 (or F1 / F2 / F3, which also work while typing).
The plain letters (t f r a l) work whenever no input field is focused.

Feedback model: every operation registers in the op status line (spinner +
elapsed time), ends with a toast, and leaves a line in the activity log at
the bottom, which stays visible across views.

Usage:
    .venv\\Scripts\\python.exe manager_tui.py
    .\\run_manager_tui.ps1

    python manager_tui.py --url https://... --token ... --t2g-token ...
    (falls back to MANAGER_URL / MANAGER_AUTH_TOKEN / T2G_AUTH_TOKEN or .env)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.notifications import SeverityLevel
from textual.suggester import SuggestFromList
from textual.timer import Timer
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Footer,
    Input,
    ProgressBar,
    RichLog,
    Select,
    Static,
    Tab,
    Tabs,
    TextArea,
)
from textual.worker import Worker, WorkerState

# ── config ───────────────────────────────────────────────────────────────

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

DEFAULT_URL = os.environ.get("MANAGER_URL", "")
DEFAULT_TOKEN = os.environ.get("MANAGER_AUTH_TOKEN", "")
DEFAULT_T2G_TOKEN = os.environ.get("T2G_AUTH_TOKEN", "")

AUTO_REFRESH_S = 30
THEMES = [
    "tokyo-night",
    "textual-dark",
    "nord",
    "gruvbox",
    "catppuccin-mocha",
    "dracula",
    "flexoki",
]

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# dest -> repo_path remap: the /services manifest only exposes dest paths,
# but GitHub wants the repository path (t2g/committer/credit differ).
SOURCE_REMAPS = [
    ("t2g/app.py", "remote/app.py"),
    ("t2g/cluster_helper.sh", "remote/cluster_helper.sh"),
    ("committer/app.py", "app.py"),
    ("credit/main.py", "backend/main.py"),
]


# ── small helpers ────────────────────────────────────────────────────────


def fmt_dur(seconds: float) -> str:
    """Compact duration: 843ms / 12.4s / 14m05s."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m"


def fmt_age(seconds: float | None) -> str:
    """Compact age: now / 5s / 12m / 3h / 2d / never."""
    if seconds is None:
        return "never"
    if seconds < 0:
        seconds = 0.0
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m"
    if seconds < 86400:
        return f"{seconds // 3600:.0f}h"
    return f"{seconds // 86400:.0f}d"


def parse_ts(value: Any) -> datetime | None:
    """Best-effort ISO timestamp parse (backend emits .isoformat() with tz)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ts_age_seconds(value: Any) -> float | None:
    ts = parse_ts(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


def clip(text: Any, limit: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ── API client (async httpx) ─────────────────────────────────────────────


@dataclass(slots=True)
class ApiResponse:
    status_code: int
    body: Any
    text: str
    elapsed_ms: float | None


class ManagerAPI:
    """Thin async HTTP client for the render-service-manager.

    Auth: every path under /t2g (and the t2g service's own actions) uses the
    T2G token; everything else uses the manager token. Matches the backend's
    AuthMiddleware, which accepts either token for manager routes.
    """

    def __init__(self, base_url: str, token: str = "", t2g_token: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token
        self.t2g_token = t2g_token
        self._client = httpx.AsyncClient(follow_redirects=True)

    def _headers(self, use_t2g: bool) -> dict:
        tok = self.t2g_token if use_t2g else self.token
        return {"X-Auth-Token": tok} if tok else {}

    async def request(
        self, method: str, path: str, *, use_t2g: bool = False, timeout: float = 60.0
    ) -> ApiResponse:
        resp = await self._client.request(
            method,
            f"{self.base}{path}",
            headers=self._headers(use_t2g),
            timeout=timeout,
        )
        try:
            body: Any = resp.json()
        except Exception:
            body = None
        elapsed = (
            resp.elapsed.total_seconds() * 1000 if resp.elapsed is not None else None
        )
        return ApiResponse(resp.status_code, body, resp.text, elapsed)

    async def get(
        self, path: str, *, use_t2g: bool = False, timeout: float = 60.0
    ) -> ApiResponse:
        return await self.request("GET", path, use_t2g=use_t2g, timeout=timeout)

    async def post(
        self, path: str, *, use_t2g: bool = False, timeout: float = 60.0
    ) -> ApiResponse:
        return await self.request("POST", path, use_t2g=use_t2g, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()


async def fetch_source_file(repo: str, branch: str, path: str, mode: str) -> dict:
    """Fetch one source file from GitHub (raw URL or Contents API)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if mode == "raw":
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        headers = {}
    else:
        url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
        headers = {"Accept": "application/vnd.github.raw"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(url, headers=headers, timeout=30.0)
    except Exception as exc:  # network / DNS / TLS
        return {"error": f"{type(exc).__name__}: {exc}", "content": ""}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} for {path}", "content": ""}
    return {"content": r.text, "bytes": len(r.content)}


# ── operation tracker ────────────────────────────────────────────────────


@dataclass(slots=True)
class OpRun:
    key: str
    label: str
    started: float
    finished: float | None = None
    ok: bool | None = None
    state: str = "ok"  # ok | warn | error


class OpTracker:
    """Registry of in-flight and finished operations (feedback model)."""

    def __init__(self, history: int = 100):
        self.active: dict[str, OpRun] = {}
        self.history: deque[OpRun] = deque(maxlen=history)

    def start(self, key: str, label: str) -> OpRun:
        run = OpRun(key=key, label=label, started=time.monotonic())
        self.active[key] = run
        return run

    def end(
        self, key: str, *, ok: bool, state: str = "ok", detail: str = ""
    ) -> OpRun | None:
        run = self.active.pop(key, None)
        if run is not None:
            run.finished = time.monotonic()
            run.ok = ok
            run.state = state
            self.history.append(run)
        return run

    def is_active(self, key: str) -> bool:
        return key in self.active

    def elapsed(self, key: str) -> float:
        run = self.active.get(key)
        return time.monotonic() - run.started if run else 0.0


# ── CSS (design system) ──────────────────────────────────────────────────

CSS = """
Screen {
    background: $background;
}

/* ── persistent chrome: topbar / command bar / op strip ── */

#topbar {
    height: 1;
    background: $panel;
}
#topbar-left {
    width: 1fr;
    padding: 0 1;
    overflow: hidden;
}
#topbar-right {
    width: auto;
    padding: 0 1;
    color: $text-muted;
}

#cmdbar {
    height: 3;
    background: $panel;
    padding: 0 1;
}
#cmdbar Button {
    min-width: 4;
    margin-right: 1;
}
#cmdbar-spacer {
    width: 1fr;
}
#view-tabs {
    width: auto;
    background: $panel;
}

#opstrip {
    height: auto;
    background: $panel;
    padding: 0 1;
}
#op-status {
    height: 1;
    color: $text-muted;
}
#op-progress {
    height: 1;
    display: none;
}
#opstrip.busy #op-progress {
    display: block;
}

/* ── views ── */

#views {
    height: 1fr;
}
.view {
    display: none;
    height: 1fr;
}
.view.active {
    display: block;
}

/* shared bordered panel look */
.panel {
    border: round $border;
    background: $surface;
    border-title-color: $text-muted;
    border-title-style: b;
    border-title-align: left;
}

/* ── operations view ── */

#notice {
    display: none;
    height: auto;
    padding: 0 1;
    margin: 0 0 1 0;
    background: $boost;
    border-left: heavy $error;
    color: $error;
    overflow: hidden;
}
#notice.warning {
    border-left: heavy $warning;
    color: $warning;
}

#kpi-row {
    height: 4;
    layout: grid;
    grid-size: 5 1;
    grid-gutter: 0 1;
    margin: 0 0 1 0;
}
.kpi {
    border: round $border;
    background: $surface;
    border-title-color: $text-muted;
    border-title-style: b;
    border-title-align: left;
    padding: 0 1;
}
.kpi-value {
    height: 1;
    text-style: b;
}
.kpi-sub {
    height: 1;
    color: $text-muted;
    overflow: hidden;
}

#ops-main {
    height: 1fr;
}
#svc-panel {
    width: 3fr;
    padding: 0 1;
}
#svc-table {
    height: 1fr;
}
#detail-panel {
    width: 2fr;
    padding: 0 1;
}
#detail-head {
    height: auto;
}
#detail-desc {
    height: auto;
    color: $text-muted;
    margin: 0 0 1 0;
}
#detail-tick, #detail-status {
    height: auto;
    margin: 0 0 1 0;
}
#detail-actions {
    height: auto;
    margin: 0 0 1 0;
}
#detail-actions Button {
    min-width: 4;
    margin-right: 1;
}
#detail-output-panel {
    height: 10;
    padding: 0 1;
    margin: 1 0 0 0;
}
#detail-op-status {
    height: 1;
    color: $text-muted;
    overflow: hidden;
}
#detail-output {
    height: 1fr;
}
#detail-config, #detail-endpoints {
    margin: 0 0 1 0;
}

/* ── sources view ── */

#src-main {
    height: 1fr;
}
#src-left {
    width: 2fr;
}
#src-service {
    width: 1fr;
    margin: 0 0 1 0;
}
#src-files-panel {
    height: 1fr;
    padding: 0 1;
}
#src-files {
    height: 1fr;
}
#src-right {
    width: 3fr;
}
#src-meta {
    height: auto;
    color: $text-muted;
    padding: 0 1;
    margin: 0 0 1 0;
}
#src-viewer-panel {
    height: 1fr;
}
#src-viewer {
    height: 1fr;
}

/* ── routes view ── */

#route-form {
    height: 3;
    padding: 0 1;
}
#route-method {
    width: 12;
    height: 3;
}
#route-input {
    width: 1fr;
    margin: 0 1 0 1;
}
#route-note {
    height: auto;
    color: $text-muted;
    padding: 0 1;
    margin: 0 0 1 0;
}
#route-output-panel {
    height: 1fr;
    padding: 0 1;
}
#route-status {
    height: 1;
    overflow: hidden;
}
#route-body {
    height: 1fr;
}

/* read-only viewers live inside their own panels: no nested border */
#detail-output, #src-viewer, #route-body {
    border: none;
    background: transparent;
}

/* ── activity log (persistent bottom panel) ── */

#log-panel {
    height: 10;
    padding: 0 1;
}
#log-panel.hidden {
    display: none;
}
#activity-log {
    height: 1fr;
}
"""


# ── the app ──────────────────────────────────────────────────────────────


class ManagerTUI(App):
    """render-service-manager dashboard — one screen, global actions, live feedback."""

    TITLE = "render-service-manager"
    CSS = CSS
    AUTO_FOCUS = "#svc-table"

    BINDINGS: ClassVar[list[BindingType]] = [
        # plain letters work whenever no input-type widget has focus
        Binding("t", "tick", "Tick", show=False),
        Binding("f", "fetch", "Fetch", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("a", "auto_toggle", "Auto", show=False),
        Binding("l", "toggle_log", "Log", show=False),
        Binding("T", "cycle_theme", "Theme", show=False),
        # modifier versions always work, even while typing in an input
        Binding("ctrl+t", "tick", "Tick", priority=True),
        Binding("ctrl+f", "fetch", "Fetch", priority=True),
        Binding("ctrl+r", "refresh", "Refresh", priority=True),
        Binding("ctrl+a", "auto_toggle", "Auto 30s", priority=True),
        Binding("ctrl+l", "toggle_log", "Log", priority=True),
        # view switching: plain digits when no input has focus, F-keys always
        Binding("1", "view('ops')", "Operations", show=False),
        Binding("2", "view('sources')", "Sources", show=False),
        Binding("3", "view('routes')", "Routes", show=False),
        Binding("f1", "view('ops')", "Operations", show=False, priority=True),
        Binding("f2", "view('sources')", "Sources", show=False, priority=True),
        Binding("f3", "view('routes')", "Routes", show=False, priority=True),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        url: str = DEFAULT_URL,
        token: str = DEFAULT_TOKEN,
        t2g_token: str = DEFAULT_T2G_TOKEN,
    ):
        super().__init__()
        self.url = url
        self.token = token
        self.t2g_token = t2g_token
        self.api = ManagerAPI(url, token, t2g_token)
        self.ops = OpTracker()

        # caches straight from the backend
        self._services: dict[str, dict] = {}
        self._status: dict = {}
        self._last_tick: dict | None = None
        self._health_ms: float | None = None
        self._config_error: Any = None
        # connection state: connecting | online | auth | offline
        self._conn = "connecting"
        self._conn_detail = ""
        self._last_refresh_ok: float | None = None
        self._ever_connected = False

        self._view = "ops"
        self._selected: str | None = None
        self._tick_pending = False
        self._auto = True
        self._spin = 0
        self._action_buttons: dict[Button, tuple[str, str]] = {}
        self._route_suggester = SuggestFromList([], case_sensitive=False)
        self._ui_timer: Timer | None = None
        self._age_timer: Timer | None = None
        self._auto_timer: Timer | None = None

    # ── layout ───────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # persistent chrome ------------------------------------------------
        with Horizontal(id="topbar"):
            yield Static(id="topbar-left", markup=False)
            yield Static(id="topbar-right", markup=False)

        with Horizontal(id="cmdbar"):
            yield Button(
                Text("Tick"),
                variant="primary",
                id="btn-tick",
                tooltip="Run /tick — every service, per its schedule (30-90s cold)",
            )
            yield Button(
                Text("Fetch"),
                id="btn-fetch",
                tooltip="GET /fetch — force-update scripts from GitHub",
            )
            yield Button(
                Text("Refresh"),
                id="btn-refresh",
                tooltip="Reload health, services and status from the manager",
            )
            yield Button(
                Text("Auto 30s"),
                variant="success",
                id="btn-auto",
                tooltip="Toggle background refresh every 30 seconds",
            )
            yield Static(id="cmdbar-spacer", markup=False)
            yield Tabs(
                Tab(Text("1 Operations"), id="ops"),
                Tab(Text("2 Sources"), id="sources"),
                Tab(Text("3 Routes"), id="routes"),
                active="ops",
                id="view-tabs",
            )

        with Vertical(id="opstrip"):
            yield Static(id="op-status", markup=False)
            yield ProgressBar(
                total=None, show_eta=False, show_percentage=False, id="op-progress"
            )

        # views ------------------------------------------------------------
        with Vertical(id="views"):
            with Vertical(id="view-ops", classes="view active"):
                yield Static(id="notice", markup=False)
                with Horizontal(id="kpi-row"):
                    for kid in ("services", "loaded", "tick", "errors", "fetch"):
                        with Vertical(classes="kpi", id=f"kpi-{kid}"):
                            yield Static(classes="kpi-value", markup=False)
                            yield Static(classes="kpi-sub", markup=False)
                with Horizontal(id="ops-main"):
                    with Vertical(classes="panel", id="svc-panel"):
                        yield DataTable(
                            id="svc-table", zebra_stripes=True, cursor_type="row"
                        )
                    with VerticalScroll(classes="panel", id="detail-panel"):
                        yield Static(id="detail-head", markup=False)
                        yield Static(id="detail-desc", markup=False)
                        yield Static(id="detail-tick", markup=False)
                        yield Static(id="detail-status", markup=False)
                        yield Horizontal(id="detail-actions")
                        with Vertical(classes="panel", id="detail-output-panel"):
                            yield Static(id="detail-op-status", markup=False)
                            yield TextArea.code_editor(
                                "",
                                id="detail-output",
                                read_only=True,
                                soft_wrap=True,
                                show_line_numbers=False,
                            )
                        with Collapsible(title="Configuration", id="detail-config"):
                            yield Static(id="detail-facts", markup=False)
                        with Collapsible(title="API endpoints", id="detail-endpoints"):
                            yield Static(id="detail-eps", markup=False)

            with Vertical(id="view-sources", classes="view"):
                with Horizontal(id="src-main"):
                    with Vertical(id="src-left"):
                        yield Select(
                            [],
                            prompt="Service",
                            id="src-service",
                            allow_blank=True,
                            type_to_search=True,
                        )
                        with Vertical(classes="panel", id="src-files-panel"):
                            yield DataTable(id="src-files", cursor_type="row")
                    with Vertical(id="src-right"):
                        yield Static(id="src-meta", markup=False)
                        with Vertical(classes="panel", id="src-viewer-panel"):
                            yield TextArea.code_editor(
                                "",
                                id="src-viewer",
                                read_only=True,
                                soft_wrap=False,
                                show_line_numbers=True,
                            )

            with Vertical(id="view-routes", classes="view"):
                with Horizontal(id="route-form"):
                    yield Select(
                        [("GET", "GET"), ("POST", "POST")],
                        value="GET",
                        id="route-method",
                        allow_blank=False,
                    )
                    yield Input(
                        placeholder="/t2g/status  /credit/api/data  /services/committer/tick …",
                        id="route-input",
                        suggester=self._route_suggester,
                    )
                    yield Button(Text("Send"), variant="primary", id="route-send")
                yield Static(id="route-note", markup=False)
                with Vertical(classes="panel", id="route-output-panel"):
                    yield Static(id="route-status", markup=False)
                    yield TextArea.code_editor(
                        "",
                        id="route-body",
                        read_only=True,
                        soft_wrap=True,
                        show_line_numbers=False,
                    )

        # persistent activity log ------------------------------------------
        with Vertical(classes="panel", id="log-panel"):
            yield RichLog(
                id="activity-log",
                markup=False,
                highlight=False,
                wrap=True,
                max_lines=400,
            )

        yield Footer()

    # ── lifecycle ────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.theme = THEMES[0]

        table = self.query_one("#svc-table", DataTable)
        table.add_column("Service", key="service")
        table.add_column("Tick", key="tick", width=12)
        table.add_column("Status", key="status")
        files = self.query_one("#src-files", DataTable)
        files.add_column("File", key="file")

        self.query_one("#svc-panel", Vertical).border_title = "SERVICES"
        self.query_one("#detail-panel", VerticalScroll).border_title = "SERVICE DETAIL"
        self.query_one("#detail-output-panel", Vertical).border_title = "ACTION OUTPUT"
        self.query_one("#src-files-panel", Vertical).border_title = "SOURCE FILES"
        self.query_one("#src-viewer-panel", Vertical).border_title = "SOURCE VIEWER"
        self.query_one("#route-output-panel", Vertical).border_title = "RESPONSE"
        self.query_one("#log-panel", Vertical).border_title = "ACTIVITY LOG"
        for kid, title in (
            ("services", "SERVICES"),
            ("loaded", "LOADED"),
            ("tick", "LAST TICK"),
            ("errors", "ERRORS"),
            ("fetch", "FETCH"),
        ):
            self.query_one(f"#kpi-{kid}", Vertical).border_title = title

        # first paint: intentional placeholders
        self._render_topbar()
        self._render_kpis()
        self._render_op_strip()
        self._render_detail()
        self._render_route_note()
        self.query_one("#route-status", Static).update(
            Text("no request yet — type a path above and press enter", "dim")
        )

        self._log_line(
            "",
            f"render-service-manager TUI — manager at {self.url}",
            color="b",
        )
        self._log_line(
            "",
            "keys: t tick · f fetch · r refresh · a auto · l log · 1/2/3 or F1-F3 views · T theme",
            color="dim",
        )

        self._ui_timer = self.set_interval(0.12, self._on_ui_tick)
        self._age_timer = self.set_interval(1.0, self._on_age_tick)
        self._auto_timer = self.set_interval(AUTO_REFRESH_S, self._on_auto_refresh)

        # the services table is the landing widget: arrows work immediately
        self.query_one("#svc-table", DataTable).focus()

        self._spawn(self._refresh_data(), name="op-refresh", group="op-refresh")

    async def on_unmount(self) -> None:
        for timer in (self._ui_timer, self._age_timer, self._auto_timer):
            if timer is not None:
                timer.stop()
        await self.api.aclose()

    def _spawn(self, coro, *, name: str, group: str, exclusive: bool = True) -> None:
        try:
            self.run_worker(
                coro,
                name=name,
                group=group,
                description=name,
                exclusive=exclusive,
                exit_on_error=False,
            )
        except Exception as exc:
            self._log_line("✗", f"could not start {name}: {exc}", color="red")

    @on(Worker.StateChanged)
    def _on_worker_state(self, event: Worker.StateChanged) -> None:
        if event.worker.state == WorkerState.ERROR:
            self._log_line("✗", f"worker {event.worker.name} failed", color="red")

    # ── timers ───────────────────────────────────────────────────────────

    def _on_ui_tick(self) -> None:
        """Fast ticker: spinner + elapsed time of in-flight operations."""
        try:
            self._spin = (self._spin + 1) % len(SPINNER)
            self._render_op_strip()
        except Exception:
            pass  # teardown race: widgets may already be gone

    def _on_age_tick(self) -> None:
        """Slow ticker: refresh age in the topbar, tick age in the KPIs."""
        try:
            self._render_topbar()
            self._render_kpis()
        except Exception:
            pass  # teardown race: widgets may already be gone

    def _on_auto_refresh(self) -> None:
        if self.ops.is_active("tick") or self.ops.is_active("fetch"):
            return
        self._spawn(
            self._refresh_data(auto=True), name="op-refresh-auto", group="op-refresh"
        )

    # ── view switching ───────────────────────────────────────────────────

    VIEWS = ("ops", "sources", "routes")

    def action_view(self, which: str) -> None:
        self._switch_view(which)

    def _switch_view(self, which: str) -> None:
        if which not in self.VIEWS or which == self._view:
            return
        self._view = which
        for view_id in self.VIEWS:
            self.query_one(f"#view-{view_id}").set_class(view_id == which, "active")
        tabs = self.query_one("#view-tabs", Tabs)
        if tabs.active != which:
            tabs.active = which

        focus_target = {
            "ops": "#svc-table",
            "sources": "#src-service",
            "routes": "#route-input",
        }[which]
        self.call_after_refresh(lambda: self._focus(focus_target))

    def _focus(self, selector: str) -> None:
        try:
            self.query_one(selector).focus()
        except Exception:
            pass

    @on(Tabs.TabActivated)
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab is not None and event.tab.id:
            self._switch_view(event.tab.id)

    # ── global actions (keys + command bar) ──────────────────────────────

    def action_tick(self) -> None:
        self._start_tick()

    def action_fetch(self) -> None:
        self._start_fetch()

    def action_refresh(self) -> None:
        self._spawn(
            self._refresh_data(manual=True),
            name="op-refresh-manual",
            group="op-refresh",
        )

    def action_auto_toggle(self) -> None:
        self._set_auto(not self._auto)

    def action_toggle_log(self) -> None:
        panel = self.query_one("#log-panel")
        hidden = panel.has_class("hidden")
        panel.set_class(not hidden, "hidden")
        self._log_line(
            "", "activity log " + ("shown" if hidden else "hidden"), color="dim"
        )

    def action_cycle_theme(self) -> None:
        current = self.theme if self.theme in THEMES else THEMES[0]
        nxt = THEMES[(THEMES.index(current) + 1) % len(THEMES)]
        self.theme = nxt
        self.notify(f"theme: {nxt}", title="Theme", timeout=3, markup=False)

    def _set_auto(self, on: bool) -> None:
        self._auto = on
        if self._auto_timer is not None:
            if on:
                self._auto_timer.resume()
            else:
                self._auto_timer.pause()
        btn = self.query_one("#btn-auto", Button)
        btn.label = Text("Auto 30s") if on else Text("Auto off")
        btn.variant = "success" if on else "default"
        self._log_line(
            "", f"auto-refresh {'on (every 30s)' if on else 'off'}", color="dim"
        )

    # ── data refresh pipeline ────────────────────────────────────────────

    async def _refresh_data(self, *, manual: bool = False, auto: bool = False) -> None:
        """Pull /health, /services and /status; update every view + caches."""
        if self.ops.is_active("refresh"):
            return
        self.ops.start("refresh", "refresh")
        was_online = self._ever_connected
        try:
            self.query_one("#svc-table", DataTable).loading = True
            self._render_topbar()

            results = await asyncio.gather(
                self.api.get("/health", timeout=75.0),
                self.api.get("/services", timeout=75.0),
                self.api.get("/status", timeout=75.0),
                return_exceptions=True,
            )
            health_r, services_r, status_r = results

            # classify the connection from what answered at all
            responses = [r for r in results if isinstance(r, ApiResponse)]
            exceptions = [r for r in results if isinstance(r, Exception)]

            if responses:
                unauthorized = any(r.status_code in (401, 403) for r in responses)
                any_ok = any(r.status_code == 200 for r in responses)
                if any_ok:
                    self._conn = "online"
                elif unauthorized:
                    self._conn = "auth"
                else:
                    self._conn = "online"  # server answered, odd status — still alive
                self._ever_connected = True
                if exceptions:
                    self._conn_detail = clip(exceptions[0], 120)
                else:
                    self._conn_detail = ""
            else:
                self._conn = "offline"
                self._conn_detail = (
                    clip(exceptions[0], 160) if exceptions else "no response"
                )

            if isinstance(health_r, ApiResponse) and health_r.status_code == 200:
                self._health_ms = health_r.elapsed_ms
            else:
                self._health_ms = None
            if isinstance(services_r, ApiResponse) and services_r.status_code == 200:
                self._services = services_r.body.get("services") or {}
                self._config_error = services_r.body.get("config_error")
            if isinstance(status_r, ApiResponse) and status_r.status_code == 200:
                self._status = status_r.body or {}
                self._config_error = (
                    self._status.get("config_error") or self._config_error
                )
                self._last_tick = self._status.get("last_tick") or self._last_tick

            self._last_refresh_ok = (
                time.monotonic() if self._conn != "offline" else None
            )

            # render everything from the caches
            self._render_topbar()
            self._render_notice()
            self._render_kpis()
            self._render_services_table()
            self._render_detail()
            self._sync_sources_select()
            self._update_route_suggestions()
        except Exception as exc:
            self._conn = "offline"
            self._conn_detail = clip(exc, 160)
            self._render_topbar()
            self._render_notice()
        finally:
            try:
                table = self.query_one("#svc-table", DataTable)
                table.loading = False
                # the loading overlay drops focus: restore it to the table
                # only when nothing else has it (never steal user focus)
                if self.focused is None:
                    table.focus()
            except Exception:
                pass
            ok = self._conn == "online"
            if ok:
                detail = f"{len(self._services)} services"
                if self._health_ms:
                    detail += f" · {self._health_ms:.0f} ms"
            else:
                detail = self._conn_detail or self._conn
            # log manual refreshes, failures and recoveries; toast failures
            # only for the initial connect and manual refreshes
            self._op_end(
                "refresh",
                ok=ok,
                state="ok" if ok else "error",
                detail=detail,
                log=manual or not ok or (ok and not was_online),
                toast=None if (ok or auto) else f"unreachable — {self._conn_detail}",
            )

    # ── rendering: topbar / notice / KPIs ────────────────────────────────

    def _render_topbar(self) -> None:
        left = Text.assemble(("render-service-manager ", "b"), (self.url, "dim"))
        right = Text()
        if self._conn == "online":
            ms = f" · {self._health_ms:.0f} ms" if self._health_ms else ""
            right.append(f"● online{ms}", "green")
        elif self._conn == "connecting":
            if self.ops.is_active("refresh"):
                right.append("◌ connecting…", "yellow")
            else:
                right.append("◌ not connected yet", "yellow")
        elif self._conn == "auth":
            right.append("● auth failed (401)", "red")
        else:
            right.append("● offline", "red")
        if self._last_refresh_ok is not None:
            age = fmt_age(time.monotonic() - self._last_refresh_ok)
            if age == "now":
                right.append(" · updated just now", "dim")
            else:
                right.append(f" · updated {age} ago", "dim")
        self.query_one("#topbar-left", Static).update(left)
        self.query_one("#topbar-right", Static).update(right)

    def _render_notice(self) -> None:
        """Intentional offline / config-error banner in the operations view."""
        notice = self.query_one("#notice", Static)
        lines: list[Text] = []
        level = "error"
        if self._conn == "offline":
            lines.append(Text.assemble(("Backend unreachable — ", "b"), (self.url, "")))
            if self._conn_detail:
                lines.append(Text(f"  {self._conn_detail}", "dim"))
            lines.append(
                Text(
                    "  Render free-tier services sleep: the first request can take 30-60s to wake."
                    " Press r / ctrl+r to retry.",
                    "dim",
                )
            )
        elif self._conn == "auth":
            lines.append(Text("401 — the manager rejected the token.", "b"))
            lines.append(
                Text(
                    "  Check --token (MANAGER_AUTH_TOKEN) or --t2g-token (T2G_AUTH_TOKEN).",
                    "dim",
                )
            )
        else:
            if self._config_error:
                level = "warning"
                lines.append(Text("Manager config error:", "b"))
                lines.append(Text(f"  {clip(self._config_error, 200)}", "dim"))
        if not lines:
            notice.display = False
        else:
            notice.display = True
            notice.set_class(level == "warning", "warning")
            notice.update(Text("\n").join(lines))

    def _services_with_errors(self) -> list[str]:
        out = []
        for name, s in self._services.items():
            if s.get("error"):
                out.append(name)
            else:
                tick = ((self._last_tick or {}).get("services") or {}).get(name) or {}
                if not tick.get("ok") and not tick.get("skipped") and tick:
                    out.append(name)
        return out

    def _render_kpis(self) -> None:
        def set_kpi(kid: str, value: Text, sub: str) -> None:
            self.query_one(f"#kpi-{kid} .kpi-value", Static).update(value)
            self.query_one(f"#kpi-{kid} .kpi-sub", Static).update(
                Text(clip(sub, 40), "dim")
            )

        total = len(self._services)
        if self._conn == "connecting" and not total:
            for kid in ("services", "loaded", "tick", "errors", "fetch"):
                set_kpi(kid, Text("…", "dim"), "loading")
            return

        loaded = sum(1 for s in self._services.values() if s.get("loaded"))
        enabled = sum(1 for s in self._services.values() if s.get("enabled"))
        set_kpi(
            "services",
            Text(str(total), "b"),
            f"{enabled} enabled" if self._conn != "offline" else "unreachable",
        )
        if total:
            color = "green" if loaded == total else ("yellow" if loaded else "red")
            load_errors = [n for n, s in self._services.items() if s.get("error")]
            if load_errors:
                sub = f"{len(load_errors)} load error" + (
                    "s" if len(load_errors) > 1 else ""
                )
            else:
                sub = "all loaded"
            set_kpi("loaded", Text(f"{loaded}/{total}", color), sub)
        else:
            set_kpi("loaded", Text("—", "dim"), "no data")

        tick_services = (self._last_tick or {}).get("services") or {}
        if self._tick_pending:
            set_kpi("tick", Text("running…", "yellow"), "tick in flight")
        elif tick_services:
            ok_n = sum(1 for r in tick_services.values() if r.get("ok"))
            skip_n = sum(1 for r in tick_services.values() if r.get("skipped"))
            err_n = len(tick_services) - ok_n - skip_n
            age = ts_age_seconds((self._last_tick or {}).get("ts"))
            color = "green" if err_n == 0 else "red"
            parts = [
                p
                for p in (f"{ok_n} ok", f"{skip_n} skip", f"{err_n} err")
                if not p.startswith("0")
            ]
            set_kpi(
                "tick", Text(fmt_age(age), color), " · ".join(parts) or "no services"
            )
        else:
            set_kpi("tick", Text("never", "dim"), "no ticks recorded")

        errs = self._services_with_errors()
        set_kpi(
            "errors",
            Text(str(len(errs)), "red" if errs else "green"),
            ", ".join(errs)[:40] if errs else "no errors",
        )

        fetch_day = (self._status or {}).get("last_fetch_day")
        daily = (self._status or {}).get("daily_fetch")
        set_kpi(
            "fetch",
            Text(fetch_day if isinstance(fetch_day, str) else "never", "b"),
            f"daily fetch {'on' if daily else 'off'}",
        )

    # ── rendering: services table ────────────────────────────────────────

    def _tick_cell(self, name: str) -> Text:
        if self._tick_pending:
            return Text("…", "dim")
        tick = ((self._last_tick or {}).get("services") or {}).get(name)
        if not tick:
            return Text("—", "dim")
        if tick.get("skipped"):
            return Text("⊘ skip", "yellow")
        if tick.get("ok"):
            ms = tick.get("ms")
            return Text(
                f"✓ {ms}ms" if isinstance(ms, (int, float)) else "✓ ok", "green"
            )
        return Text("✗ error", "red")

    def _status_cell(self, name: str) -> Text:
        st = ((self._status or {}).get("services") or {}).get(name) or {}
        err = st.get("error")
        if err:
            return Text(clip(f"load: {err}", 40), "red")
        hint, color = self._hint(st.get("status"))
        return Text(hint, color)

    @staticmethod
    def _hint(status: Any) -> tuple[str, str]:
        """One-line human hint from a service status payload."""
        if status is None:
            return "—", "dim"
        if not isinstance(status, dict):
            return clip(status, 60), "dim"
        if "error" in status:
            return clip(f"error: {status['error']}", 60), "red"
        if status.get("status") == "sleeping":
            return "sleeping (outside hours)", "yellow"
        if "cluster_reachable" in status:
            ok = status.get("cluster_reachable")
            return ("cluster reachable" if ok else "cluster unreachable"), (
                "green" if ok else "red"
            )
        if "pending_entries" in status:
            n = status.get("pending_entries")
            return f"{n} pending backup entries", ("yellow" if n else "dim")
        if (
            isinstance(status.get("entry"), dict)
            and status["entry"].get("balance") is not None
        ):
            return f"balance ${status['entry']['balance']}", "green"
        if not status:
            return "—", "dim"
        return (
            clip(" ".join(f"{k}={v}" for k, v in list(status.items())[:2]), 40),
            "dim",
        )

    def _render_services_table(self) -> None:
        table = self.query_one("#svc-table", DataTable)
        current = self._selected if self._selected in self._services else None
        table.clear()
        if not self._services:
            placeholder = (
                "no services in the manifest"
                if self._conn == "online"
                else "no data — backend unreachable, press r to retry"
            )
            table.add_row(Text(placeholder, "dim"), Text(""), Text(""), key="__empty__")
        else:
            for name, s in self._services.items():
                display = clip(s.get("display_name") or name, 20)
                if not s.get("enabled"):
                    glyph, color = "○", "dim"
                elif s.get("loaded"):
                    glyph, color = "●", "green"
                else:
                    glyph, color = "×", "red"
                table.add_row(
                    Text.assemble((glyph + " ", color), (display, "")),
                    self._tick_cell(name),
                    self._status_cell(name),
                    key=name,
                )
        if current is not None:
            try:
                table.move_cursor(row=table.get_row_index(current))
            except Exception:
                pass
        elif not current and self._selected:
            self._selected = None
            self._render_detail()

    def _set_pending_tick_ui(self) -> None:
        self._tick_pending = True
        table = self.query_one("#svc-table", DataTable)
        for name in self._services:
            try:
                table.update_cell(name, "tick", Text("…", "dim"))
            except Exception:
                pass
        self._render_kpis()

    def _clear_pending_tick_ui(self) -> None:
        self._tick_pending = False
        self._render_kpis()
        self._render_services_table()

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "svc-table" and event.row_key is not None:
            name = event.row_key.value
            if name and name in self._services:
                self._selected = name
                self._render_detail()

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        # selecting a row in the services table focuses its actions
        if event.data_table.id == "svc-table" and self._selected:
            self._focus("#detail-actions Button")

    # ── rendering: service detail pane ───────────────────────────────────

    def _render_detail(self) -> None:
        head = self.query_one("#detail-head", Static)
        desc = self.query_one("#detail-desc", Static)
        tick_w = self.query_one("#detail-tick", Static)
        status_w = self.query_one("#detail-status", Static)
        facts = self.query_one("#detail-facts", Static)
        eps = self.query_one("#detail-eps", Static)
        actions_bar = self.query_one("#detail-actions", Horizontal)

        self._action_buttons = {}
        actions_bar.remove_children()
        name = self._selected
        if not name or name not in self._services:
            head.update(Text("select a service in the table", "dim"))
            desc.update(Text(""))
            tick_w.update(Text(""))
            status_w.update(Text(""))
            facts.update(Text(""))
            eps.update(Text(""))
            self.query_one("#detail-op-status", Static).update(
                Text("— run an action to see its result", "dim")
            )
            return

        s = self._services[name]
        st = ((self._status or {}).get("services") or {}).get(name) or {}
        display = s.get("display_name") or name
        if not s.get("enabled"):
            chip = ("  ○ disabled", "dim")
        elif s.get("loaded"):
            chip = ("  ● loaded", "green")
        else:
            chip = ("  × load failed", "red")
        head.update(Text.assemble((clip(display, 44), "b"), (f"\n{name}", "dim"), chip))

        desc_text = (s.get("description") or "").strip()
        desc.update(Text(desc_text, "dim") if desc_text else Text(""))

        # last tick block
        tick_entry = ((self._last_tick or {}).get("services") or {}).get(name)
        tick_line = Text.assemble(("last tick  ", "b"))
        tick_line.append_text(self._tick_text(name, tick_entry))
        tick_w.update(tick_line)

        # current status block
        err = st.get("error")
        if err:
            status_w.update(
                Text.assemble(
                    ("status      ", "b"), (clip(f"load error: {err}", 70), "red")
                )
            )
        else:
            hint, color = self._hint(st.get("status"))
            status_w.update(Text.assemble(("status      ", "b"), (hint, color)))

        # configuration facts (inside the Collapsible)
        src = s.get("source") or {}
        api = s.get("api") or {}
        schedule = s.get("schedule") or {}
        label_w = 12
        fact_lines = []

        def fact(label: str, value: Any) -> None:
            fact_lines.append(
                Text.assemble(
                    (f"{label:<{label_w}}", "dim"),
                    (clip(value, 80) if value is not None else "—", ""),
                )
            )

        fact("mount", api.get("mount"))
        fact("auth", api.get("auth"))
        fact("repo", src.get("repo"))
        fact("branch", src.get("branch"))
        fact("source mode", src.get("mode"))
        fact("files", len(src.get("files") or []))
        fact("schedule", schedule.get("mode"))
        fact("public", ", ".join(api.get("public_paths") or []) or "—")
        facts.update(Text("\n").join(fact_lines))

        # endpoints (inside the Collapsible)
        ep_lines = []
        for ep, doc in (api.get("endpoints") or {}).items():
            ep_lines.append(
                Text.assemble(
                    (clip(ep, 34), ""), (f" — {clip(doc, 80)}" if doc else "", "dim")
                )
            )
        eps.update(
            Text("\n").join(ep_lines)
            if ep_lines
            else Text("no endpoints declared", "dim")
        )

        # action buttons — runnable inline from the detail pane
        actions = sorted(s.get("actions") or [])
        for action in actions:
            btn = Button(
                Text(action),
                variant="primary" if action == "tick" else "default",
            )
            self._action_buttons[btn] = (name, action)
            actions_bar.mount(btn)

    def _tick_text(self, name: str, tick_entry: dict | None) -> Text:
        if self._tick_pending:
            return Text("tick running…", "yellow")
        if not tick_entry:
            return Text("no tick recorded", "dim")
        if tick_entry.get("skipped"):
            return Text(f"skipped — {clip(tick_entry.get('reason'), 60)}", "yellow")
        if tick_entry.get("ok"):
            ms = tick_entry.get("ms")
            return Text(
                f"ok · {ms}ms" if isinstance(ms, (int, float)) else "ok", "green"
            )
        return Text(f"error — {clip(tick_entry.get('error'), 60)}", "red")

    @on(Button.Pressed)
    def _on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if button.id == "btn-tick":
            self._start_tick()
        elif button.id == "btn-fetch":
            self._start_fetch()
        elif button.id == "btn-refresh":
            self._spawn(
                self._refresh_data(manual=True),
                name="op-refresh-manual",
                group="op-refresh",
            )
        elif button.id == "btn-auto":
            self._set_auto(not self._auto)
        elif button.id == "route-send":
            self._send_route()
        else:
            target = self._action_buttons.get(button)
            if target:
                svc, action = target
                self._start_service_action(svc, action, button)

    # ── global tick ──────────────────────────────────────────────────────

    def _start_tick(self) -> None:
        if self.ops.is_active("tick"):
            elapsed = fmt_dur(self.ops.elapsed("tick"))
            self.notify(
                f"a tick is already running ({elapsed} elapsed)",
                title="Tick",
                severity="warning",
                timeout=4,
                markup=False,
            )
            self._log_line(
                "⚠", f"tick ignored — already in flight ({elapsed})", color="yellow"
            )
            return
        self.ops.start("tick", "tick")
        self._log_line("→", "tick started (may take 30-90s on cold start)", color="dim")
        self._set_pending_tick_ui()
        self._render_op_strip()
        self._spawn(self._op_tick(), name="op-tick", group="op-tick")

    async def _op_tick(self) -> None:
        try:
            resp = await self.api.get("/tick", timeout=240.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._op_fail("tick", exc)
            self._clear_pending_tick_ui()
            return

        if resp.status_code == 409:
            body = resp.body if isinstance(resp.body, dict) else {}
            err = body.get("error") or clip(resp.text, 120)
            self._op_end(
                "tick",
                ok=False,
                state="warn",
                detail=f"backend busy — {err}",
                toast=f"backend busy: {err}",
                toast_sev="warning",
            )
            self._clear_pending_tick_ui()
            return
        if resp.status_code != 200 or not isinstance(resp.body, dict):
            self._op_end(
                "tick",
                ok=False,
                state="error",
                detail=f"HTTP {resp.status_code} — {clip(resp.text, 120)}",
                toast=f"HTTP {resp.status_code}",
                toast_sev="error",
            )
            self._clear_pending_tick_ui()
            return

        body = resp.body
        services = body.get("services") or {}
        ok_n = sum(1 for r in services.values() if r.get("ok"))
        skip_n = sum(1 for r in services.values() if r.get("skipped"))
        err_n = len(services) - ok_n - skip_n

        # merge the fresh results into the caches and re-render
        self._last_tick = {
            "ts": body.get("ts"),
            "services": {
                n: {k: v for k, v in r.items() if k != "data"}
                for n, r in services.items()
            },
        }
        self._clear_pending_tick_ui()
        self._render_topbar()
        self._render_kpis()

        state = "error" if err_n else ("warn" if skip_n else "ok")
        detail = f"{ok_n} ok · {skip_n} skip · {err_n} err"
        toast = f"{ok_n} ok · {skip_n} skipped · {err_n} errors"
        self._op_end(
            "tick",
            ok=err_n == 0,
            state=state,
            detail=detail,
            toast=toast,
            toast_sev="error" if err_n else ("warning" if skip_n else "information"),
        )

        # per-service report in the activity log
        for svc_name, r in services.items():
            if r.get("skipped"):
                self._log_line(
                    "  ⊘",
                    f"{svc_name} — skipped ({r.get('reason', '?')})",
                    color="yellow",
                    stamp=False,
                )
            elif r.get("ok"):
                hint, _ = self._hint(r.get("data"))
                ms = r.get("ms")
                ms_txt = f" · {ms}ms" if isinstance(ms, (int, float)) else ""
                self._log_line(
                    "  ✓",
                    (
                        f"{svc_name}{ms_txt} · {hint}"
                        if hint != "—"
                        else f"{svc_name}{ms_txt}"
                    ),
                    color="green",
                    stamp=False,
                )
            else:
                self._log_line(
                    "  ✗",
                    f"{svc_name} — {clip(r.get('error'), 100)}",
                    color="red",
                    stamp=False,
                )

        # status hints may have changed — quiet refresh in the background
        self._spawn(
            self._refresh_data(auto=True),
            name="op-refresh-after-tick",
            group="op-refresh",
        )

    # ── global fetch ─────────────────────────────────────────────────────

    def _start_fetch(self) -> None:
        if self.ops.is_active("fetch"):
            elapsed = fmt_dur(self.ops.elapsed("fetch"))
            self.notify(
                f"a fetch is already running ({elapsed} elapsed)",
                title="Fetch",
                severity="warning",
                timeout=4,
                markup=False,
            )
            return
        self.ops.start("fetch", "fetch")
        self._log_line(
            "→", "fetch started — updating scripts from GitHub (10-30s)", color="dim"
        )
        self._render_op_strip()
        self._spawn(self._op_fetch(), name="op-fetch", group="op-fetch")

    async def _op_fetch(self) -> None:
        try:
            resp = await self.api.get("/fetch", timeout=150.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._op_fail("fetch", exc)
            return

        if resp.status_code == 409:
            body = resp.body if isinstance(resp.body, dict) else {}
            err = body.get("error") or clip(resp.text, 120)
            self._op_end(
                "fetch",
                ok=False,
                state="warn",
                detail=f"backend busy — {err}",
                toast=f"backend busy: {err}",
                toast_sev="warning",
            )
            return
        if resp.status_code != 200 or not isinstance(resp.body, dict):
            self._op_end(
                "fetch",
                ok=False,
                state="error",
                detail=f"HTTP {resp.status_code} — {clip(resp.text, 120)}",
                toast=f"HTTP {resp.status_code}",
                toast_sev="error",
            )
            return

        body = resp.body
        fetch_info = body.get("fetch") or {}
        changed = fetch_info.get("changed") or []
        errors = fetch_info.get("errors") or []
        fetched = fetch_info.get("fetched", 0)
        ok = bool(body.get("ok"))

        detail = f"{fetched} files · {len(changed)} changed · {len(errors)} errors"
        if errors:
            state, sev = "error", "error"
            toast = f"{len(errors)} fetch errors"
        elif changed:
            state, sev = "warn", "warning"
            toast = f"{len(changed)} files changed — manager is reloading"
        else:
            state, sev = "ok", "information"
            toast = f"up to date — {fetched} files"
        self._op_end(
            "fetch", ok=ok, state=state, detail=detail, toast=toast, toast_sev=sev
        )

        for f in changed:
            self._log_line("  ±", f"changed: {f}", color="yellow", stamp=False)
        for e in errors:
            self._log_line("  ✗", f"error: {e}", color="red", stamp=False)
        if changed:
            self._log_line(
                "",
                "changed files trigger a manager reload — the backend may be",
                color="dim",
            )
            self._log_line("", "briefly unavailable; reconnecting in ~20s", color="dim")
            self.set_timer(20.0, self._reconnect_after_change)

    def _reconnect_after_change(self) -> None:
        self._log_line("→", "re-checking the backend after the reload", color="dim")
        self._spawn(
            self._refresh_data(manual=True),
            name="op-refresh-reconnect",
            group="op-refresh",
        )

    # ── per-service actions ──────────────────────────────────────────────

    def _start_service_action(
        self, svc: str, action: str, button: Button | None = None
    ) -> None:
        key = f"svc:{svc}:{action}"
        if self.ops.is_active(key):
            elapsed = fmt_dur(self.ops.elapsed(key))
            self.notify(
                f"{action} is already running ({elapsed} elapsed)",
                title=svc,
                severity="warning",
                timeout=4,
                markup=False,
            )
            return
        self.ops.start(key, f"{svc} · {action}")
        self._log_line("→", f"GET /services/{svc}/{action}", color="dim")
        self._render_op_strip()
        if button is not None:
            button.disabled = True
        self._spawn(
            self._op_service_action(svc, action, button),
            name=f"op-svc-{svc}-{action}",
            group=f"svc-{svc}",
        )

    async def _op_service_action(
        self, svc: str, action: str, button: Button | None
    ) -> None:
        key = f"svc:{svc}:{action}"
        output_panel = self.query_one("#detail-output-panel", Vertical)
        status_w = self.query_one("#detail-op-status", Static)
        output = self.query_one("#detail-output", TextArea)
        output_panel.loading = True
        status_w.update(Text(f"GET /services/{svc}/{action} — running…", "dim"))
        try:
            # the t2g service's actions authenticate with the T2G token
            resp = await self.api.get(
                f"/services/{svc}/{action}", use_t2g=(svc == "t2g"), timeout=120.0
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._op_fail(key, exc)
            return
        finally:
            output_panel.loading = False
            if button is not None:
                button.disabled = False

        dur = fmt_dur(self.ops.elapsed(key))
        code = resp.status_code
        body = resp.body

        if isinstance(body, dict):
            body_str = json_dump(body)
        else:
            body_str = clip(resp.text, 100_000)
        output.load_text(body_str)

        ok = 200 <= code < 300
        color = "green" if ok else "red"
        status_w.update(
            Text.assemble(
                (f"GET /services/{svc}/{action} → ", ""),
                (f"{code}", color),
                (f" · {dur}", "dim"),
            )
        )
        brief = self._action_brief(body)
        self._op_end(
            key,
            ok=ok,
            state="ok" if ok else "error",
            detail=f"HTTP {code}{f' · {brief}' if brief else ''}",
            toast=f"HTTP {code}{f' · {brief}' if brief else ''}" if not ok else None,
            toast_sev="error",
            title=svc,
        )

    @staticmethod
    def _action_brief(body: Any) -> str:
        """Short k=v summary of a service action result (result nesting)."""
        if not isinstance(body, dict):
            return ""
        result = body.get("result")
        if isinstance(result, dict):
            parts = [
                f"{k}={v}"
                for k, v in list(result.items())[:4]
                if not isinstance(v, (dict, list))
            ]
            return clip(" · ".join(parts), 60)
        if result is not None:
            return clip(result, 60)
        return clip(
            " · ".join(
                f"{k}={v}"
                for k, v in list(body.items())[:3]
                if not isinstance(v, (dict, list))
            ),
            60,
        )

    # ── routes view ──────────────────────────────────────────────────────

    def _render_route_note(self) -> None:
        note = "send a GET/POST to any manager or service route · enter to send"
        if self.t2g_token:
            note += " · paths under /t2g use the T2G token, others the manager token"
        else:
            note += " · no T2G token configured: /t2g routes may return 401"
        self.query_one("#route-note", Static).update(Text(note, "dim"))

    def _update_route_suggestions(self) -> None:
        suggestions = ["/", "/health", "/tick", "/fetch", "/status", "/services"]
        for s in self._services.values():
            mount = (s.get("api") or {}).get("mount") or ""
            for ep in (s.get("api") or {}).get("endpoints") or {}:
                path = ep.split()[-1] if " " in ep else ep
                if path and path.startswith("/"):
                    suggestions.append(f"{mount}{path}")
        # SuggestFromList keeps its data in _suggestions/_for_comparison and
        # exposes no setter: assigning `.suggestions` only created a dead
        # attribute, so the discovered routes never reached the completion.
        # Rebuild the suggester and hand it to the Input instead.
        self._route_suggester = SuggestFromList(
            sorted(set(suggestions)), case_sensitive=False
        )
        self.query_one("#route-input", Input).suggester = self._route_suggester

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "route-input":
            self._send_route()

    def _send_route(self) -> None:
        input_w = self.query_one("#route-input", Input)
        path = input_w.value.strip()
        if not path:
            input_w.focus()
            return
        method = self.query_one("#route-method", Select).value
        method = "GET" if method is Select.NULL or not method else str(method)
        key = f"route:{method}:{path}"
        if self.ops.is_active(key):
            self.notify(
                "that request is already in flight",
                title="Routes",
                severity="warning",
                timeout=4,
                markup=False,
            )
            return
        self.ops.start(key, f"{method} {clip(path, 30)}")
        self._log_line("→", f"{method} {path}", color="dim")
        self._render_op_strip()
        self._spawn(self._op_route(method, path), name="op-route", group="route")

    async def _op_route(self, method: str, path: str) -> None:
        key = f"route:{method}:{path}"
        status_w = self.query_one("#route-status", Static)
        body_w = self.query_one("#route-body", TextArea)
        panel = self.query_one("#route-output-panel", Vertical)
        panel.loading = True
        try:
            use_t2g = path.startswith("/t2g")
            if method == "POST":
                resp = await self.api.post(path, use_t2g=use_t2g, timeout=90.0)
            else:
                resp = await self.api.get(path, use_t2g=use_t2g, timeout=90.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._op_fail(key, exc)
            status_w.update(Text(f"{method} {path} — request failed", "red"))
            return
        finally:
            panel.loading = False

        dur = fmt_dur(self.ops.elapsed(key))
        code = resp.status_code
        ok = 200 <= code < 300
        color = "green" if ok else ("yellow" if code < 500 else "red")
        status_w.update(
            Text.assemble(
                (f"{method} {path} → ", ""),
                (f"{code}", f"b {color}"),
                (
                    (
                        f" · {dur} · {resp.elapsed_ms:.0f} ms"
                        if resp.elapsed_ms
                        else f" · {dur}"
                    ),
                    "dim",
                ),
            )
        )
        if isinstance(resp.body, (dict, list)):
            body_w.load_text(json_dump(resp.body))
            self._set_text_language(body_w, ".json")
        else:
            body_w.load_text(clip(resp.text, 100_000))
            self._set_text_language(body_w, None)

        self._op_end(
            key, ok=ok, state="ok" if ok else "warn", detail=f"HTTP {code}", toast=None
        )
        self._log_line(
            "←" if ok else "⚠",
            f"{code} · {dur} · {clip(path, 50)}",
            color=color,
            stamp=False,
        )

    # ── sources view ─────────────────────────────────────────────────────

    def _sync_sources_select(self) -> None:
        select = self.query_one("#src-service", Select)
        current = select.value
        options = [
            (Text(s.get("display_name") or n), n) for n, s in self._services.items()
        ]
        select.set_options(options)
        if current is not Select.NULL and current in self._services:
            select.value = current
        elif options:
            select.value = options[0][1]
            self._populate_source_files(str(options[0][1]))
        else:
            select.clear()
            self._populate_source_files(None)

    @on(Select.Changed)
    def _on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "src-service":
            if event.value is Select.NULL or not isinstance(event.value, str):
                return
            if event.value in self._services:
                self._populate_source_files(event.value)

    def _populate_source_files(self, svc_name: str | None) -> None:
        table = self.query_one("#src-files", DataTable)
        table.clear()
        if not svc_name or svc_name not in self._services:
            table.add_row(Text("select a service above", "dim"), key="__empty__")
            return
        svc = self._services[svc_name]
        files = (svc.get("source") or {}).get("files") or []
        if not files:
            table.add_row(Text("no source files declared", "dim"), key="__empty__")
            return
        for f in files:
            table.add_row(Text(f), key=f)

    @on(DataTable.RowSelected)
    def _on_file_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "src-files":
            return
        select = self.query_one("#src-service", Select)
        svc_name = select.value
        if svc_name is Select.NULL or not isinstance(svc_name, str):
            return
        file_dest = event.row_key.value if event.row_key else None
        if svc_name and file_dest and file_dest != "__empty__":
            self._start_source_load(svc_name, file_dest)

    def _start_source_load(self, svc_name: str, file_dest: str) -> None:
        key = f"src:{svc_name}:{file_dest}"
        if self.ops.is_active(key):
            return
        self.ops.start(key, f"src {clip(file_dest, 30)}")
        self._log_line(
            "→", f"fetch source {svc_name}/{file_dest} from GitHub", color="dim"
        )
        self._render_op_strip()
        self._spawn(
            self._op_source(svc_name, file_dest), name="op-source", group="source"
        )

    async def _op_source(self, svc_name: str, file_dest: str) -> None:
        key = f"src:{svc_name}:{file_dest}"
        meta = self.query_one("#src-meta", Static)
        viewer = self.query_one("#src-viewer", TextArea)
        panel = self.query_one("#src-viewer-panel", Vertical)
        svc = self._services.get(svc_name) or {}
        src = svc.get("source") or {}
        repo = src.get("repo") or "?"
        branch = src.get("branch") or "main"
        mode = src.get("mode") or "raw"

        meta.update(
            Text.assemble(
                (f"{clip(svc.get('display_name') or svc_name, 30)} · ", ""),
                (f"{repo} · {branch} · {mode}", "dim"),
            )
        )
        panel.loading = True
        try:
            # try the dest path first, then the known dest→repo_path remaps
            r = await fetch_source_file(repo, branch, file_dest, mode)
            repo_path = file_dest
            if "error" in r:
                repo_path = self._remap_source_path(file_dest)
                if repo_path != file_dest:
                    r = await fetch_source_file(repo, branch, repo_path, mode)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._op_fail(key, exc)
            return
        finally:
            panel.loading = False

        if "error" in r:
            err = r["error"]
            meta.update(
                Text.assemble(
                    (f"{clip(svc.get('display_name') or svc_name, 30)} · ", ""),
                    (f"{repo} · {branch} · {mode}", "dim"),
                    (f"\n{file_dest} — ", ""),
                    (clip(err, 90), "red"),
                )
            )
            viewer.load_text("")
            self._op_end(
                key,
                ok=False,
                state="error",
                detail=clip(err, 60),
                toast=clip(err, 80),
                toast_sev="error",
            )
            return

        content = r.get("content", "")
        size = r.get("bytes", len(content.encode("utf-8", errors="replace")))
        lines = content.splitlines()
        shown = "\n".join(lines[:4000])
        if len(lines) > 4000:
            shown += f"\n… ({len(lines) - 4000} more lines not shown)"
        viewer.load_text(shown)
        self._set_text_language(viewer, Path(repo_path).suffix)
        meta.update(
            Text.assemble(
                (f"{clip(svc.get('display_name') or svc_name, 30)} · ", ""),
                (f"{repo} · {branch} · {mode}", "dim"),
                (f"\n{repo_path} · ", ""),
                (f"{len(lines)} lines · {size:,} bytes", ""),
            )
        )
        self._op_end(
            key,
            ok=True,
            state="ok",
            detail=f"{len(lines)} lines · {size:,} B",
            toast=None,
        )

    @staticmethod
    def _remap_source_path(file_dest: str) -> str:
        """dest path → repo path (t2g/committer/credit layouts differ)."""
        for dest_prefix, repo_prefix in SOURCE_REMAPS:
            if file_dest == dest_prefix:
                return repo_prefix
        parts = file_dest.split("/", 1)
        return parts[1] if len(parts) == 2 else file_dest

    @staticmethod
    def _set_text_language(area: TextArea, ext: str | None) -> None:
        """Best-effort syntax highlighting for the read-only viewers."""
        if ext is None:
            area.language = None
            return
        wanted = {
            ".py": "python",
            ".sh": "bash",
            ".bash": "bash",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".json": "json",
            ".js": "javascript",
            ".html": "html",
            ".htm": "html",
            ".md": "markdown",
            ".toml": "toml",
            ".sql": "sql",
        }.get(ext.lower())
        if not wanted:
            area.language = None
            return
        try:
            area.language = wanted
        except Exception:
            area.language = None

    # ── activity log / toasts / op plumbing ──────────────────────────────

    def _log_line(
        self, glyph: str, text: str, *, color: str = "", stamp: bool = True
    ) -> None:
        log = self.query_one("#activity-log", RichLog)
        if stamp:
            # Deliberately LOCAL wall-clock time: this stamp is only read by
            # the person watching the log, so their own clock is the useful
            # reference. astimezone() attaches the local zone instead of
            # leaving a naive datetime.
            now = datetime.now(UTC).astimezone().strftime("%H:%M:%S")
            line = Text.assemble((f"{now} ", "dim"))
        else:
            line = Text()
        if glyph:
            line.append(f"{glyph} ", color or "")
        line.append(text)
        log.write(line)

    def _op_fail(self, key: str, exc: Exception) -> None:
        run = self.ops.end(key, ok=False, state="error", detail=str(exc))
        if run is None:
            return
        dur = fmt_dur((run.finished or time.monotonic()) - run.started)
        self._log_line(
            "✗", f"{run.label} failed in {dur} — {clip(exc, 120)}", color="red"
        )
        self.notify(
            f"{clip(exc, 160)}",
            title=f"{run.label} failed",
            severity="error",
            timeout=6,
            markup=False,
        )
        self._render_op_strip()

    def _op_end(
        self,
        key: str,
        *,
        ok: bool,
        state: str = "ok",
        detail: str = "",
        log: bool = True,
        toast: str | None = None,
        toast_sev: SeverityLevel = "information",
        title: str | None = None,
    ) -> None:
        run = self.ops.end(key, ok=ok, state=state, detail=detail)
        if run is None:
            return
        dur = fmt_dur(run.finished - run.started) if run.finished else "?"
        if log:
            glyph, color = {
                "ok": ("✓", "green"),
                "warn": ("⚠", "yellow"),
                "error": ("✗", "red"),
            }.get(state, ("·", ""))
            line = f"{run.label} finished in {dur}" + (f" — {detail}" if detail else "")
            self._log_line(glyph, line, color=color)
        if toast:
            self.notify(
                f"{toast} · {dur}",
                title=title or run.label.capitalize(),
                severity=toast_sev,
                timeout=6,
                markup=False,
            )
        self._render_op_strip()

    def _render_op_strip(self) -> None:
        status = self.query_one("#op-status", Static)
        strip = self.query_one("#opstrip", Vertical)
        parts: list[Text] = []
        for run in self.ops.active.values():
            elapsed = time.monotonic() - run.started
            spin = SPINNER[self._spin % len(SPINNER)]
            parts.append(
                Text.assemble(
                    (f"{spin} ", "cyan"), (f"{run.label} ", ""), (fmt_dur(elapsed), "b")
                )
            )
        if parts:
            text = Text()
            for i, part in enumerate(parts):
                if i:
                    text.append("  ·  ", "dim")
                text.append_text(part)
            strip.set_class(True, "busy")
        else:
            text = Text()
            if self.ops.history:
                last = self.ops.history[-1]
                glyph, color = {
                    "ok": ("✓", "green"),
                    "warn": ("⚠", "yellow"),
                    "error": ("✗", "red"),
                }.get(last.state, ("·", ""))
                dur = fmt_dur(last.finished - last.started) if last.finished else "?"
                text.append("idle · last: ", "dim")
                text.append(f"{last.label} ", "dim")
                text.append(f"{glyph} {dur}", color)
            else:
                text.append("no operations yet — t tick · f fetch · r refresh", "dim")
            strip.set_class(False, "busy")
        status.update(text)


def json_dump(body: Any) -> str:
    """Pretty JSON for the read-only viewers, truncated for sanity."""
    try:
        s = json.dumps(body, indent=2, default=str)
    except Exception:
        s = str(body)
    if len(s) > 100_000:
        s = s[:100_000] + "\n… (truncated)"
    return s


# ── entry point ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="render-service-manager TUI")
    parser.add_argument("--url", default=DEFAULT_URL, help="Manager URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="MANAGER_AUTH_TOKEN")
    parser.add_argument("--t2g-token", default=DEFAULT_T2G_TOKEN, help="T2G_AUTH_TOKEN")
    args = parser.parse_args()
    if not args.url.strip():
        parser.error("no manager URL: set MANAGER_URL in .env or pass --url <url>")
    app = ManagerTUI(url=args.url, token=args.token, t2g_token=args.t2g_token)
    app.run()


if __name__ == "__main__":
    main()
