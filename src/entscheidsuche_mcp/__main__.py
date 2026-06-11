"""CLI-Einstiegspunkt für den entscheidsuche-MCP-Server.

Beispiele:

    # Streamable HTTP (Default), Listen auf 127.0.0.1:8765/mcp
    python -m entscheidsuche_mcp

    # Anderes Interface/Port
    python -m entscheidsuche_mcp --host 0.0.0.0 --port 9000

    # Stdio (für lokale CLI-Clients ohne HTTP)
    python -m entscheidsuche_mcp --transport stdio
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn
from starlette.middleware.cors import CORSMiddleware

from .access_log import configure_file_handler
from .server import build_server, create_app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="entscheidsuche-mcp",
        description="MCP-Server für entscheidsuche.ch (Streamable HTTP / stdio).",
    )
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "sse", "stdio"),
        default=os.environ.get("MCP_TRANSPORT", "streamable-http"),
        help="Transport-Protokoll (Default: streamable-http).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Listen-Host (Default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8765")),
        help="Listen-Port (Default: 8765).",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("MCP_PATH", "/mcp"),
        help="HTTP-Pfad-Präfix (Default: /mcp).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Loglevel (Default: INFO).",
    )
    return parser.parse_args(argv)


def _parse_cors_origins() -> list[str]:
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "*").strip()
    if not raw:
        return []
    if raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # force=True ueberschreibt evtl. schon installierte Handler (Default-Verhalten
    # von logging.basicConfig: no-op wenn Root bereits Handler hat).
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        force=True,
    )
    logging.getLogger("entscheidsuche_mcp").setLevel(logging.INFO)
    # Optionaler dedizierter Access-Log-File-Handler (ESC_ACCESS_LOG_FILE).
    configure_file_handler()

    server = build_server()
    cors_origins = _parse_cors_origins()

    if args.transport == "stdio":
        # FastMCP.run() ist synchron und startet den passenden Loop intern.
        server.run(transport="stdio")
        return 0

    # HTTP-Modi: Settings am Server konfigurieren und mit optionalem CORS starten.
    # Achtung: `create_app(server)` muss die *gleiche* Instanz erhalten, damit die
    # hier gesetzten Settings auch in der gemounteten ASGI-App wirken.
    server.settings.host = args.host
    server.settings.port = args.port
    if args.transport == "streamable-http":
        server.settings.streamable_http_path = args.path
        server.settings.stateless_http = _parse_bool_env("MCP_STATELESS_HTTP", True)
        # JSON-Response-Modus: POST /mcp wird mit einer einzelnen JSON-Antwort
        # statt mit einem SSE-Stream beantwortet. Damit akzeptiert FastMCP auch
        # Clients, die nur "application/json" im Accept-Header senden — die
        # Streamable-HTTP-Spec verlangt sonst zwingend zusätzlich
        # "text/event-stream", was viele MCP-Verzeichnisse und kleinere
        # MCP-Clients (python-requests/2.33.1, relay-registry, mcp-spider,
        # Chiark, agent-tools.cloud-crawler, AgentDiscoveryIndex …) nicht
        # senden — Folge waren 406er-Antworten und Nicht-Erkennung.
        server.settings.json_response = _parse_bool_env("MCP_JSON_RESPONSE", True)
        app = create_app(server)
    else:  # sse
        server.settings.sse_path = args.path
        server.settings.message_path = args.path.rstrip("/") + "/messages/"
        app = server.sse_app()

    if cors_origins:
        app = CORSMiddleware(
            app,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["Mcp-Session-Id"],
        )

    # log_config=None: uvicorn benutzt unser via basicConfig konfiguriertes
    # Logging unveraendert, statt seine Default-dictConfig ueber unsere Logger
    # zu legen.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
