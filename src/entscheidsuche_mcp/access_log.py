"""JSON-RPC Access-Log-Middleware fuer den entscheidsuche-MCP-Server.

Schreibt eine Zeile pro JSON-RPC-Call in den Logger
``entscheidsuche_mcp.access`` — genug Information, um Nutzungsmuster
auszuwerten, ohne den Body komplett zu speichern. Wird vor die ASGI-App
von FastMCP gehaengt; greift den Request-Body ab, parst ihn als JSON-RPC
(Single oder Batch) und schreibt die identifizierte Methode + Tool-Name +
(gekuerzte) Argumente sowie Status/Bytes/Latenz.

Konfiguration via Env-Variablen:

* ``ESC_ACCESS_LOG`` — ``true`` (default) aktiviert die Middleware
* ``ESC_ACCESS_LOG_ARGS_MAX`` — max. Zeichen pro args-Repr (Default 200)
* ``ESC_ACCESS_LOG_FILE`` — optional separater Pfad; ohne Angabe Default
  ``/var/log/entscheidsuche-mcp/access.log`` (mit Fallback auf
  ``~/entscheidsuche-mcp.access.log``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Iterable, Optional

log = logging.getLogger("entscheidsuche_mcp.access")

_SAFE_FIELD_RE = re.compile(r"[^A-Za-z0-9._\-+/]")


def _safe_field(value: Any, max_len: int = 40) -> str:
    if value is None:
        return "-"
    s = _SAFE_FIELD_RE.sub("", str(value))[:max_len]
    return s or "-"


def _args_max() -> int:
    try:
        return max(0, int(os.environ.get("ESC_ACCESS_LOG_ARGS_MAX", "200")))
    except ValueError:
        return 200


def _summarize_args(args: Any) -> str:
    if args is None:
        return ""
    try:
        s = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""
    limit = _args_max()
    if limit and len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _describe_rpc(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    items: Iterable[Any] = payload if isinstance(payload, list) else [payload]
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        method = it.get("method")
        rpc_id = it.get("id")
        tool: Optional[str] = None
        args: Any = None
        params = it.get("params")
        if isinstance(params, dict):
            if method == "tools/call":
                tool = params.get("name")
                args = params.get("arguments")
            elif method == "resources/read":
                args = {"uri": params.get("uri")}
            elif method == "prompts/get":
                args = {"name": params.get("name")}
            elif method == "initialize":
                ci = params.get("clientInfo") or {}
                args = {
                    "client": ci.get("name"),
                    "version": ci.get("version"),
                    "protocol": params.get("protocolVersion"),
                }
        out.append({"method": method, "tool": tool, "id": rpc_id, "args": args})
    return out


class JsonRpcAccessLogMiddleware:
    """ASGI-Middleware, die POST-Requests auf den MCP-Pfad aufschluesselt.

    Eine Logzeile pro RPC-Call. Bei Batches wird jede Methode separat
    geloggt; Status/Size/Dauer sind dann fuer alle gleich.
    """

    def __init__(self, app, mcp_path: str = "/mcp") -> None:
        self.app = app
        self.mcp_path = mcp_path

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != self.mcp_path
        ):
            await self.app(scope, receive, send)
            return

        body = b""
        msgs: list[dict[str, Any]] = []
        while True:
            msg = await receive()
            msgs.append(msg)
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"") or b""
            if not msg.get("more_body", False):
                break

        rpcs: list[dict[str, Any]] = []
        if body:
            try:
                rpcs = _describe_rpc(json.loads(body))
            except Exception:
                rpcs = []

        msgs_iter = iter(msgs)

        async def replay():
            try:
                return next(msgs_iter)
            except StopIteration:
                return await receive()

        status_code: Optional[int] = None
        resp_size = 0

        async def send_wrapper(msg):
            nonlocal status_code, resp_size
            t = msg.get("type")
            if t == "http.response.start":
                status_code = msg.get("status")
            elif t == "http.response.body":
                resp_size += len(msg.get("body", b"") or b"")
            await send(msg)

        t0 = time.monotonic()
        try:
            await self.app(scope, replay, send_wrapper)
        finally:
            dur_ms = int((time.monotonic() - t0) * 1000)
            client = (scope.get("client") or (None, None))[0] or "-"
            if not rpcs:
                log.info(
                    "method=- tool=- id=- status=%s size=%d ms=%d client=%s "
                    "app=- appver=- args=",
                    status_code, resp_size, dur_ms, client,
                )
                return
            for r in rpcs:
                app = "-"
                appver = "-"
                if r["method"] == "initialize" and isinstance(r["args"], dict):
                    app = _safe_field(r["args"].get("client"))
                    appver = _safe_field(r["args"].get("version"))
                log.info(
                    "method=%s tool=%s id=%s status=%s size=%d ms=%d "
                    "client=%s app=%s appver=%s args=%s",
                    r["method"] or "-",
                    r["tool"] or "-",
                    "-" if r["id"] is None else r["id"],
                    status_code,
                    resp_size,
                    dur_ms,
                    client,
                    app,
                    appver,
                    _summarize_args(r["args"]),
                )


def _enabled() -> bool:
    val = os.environ.get("ESC_ACCESS_LOG")
    if val is None:
        return True
    return val.strip().lower() not in {"0", "false", "no", "off"}


_SYSTEM_DEFAULT_PATH = "/var/log/entscheidsuche-mcp/access.log"
_USER_DEFAULT_PATH = os.path.join(os.path.expanduser("~"),
                                  "entscheidsuche-mcp.access.log")


def _default_log_path() -> str:
    """Bevorzugt /var/log/entscheidsuche-mcp/access.log (Debian/systemd),
    faellt auf ~/entscheidsuche-mcp.access.log zurueck, wenn der Systempfad
    nicht beschreibbar ist."""
    sys_dir = os.path.dirname(_SYSTEM_DEFAULT_PATH)
    if os.path.isdir(sys_dir) and os.access(sys_dir, os.W_OK):
        return _SYSTEM_DEFAULT_PATH
    return _USER_DEFAULT_PATH


def configure_file_handler(level: int = logging.INFO) -> None:
    """Optionalen FileHandler einrichten, wenn ``ESC_ACCESS_LOG_FILE`` gesetzt
    ist. Sonst greift ``_ensure_logger_has_handler`` mit dem Default-Pfad."""
    path = os.environ.get("ESC_ACCESS_LOG_FILE")
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s — %(message)s"))
    log.addHandler(handler)
    log.propagate = False
    log.setLevel(level)


def _ensure_logger_has_handler() -> None:
    """Eigene Datei mit eigenem Formatter — unabhaengig von basicConfig /
    uvicorn-Logging-Setup. ``propagate=False`` verhindert doppeltes Loggen
    im uvicorn-Default-Format."""
    if log.handlers:
        return
    path = os.environ.get("ESC_ACCESS_LOG_FILE") or _default_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s — %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def wrap_if_enabled(app, mcp_path: str = "/mcp"):
    """Wickelt ``app`` in die Middleware, sofern via Env aktiv (Default an)."""
    if not _enabled():
        return app
    _ensure_logger_has_handler()
    return JsonRpcAccessLogMiddleware(app, mcp_path=mcp_path)
