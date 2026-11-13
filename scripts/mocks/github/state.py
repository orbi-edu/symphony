"""The mock's state: accounts, the App and its installations, tokens and repositories, kept in
memory under one lock and written to `<state>/state.json` after every change. Bare git
repositories live in `<state>/git/<owner>/<name>.git`.

Identifiers come from one counter and tokens from a seeded hash of it, so a scripted run
produces the same ids and tokens every time.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

LEVEL = {"none": 0, "read": 1, "write": 2, "admin": 3}
TOKEN_TTL_S = 3600
# What `docs/install.md` asks the App for.
DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write", "checks": "read",
                       "actions": "write", "issues": "write", "organization_projects": "write",
                       "metadata": "read"}
DEFAULT_LABELS = [
    ("bug", "d73a4a", "Something isn't working"),
    ("documentation", "0075ca", "Improvements or additions to documentation"),
    ("duplicate", "cfd3d7", "This issue or pull request already exists"),
    ("enhancement", "a2eeef", "New feature or request"),
    ("good first issue", "7057ff", "Good for newcomers"),
    ("help wanted", "008672", "Extra attention is needed"),
    ("invalid", "e4e669", "This doesn't seem right"),
    ("question", "d876e3", "Further information is requested"),
    ("wontfix", "ffffff", "This will not be worked on"),
]


def iso(epoch: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch, dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def node_id(prefix: str, *parts) -> str:
    raw = ":".join(str(p) for p in parts).encode()
    return f"{prefix}_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_node_id(value: str) -> tuple[str, list[str]] | None:
    prefix, _, rest = value.partition("_")
    if not rest:
        return None
    try:
        raw = base64.urlsafe_b64decode(rest + "=" * (-len(rest) % 4)).decode()
    except Exception:  # noqa: BLE001 — a malformed id is just unknown
        return None
    return prefix, raw.split(":")


@dataclass
class Actor:
    kind: str  # installation | user
    login: str
    user_type: str  # Bot | User
    token: str = ""
    installation_id: int | None = None
    account: str | None = None
    repos: frozenset[str] | None = None  # lower-case full names; None = every repository it covers
    permissions: dict | None = None  # None = full (a person)

    def level(self, permission: str) -> int:
        if self.permissions is None:
            return LEVEL["admin"]
        return LEVEL.get(self.permissions.get(permission, "none"), 0)


def _fresh(app_id: int, slug: str) -> dict:
    return {
        "seq": 1000,
        "tokens_minted": 0,
        "app": {"id": app_id, "slug": slug, "name": f"{slug} (mock)", "client_id": f"Iv1.mock{app_id:08d}",
                "owner": "mock-apps", "private_key": None, "public_key": None,
                "created_at": "2026-01-01T00:00:00Z"},
        "accounts": {},
        "tokens": {},
        "installations": {},
        "repos": {},
        "projects": {},
        "webhook": {"url": None, "secret": None},
    }


class Store:
    def __init__(self, state_dir: str, app_id: int = 1, app_slug: str = "symphony") -> None:
        self.dir = os.path.abspath(state_dir)
        self.git_root = os.path.join(self.dir, "git")
        os.makedirs(self.git_root, exist_ok=True)
        self.lock = threading.RLock()
        self.path = os.path.join(self.dir, "state.json")
        if os.path.exists(self.path):
            with open(self.path) as fh:
                self.data = json.load(fh)
        else:
            self.data = _fresh(app_id, app_slug)
        self._ensure_app_key()
        self.save()

    # -- persistence -----------------------------------------------------------------
    def save(self) -> None:
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.data, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)

    def reset(self) -> None:
        """Back to an empty GitHub; the App (and its key) and the webhook target survive, so a
        configured Symphony keeps working."""
        import shutil

        with self.lock:
            app, hook = self.data["app"], self.data["webhook"]
            self.data = _fresh(app["id"], app["slug"])
            self.data["app"], self.data["webhook"] = app, hook
            shutil.rmtree(self.git_root, ignore_errors=True)
            os.makedirs(self.git_root, exist_ok=True)
            self.save()

    def next_id(self) -> int:
        with self.lock:
            self.data["seq"] += 1
            return self.data["seq"]

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    # -- the App -------------------------------------------------------------------------
    def _ensure_app_key(self) -> None:
        app = self.data["app"]
        if app.get("public_key"):
            return
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        app["private_key"] = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        app["public_key"] = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def register_app_key(self, public_key: str | None = None, private_key: str | None = None) -> None:
        """Use the operator's App key: a public key (the mock verifies only), or a private key
        (the mock derives the public one and can hand the private one out via `/_mock/app`)."""
        from cryptography.hazmat.primitives import serialization

        with self.lock:
            app = self.data["app"]
            if private_key:
                key = serialization.load_pem_private_key(private_key.encode(), None)
                public_key = key.public_key().public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            elif public_key:
                serialization.load_pem_public_key(public_key.encode())
            else:
                raise ValueError("public_key or private_key is required")
            app["public_key"], app["private_key"] = public_key, private_key
            self.save()

    @property
    def app(self) -> dict:
        return self.data["app"]

    def bot_login(self) -> str:
        return f"{self.app['slug']}[bot]"

    # -- accounts -------------------------------------------------------------------------
    def account(self, login: str, kind: str = "User", create: bool = True) -> dict | None:
        with self.lock:
            acc = self.data["accounts"].get(login.lower())
            if acc is None and create:
                acc = {"login": login, "id": self.next_id(), "type": kind, "name": login}
                self.data["accounts"][login.lower()] = acc
            return acc

    def bot(self) -> dict:
        acc = self.account(self.bot_login(), "Bot")
        assert acc is not None
        return acc

    def user_token(self, login: str) -> str:
        with self.lock:
            for tok, rec in self.data["tokens"].items():
                if rec["kind"] == "user" and rec["login"].lower() == login.lower():
                    return tok
            tok = "ghp_" + self._token_body("user", login)
            self.data["tokens"][tok] = {"kind": "user", "login": login, "expires_at": None}
            self.save()
            return tok

    def _token_body(self, *parts) -> str:
        self.data["tokens_minted"] += 1
        raw = ":".join(str(p) for p in (self.app["id"], self.data["tokens_minted"], *parts))
        return hashlib.sha256(raw.encode()).hexdigest()[:36]

    # -- installations ----------------------------------------------------------------------
    def install(self, account: str, repos: list[str] | None, permissions: dict | None) -> dict:
        """Installs the App on `account` (`repos` None = all its repositories)."""
        with self.lock:
            acc = self.account(account, "Organization")
            assert acc is not None
            for inst in self.data["installations"].values():
                if inst["account"].lower() == account.lower():
                    break
            else:
                inst = {"id": self.next_id(), "account": acc["login"], "created_at": iso()}
                self.data["installations"][str(inst["id"])] = inst
            inst["repository_selection"] = "all" if repos is None else "selected"
            inst["repos"] = sorted({r.lower() for r in repos or []})
            inst["permissions"] = dict(permissions or DEFAULT_PERMISSIONS)
            inst["permissions"].setdefault("metadata", "read")
            inst["updated_at"] = iso()
            self.save()
            return inst

    def installation(self, inst_id) -> dict | None:
        return self.data["installations"].get(str(inst_id))

    def installation_on(self, account: str) -> dict | None:
        return next((i for i in self.data["installations"].values() if i["account"].lower() == account.lower()),
                    None)

    def installation_for(self, full_name: str) -> dict | None:
        owner = full_name.split("/", 1)[0].lower()
        for inst in self.data["installations"].values():
            if inst["account"].lower() == owner and (
                    inst["repository_selection"] == "all" or full_name.lower() in inst["repos"]):
                return inst
        return None

    def installation_repos(self, inst: dict) -> list[str]:
        owner = inst["account"].lower()
        if inst["repository_selection"] == "all":
            return sorted(k for k, r in self.data["repos"].items() if r["owner"].lower() == owner)
        return [r for r in inst["repos"] if r in self.data["repos"]]

    def mint(self, inst: dict, repos: list[str] | None, permissions: dict) -> dict:
        with self.lock:
            tok = "ghs_" + self._token_body("installation", inst["id"])
            rec = {"kind": "installation", "login": self.bot_login(), "installation": inst["id"],
                   "repos": sorted(repos) if repos is not None else None,
                   "permissions": permissions, "expires_at": int(time.time()) + TOKEN_TTL_S}
            self.data["tokens"][tok] = rec
            self.bot()
            self.save()
            return {"token": tok, **rec}

    def actor(self, token: str) -> Actor | None:
        with self.lock:
            rec = self.data["tokens"].get(token)
            if rec is None:
                return None
            if rec.get("expires_at") and rec["expires_at"] < time.time():
                return None
            if rec["kind"] == "user":
                acc = self.account(rec["login"])
                return Actor("user", rec["login"], acc["type"] if acc else "User", token)
            inst = self.installation(rec["installation"])
            if inst is None:
                return None
            repos = rec["repos"]
            return Actor("installation", rec["login"], "Bot", token, inst["id"], inst["account"],
                         frozenset(repos) if repos is not None else None, rec["permissions"])

    def can_see(self, actor: Actor, full_name: str) -> bool:
        if actor.kind == "user":
            return True
        inst = self.installation(actor.installation_id)
        if inst is None or self.installation_for(full_name) is not inst:
            return False
        return actor.repos is None or full_name.lower() in actor.repos

    # -- repositories ---------------------------------------------------------------------------
    def repo(self, owner: str, name: str) -> dict | None:
        return self.data["repos"].get(f"{owner}/{name}".lower())

    def repo_path(self, repo: dict) -> str:
        return os.path.join(self.git_root, repo["owner"], f"{repo['name']}.git")

    def create_repo(self, owner: str, name: str, default_branch: str = "main", private: bool = True,
                    description: str = "") -> dict:
        with self.lock:
            acc = self.account(owner, "Organization")
            assert acc is not None
            repo = {
                "id": self.next_id(), "owner": acc["login"], "name": name, "full_name": f"{acc['login']}/{name}",
                "default_branch": default_branch, "private": private, "description": description,
                "created_at": iso(), "pushed_at": iso(), "labels": {}, "next_number": 1, "issues": {},
                "comments": {}, "review_comments": {}, "reviews": {}, "check_suites": {}, "check_runs": {},
                "workflow_runs": {}, "jobs": {}, "statuses": [], "protection": {}, "run_number": 0,
                "ci": {"workflow": "CI", "path": ".github/workflows/ci.yml", "jobs": ["test"],
                       "mode": "auto", "command": None, "delay_s": 0.0, "skip_drafts": True},
                "ci_results": {},
            }
            for label, color, desc in DEFAULT_LABELS:
                repo["labels"][label] = {"id": self.next_id(), "name": label, "color": color,
                                         "description": desc, "default": True}
            self.data["repos"][repo["full_name"].lower()] = repo
            self.save()
            return repo

    def label(self, repo: dict, name: str, create: bool = False) -> dict | None:
        lab = repo["labels"].get(name.lower())
        if lab is None and create:
            lab = {"id": self.next_id(), "name": name, "color": "ededed", "description": None, "default": False}
            repo["labels"][name.lower()] = lab
        return lab
