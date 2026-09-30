"""Socket glue: `http.server` in front of `InspectorApp`.

Stdlib only, deliberately. Nothing else is installed on this machine and the
inspector must keep working after any future environment rebuild, so there is
no framework here - just a handler that hands the request to the app and writes
back whatever it returns.

The default bind address is 127.0.0.1 and that is not a detail: this is a
shared multi-user server and `/api/overlay` writes files. Reaching it from a
laptop is an SSH tunnel, not a network listener.
"""

from __future__ import annotations

import shutil
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .app import InspectorApp, parse_target

#: Refuse to buffer an unbounded request body. Overlay documents are kilobytes.
MAX_BODY_BYTES = 8 << 20

TUNNEL_HINT = (
    "From a laptop:  ssh -L {port}:localhost:{port} <server>\n"
    "then open       http://localhost:{port}"
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "deskshot-inspector"
    #: The stdlib default writes headers and body as separate unbuffered
    #: `send`s with Nagle enabled, which over an SSH tunnel costs an extra
    #: round trip per response. A labelling session makes five or six requests
    #: per item, so that is five or six avoidable RTTs on every keystroke.
    #: Buffering the write and turning Nagle off sends each response in one go.
    wbufsize = 64 * 1024
    disable_nagle_algorithm = True

    @property
    def app(self) -> InspectorApp:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        path, query = parse_target(self.path)
        body = b""
        length = self.headers.get("Content-Length")
        if length:
            try:
                size = int(length)
            except ValueError:
                size = 0
            if size > MAX_BODY_BYTES:
                self.send_error(413, "request body too large")
                return
            body = self.rfile.read(max(0, size))

        response = self.app.handle(
            "GET" if method == "HEAD" else method,
            path,
            query,
            body,
            dict(self.headers.items()),
        )

        payload = response.body if response.path is None else b""
        length = response.headers.get("Content-Length")
        if length is None:
            length = str(len(payload) if response.path is None else Path(response.path).stat().st_size)

        self.send_response(response.status)
        for key, value in response.headers.items():
            if key != "Content-Length":
                self.send_header(key, value)
        self.send_header("Content-Length", length)
        self.end_headers()
        if method == "HEAD" or response.status == 304:
            self._flush()
            return
        try:
            if response.path is not None:
                with open(str(response.path), "rb") as handle:
                    shutil.copyfileobj(handle, self.wfile, 64 * 1024)
            else:
                self.wfile.write(payload)
            self._flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser navigated away mid-image; not worth a traceback

    def _flush(self):
        """`wbufsize` makes wfile buffered, so it has to be flushed by hand."""
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class InspectorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: InspectorApp, verbose: bool = False):
        socketserver.TCPServer.__init__(self, address, _Handler)
        self.app = app
        self.verbose = verbose


def build_server(
    root: Path,
    golden_root: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    author: str = "unknown",
    cache_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
    read_only: bool = False,
    verbose: bool = False,
    corpus_root: Optional[Path] = None,
    audit_root: Optional[Path] = None,
    rater: Optional[str] = None,
    audit_read_only: bool = False,
    cache_max_bytes: int = 4 << 30,
) -> InspectorServer:
    app = InspectorApp(
        root=root,
        golden_root=golden_root,
        author=author,
        cache_dir=cache_dir,
        cache_max_bytes=cache_max_bytes,
        project_root=project_root,
        read_only=read_only,
        corpus_root=corpus_root,
        audit_root=audit_root,
        rater=rater,
        audit_read_only=audit_read_only,
    )
    return InspectorServer((host, port), app, verbose=verbose)


def serve(server: InspectorServer) -> None:
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
