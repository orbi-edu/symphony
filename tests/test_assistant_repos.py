"""The assistant reaches every repository the GitHub App reaches, opening the others on demand."""

from __future__ import annotations

import json
import os

from tests.test_assistant_soul import cfg, git

from symphony.assistant import tools
from symphony.assistant.engine import instructions
from symphony.assistant.runtime import OPENED_IDLE_S, ReadOnlyRepos, reachable_repositories
from symphony.discovery import DISCOVERY_KEY


def bare_repo(tmp_path, name: str) -> None:
    work = tmp_path / "seed" / name
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=work)
    (work / "main.tf").write_text('resource "aws_rds_cluster" "x" {}\n')
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=work)
    bare = tmp_path / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)


def test_reachable_repositories_add_what_discovery_found(redis_client):
    redis_client.set(DISCOVERY_KEY, json.dumps({"items": [
        {"name": "acme/infra", "archived": False}, {"name": "acme/old", "archived": True}]}))
    c = cfg(top="kubernetes: {default_repo: acme/app}\n")
    assert reachable_repositories(redis_client, "test", c) == ["acme/app", "acme/infra"]


def test_a_repository_opened_on_demand_is_checked_out_and_kept_until_idle(tmp_path):
    for name in ("acme/app", "acme/infra"):
        bare_repo(tmp_path, name)
    now = [0.0]
    repos = ReadOnlyRepos(str(tmp_path / "assistant"), lambda: ["acme/app"],
                          server_url=f"file://{tmp_path / 'remotes'}", clock=lambda: now[0])
    work = repos.refresh()
    assert not os.path.exists(os.path.join(work, "acme/infra"))
    sha = repos.open("Acme/Infra")
    assert os.path.isfile(os.path.join(work, "acme/infra/main.tf"))
    assert repos.commits()["acme/infra"] == sha
    now[0] += 600
    repos.refresh()
    assert "acme/infra" in repos.commits()  # still used recently
    now[0] += OPENED_IDLE_S
    repos.refresh()
    assert "acme/infra" not in repos.commits()  # idle a day: dropped
    assert "acme/app" in repos.commits()


def test_the_tool_opens_reachable_repositories_and_names_the_others_when_refused():
    opened = []
    tool = tools.open_repository_tool(lambda: ["acme/app", "acme/infra"],
                                      lambda repo: opened.append(repo) or "c0ffee")
    assert tool.check({"repo": "ACME/infra"}) is None
    assert json.loads(tool.answer({"repo": "ACME/infra"}, 0)) == {"path": "./acme/infra",
                                                                    "commit": "c0ffee"}
    assert opened == ["acme/infra"]
    refusal = tool.check({"repo": "acme/secret"})
    assert "not reachable" in refusal and "acme/infra" in refusal


def test_the_instructions_list_what_can_be_opened_and_forbid_saying_it_cannot_be_seen():
    text = instructions("assistant", transport="slack", is_dm=False, repos=["acme/app"],
                        openable=["acme/infra"])
    assert "open_repository" in text and "acme/infra" in text
    assert "never answer that you cannot see a repository on this list" in text
