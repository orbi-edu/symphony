"""The team knowledge base: config, the index at each commit, the prompt and search_knowledge."""

from __future__ import annotations

import json
import os

import pytest
from tests.test_admin_workflow import ConfigMaps, reviewed
from tests.test_assistant_engine import FakeAgents, FakeOutbound, eventually, msg
from tests.test_assistant_soul import git

from symphony.admin_workflow import WorkflowError, WorkflowRepository
from symphony.assistant.confirm import Confirmer, Policy, ProposalStore
from symphony.assistant.engine import Engine, link_citations, to_slack
from symphony.assistant.knowledge import (
    MAX_FILE_BYTES,
    PROMPT_PAGES,
    Knowledge,
    KnowledgeBase,
    tokens,
)
from symphony.assistant.runtime import ReadOnlyRepos
from symphony.config import ConfigError, parse_workflow

RELEASES = """---
owner: platform
---
# Releases

We ship every Tuesday from main, after the staging soak.

## Hotfixes

A hotfix branches from the last tag.
Ask the on-call engineer first.

## Rollback

To roll back, redeploy the previous tag with the deploy job.
"""

ONCALL = """
- The pager rotates weekly; the rollback runbook is in the releases page.

### Escalation

Page the team lead after 30 minutes.
"""

DEPLOY = """# Implantação

A implantação em produção acontece às terças, depois da validação.
"""


def parse(section: str):
    return parse_workflow(f"---\ntracker: {{kind: memory}}\nassistant:\n  enabled: true\n{section}---\nx")


def commit(work, files: dict[str, str], message="docs") -> str:
    for path, text in files.items():
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git("add", ".", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message, cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return git("rev-parse", "HEAD", cwd=work)


@pytest.fixture
def handbook(tmp_path):
    """`file://<tmp>/remotes/acme/handbook.git` and its seed work tree (push with `commit`)."""
    bare = tmp_path / "remotes" / "acme" / "handbook.git"
    bare.parent.mkdir(parents=True)
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "seed"
    git("clone", "-q", str(bare), str(work), cwd=tmp_path)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
    sha = commit(work, {
        "README.md": "# Team handbook\n\n<!-- toc -->\nHow the team works, in one place.\n",
        "process/releases.md": RELEASES,
        "process/deep/oncall.md": ONCALL,
        "pt/implantacao.md": DEPLOY,
        "node_modules/pkg/README.md": "# Vendored rollback\n\nrollback rollback\n",
        "notes.txt": "rollback\n",
        "huge.md": "# Huge\n\n" + "x" * MAX_FILE_BYTES,
    })
    return f"file://{tmp_path / 'remotes'}", work, sha


def knowledge_for(tmp_path, server_url, bases, **kw):
    knowledge = Knowledge(lambda: bases, server_url="https://github.example.com/")
    repos = ReadOnlyRepos(str(tmp_path / "assistant"), lambda: [], server_url=server_url,
                          knowledge=knowledge, **kw)
    return knowledge, repos


BASE = KnowledgeBase("acme/handbook", name="handbook", when="how we release and run on-call")


# -- config and the workflow API ---------------------------------------------------------------
def test_config_reads_bases_with_defaults():
    cfg = parse("  knowledge:\n"
                "    - {repo: acme/handbook, name: handbook, when: how the team works}\n"
                "    - {repo: acme/app, paths: [docs/adr, 'runbooks/**/*.md']}\n")
    first, second = cfg.assistant_knowledge
    assert (first.repo, first.paths, first.label, first.when) == (
        "acme/handbook", ("**/*.md",), "handbook", "how the team works")
    assert (second.paths, second.label, second.name) == (("docs/adr", "runbooks/**/*.md"),
                                                          "acme/app", None)
    assert second.wants("docs/adr/0001-redis.md") and second.wants("runbooks/a/b/db.md")
    assert not second.wants("docs/other.md") and not second.wants("docs/adr/x.txt")
    assert first.wants("README.md") and not first.wants("node_modules/x/README.md")
    assert parse("").assistant_knowledge == ()


@pytest.mark.parametrize("section, message", [
    ("  knowledge: [{repo: handbook}]\n", "repo must be owner/name"),
    ("  knowledge: [{repo: acme/handbook, paths: [../secrets]}]\n", "without .."),
    ("  knowledge: [{repo: acme/handbook, paths: [docs/../../etc]}]\n", "without .."),
    ("  knowledge: [{repo: acme/handbook, paths: [/etc]}]\n", "without .."),
    ("  knowledge: [{repo: acme/handbook, paths: []}]\n", "nonempty list"),
    ("  knowledge: [{repo: acme/handbook, url: x}]\n", "unknown keys url"),
    ("  knowledge: [{repo: acme/a}, {repo: acme/a}]\n", "give each a name"),
    ("  knowledge: {repo: acme/a}\n", "must be a list"),
    ("  knowledge: [" + ", ".join(f"{{repo: acme/r{i}}}" for i in range(11)) + "]\n",
     "at most 10 entries"),
    ("  knowledge: [{repo: acme/a, paths: [" + ", ".join(f"d{i}" for i in range(21)) + "]}]\n",
     "at most 20 entries"),
])
def test_invalid_bases_are_refused(section, message):
    with pytest.raises(ConfigError, match=message):
        parse(section)


def test_the_workflow_api_publishes_bases_and_refuses_bad_ones():
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    bases = [{"repo": "acme/handbook", "paths": ["process", "**/adr/*.md"], "name": "handbook",
              "when": "process and releases"}]
    payload = reviewed(repository, {"assistant.knowledge": json.dumps(bases)})
    repository.publish(payload, "command-knowledge", before_write=lambda: None)
    cfg = parse_workflow(core.current.data["WORKFLOW.md"])
    assert [b.as_entry() for b in cfg.assistant_knowledge] == bases
    assert json.loads(repository.read()["values"]["assistant.knowledge"]) == bases

    revision = repository.read()["revision"]
    for bad, message in [
        ([{"repo": "handbook"}], "owner/name"),
        ([{"repo": "acme/handbook", "paths": ["../x"]}], "without .."),
        ([{"repo": f"acme/r{i}"} for i in range(11)], "at most 10"),
    ]:
        with pytest.raises(WorkflowError) as error:
            repository.validate(revision, {"assistant.knowledge": json.dumps(bad)})
        assert error.value.status_code == 422
        assert message in error.value.diagnostics[0]["message"]


# -- the index ----------------------------------------------------------------------------------
def test_the_index_reads_nested_markdown_at_the_commit(tmp_path, handbook):
    server_url, _work, sha = handbook
    knowledge, repos = knowledge_for(tmp_path, server_url, [BASE])
    work = repos.refresh()
    assert os.path.isfile(os.path.join(work, "acme/handbook/process/releases.md"))  # always out
    index = knowledge.index("handbook")
    assert index.sha == sha == repos.commits()["acme/handbook"]
    pages = {p.path: p for p in index.pages}
    assert sorted(pages) == ["README.md", "process/deep/oncall.md", "process/releases.md",
                             "pt/implantacao.md"]  # no node_modules, .txt or file over 1 MB
    assert (pages["README.md"].title, pages["README.md"].first) == (
        "Team handbook", "How the team works, in one place.")
    assert pages["process/releases.md"].title == "Releases"
    assert pages["process/releases.md"].first.startswith("We ship every Tuesday")
    assert pages["process/deep/oncall.md"].title == "oncall"  # no heading: the file name
    assert pages["process/deep/oncall.md"].first.startswith("The pager rotates weekly")


def test_paths_limit_the_index_to_folders_and_globs(tmp_path, handbook):
    server_url, _work, _sha = handbook
    bases = [KnowledgeBase("acme/handbook", ("process",), "process"),
             KnowledgeBase("acme/handbook", ("**/implantacao.md",), "pt")]
    knowledge, repos = knowledge_for(tmp_path, server_url, bases)
    repos.refresh()
    assert [p.path for p in knowledge.index("process").pages] == [
        "process/deep/oncall.md", "process/releases.md"]
    assert [p.path for p in knowledge.index("pt").pages] == ["pt/implantacao.md"]


def test_the_index_is_rebuilt_only_when_the_commit_changes_and_kept_when_sync_fails(
        tmp_path, handbook):
    server_url, work, _sha = handbook
    knowledge, repos = knowledge_for(tmp_path, server_url, [BASE])
    repos.refresh()
    first = knowledge.index("handbook")
    repos.refresh(force=True)
    assert knowledge.index("handbook") is first  # same commit: not rebuilt
    assert "may be stale" not in knowledge.prompt()

    sha = commit(work, {"process/new.md": "# New page\n\nFresh.\n"})
    repos.refresh(force=True)
    second = knowledge.index("handbook")
    assert second is not first and second.sha == sha
    assert "process/new.md" in {p.path for p in second.pages}

    remote = tmp_path / "remotes" / "acme" / "handbook.git"
    remote.rename(remote.with_name("gone.git"))
    repos.refresh(force=True)
    assert "acme/handbook" not in repos.commits()
    assert knowledge.index("handbook") is second  # the last good index stays
    text = knowledge.prompt()
    assert "may be stale" in text and sha in text
    assert knowledge.search("fresh")[0]["path"] == "process/new.md"


# -- the prompt ---------------------------------------------------------------------------------
def engine_with(knowledge, redis_client, out=None):
    cfg = parse_workflow("---\ntracker: {kind: memory}\nnotify:\n  users:\n    slack: {alice: U1}\n"
                         "assistant:\n  enabled: true\n  slack: {channels: [C1]}\n---\nx\n")
    out = out or FakeOutbound()
    agents = FakeAgents()
    confirmer = Confirmer(ProposalStore(redis_client, "t"), tracker=lambda: None,
                          audit=lambda *a: None, policy=lambda: Policy({}, "Backlog", "Todo", (), ()))
    engine = Engine(outbounds={out.name: out}, agents=agents, policy=lambda: cfg,
                    confirmer=confirmer, knowledge=knowledge,
                    server_url="https://github.example.com")
    return engine, out, agents


def test_every_role_gets_the_knowledge_section_with_the_commit_and_the_tool(
        tmp_path, handbook, redis_client):
    server_url, _work, sha = handbook
    knowledge, repos = knowledge_for(tmp_path, server_url, [BASE])
    repos.refresh()
    engine, _out, agents = engine_with(knowledge, redis_client)
    for thread, text in (("a", "how do we release?"), ("b", "research our releases"),
                         ("c", "plan a release checklist")):
        engine.on_message(msg(text, thread=thread))
    eventually(lambda: len(agents.opened) == 3 and all(a.prompts for a in agents.opened))
    assert {a.role for a in agents.opened} == {"assistant", "researcher", "planner"}
    for agent in agents.opened:
        prompt = agent.prompts[0]
        assert "Team knowledge" in prompt and "consult the knowledge base FIRST" in prompt
        assert f"commit {sha}" in prompt
        assert f"https://github.example.com/acme/handbook/blob/{sha}/" in prompt
        assert "It answers: how we release and run on-call" in prompt
        assert "- process/releases.md — Releases — We ship every Tuesday" in prompt
        assert "search_knowledge" in agent.tools


def test_a_large_base_lists_folders_instead_of_pages(tmp_path, handbook):
    server_url, work, _sha = handbook
    commit(work, {f"adr/{i // 100}/{i:04d}.md": f"# Decision {i}\n\nWe chose {i}.\n"
                  for i in range(PROMPT_PAGES + 1)})
    knowledge, repos = knowledge_for(tmp_path, server_url, [BASE])
    repos.refresh()
    text = knowledge.prompt()
    assert "Decision 7" not in text
    assert "too many to list" in text and "search_knowledge" in text
    assert "- adr/0/ (100)" in text and "- adr/3/ (1)" in text


def test_no_base_means_no_section_and_no_tool(redis_client):
    engine, _out, agents = engine_with(Knowledge(lambda: (), server_url="https://github.com"),
                                       redis_client)
    engine.on_message(msg("how do we release?", thread="z"))
    eventually(lambda: agents.opened and agents.opened[0].prompts)
    assert "search_knowledge" not in agents.opened[0].tools
    assert "Team knowledge" not in agents.opened[0].prompts[0]


# -- search -------------------------------------------------------------------------------------
def test_search_ranks_heading_matches_first_with_line_ranges_and_blob_urls(tmp_path, handbook):
    server_url, _work, sha = handbook
    knowledge, repos = knowledge_for(tmp_path, server_url, [BASE])
    repos.refresh()
    results = knowledge.search("rollback")
    top = results[0]
    assert (top["path"], top["heading"]) == ("process/releases.md", "Releases > Rollback")
    assert top["lines"] == [13, 15]
    assert top["url"] == (f"https://github.example.com/acme/handbook/blob/{sha}/"
                          "process/releases.md#L13-L15")
    assert top["base"] == "handbook" and "redeploy the previous tag" in top["snippet"]
    assert len(top["snippet"]) <= 300
    assert {r["path"] for r in results} == {"process/releases.md", "process/deep/oncall.md"}
    assert top["score"] > results[1]["score"]

    found = knowledge.search("implantacao producao")  # no accents in the query
    assert found[0]["path"] == "pt/implantacao.md" and "produção" in found[0]["snippet"]
    assert found[0]["lines"] == [1, 3]
    assert knowledge.search("rollback", limit=1) == results[:1]
    assert knowledge.search("the and of") == []  # stopwords only
    assert tokens("Implantação em produção, às 9h") == ["implantacao", "producao", "9h"]


def test_the_tool_checks_its_arguments_and_answers_json(tmp_path, handbook):
    from symphony.assistant.tools import knowledge_tool

    server_url, _work, _sha = handbook
    knowledge, repos = knowledge_for(tmp_path, server_url, [BASE])
    repos.refresh()
    tool = knowledge_tool(knowledge)
    assert tool.check({"query": " "}) == "query must not be empty"
    assert tool.check({"query": "x", "limit": 21}) == "limit must be 1-20"
    assert "Bases: handbook" in tool.check({"query": "x", "base": "wiki"})
    assert tool.check({"query": "rollback", "base": "Acme/Handbook"}) is None
    answer = json.loads(tool.answer({"query": "hotfix", "base": "handbook", "limit": 2}, 0))
    assert answer[0]["heading"] == "Releases > Hotfixes" and answer[0]["lines"] == [8, 11]


def test_knowledge_links_follow_each_transport():
    url = "https://github.example.com/acme/handbook/blob/abc/process/releases.md#L15-L17"
    text = f"See [Rollback]({url})."
    assert link_citations(text, "discord", {}, "https://github.example.com") == \
        f"See [Rollback](<{url}>)."
    assert to_slack(link_citations(text, "slack", {}, "https://github.example.com")) == \
        f"See <{url}|Rollback>."
