"""Локальный экран подбора. Принимает JSON и вызывает Planner."""
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .model import Dataset, Policy, Request
from .planner import Planner, SearchLimit

ASSETS = Path(__file__).with_name("web_assets")
EXAMPLE = ASSETS.parent.parent / "examples" / "scenario.json"
MAX_REQUEST_BYTES = 2_000_000
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/brand.css": ("brand.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/logo-light.png": ("logo-light.png", "image/png"),
    "/logo-dark.png": ("logo-dark.png", "image/png"),
    "/open-sans.ttf": ("open-sans.ttf", "font/ttf"),
}


class Handler(BaseHTTPRequestHandler):
    def send_content(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; form-action 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        self.send_content(
            status, "application/json; charset=utf-8",
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
        )

    def do_GET(self):
        if self.path == "/api/example":
            self.send_content(HTTPStatus.OK, "application/json; charset=utf-8", EXAMPLE.read_bytes())
        elif self.path in FILES:
            name, content_type = FILES[self.path]
            self.send_content(HTTPStatus.OK, content_type, (ASSETS / name).read_bytes())
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Страница не найдена"})

    def do_POST(self):
        if self.path != "/api/plan":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Действие не найдено"})
            return
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Origin", expected_origin) != expected_origin:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Откройте интерфейс по локальному адресу"})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Ожидается JSON"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST_BYTES:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Размер файла должен быть от 1 байта до 2 МБ"})
                return
            raw = json.loads(self.rfile.read(size))
            data = Dataset.from_dict(raw["data"])
            policy = Policy(**raw.get("policy", {}))
            result = Planner(data, policy).plan(Request(**raw["request"]))
        except SearchLimit as error:
            self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": f"Проверьте данные сценария. {error}"})
        else:
            self.send_json(HTTPStatus.OK, result)

    def log_message(self, format, *args):
        pass


def serve(port=8765):
    with HTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Откройте в браузере http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
