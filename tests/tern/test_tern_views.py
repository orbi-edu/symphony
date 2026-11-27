"""The Telemetry and Dashboard views' node shapes, rendered outside Tern.

`render_views.luau` runs the plugin's own `views.render` under the `luau` CLI. The test copies
the plugin's modules to a temporary directory, each starting with `local tern =
require("./tern_stub")`: a stub whose view builders return `{k, p, c}` nodes, as Tern's do. The
tests check the charts' kinds, series lengths, summaries and empty states for the history
documents of `history_fixture.py`. Skipped when `luau` is not installed.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from history_fixture import empty_history_doc, history_doc  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("luau") is None, reason="the luau CLI is not installed")

TODAY = dt.datetime.now(dt.UTC).date()
CARDS = ("cost", "tokens", "kinds", "outcomes", "models", "repos", "prs", "rework", "questions", "activity")


def luau(v) -> str:
    """A JSON value as a Luau table literal; null members are dropped, as Tern's decoder does."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "N"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, list):
        return "{" + ",".join(luau(x) for x in v) + "}"
    return "{" + ",".join(f"[{luau(k)}]={luau(x)}" for k, x in v.items() if x is not None) + "}"


def base_data(**extra) -> dict:
    overview = {
        "daemon": {"alive": True, "paused": False}, "counts": {"running": 1, "waiting_input": 2,
                                                             "blocked_on_person": 1},
        "tokens_today": 120_000, "cost_today": 1.5, "merger": {"reviewing": 1, "train": 0, "parked": 0},
        "rate_limits": [],
    }
    telemetry = {
        "observedAt": "2026-11-25T12:00:00Z",
        "totals": {"input": 4_120_000, "output": 512_000, "cacheRead": 9_800_000, "total": 15_072_000,
                   "costEstimate": 61.37},
        "models": [], "monthly": [], "sessions": [], "sources": {}, "limits": {"truncated": False},
    }
    return {"overview": overview, "sessions": {"items": []}, "issues": {"items": []}, "telemetry": telemetry,
            **extra}


@pytest.fixture(scope="module")
def runner(luau_plugin) -> Path:
    return luau_plugin("render", "render_views.luau")


@pytest.fixture
def render(runner: Path):
    def run(cases: dict) -> dict:
        proc = subprocess.run(["luau", str(runner), "-a", luau(cases)], capture_output=True, text=True,
                              timeout=60, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)
    return run


def child(n: dict, key: str) -> dict:
    for c in n.get("c") or []:
        if (c.get("p") or {}).get("key") == key:
            return c
    raise AssertionError(f"no child {key!r} in {[(c.get('p') or {}).get('key') for c in n.get('c') or []]}")


def at(n: dict, *keys: str) -> dict:
    for k in keys:
        n = child(n, k)
    return n


def walk(n: dict):
    yield n
    for c in n.get("c") or []:
        yield from walk(c)


def charts(n: dict) -> list[dict]:
    return [x["p"] for x in walk(n) if x.get("k") == "chart"]


def texts(n: dict) -> str:
    return " ".join(s.get("t", "") for x in walk(n) if x.get("k") == "text" for s in x["p"].get("spans") or [])


def page(view: dict) -> dict:
    """The view's body: the third child of the main column (after the head and the tab strip)."""
    return view["main"]["c"][2]


def by_kind(n: dict, kind: str) -> list[dict]:
    return [c for c in charts(n) if c["kind"] == kind]


def test_telemetry_charts_cover_the_history(render):
    doc = history_doc(TODAY)
    out = render({"t": {"view": "telemetry", "data": base_data(history=doc)}})["t"]
    hist = child(page(out), "history")
    tabs = at(hist, "bar", "period")
    assert tabs["p"]["active"] == "all"
    assert [i["id"] for i in tabs["p"]["items"]] == ["all", "90d", "30d", "7d"]
    n = len(doc["points"])
    cards = {k: at(hist, "charts", k) for k in CARDS}
    for key, card in cards.items():
        assert charts(card), f"{key} has no chart"
        for c in charts(card):
            assert c.get("summary"), f"{key}.{c['key']} has no summary"

    (cost,) = charts(cards["cost"])
    assert cost["kind"] == "area" and len(cost["series"]) == n and len(cost["labels"]) == n
    # Narrow cards keep five axis labels, the first and the last among them; the wide
    # Questions chart keeps every label.
    for key in ("cost", "tokens", "kinds", "prs", "rework"):
        lines = [c for c in charts(cards[key]) if c["kind"] in ("area", "line")]
        assert len(lines) == 1, key
        shown = [label for label in lines[0]["labels"] if label]
        assert len(lines[0]["labels"]) == n and len(shown) == 5, key
        assert lines[0]["labels"][0] == doc["points"][0]["start"][5:]
        assert lines[0]["labels"][-1] == doc["points"][-1]["start"][5:]
    (questions_line,) = by_kind(cards["questions"], "line")
    assert all(questions_line["labels"]) and len(questions_line["labels"]) == n
    assert cost["last"].startswith("$") and cost["last"].endswith("total")
    assert "estimated" in cost["summary"]

    (tokens,) = charts(cards["tokens"])
    assert tokens["kind"] == "area"
    assert [s["label"] for s in tokens["series"]] == ["Input", "Output", "Cache read", "Cache write"]
    assert all(len(s["values"]) == n for s in tokens["series"])

    (kinds,) = charts(cards["kinds"])
    assert kinds["kind"] == "line" and 1 <= len(kinds["series"]) <= 7
    assert all(len(s["values"]) == n for s in kinds["series"])

    bars, donut = by_kind(cards["outcomes"], "bars"), by_kind(cards["outcomes"], "donut")
    assert len(bars) == 1 and len(donut) == 1
    outcomes = {k: v for k, v in doc["totals"]["outcomes"].items() if v}
    assert sum(b["value"] for b in bars[0]["series"]) == sum(outcomes.values())
    assert donut[0]["total"] == str(sum(outcomes.values()))
    assert bars[0]["series"][0]["tone"] == "success"  # "done" leads and reads as success

    (models_donut,) = by_kind(cards["models"], "donut")
    assert models_donut["unit"] == "$" and models_donut["total"].startswith("$")
    table = next(x for x in walk(cards["models"]) if x.get("k") == "table")
    assert len(table["p"]["rows"]) == len(doc["by_model"])
    assert all(len(r["cells"]["trend"]["spark"]["values"]) == n for r in table["p"]["rows"])

    repo_bars = by_kind(cards["repos"], "bars")
    assert [b["key"] for b in repo_bars] == ["cost", "sessions"]
    assert all(0 < len(b["series"]) <= 10 for b in repo_bars)

    prs, ttm = by_kind(cards["prs"], "line"), by_kind(cards["prs"], "spark")
    assert [s["label"] for s in prs[0]["series"]] == ["Opened", "Merged"]
    assert len(ttm) == 1 and "Mean time to merge" in ttm[0]["summary"]
    assert "mean, not a median" in texts(cards["prs"])

    (rework,) = charts(cards["rework"])
    assert rework["kind"] == "line" and rework["unit"] == "%" and len(rework["series"]) == n
    avg = round(doc["totals"]["rework_rate"] * 100, 1)
    assert rework["marks"] == [{"value": avg, "tone": "muted", "title": rework["marks"][0]["title"]}]

    assert by_kind(cards["questions"], "line") and by_kind(cards["questions"], "donut")
    assert "mean time to answer" in by_kind(cards["questions"], "line")[0]["summary"]

    (heat,) = charts(cards["activity"])
    assert heat["kind"] == "heatmap" and len(heat["cells"]) == 7 and all(len(r) == 24 for r in heat["cells"])
    assert heat["rows"][0] == "Mon" and len(heat["tips"]) == 7
    assert str(doc["activity"]["total"]) in heat["summary"]

    # The existing usage content stays.
    assert "Agent usage" in texts(page(out))
    legend = texts(out["dock"])
    assert "[ ]" in legend and "period" in legend


def test_telemetry_period_and_weekly_buckets(render):
    doc = history_doc(TODAY, TODAY - dt.timedelta(days=200), "day")  # coarsened to weeks
    assert doc["bucket"] == "week"
    out = render({"t": {"view": "telemetry", "period": "90d", "data": base_data(history=doc)}})["t"]
    hist = child(page(out), "history")
    assert at(hist, "bar", "period")["p"]["active"] == "90d"
    (cost,) = charts(at(hist, "charts", "cost"))
    assert len(cost["series"]) == len(doc["points"])
    assert at(hist, "charts", "cost")["p"]["head"][0]["t"] == "Estimated cost per week"
    weeks = [f"W{dt.date.fromisoformat(p['start']).isocalendar().week}" for p in doc["points"]]
    shown = [(i, label) for i, label in enumerate(cost["labels"]) if label]
    assert len(shown) == 5 and all(label == weeks[i] for i, label in shown)
    assert shown[0][0] == 0 and shown[-1][0] == len(weeks) - 1


def test_monthly_buckets_have_month_names(render):
    doc = history_doc(TODAY, TODAY - dt.timedelta(days=1200), "week")  # coarsened to months
    assert doc["bucket"] == "month"
    out = render({"t": {"view": "telemetry", "data": base_data(history=doc)}})["t"]
    (cost,) = charts(at(child(page(out), "history"), "charts", "cost"))
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    shown = [(i, label) for i, label in enumerate(cost["labels"]) if label]
    assert len(shown) == 5
    assert all(label == names[int(doc["points"][i]["start"][5:7]) - 1] for i, label in shown)


def test_telemetry_empty_history_shows_empty_states(render):
    out = render({"t": {"view": "telemetry", "data": base_data(history=empty_history_doc(TODAY))}})["t"]
    hist = child(page(out), "history")
    assert charts(hist) == []
    for key in CARDS:
        line = texts(at(hist, "charts", key))
        assert "No " in line or "Nothing" in line, f"{key}: {line!r}"
    assert "no data yet" in texts(child(hist, "bar"))


def test_telemetry_history_loading_and_error(render):
    out = render({
        "loading": {"view": "telemetry", "data": base_data()},
        "error": {"view": "telemetry", "data": base_data(), "errs": {"history": "HTTP 503"}},
    })
    assert at(page(out["loading"]), "history", "charts")["k"] == "shimmer"
    err = at(page(out["error"]), "history", "charts")
    assert err["k"] == "card" and "HTTP 503" in texts(err)


def test_dashboard_trends_tiles_and_cost_chart(render):
    doc = history_doc(TODAY, TODAY - dt.timedelta(days=29), "day")
    assert len(doc["points"]) == 30
    out = render({"d": {"view": "dashboard", "data": base_data(trends=doc)}})["d"]
    trends = child(page(out), "trends")
    tiles = child(trends, "tiles")
    keys = [c["p"]["key"] for c in tiles["c"]]
    assert keys == ["cost", "sessions", "merged", "rework", "questions", "blocked"]
    for tile in tiles["c"]:
        (spark,) = charts(tile)
        assert spark["kind"] == "spark" and len(spark["series"]) == 30 and spark["summary"]
    assert texts(child(tiles, "questions")).startswith("2")  # waiting now, from the overview
    assert texts(child(tiles, "blocked")).startswith("1")
    assert texts(child(tiles, "cost")).startswith("$")
    (area,) = [c for c in charts(trends) if c["kind"] == "area"]
    assert len(area["series"]) == 30 and area["last"].endswith("total")
    # The rest of the dashboard stays.
    assert "Active sessions" in texts(page(out)) and "Merger" in texts(page(out))


def test_dashboard_trends_without_data(render):
    out = render({
        "empty": {"view": "dashboard", "data": base_data(trends=empty_history_doc(TODAY))},
        "loading": {"view": "dashboard", "data": base_data()},
    })
    empty = child(page(out["empty"]), "trends")
    assert [c for c in charts(empty) if c["kind"] == "area"] == []
    assert "No cost recorded in the last 30 days." in texts(empty)
    assert child(page(out["loading"]), "trends")["k"] == "shimmer"


def test_merger_parked_rows_say_what_they_wait_for_and_name_the_pr(render, redis_client):
    """The rows come from the real `MergerStore.snapshot` (what `GET /v1/merger` serves). A
    clean review whose CI passed waits for an approval and says so; the row names the PR by
    its title, and an older record with no PR title by `PR #n`, never by the report's URL."""
    from symphony.merger.store import MergerStore

    store = MergerStore(redis_client, "tern-views", "acme/web")
    url = "https://github.com/acme/web/pull/{}#issuecomment-1"
    for pr, extra in ((818, {"waiting": "approval", "pr_title": "Show the delivery date"}),
                      (819, {"waiting": "ci", "pr_title": "Fix the login redirect"}),
                      (820, {})):  # parked before the PR title and `waiting` were recorded
        store.park({"pr": pr, "head_sha": "a" * 40, "kind": "clean", "title": "**Waiting for CI**",
                    "parked_at": 1_700_000_000_000 + pr, "comment": url.format(pr), **extra})
    snap = store.snapshot()
    assert [(p["pr"], p.get("title"), p["waiting"]) for p in snap["parked"]] == [
        (818, "Show the delivery date", "approval"), (819, "Fix the login redirect", "ci"), (820, None, "ci")]
    out = render({"m": {"view": "merger", "data": base_data(merger={"repos": [snap]})}})["m"]
    rows = {"".join(s["t"] for s in it["p"]["label"]): "".join(s["t"] for s in it["p"]["value"])
            for it in walk(out["main"]) if it.get("k") == "item"}
    assert rows.keys() == {"#818  Show the delivery date", "#819  Fix the login redirect", "PR #820"}
    assert rows["#818  Show the delivery date"].startswith("waiting for approval · since ")
    assert rows["#819  Fix the login redirect"].startswith("waiting for CI · since ")
    assert rows["PR #820"].startswith("waiting for CI · since ")
    assert "issuecomment" not in json.dumps(out)


def spans_text(spans) -> str:
    return "".join(s.get("t", "") for s in spans) if isinstance(spans, list) else str(spans)


def test_tickets_show_the_assignee_and_the_session_card_too(render):
    """`GET /v1/issues` names each ticket's `assignee` (null when none): the Tickets table has
    an Assignee column, kept as long as the tracker state, and the session card of the
    ticket's attempt says who it is assigned to."""
    issues = {"items": [
        {"issue_id": "iss-1", "identifier": "ENG-1", "title": "Export", "phase": "running",
         "session_id": "iss-1:1", "assignee": {"name": "alice", "login": "alice"}},
        {"issue_id": "iss-2", "identifier": "ENG-2", "title": "Import", "phase": "running", "assignee": None},
    ]}
    sessions = {"items": [{"sid": "iss-1:1", "issue_id": "iss-1", "identifier": "ENG-1", "kind": "implement",
                           "state": "running", "updated_at": "2026-11-25T12:00:00Z"}]}
    out = render({
        "t": {"view": "tickets", "data": base_data(issues=issues)},
        "s": {"view": "sessions", "data": base_data(issues=issues, sessions=sessions), "sel": {"sessions": "iss-1:1"}},
    })
    [table] = [x["p"] for x in walk(out["t"]["main"]) if x.get("k") == "table"]
    cols = {c["id"]: c for c in table["cols"]}
    assert cols["assignee"]["head"] == "Assignee"
    assert cols["assignee"]["priority"] == cols["tracker"]["priority"]
    assert [spans_text(r["cells"]["assignee"]) for r in table["rows"]] == ["alice", "—"]
    [facts] = [x["p"]["items"] for x in walk(out["s"]["main"]) if x.get("k") == "kv"]
    assert {spans_text(f[0]): spans_text(f[1]) for f in facts}["Assignee"] == "alice"


def test_merger_rows_name_the_author_and_reviewers(render):
    """Each merger row says `author · reviewers` with each reviewer's latest state; a PR
    parked for an approval says who it waits on, or anyone with write access."""
    people = {"author": "alice", "reviewers": [{"login": "bob", "state": "approved"},
                                               {"login": "carol", "state": "requested"}]}
    snap = {
        "repo": "acme/web", "alive": True, "batch": {}, "label_filter": None,
        "reviewing": [{"pr": 815, "phase": "review", "head_sha": "a" * 40, "worker": "w0", "started_at": 1,
                       "sessions": [], **people}],
        "parked": [
            {"pr": 818, "kind": "clean", "waiting": "approval", "title": "Show the date", "parked_at": 1,
             "author": "alice", "reviewers": [{"login": "bob", "state": "requested"}], "waiting_on": ["bob"]},
            {"pr": 819, "kind": "clean", "waiting": "approval", "title": "Fix the redirect", "parked_at": 2,
             "author": "dave", "reviewers": [], "waiting_on": []},
        ],
        "train": {"lease": None, "records": [{"pr": 402, "state": "train", "branch": "feat/a", "reviewed_at": 1,
                                              **people}]},
        "queue": [{"pr": 821, "title": "Export", "kind": "first", "ready_at": 1, **people}],
    }
    out = render({"m": {"view": "merger", "data": base_data(merger={"repos": [snap]})}})["m"]
    # The data tables (`key` t): the mergers, in review, the train; the count strip is one too.
    tables = [x["p"] for x in walk(out["main"]) if x.get("k") == "table" and x["p"].get("key") == "t"]
    cells = [spans_text(r["cells"]["people"]) for t in tables for r in t["rows"] if "people" in r["cells"]]
    assert cells == ["alice · bob ✓, carol requested"] * 2  # in review, then the train
    assert all("people" in {c["id"] for c in t["cols"]} for t in tables[1:])
    details = {spans_text(it["p"]["label"]): spans_text(it["p"]["detail"])
               for it in walk(out["main"]) if it.get("k") == "item"}
    assert details == {
        "#821  Export": "  acme/web · alice · bob ✓, carol requested · first review",
        "#818  Show the date": "  acme/web · clean review · alice · bob requested · waiting on bob",
        "#819  Fix the redirect": "  acme/web · clean review · dave · waiting on anyone with write access",
    }


def test_the_views_render_the_real_history_document(render, redis_client):
    """The documents `rollups.history` builds (the API's), not the fixture's: live bumps of a
    few sessions, a dispatch, a merged PR and an answered question."""
    from symphony import questions, rollups
    from symphony.sessions import SessionStore

    start = dt.datetime.combine(TODAY - dt.timedelta(days=3), dt.time(9), dt.UTC)
    now = [int(start.timestamp() * 1000)]
    sessions = SessionStore(redis_client, "test", now_ms=lambda: now[0])
    usage = {"type": "message_end", "message": {"role": "assistant", "usage": {
        "input": 900, "output": 100, "cacheRead": 4000, "cacheWrite": 50, "cost": {"total": 0.42}}}}
    for n, (kind, repo, outcome) in enumerate([("implement", "acme/app", "done"),
                                               ("review", "acme/app", "done"),
                                               ("implement", "acme/web", "failed")]):
        sid = f"s{n}"
        sessions.open(sid, kind=kind, repo=repo, model="anthropic/claude-x")
        sessions.append_event(sid, "message_end", usage)
        now[0] += 3_600_000
        sessions.close(sid, outcome)
    rollups.bump(redis_client, now[0], {"dispatched": 2, "rework": 1, "prs_opened": 1, "prs_merged": 1,
                                        "time_to_merge_ms_sum": 7_200_000, "time_to_merge_count": 1})
    questions.ask(redis_client, "test", qid="q1", issue_id="i1", identifier="ENG-1", sid="s0",
                  url=None, question="Which?", options=["a"], to=None, now_ms=now[0])
    questions.submit(redis_client, "test", "q1", "a", by="alice", via="slack", now_ms=now[0] + 60_000)

    history = rollups.history(redis_client, first=None, last=TODAY, bucket=None, observed_at="now")
    trends = rollups.history(redis_client, first=TODAY - dt.timedelta(days=29), last=TODAY,
                             bucket="day", observed_at="now")
    out = render({"t": {"view": "telemetry", "data": base_data(history=history)},
                  "d": {"view": "dashboard", "data": base_data(trends=trends)}})
    hist = child(page(out["t"]), "history")
    n = len(history["points"])
    for key in CARDS:
        card = at(hist, "charts", key)
        assert charts(card), f"{key} has no chart: {texts(card)!r}"
        assert all(c.get("summary") for c in charts(card)), key
    (cost,) = charts(at(hist, "charts", "cost"))
    assert len(cost["series"]) == n and cost["last"] == "$1.26 total"
    (heat,) = charts(at(hist, "charts", "activity"))
    assert sum(map(sum, heat["cells"])) == 3
    tiles = child(child(page(out["d"]), "trends"), "tiles")
    for tile in tiles["c"]:
        (spark,) = charts(tile)
        assert len(spark["series"]) == 30

    redis_client.flushdb()
    empty = rollups.history(redis_client, first=None, last=TODAY, bucket=None, observed_at="now")
    out = render({"t": {"view": "telemetry", "data": base_data(history=empty)}})["t"]
    hist = child(page(out), "history")
    assert charts(hist) == []
