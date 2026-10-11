"""Run the actual production Caddy routes against a local synthetic backend.

Set CADDY_BIN to an installed Caddy executable. No Docker, ACME, or real API.
"""
import http.server
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


CADDY = os.environ.get("CADDY_BIN") or shutil.which("caddy")
CONFIG = Path(__file__).resolve().parents[2] / "frontend/deploy/Caddyfile"


class Backend(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        content_type = "text/html"
        body = "published site"
        if self.path.endswith("robots.txt"):
            content_type, body = "text/plain", "User-agent: *"
        elif self.path.endswith("sitemap.xml"):
            content_type, body = "application/xml", "<urlset/>"
        elif self.path == "/openapi-public.json":
            content_type, body = "application/json", '{"openapi":"3.1.0"}'
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args):
        pass


@unittest.skipUnless(CADDY, "set CADDY_BIN to run real proxy integration tests")
class PublicRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        (root / "index.html").write_text("frontend app")
        (root / "assets").mkdir()
        (root / "assets/app.js").write_text("frontend asset")
        cls.backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        cls.addClassCleanup(cls.backend.server_close)
        cls.addClassCleanup(cls.backend.shutdown)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        config = CONFIG.read_text().replace("{\n", "{\n\tadmin off\n\tauto_https off\n", 1)
        config = config.replace("backend:8000", f"127.0.0.1:{cls.backend.server_port}").replace("root * /srv", f"root * {root}")
        conf = root / "Caddyfile"
        conf.write_text(config)
        cls.log = (root / "caddy.log").open("w+")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([CADDY, "run", "--config", str(conf), "--adapter", "caddyfile"],
                                       env={**os.environ, "APP_DOMAIN": cls.base, "ACME_EMAIL": "test@example.test"},
                                       stdout=cls.log, stderr=cls.log)
        cls.addClassCleanup(cls.stop)
        for _ in range(100):
            try:
                with urllib.request.urlopen(cls.base + "/livez", timeout=0.2):
                    return
            except (OSError, urllib.error.URLError):
                if cls.process.poll() is not None:
                    cls.log.seek(0)
                    raise RuntimeError(cls.log.read())
                time.sleep(0.05)
        raise RuntimeError("local Caddy did not become ready")

    @classmethod
    def stop(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return response.headers, response.read().decode()

    def test_published_site_and_discovery_paths_reach_backend(self):
        for path, content_type, body in [
            ("/s/demo/", "text/html", "published site"),
            ("/s/demo/robots.txt", "text/plain", "User-agent: *"),
            ("/s/demo/sitemap.xml", "application/xml", "<urlset/>"),
            ("/openapi-public.json", "application/json", '{"openapi":"3.1.0"}'),
        ]:
            with self.subTest(path=path):
                headers, actual = self.get(path)
                self.assertEqual(headers.get_content_type(), content_type)
                self.assertEqual(actual, body)

    def test_frontend_assets_and_src_are_not_captured_by_site_proxy(self):
        self.assertEqual(self.get("/assets/app.js")[1], "frontend asset")
        self.assertEqual(self.get("/src/main.tsx")[1], "frontend app")
        self.assertEqual(self.get("/w/form")[1], "frontend app")
        self.assertIsNone(self.get("/w/form")[0].get("X-Frame-Options"))

    def test_metrics_remains_private(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get("/metrics")
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
