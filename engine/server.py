"""本机回环的 HTTP 接口与 web/ 静态托管（标准库 ThreadingHTTPServer）。"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .index import IndexReader
from .scoring import MAX_K, MAX_WORD_CODEPOINTS, format_score

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class _StatsCache:
    def __init__(self, reader: IndexReader):
        self.reader = reader
        self._size = None
        self._lock = threading.Lock()

    def index_bytes(self) -> int:
        if self._size is None:
            total = 0
            for name in os.listdir(self.reader.dir):
                full = os.path.join(self.reader.dir, name)
                if os.path.isfile(full):
                    total += os.path.getsize(full)
            with self._lock:
                self._size = total
        return self._size

    def payload(self) -> dict:
        manifest = self.reader.manifest
        try:
            import resource

            rss_mb = round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2
            )
        except Exception:
            rss_mb = None
        return {
            "entry_count": self.reader.entry_count,
            "source_sha256": manifest.get("source_sha256"),
            "build_ms": manifest.get("build_ms"),
            "build_rss_mb": manifest.get("build_rss_mb"),
            "index_bytes": self.index_bytes(),
            "query_rss_mb": rss_mb,
        }


def make_handler(reader: IndexReader, web_dir: str):
    stats = _StatsCache(reader)

    class Handler(BaseHTTPRequestHandler):
        server_version = "PrefixPick/1"
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, *_args):
            pass

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: int, message: str):
            self._send_json(status, {"error": message})

        def do_GET(self):
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/api/complete":
                self._handle_complete(parts.query)
            elif path == "/api/stats":
                self._send_json(200, stats.payload())
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self._serve_static(path)

        def _handle_complete(self, query: str):
            params = parse_qs(query, keep_blank_values=True)
            prefix = params.get("prefix", [""])[0]
            k_raw = params.get("k", ["10"])[0]
            if len(prefix) > MAX_WORD_CODEPOINTS or any(
                ch.isspace() for ch in prefix
            ):
                self._send_error_json(
                    400, "prefix 非法：不得含空白字符，码点数不超过 4096"
                )
                return
            if not (k_raw.isascii() and k_raw.isdigit()):
                self._send_error_json(400, "k 必须是 0..1000 的十进制整数")
                return
            k = int(k_raw)
            if not 0 <= k <= MAX_K:
                self._send_error_json(400, "k 超出范围 0..1000")
                return

            import time

            started = time.perf_counter()
            rows = reader.complete(prefix, k)
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            self._send_json(
                200,
                {
                    "k": k,
                    "elapsed_ms": elapsed_ms,
                    "candidates": [
                        {"word": word, "score": format_score(s100)}
                        for word, s100 in rows
                    ],
                },
            )

        def _serve_static(self, path: str):
            if path == "/":
                path = "/index.html"
            rel = os.path.normpath(path.lstrip("/"))
            if os.path.isabs(rel) or rel.startswith(".."):
                self.send_error(403)
                return
            full = os.path.normpath(os.path.join(web_dir, rel))
            if not full.startswith(os.path.abspath(web_dir) + os.sep):
                self.send_error(403)
                return
            if not os.path.isfile(full):
                self.send_error(404)
                return
            ext = os.path.splitext(full)[1].lower()
            try:
                with open(full, "rb") as fh:
                    body = fh.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", _CONTENT_TYPES.get(ext, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(reader: IndexReader, web_dir: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(reader, web_dir))
    httpd.timeout = 10
    return httpd
