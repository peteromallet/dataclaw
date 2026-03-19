"""Local daemon for the scientist workbench — scanner + HTTP API."""

import json
import logging
import os
import threading
import time
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .anonymizer import Anonymizer
from .badges import compute_all_badges
from .config import CONFIG_DIR, load_config
from .index import (
    add_policy,
    create_bundle,
    get_bundle,
    get_bundles,
    get_dashboard_analytics,
    get_policies,
    get_session_detail,
    get_stats,
    open_index,
    query_sessions,
    remove_policy,
    search_fts,
    update_session,
    upsert_sessions,
)
from .parser import (
    CLAUDE_SOURCE,
    CODEX_SOURCE,
    OPENCLAW_SOURCE,
    discover_projects,
    parse_project_sessions,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8384
SCAN_INTERVAL = 60  # seconds

# Sources supported in the workbench (scientist-facing subset)
WORKBENCH_SOURCES = {CLAUDE_SOURCE, CODEX_SOURCE, OPENCLAW_SOURCE}

# Path to the built frontend dist directory
FRONTEND_DIST = Path(__file__).parent / "web" / "frontend" / "dist"


class Scanner:
    """Periodically scans source directories and indexes new sessions."""

    def __init__(self, source_filter: str | None = None):
        self.source_filter = source_filter
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_scan_mtimes: dict[str, float] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def scan_once(self) -> dict[str, int]:
        """Run a single scan pass. Returns {source: new_session_count}."""
        conn = open_index()
        config = load_config()
        extra_usernames = config.get("redact_usernames", [])
        anonymizer = Anonymizer(extra_usernames=extra_usernames)

        results: dict[str, int] = {}
        projects = discover_projects()

        for project in projects:
            source = project.get("source", "")
            if source not in WORKBENCH_SOURCES:
                continue
            if self.source_filter and source != self.source_filter:
                continue

            try:
                sessions = parse_project_sessions(
                    project["dir_name"],
                    anonymizer=anonymizer,
                    include_thinking=True,
                    source=source,
                )
                if sessions:
                    new_count = upsert_sessions(conn, sessions)
                    results[source] = results.get(source, 0) + new_count
            except Exception:
                logger.exception("Error parsing project %s", project["dir_name"])

        conn.close()
        return results

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                results = self.scan_once()
                total_new = sum(results.values())
                if total_new > 0:
                    logger.info("Indexed %d new sessions: %s", total_new, results)
            except Exception:
                logger.exception("Scanner error")
            self._stop_event.wait(SCAN_INTERVAL)


def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    """Send a JSON response."""
    body = json.dumps(data, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: BaseHTTPRequestHandler) -> dict:
    """Read and parse JSON body from request."""
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw)


def _parse_json_fields(rows: list[dict]) -> None:
    """Parse JSON string fields in session rows into Python objects."""
    for row in rows:
        for field in ("value_badges", "risk_badges", "files_touched", "commands_run"):
            if isinstance(row.get(field), str):
                try:
                    row[field] = json.loads(row[field])
                except (json.JSONDecodeError, ValueError):
                    pass


class WorkbenchHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the workbench API + static files."""

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format, *args)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        # API routes
        if path == "/api/sessions":
            self._handle_list_sessions(params)
        elif path.startswith("/api/sessions/"):
            session_id = path[len("/api/sessions/"):]
            self._handle_get_session(session_id)
        elif path == "/api/search":
            self._handle_search(params)
        elif path == "/api/stats":
            self._handle_stats()
        elif path == "/api/dashboard":
            self._handle_dashboard()
        elif path == "/api/projects":
            self._handle_projects()
        elif path == "/api/bundles":
            self._handle_list_bundles()
        elif path.startswith("/api/bundles/"):
            bundle_id = path[len("/api/bundles/"):]
            self._handle_get_bundle(bundle_id)
        elif path == "/api/policies":
            self._handle_list_policies()
        else:
            self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/sessions/"):
            session_id = path[len("/api/sessions/"):]
            self._handle_update_session(session_id)
        elif path == "/api/bundles":
            self._handle_create_bundle()
        elif path.startswith("/api/bundles/") and path.endswith("/export"):
            bundle_id = path[len("/api/bundles/"):-len("/export")]
            self._handle_export_bundle(bundle_id)
        elif path == "/api/policies":
            self._handle_add_policy()
        elif path == "/api/scan":
            self._handle_trigger_scan()
        else:
            _json_response(self, {"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/policies/"):
            policy_id = path[len("/api/policies/"):]
            self._handle_remove_policy(policy_id)
        else:
            _json_response(self, {"error": "Not found"}, 404)

    # --- API handlers ---

    def _handle_list_sessions(self, params: dict[str, list[str]]) -> None:
        conn = open_index()
        try:
            result = query_sessions(
                conn,
                status=params.get("status", [None])[0],
                source=params.get("source", [None])[0],
                project=params.get("project", [None])[0],
                search_text=params.get("q", [None])[0],
                sort=params.get("sort", ["start_time"])[0],
                order=params.get("order", ["desc"])[0],
                limit=int(params.get("limit", ["50"])[0]),
                offset=int(params.get("offset", ["0"])[0]),
            )
            _parse_json_fields(result)
            _json_response(self, result)
        finally:
            conn.close()

    def _handle_get_session(self, session_id: str) -> None:
        conn = open_index()
        try:
            detail = get_session_detail(conn, session_id)
            if detail is None:
                _json_response(self, {"error": "Session not found"}, 404)
                return
            _parse_json_fields([detail])
            _json_response(self, detail)
        finally:
            conn.close()

    def _handle_update_session(self, session_id: str) -> None:
        body = _read_body(self)
        conn = open_index()
        try:
            ok = update_session(
                conn, session_id,
                status=body.get("status"),
                notes=body.get("notes"),
                reason=body.get("reason"),
                ai_quality_score=body.get("ai_quality_score"),
                ai_score_reason=body.get("ai_score_reason"),
            )
            if ok:
                _json_response(self, {"ok": True})
            else:
                _json_response(self, {"error": "Session not found"}, 404)
        finally:
            conn.close()

    def _handle_search(self, params: dict[str, list[str]]) -> None:
        q = params.get("q", [""])[0]
        if not q:
            _json_response(self, [])
            return
        conn = open_index()
        try:
            results = search_fts(
                conn, q,
                limit=int(params.get("limit", ["50"])[0]),
                offset=int(params.get("offset", ["0"])[0]),
            )
            _parse_json_fields(results)
            _json_response(self, results)
        finally:
            conn.close()

    def _handle_stats(self) -> None:
        conn = open_index()
        try:
            stats = get_stats(conn)
            _json_response(self, stats)
        finally:
            conn.close()

    def _handle_dashboard(self) -> None:
        conn = open_index()
        try:
            data = get_dashboard_analytics(conn)
            _json_response(self, data)
        finally:
            conn.close()

    def _handle_projects(self) -> None:
        conn = open_index()
        try:
            rows = conn.execute(
                "SELECT project, source, COUNT(*) as session_count, "
                "SUM(input_tokens + output_tokens) as total_tokens "
                "FROM sessions GROUP BY project, source ORDER BY project"
            ).fetchall()
            _json_response(self, [dict(r) for r in rows])
        finally:
            conn.close()

    def _handle_list_bundles(self) -> None:
        conn = open_index()
        try:
            bundles = get_bundles(conn)
            _json_response(self, bundles)
        finally:
            conn.close()

    def _handle_get_bundle(self, bundle_id: str) -> None:
        conn = open_index()
        try:
            bundle = get_bundle(conn, bundle_id)
            if bundle is None:
                _json_response(self, {"error": "Bundle not found"}, 404)
                return
            _json_response(self, bundle)
        finally:
            conn.close()

    def _handle_create_bundle(self) -> None:
        body = _read_body(self)
        session_ids = body.get("session_ids", [])
        if not session_ids:
            _json_response(self, {"error": "session_ids required"}, 400)
            return
        conn = open_index()
        try:
            bundle_id = create_bundle(
                conn, session_ids,
                attestation=body.get("attestation"),
                note=body.get("note"),
            )
            _json_response(self, {"bundle_id": bundle_id}, 201)
        finally:
            conn.close()

    def _handle_export_bundle(self, bundle_id: str) -> None:
        conn = open_index()
        try:
            bundle = get_bundle(conn, bundle_id)
            if bundle is None:
                _json_response(self, {"error": "Bundle not found"}, 404)
                return

            # Export bundle to disk
            export_dir = CONFIG_DIR / "bundles" / bundle_id
            export_dir.mkdir(parents=True, exist_ok=True)

            sessions_file = export_dir / "sessions.jsonl"
            manifest = {
                "bundle_id": bundle_id,
                "session_count": bundle.get("session_count", 0),
                "attestation": bundle.get("attestation"),
                "submission_note": bundle.get("submission_note"),
                "sessions": [],
            }

            with open(sessions_file, "w") as f:
                for s in bundle.get("sessions", []):
                    detail = get_session_detail(conn, s["session_id"])
                    if detail:
                        f.write(json.dumps(detail, default=str) + "\n")
                        manifest["sessions"].append({
                            "session_id": s["session_id"],
                            "project": s.get("project"),
                            "source": s.get("source"),
                            "model": s.get("model"),
                        })

            with open(export_dir / "manifest.json", "w") as f:
                json.dump(manifest, f, indent=2, default=str)

            # Update bundle status
            conn.execute(
                "UPDATE bundles SET status = 'exported', manifest = ? WHERE bundle_id = ?",
                (json.dumps(manifest, default=str), bundle_id),
            )
            conn.commit()

            _json_response(self, {
                "ok": True,
                "export_path": str(export_dir),
                "session_count": len(manifest["sessions"]),
            })
        finally:
            conn.close()

    def _handle_list_policies(self) -> None:
        conn = open_index()
        try:
            policies = get_policies(conn)
            _json_response(self, policies)
        finally:
            conn.close()

    def _handle_add_policy(self) -> None:
        body = _read_body(self)
        policy_type = body.get("policy_type")
        value = body.get("value")
        if not policy_type or not value:
            _json_response(self, {"error": "policy_type and value required"}, 400)
            return
        conn = open_index()
        try:
            policy_id = add_policy(conn, policy_type, value, reason=body.get("reason"))
            _json_response(self, {"policy_id": policy_id}, 201)
        finally:
            conn.close()

    def _handle_remove_policy(self, policy_id: str) -> None:
        conn = open_index()
        try:
            ok = remove_policy(conn, policy_id)
            if ok:
                _json_response(self, {"ok": True})
            else:
                _json_response(self, {"error": "Policy not found"}, 404)
        finally:
            conn.close()

    def _handle_trigger_scan(self) -> None:
        """Trigger an immediate scan (used by the UI refresh button)."""
        scanner = getattr(self.server, "_scanner", None)
        if scanner:
            results = scanner.scan_once()
            _json_response(self, {"ok": True, "new_sessions": results})
        else:
            _json_response(self, {"error": "Scanner not available"}, 503)

    # --- Static file serving ---

    def _serve_static(self, path: str) -> None:
        """Serve frontend static files, falling back to index.html for SPA routing."""
        if path == "/" or path == "":
            path = "/index.html"

        file_path = FRONTEND_DIST / path.lstrip("/")

        # SPA fallback: if file doesn't exist, serve index.html
        if not file_path.exists() or not file_path.is_file():
            file_path = FRONTEND_DIST / "index.html"

        if not file_path.exists():
            # No frontend built yet — serve a placeholder
            self._serve_placeholder()
            return

        content_types = {
            ".html": "text/html",
            ".js": "application/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".map": "application/json",
        }
        ext = file_path.suffix.lower()
        content_type = content_types.get(ext, "application/octet-stream")

        try:
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.send_error(404)

    def _serve_placeholder(self) -> None:
        """Serve a minimal HTML page when the frontend isn't built yet."""
        html = """<!DOCTYPE html>
<html>
<head><title>DataClaw Workbench</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #333; }
h1 { font-size: 1.4em; }
code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px; }
pre { background: #f0f0f0; padding: 12px; border-radius: 6px; overflow-x: auto; }
.api-link { color: #0066cc; }
</style>
</head>
<body>
<h1>DataClaw Workbench</h1>
<p>The API is running. The frontend hasn't been built yet.</p>
<p>To build the frontend:</p>
<pre>cd dataclaw/web/frontend
npm install
npm run build</pre>
<p>API endpoints available:</p>
<ul>
<li><a class="api-link" href="/api/stats">/api/stats</a> — Index statistics</li>
<li><a class="api-link" href="/api/sessions">/api/sessions</a> — Session list</li>
<li><a class="api-link" href="/api/projects">/api/projects</a> — Projects</li>
<li><a class="api-link" href="/api/bundles">/api/bundles</a> — Bundles</li>
<li><a class="api-link" href="/api/policies">/api/policies</a> — Policies</li>
</ul>
</body>
</html>"""
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_server(
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    source_filter: str | None = None,
) -> None:
    """Start the workbench daemon — scanner + HTTP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    scanner = Scanner(source_filter=source_filter)

    # Start HTTP server first so it's responsive immediately
    server = ThreadingHTTPServer(("127.0.0.1", port), WorkbenchHandler)
    server._scanner = scanner  # type: ignore[attr-defined]

    url = f"http://localhost:{port}"
    logger.info("Workbench running at %s", url)

    if open_browser:
        webbrowser.open(url)

    # Run initial scan in background, then start periodic scanner
    def _initial_scan() -> None:
        logger.info("Running initial scan...")
        results = scanner.scan_once()
        total = sum(results.values())
        logger.info("Initial scan complete: %d sessions indexed", total)
        scanner.start()
        logger.info("Background scanner started (interval: %ds)", SCAN_INTERVAL)

    threading.Thread(target=_initial_scan, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scanner.stop()
        server.shutdown()
