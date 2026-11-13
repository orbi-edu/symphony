"""`symphony watch`: a live session view. Native in Tern (Surface Protocol via `tern-sdk`),
plain streaming text anywhere else. Both draw the same model: the ACP updates the bridge
produces (`acp_map`), so the terminal view and the Tern chat never disagree."""

from __future__ import annotations

import json
import queue
import sys
import threading
from typing import IO

from symphony.cli import acp_map, render
from symphony.cli.client import Api, ApiError, EventTail

HISTORY_ITEMS = 60
QUIT_KEYS = {"q", "escape", "esc"}

_TOOL_STATUS = {"pending": "pending", "in_progress": "running", "completed": "done", "failed": "error"}
_STATE_STATUS = {"starting": "pending", "running": "running", "waiting_input": "pending",
                 "idle": "done", "ended": "done"}


class WatchModel:
    def __init__(self, meta: dict) -> None:
        self.meta = dict(meta)
        self.items: list[dict] = []
        self._tools: dict[str, dict] = {}
        self.plan: list[dict] = []
        self._seq = 0

    def _add(self, item: dict) -> dict:
        self._seq += 1
        item["key"] = f"i{self._seq}"
        self.items.append(item)
        return item

    def apply(self, upd: dict) -> None:
        kind = upd.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk", "user_message_chunk"):
            content = upd.get("content") or {}
            if content.get("type") != "text":
                return
            role = {"agent_message_chunk": "agent", "agent_thought_chunk": "thought"}.get(kind, "user")
            mid = upd.get("messageId")
            last = self.items[-1] if self.items else None
            if last and last["type"] == "text" and last["role"] == role and last["mid"] == mid:
                last["text"] += content["text"]
            else:
                self._add({"type": "text", "role": role, "mid": mid, "text": content["text"]})
        elif kind == "tool_call":
            item = self._add({"type": "tool", "id": upd["toolCallId"], "title": upd.get("title", ""),
                              "kind": upd.get("kind", "other"),
                              "status": upd.get("status", "pending"), "output": ""})
            self._tools[item["id"]] = item
        elif kind == "tool_call_update":
            item = self._tools.get(upd.get("toolCallId"))
            if item is None:
                return
            item["status"] = upd.get("status", item["status"])
            texts = [c["content"].get("text", "") for c in upd.get("content") or []
                     if c.get("type") == "content" and c["content"].get("type") == "text"]
            if texts:
                item["output"] = texts[-1]
        elif kind == "plan":
            self.plan = upd.get("entries") or []
        del self.items[:-HISTORY_ITEMS]

    # -- Tern view -----------------------------------------------------------
    def view(self, caps):
        from tern_sdk import ui

        meta = self.meta
        tokens = meta.get("tokens") or {}
        figures = [
            ("State", render.state_label(meta.get("state"))),
            ("Turn", str(meta.get("turn", "—"))),
            ("Tokens", render.num(tokens.get("total"))),
            ("Cost", render.money(tokens.get("cost")) if tokens.get("cost") is not None else "—"),
        ]
        if caps.draws("stats"):
            head = ui.stats([{"label": k, "value": v} for k, v in figures])
        else:
            extra = [("Model", meta.get("model") or "—"), ("PR", meta.get("pr_url") or "—")]
            head = ui.kv(figures + extra)
        body = [self._node(ui, caps, item) for item in self.items]
        if self.plan:
            body.append(ui.card(ui.md("\n".join(
                f"- [{'x' if e.get('status') == 'completed' else ' '}] {e.get('content', '')}"
                for e in self.plan)), head="Plan", key="plan"))
        request = meta.get("input_request")
        if request:
            options = "\n".join(f"{i}. {o}" for i, o in enumerate(request.get("options") or [], 1))
            ref = render.session_label(meta)
            body.append(ui.card(
                ui.md(f"{request.get('question', '')}\n\n{options}".strip()),
                ui.md(f"_Answer with `symphony answer {ref} <text>` or in the chat "
                      f"(`symphony chat {ref}`)._"),
                head="The agent asks", tone="warning", key="ask"))
        return ui.col(
            ui.card(head, head=render.session_title(meta), meta=meta.get("model"),
                    status=_STATE_STATUS.get(meta.get("state"), "pending"), key="head"),
            *body,
            ui.text("Press q or Esc to close.", tone="muted", key="legend"),
            gap="sm",
        )

    @staticmethod
    def _node(ui, caps, item: dict):
        key = item["key"]
        if item["type"] == "tool":
            status = _TOOL_STATUS.get(item["status"], "running")
            if caps.draws("tool"):
                child = ui.text(item["output"][-2000:], lines=8) if item["output"] else None
                return ui.tool(child, name=item["kind"], title=item["title"], status=status,
                               collapsible=True, collapsed=status == "done", key=key)
            return ui.text(f"▸ {item['title']} ({status})", key=key)
        if item["role"] == "thought":
            return ui.text(item["text"][-1500:], tone="muted", key=key)
        if item["role"] == "user":
            return ui.card(ui.md(item["text"]), head="Human", tone="user", key=key)
        return ui.md(item["text"], key=key) if caps.draws("md") else ui.text(item["text"], key=key)


class PlainPrinter:
    """Streams updates as text: message chunks inline, one line per tool call."""

    def __init__(self, out: IO[str]) -> None:
        self.out = out
        self._last: str | None = None
        self._titles: dict[str, str] = {}

    def _break(self, kind: str) -> None:
        if self._last is not None and self._last != kind:
            self.out.write("\n")
        self._last = kind

    def apply(self, upd: dict) -> None:
        kind = upd.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk", "user_message_chunk"):
            content = upd.get("content") or {}
            if content.get("type") != "text":
                return
            if self._last != kind:
                self._break(kind)
                if kind == "user_message_chunk":
                    self.out.write("\n> human: ")
                elif kind == "agent_thought_chunk":
                    self.out.write("(thinking) ")
            self.out.write(content["text"])
        elif kind == "tool_call":
            self._break("tool")
            self._titles[upd["toolCallId"]] = upd.get("title", "")
            self.out.write(f"▸ {upd.get('title', '')}\n")
            self._last = "tool_line"
        elif kind == "tool_call_update" and upd.get("status") in ("completed", "failed"):
            self._break("tool")
            mark = "✓" if upd["status"] == "completed" else "✗"
            self.out.write(f"  {mark} {self._titles.pop(upd.get('toolCallId'), '')}\n")
            self._last = "tool_line"
        self.out.flush()


def watch(api: Api, ref: str, *, as_json: bool, out: IO[str] = sys.stdout) -> int:
    meta = api.resolve(ref)
    doc = api.session(meta["sid"])
    meta = doc.get("meta") or meta
    tail = EventTail(api.cred, meta["sid"], doc.get("last_event_id"))
    if as_json:
        return _watch_json(tail, out)
    try:
        import tern_sdk

        session = tern_sdk.connect(app="symphony")
    except Exception:  # noqa: BLE001 - any TSP trouble means plain text
        session = None
    if session is None:
        return _watch_plain(tail, meta, doc, out)
    with session:
        return _watch_tern(session, api, tail, meta, doc)


def _watch_json(tail: EventTail, out: IO[str]) -> int:
    try:
        for ev in tail:
            out.write(json.dumps({"id": ev.id, "kind": ev.kind, "data": ev.data}, ensure_ascii=False) + "\n")
            out.flush()
    except KeyboardInterrupt:
        tail.stop()
    return 0


def _watch_plain(tail: EventTail, meta: dict, doc: dict, out: IO[str]) -> int:
    printer = PlainPrinter(out)
    mapper = acp_map.LiveMapper()
    mapper.meta = dict(meta)
    out.write(render.session_title(meta) + "\n" + "─" * 60 + "\n")
    for upd in acp_map.replay(doc.get("transcript") or [])[-HISTORY_ITEMS:]:
        printer.apply(upd)
    if meta.get("input_request"):
        printer.apply(acp_map.text_chunk(acp_map.question_block(meta["input_request"])))
    out.write("\n" + "─" * 60 + " live (Ctrl-C to quit)\n")
    out.flush()
    try:
        for ev in tail:
            for upd in mapper.map(ev.kind, ev.data):
                printer.apply(upd)
    except KeyboardInterrupt:
        tail.stop()
    except ApiError as exc:
        out.write(f"\nLive view stopped: {render.error_text(exc.code, exc.status, exc.message)}\n")
        return 1
    out.write("\n")
    return 0


# Meta (turn, tokens, PR) changes without an event of its own; these frames end a unit of work.
REFRESH_KINDS = {"turn_end", "agent_end", "symphony.state"}


def _watch_tern(session, api: Api, tail: EventTail, meta: dict, doc: dict) -> int:
    model = WatchModel(meta)
    mapper = acp_map.LiveMapper()
    mapper.meta = model.meta
    for upd in acp_map.replay(doc.get("transcript") or []):
        model.apply(upd)
    events: queue.Queue = queue.Queue()

    def pump() -> None:
        try:
            for ev in tail:
                events.put(ev)
                if ev.kind in REFRESH_KINDS:
                    events.put(("meta", api.session(meta["sid"]).get("meta") or {}))
        except ApiError as exc:
            events.put(exc)

    threading.Thread(target=pump, daemon=True, name="watch-sse").start()
    surface = session.open(mode="flow", title=f"symphony watch {render.session_label(meta)}",
                           font="sans")
    surface.render(model.view(session.caps))
    try:
        while True:
            dirty = False
            while True:
                try:
                    ev = events.get_nowait()
                except queue.Empty:
                    break
                dirty = True
                if isinstance(ev, ApiError):
                    model.apply(acp_map.note(
                        f"_Live view stopped: {render.error_text(ev.code, ev.status, ev.message)}_"))
                elif isinstance(ev, tuple):
                    model.meta.update(ev[1])
                else:
                    updates = mapper.map(ev.kind, ev.data)
                    # The pending question is drawn as its own card from `meta.input_request`.
                    if ev.kind != "symphony.input_requested":
                        for upd in updates:
                            model.apply(upd)
            if dirty:
                surface.render(model.view(session.caps))
            item = session.poll(0.05)
            if item is None:
                if session.closed:
                    break
                continue
            name = getattr(item, "name", None)
            if name in QUIT_KEYS or (name == "c" and getattr(item, "ctrl", False)):
                break
    except KeyboardInterrupt:
        pass
    finally:
        tail.stop()
    return 0
