"""Opt-in real Docker check: existing SSE survives reload and restart keeps route.

Run on an isolated Linux Docker validation host with two immutable images:
SNOW_CADDY_INTEGRATION=1 SNOW_TEST_CADDY_IMAGE=<digest reference>
SNOW_TEST_PYTHON_IMAGE=<public API digest reference> pytest -q this_file
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("SNOW_CADDY_INTEGRATION") != "1", reason="explicit isolated Docker validation required")

SERVER = """from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os,time
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
  count=40 if self.path=='/stream' else 1
  for i in range(count):
   self.wfile.write((os.environ['COLOUR']+':'+str(i)+'\\n').encode()); self.wfile.flush()
   if count>1: time.sleep(.1)
ThreadingHTTPServer(('0.0.0.0',8000),Handler).serve_forever()
"""


def test_real_caddy_reload_preserves_sse_and_restart_reads_durable_route(tmp_path):
    images = [os.environ.get(name, "") for name in ("SNOW_TEST_CADDY_IMAGE", "SNOW_TEST_PYTHON_IMAGE")]
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", image) for image in images), "Explicit digest pins required"
    caddy_image, python_image = images
    def docker(*arguments):
        return subprocess.run(["docker", *arguments], check=True, capture_output=True, text=True, timeout=120).stdout.strip()
    network = "snow-routing-test-" + uuid.uuid4().hex
    containers = []
    created_network = False
    route_root = tmp_path / "routing"
    route_root.mkdir()
    upstream = route_root / "upstream.caddy"
    upstream.write_bytes(b"to public-api-blue:8000\n")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_bytes(b"{\n auto_https off\n}\n:8080 {\n reverse_proxy {\n import /etc/project-snow-routing/upstream.caddy\n flush_interval -1\n }\n}\n")
    try:
        docker("network", "create", network)
        created_network = True
        for colour in ("blue", "green"):
            containers.append(docker("run", "--detach", "--network", network, "--network-alias", "public-api-" + colour,
                                     "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
                                     "--env", "COLOUR=" + colour, "--entrypoint", "python", python_image, "-u", "-c", SERVER))
        caddy = docker("run", "--detach", "--network", network, "--publish", "127.0.0.1::8080",
                       "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
                       "--mount", f"type=bind,source={route_root},target=/etc/project-snow-routing,readonly",
                       "--mount", f"type=bind,source={caddyfile},target=/etc/caddy/Caddyfile,readonly", caddy_image)
        containers.append(caddy)
        port = json.loads(docker("inspect", caddy))[0]["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        base = "http://127.0.0.1:" + port
        def response():
            with urllib.request.urlopen(base, timeout=5) as result:
                return result.read().decode()
        def await_colour(colour):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if response().startswith(colour):
                        return
                except Exception:
                    pass
                time.sleep(.2)
            raise AssertionError("Caddy did not serve the expected colour")
        await_colour("blue")
        with urllib.request.urlopen(base + "/stream", timeout=10) as stream:
            assert stream.readline().decode().startswith("blue:")
            replacement = route_root / ".next"
            replacement.write_bytes(b"to public-api-green:8000\n")
            os.replace(replacement, upstream)
            docker("exec", caddy, "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
            await_colour("green")
            remainder = stream.read().decode().splitlines()
            assert len(remainder) == 39 and all(line.startswith("blue:") for line in remainder)
        docker("restart", caddy)
        # Docker may allocate a different ephemeral host port on restart.
        port = json.loads(docker("inspect", caddy))[0]["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        base = "http://127.0.0.1:" + port
        await_colour("green")
        assert json.loads(docker("inspect", caddy))[0]["Id"] == caddy
    except Exception:
        if len(containers) == 3:
            try:
                observed = json.loads(docker("inspect", containers[-1]))[0]
                print(json.dumps({"State": observed.get("State"), "Ports": observed.get("NetworkSettings", {}).get("Ports")}))
                logs = subprocess.run(["docker", "logs", "--tail", "100", containers[-1]], capture_output=True, text=True, timeout=15)
                print(logs.stdout + logs.stderr)
            except Exception:
                print("Caddy diagnostic inspection unavailable")
        raise
    finally:
        # Only exact IDs returned by this test are removed; never use project,
        # image or global prune operations on a shared Docker daemon.
        for identifier in reversed(containers):
            if re.fullmatch("[0-9a-f]{64}", identifier):
                docker("rm", "--force", identifier)
        if created_network:
            docker("network", "rm", network)
