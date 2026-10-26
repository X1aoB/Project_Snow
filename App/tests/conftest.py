"""Shared pytest configuration for the App test suite.

The browser tests serve the public frontend from ThreadingHTTPServer fixtures.
socketserver's default listen backlog is 5: a page that requests its modules,
fonts and data in parallel can overflow it when several browser workers run at
once (pytest-xdist). Windows then refuses the connection outright and Linux
drops the SYN and retries after about a second; either way a test fails or
waits for reasons unrelated to the code under test. A deeper backlog is a
test-only setting and changes no product behaviour.
"""
import socketserver

socketserver.TCPServer.request_queue_size = 128
