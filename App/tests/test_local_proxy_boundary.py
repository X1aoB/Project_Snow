import http.client
import http.server
import socket
from threading import Thread

import pytest

from scripts.dev_server import WorkspaceHandler


@pytest.fixture
def proxy():
    class Handler(WorkspaceHandler):
        maximum_body = 64
        body_timeout = 0.15

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_proxy_rejects_rebound_host_before_reading_private_api(proxy):
    connection = http.client.HTTPConnection(*proxy)
    connection.request("GET", "/api/v1/bootstrap", headers={"Host": "attacker.example"})
    assert connection.getresponse().status == 403
    connection.close()


@pytest.mark.parametrize(
    "header,status",
    [
        ("Content-Length: 65", 413),
        ("Content-Length: -1", 400),
        ("Content-Length: abc", 400),
        ("Content-Length: 2\r\nContent-Length: 2", 400),
        ("Transfer-Encoding: chunked", 400),
        ("Content-Length: 30", 408),
    ],
)
def test_proxy_bounds_incomplete_and_ambiguous_uploads(proxy, header, status):
    with socket.create_connection(proxy, timeout=2) as connection:
        connection.sendall(f"POST /api/v1/upload HTTP/1.0\r\nHost: localhost\r\n{header}\r\n\r\n".encode())
        response = connection.recv(2048)
        assert response.startswith(f"HTTP/1.0 {status} ".encode())
