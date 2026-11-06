import http.server
import socket
import threading

from lab.healthcheck import healthy


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_healthy_when_any_url_answers():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        dead = f"http://127.0.0.1:{_free_port()}/api/v1/ping"
        live = f"http://127.0.0.1:{server.server_port}/healthz"
        assert healthy((dead, live), timeout=2)
    finally:
        server.shutdown()


def test_unhealthy_when_nothing_answers():
    assert not healthy((f"http://127.0.0.1:{_free_port()}/healthz",), timeout=2)
