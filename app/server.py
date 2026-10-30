"""Локальный веб-сервер (только стандартная библиотека).

    python -m app.server            # http://127.0.0.1:8000
    python -m app.server --port 8080

API:
    POST /api/analyze   JSON {"check_date": "2026-03-10", "files": [{"name": "...", "content": "..."}]}
    GET  /api/sample?check_date=2026-03-10   — анализ файлов из папки documents/ (для демонстрации;
                                               проходит тот же конвейер, что и загрузка через интерфейс)
"""
from __future__ import annotations

import argparse
import json
import mimetypes
from datetime import date
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .extractor import read_text_file
from .resolver import analyze_files

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DOCUMENTS = ROOT / "documents"
MAX_BODY = 20 * 1024 * 1024


def _parse_date(s: str | None) -> date:
    try:
        return date.fromisoformat(s) if s else date(2026, 3, 10)
    except ValueError:
        return date(2026, 3, 10)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, fmt, *args):  # компактный лог
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.path = "/index.html"
            return super().do_GET()
        if url.path == "/api/sample":
            q = parse_qs(url.query)
            check_date = _parse_date(q.get("check_date", [None])[0])
            files = sorted(DOCUMENTS.glob("*.txt")) if DOCUMENTS.is_dir() else []
            if not files:
                return self._json(404, {"error": "папка documents пуста или отсутствует"})
            result = analyze_files([(f.name, read_text_file(f)) for f in files], check_date)
            return self._json(200, result)
        return super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/analyze":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            return self._json(400, {"error": "пустое или слишком большое тело запроса"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            files = [(str(f["name"]), str(f["content"])) for f in payload.get("files", [])]
            if not files:
                return self._json(400, {"error": "не переданы файлы"})
            result = analyze_files(files, _parse_date(payload.get("check_date")))
            return self._json(200, result)
        except Exception as e:  # noqa: BLE001 — прототип: отдаём текст ошибки клиенту
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    mimetypes.add_type("application/javascript", ".js")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Semantic TextNavigator: http://{args.host}:{args.port}  (Ctrl+C для остановки)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
