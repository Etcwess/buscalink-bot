"""Servidor HTTP mínimo de health check para hospedagem Web Service.

Alguns provedores (ex.: Render Free) só permitem Web Services, não Workers.
Rodamos esse servidor em uma thread daemon para que o processo seja considerado
"web" e permaneça ativo, enquanto o bot faz polling normalmente.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        # silencia logs do servidor para não poluir o log do bot
        return


def start_health_server(host: str = "0.0.0.0", port: int = 10000) -> None:
    """Inicia o health server em thread daemon."""
    server = HTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
