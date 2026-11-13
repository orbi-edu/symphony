"""The HTTP(S) front of the GitHub mock: REST (also under GHES's `/api/v3`), GraphQL (`/graphql`,
`/api/graphql`), git smart HTTP (`/<owner>/<repo>.git/...` through `git http-backend`), signed
job-log downloads and the `/_mock/*` control API, on one plain port and optionally one TLS port."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from . import control, gitrepo, graphql_api, rest, spec, tls
from .core import MockGitHub

GIT_PATH = re.compile(r"^/(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/(?P<rest>info/refs|git-upload-pack|git-receive-pack)$")
JOB_LOG = re.compile(r"^/_mock/job-logs/(?P<owner>[^/]+)/(?P<name>[^/]+)/(?P<job>\d+)$")
GRAPHQL_DOCS = "https://docs.github.com/graphql"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, owner: MockServer, scheme: str) -> None:
        super().__init__(addr, handler)
        self.owner = owner
        self.scheme = scheme


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GitHub.com"
    sys_version = ""

    def log_message(self, fmt, *args) -> None:  # noqa: D401 — quiet unless --verbose
        if self.server.owner.verbose:
            super().log_message(fmt, *args)

    @property
    def mock(self) -> MockGitHub:
        return self.server.owner.mock

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes | str | dict | list | None, headers: dict | None = None,
              content_type: str = "application/json; charset=utf-8") -> None:
        headers = dict(headers or {})
        if isinstance(body, (dict, list)):
            data = json.dumps(body, separators=(",", ":")).encode()
        elif isinstance(body, str):
            data = body.encode()
            content_type = headers.pop("Content-Type", "text/plain; charset=utf-8")
        elif body is None:
            data = b""
        else:
            data = body
            content_type = headers.pop("Content-Type", "application/octet-stream")
        headers.setdefault("Content-Type", content_type)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("X-GitHub-Request-Id", f"MOCK:{id(self):x}")
        self.send_header("X-GitHub-Media-Type", "github.v3; format=json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _base(self) -> str:
        host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"{self.server.scheme}://{host}"

    def _record(self, status: int) -> None:
        self.mock.requests.append({"at": time.time(), "method": self.command, "path": self.path, "status": status})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def _handle(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        query = dict(parse_qsl(split.query, keep_blank_values=True))
        headers = {k.lower(): v for k, v in self.headers.items()}
        raw = self._body()
        try:
            if path.startswith("/_mock/"):
                m = JOB_LOG.match(path)
                if m and self.command == "GET":
                    return self._job_log(m, query)
                status, body = control.handle(self.server.owner, self.command, path, query, raw)
                self._record(status)
                return self._send(status, body)
            g = GIT_PATH.match(path)
            if g:
                return self._git(g, split.query, headers, raw)
            if path in ("/graphql", "/api/graphql") and self.command == "POST":
                return self._graphql(headers, raw)
            base = self._base()
            api = base
            if path.startswith("/api/v3"):
                path = path[len("/api/v3"):] or "/"
                api = base + "/api/v3"
            render = self.mock.render(base, api)
            resp = rest.dispatch(self.mock, self.command if self.command != "HEAD" else "GET", path, query, headers,
                                 raw, render)
            self._record(resp.status)
            self._send(resp.status, resp.body, resp.headers)
        except Exception as exc:  # noqa: BLE001 — a mock bug must surface as a 500, not a hang
            import traceback

            traceback.print_exc()
            self._record(500)
            self._send(500, {"message": f"mock error: {type(exc).__name__}: {exc}", "documentation_url": spec.DOCS})

    # -- GraphQL ----------------------------------------------------------------------------
    def _graphql(self, headers: dict, raw: bytes) -> None:
        auth = headers.get("authorization", "")
        token = rest._bearer(auth)
        if not auth:
            self._record(401)
            return self._send(401, {"message": "This endpoint requires you to be authenticated.",
                                    "documentation_url": GRAPHQL_DOCS})
        actor = self.mock.store.actor(token) if token else None
        if actor is None:
            self._record(401)
            return self._send(401, {"message": "Bad credentials", "documentation_url": GRAPHQL_DOCS, "status": "401"})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._record(400)
            return self._send(400, {"message": "Problems parsing JSON", "documentation_url": GRAPHQL_DOCS})
        result = graphql_api.execute(self.mock, actor, body, self._base())
        self._record(200)
        self._send(200, result)

    # -- git smart HTTP ------------------------------------------------------------------------
    def _git(self, g: re.Match, query: str, headers: dict, raw: bytes) -> None:
        m = self.mock
        service = g["rest"] if g["rest"] != "info/refs" else dict(parse_qsl(query)).get("service", "")
        if service not in ("git-upload-pack", "git-receive-pack"):
            self._record(403)
            return self._send(403, "Dumb HTTP is not supported.\n", {"Content-Type": "text/plain"})
        auth = headers.get("authorization", "")
        actor = None
        if auth.lower().startswith("basic "):
            try:
                _, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            except Exception:  # noqa: BLE001
                password = ""
            actor = m.store.actor(password) if password else None
        elif auth:
            actor = m.store.actor(rest._bearer(auth))
        if actor is None:
            self._record(401)
            return self._send(401, "Invalid username or token. Password authentication is not supported for Git "
                                   "operations.\n", {"WWW-Authenticate": 'Basic realm="GitHub"',
                                                     "Content-Type": "text/plain"})
        repo = m.find_repo(g["owner"], g["name"])
        if repo is None or not m.store.can_see(actor, repo["full_name"]):
            self._record(404)
            return self._send(404, "Repository not found.\n", {"Content-Type": "text/plain"})
        need = 2 if service == "git-receive-pack" else 1
        if actor.level("contents") < need:
            self._record(403)
            message = ("Write access to repository not granted.\n" if need == 2
                       else "Read access to repository not granted.\n")
            return self._send(403, message, {"Content-Type": "text/plain"})
        path = m.path(repo)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
            "GIT_PROJECT_ROOT": m.store.git_root, "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": f"/{repo['owner']}/{repo['name']}.git/{g['rest']}", "REQUEST_METHOD": self.command,
            "QUERY_STRING": query, "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(raw)), "REMOTE_USER": actor.login, "REMOTE_ADDR": self.client_address[0],
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        if headers.get("git-protocol"):
            env["GIT_PROTOCOL"] = headers["git-protocol"]
        if headers.get("content-encoding"):
            env["HTTP_CONTENT_ENCODING"] = headers["content-encoding"]
        pushing = g["rest"] == "git-receive-pack"
        lock = self.server.owner.push_lock(repo["full_name"]) if pushing else None
        if lock:
            lock.acquire()
        try:
            before = gitrepo.refs(path) if pushing else {}
            proc = subprocess.run(["git", "http-backend"], input=raw, capture_output=True, env=env)
            after = gitrepo.refs(path) if pushing else {}
        finally:
            if lock:
                lock.release()
        out = proc.stdout
        sep = out.find(b"\r\n\r\n")
        cut = 4
        if sep < 0:
            sep, cut = out.find(b"\n\n"), 2
        head, body = (out[:sep], out[sep + cut:]) if sep >= 0 else (b"", out)
        status, resp_headers = 200, {}
        for line in head.decode(errors="replace").splitlines():
            k, _, v = line.partition(":")
            if k.lower() == "status":
                status = int(v.strip().split()[0])
            elif k:
                resp_headers[k.strip()] = v.strip()
        ctype = resp_headers.pop("Content-Type", "application/octet-stream")
        self._record(status)
        self._send(status, body, {**resp_headers, "Content-Type": ctype}, ctype)
        if pushing:
            updates = [(ref, before.get(ref, gitrepo.ZERO), after.get(ref, gitrepo.ZERO))
                       for ref in sorted(set(before) | set(after)) if before.get(ref) != after.get(ref)]
            if updates:
                m.after_push(repo, updates, actor.login)

    # -- Actions job logs (the signed URL `GET /actions/jobs/{id}/logs` redirects to) ----------
    def _job_log(self, g: re.Match, query: dict) -> None:
        m = self.mock
        repo = m.find_repo(g["owner"], g["name"])
        job = repo["jobs"].get(g["job"]) if repo else None
        try:
            expires = int(query.get("expires", "0"))
        except ValueError:
            expires = 0
        good = job is not None and expires >= time.time() and \
            rest.log_signature(m, repo["full_name"], job["id"], expires) == query.get("sig")
        if not good:
            self._record(403)
            return self._send(403, "<Error><Code>AuthenticationFailed</Code></Error>", {"Content-Type": "application/xml"})
        self._record(200)
        self._send(200, job.get("log") or "", {"Content-Type": "text/plain; charset=utf-8"})


class MockServer:
    """The mock, bound and serving. `port=0`/`https_port=0` pick free ports (tests)."""

    def __init__(self, state_dir: str, *, host: str = "127.0.0.1", port: int = 4200, https_port: int | None = None,
                 public_url: str | None = None, webhook_url: str | None = None, webhook_secret: str | None = None,
                 app_id: int = 1, app_slug: str = "symphony", tls_hosts: list[str] | None = None,
                 check_responses: bool = True, verbose: bool = False) -> None:
        self.state_dir = os.path.abspath(state_dir)
        os.makedirs(self.state_dir, exist_ok=True)
        self.host = host
        self.verbose = verbose
        self._push_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        threading.Thread(target=spec.graphql_schema, name="graphql-schema", daemon=True).start()
        self.http = _Server((host, port), Handler, self, "http")
        self.port = self.http.server_address[1]
        self.url = f"http://{host}:{self.port}"
        self.https = None
        self.https_url = None
        self.ca_path = None
        if https_port is not None:
            hosts = list(dict.fromkeys([host, "localhost", "127.0.0.1", *(tls_hosts or [])]))
            self.ca_path, cert, key = tls.ensure(self.state_dir, hosts)
            self.https = _Server((host, https_port), Handler, self, "https")
            # handshake in the handler thread, not in accept(): one slow client must not stall the port
            self.https.socket = tls.context(cert, key).wrap_socket(self.https.socket, server_side=True,
                                                                   do_handshake_on_connect=False)
            self.https_url = f"https://{host}:{self.https.server_address[1]}"
        self.mock = MockGitHub(self.state_dir, public_url=public_url or self.url, webhook_url=webhook_url,
                               webhook_secret=webhook_secret, app_id=app_id, app_slug=app_slug,
                               check_responses=check_responses)
        self._threads: list[threading.Thread] = []

    def push_lock(self, full_name: str) -> threading.Lock:
        with self._locks_guard:
            return self._push_locks.setdefault(full_name.lower(), threading.Lock())

    def start(self) -> MockServer:
        for srv in (self.http, self.https):
            if srv is None:
                continue
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        for srv in (self.http, self.https):
            if srv is not None:
                srv.shutdown()
                srv.server_close()
        self.mock.close()
