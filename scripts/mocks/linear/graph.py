"""GraphQL on Linear's published schema (`scripts/mocks/specs/linear.graphql`): parse, validate
and execute with graphql-core, resolving over `store.Store` rows.

Resolution order for a field of a stored entity: COMPUTED → ONE (to-one relation) → MANY
(connection) → the row's stored value → `Unmodelled` (a loud error, never invented data).
Root fields and mutations are explicit; anything else on Query/Mutation is `Unmodelled`.
"""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import graphql
from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLInputObjectType,
    GraphQLInt,
    GraphQLSchema,
    InlineFragmentNode,
    OperationDefinitionNode,
    get_named_type,
    is_leaf_type,
)

from .store import PRIORITY_LABELS, LinearError, Store, Unmodelled, invalid

SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "linear.graphql"
DEFAULT_PAGE = 50  # documented: "The first 50 results are returned by default"
MAX_COMPLEXITY = 10_000  # documented maximum complexity of a single query


@cache
def schema() -> GraphQLSchema:
    return graphql.build_schema(SPEC_PATH.read_text())


@dataclass
class Ctx:
    store: Store
    viewer: dict


ROOT = object()


# -- connections ---------------------------------------------------------------------------
class Conn:
    """A Relay connection over already-ordered rows (`first/after`, `last/before`)."""

    def __init__(self, rows: list[dict], first: int | None, after: str | None,
                 last: int | None, before: str | None) -> None:
        for name, value in (("first", first), ("last", last)):
            if value is not None and value < 0:
                raise invalid(f"{name} must be a positive number")
        start, end = 0, len(rows)
        ids = [r["id"] for r in rows]
        if after is not None:
            if after not in ids:
                raise invalid("the after cursor does not match an entity of this list")
            start = ids.index(after) + 1
        if before is not None:
            if before not in ids:
                raise invalid("the before cursor does not match an entity of this list")
            end = ids.index(before)
        if first is None and last is None:
            first = DEFAULT_PAGE
        if first is not None:
            end = min(end, start + first)
        if last is not None:
            start = max(start, end - last)
        self.page = rows[start:end]
        self.has_previous = start > 0
        self.has_next = end < len(rows)

    def field(self, name: str) -> Any:
        if name == "nodes":
            return self.page
        if name == "edges":
            return [{"node": r, "cursor": r["id"]} for r in self.page]
        if name == "pageInfo":
            return {"hasNextPage": self.has_next, "hasPreviousPage": self.has_previous,
                    "startCursor": self.page[0]["id"] if self.page else None,
                    "endCursor": self.page[-1]["id"] if self.page else None}
        raise Unmodelled(f"connection field {name}")


CONN_ARGS = {"first", "after", "last", "before", "filter", "includeArchived", "orderBy"}
# Accepted because the mock has nothing they would exclude: no sub-teams, no disabled users.
NEUTRAL_ARGS = {"includeSubTeams", "includeDisabled"}


def connect(ctx: Ctx, rows: list[dict], args: dict, filter_type: Any = None) -> Conn:
    for key, value in args.items():
        if value is not None and key not in CONN_ARGS | NEUTRAL_ARGS:
            raise Unmodelled(f"connection argument {key}")
    if not args.get("includeArchived"):
        rows = [r for r in rows if r.get("archivedAt") is None]
    flt = args.get("filter")
    if flt:
        rows = [r for r in rows if match(ctx, r, flt, filter_type)]
    # Default order is createdAt; both orders list the most recent first.
    key = "updatedAt" if args.get("orderBy") == "updatedAt" else "createdAt"
    rows = sorted(rows, key=lambda r: (r[key], r["_seq"]), reverse=True)
    return Conn(rows, args.get("first"), args.get("after"), args.get("last"), args.get("before"))


# -- relations -----------------------------------------------------------------------------
def _by(typename: str, key: str) -> Callable[[Store, dict], list[dict]]:
    return lambda s, row: [r for r in s.rows(typename, archived=True) if r[key] == row["id"]]


def _ids(typename: str, key: str) -> Callable[[Store, dict], list[dict]]:
    return lambda s, row: [r for r in (s.get(typename, i) for i in row[key]) if r is not None]


ONE: dict[tuple[str, str], tuple[str, str]] = {
    ("Issue", "team"): ("Team", "teamId"),
    ("Issue", "state"): ("WorkflowState", "stateId"),
    ("Issue", "parent"): ("Issue", "parentId"),
    ("Issue", "assignee"): ("User", "assigneeId"),
    ("Issue", "creator"): ("User", "creatorId"),
    ("Issue", "project"): ("Project", "projectId"),
    ("WorkflowState", "team"): ("Team", "teamId"),
    ("IssueLabel", "team"): ("Team", "teamId"),
    ("IssueLabel", "parent"): ("IssueLabel", "parentId"),
    ("IssueLabel", "creator"): ("User", "creatorId"),
    ("Comment", "issue"): ("Issue", "issueId"),
    ("Comment", "user"): ("User", "userId"),
    ("Comment", "parent"): ("Comment", "parentId"),
    ("Comment", "resolvingComment"): ("Comment", "resolvingCommentId"),
    ("Comment", "resolvingUser"): ("User", "resolvingUserId"),
    ("IssueRelation", "issue"): ("Issue", "issueId"),
    ("IssueRelation", "relatedIssue"): ("Issue", "relatedIssueId"),
    ("Attachment", "issue"): ("Issue", "issueId"),
    ("Attachment", "creator"): ("User", "creatorId"),
    ("Document", "issue"): ("Issue", "issueId"),
    ("Document", "project"): ("Project", "projectId"),
    ("Document", "creator"): ("User", "creatorId"),
    ("Document", "updatedBy"): ("User", "updatedById"),
    ("Project", "creator"): ("User", "creatorId"),
}

MANY: dict[tuple[str, str], Callable[[Store, dict], list[dict]]] = {
    ("Issue", "children"): _by("Issue", "parentId"),
    ("Issue", "comments"): _by("Comment", "issueId"),
    ("Issue", "attachments"): _by("Attachment", "issueId"),
    ("Issue", "documents"): _by("Document", "issueId"),
    ("Issue", "relations"): _by("IssueRelation", "issueId"),
    ("Issue", "inverseRelations"): _by("IssueRelation", "relatedIssueId"),
    ("Issue", "labels"): _ids("IssueLabel", "labelIds"),
    ("Issue", "subscribers"): _ids("User", "subscriberIds"),
    ("Team", "states"): _by("WorkflowState", "teamId"),
    ("Team", "labels"): _by("IssueLabel", "teamId"),
    ("Team", "issues"): _by("Issue", "teamId"),
    ("Team", "members"): _ids("User", "memberIds"),
    ("Team", "projects"): lambda s, row: [p for p in s.rows("Project", archived=True)
                                          if row["id"] in p["teamIds"]],
    ("WorkflowState", "issues"): _by("Issue", "stateId"),
    ("IssueLabel", "children"): _by("IssueLabel", "parentId"),
    ("IssueLabel", "issues"): lambda s, row: [i for i in s.rows("Issue", archived=True)
                                              if row["id"] in i["labelIds"]],
    ("Comment", "children"): _by("Comment", "parentId"),
    ("User", "assignedIssues"): _by("Issue", "assigneeId"),
    ("User", "createdIssues"): _by("Issue", "creatorId"),
    ("User", "teams"): lambda s, row: [t for t in s.rows("Team", archived=True)
                                       if row["id"] in t["memberIds"]],
    ("Organization", "teams"): lambda s, row: s.rows("Team", archived=True),
    ("Organization", "users"): lambda s, row: s.rows("User", archived=True),
    ("Organization", "labels"): lambda s, row: s.rows("IssueLabel", archived=True),
    ("Project", "issues"): _by("Issue", "projectId"),
    ("Project", "teams"): _ids("Team", "teamIds"),
    ("Project", "documents"): _by("Document", "projectId"),
}


COMPUTED: dict[tuple[str, str], Callable[[Ctx, dict, dict], Any]] = {
    ("Issue", "identifier"): lambda c, r, a: c.store.identifier(r),
    ("Issue", "url"): lambda c, r, a: c.store.issue_url(r),
    ("Issue", "branchName"): lambda c, r, a: c.store.branch_name(r, c.viewer),
    ("Issue", "priorityLabel"): lambda c, r, a: PRIORITY_LABELS[int(r["priority"])],
    ("Issue", "boardOrder"): lambda c, r, a: r["sortOrder"],
    ("Comment", "url"): lambda c, r, a: c.store.comment_url(r),
    ("Document", "url"): lambda c, r, a: c.store.document_url(r),
    ("Project", "url"): lambda c, r, a: c.store.project_url(r),
    ("User", "url"): lambda c, r, a: c.store.user_url(r),
    ("User", "isMe"): lambda c, r, a: r["id"] == c.viewer["id"],
    ("User", "initials"): lambda c, r, a: "".join(p[0] for p in r["name"].split()[:2]).upper(),
    ("User", "organization"): lambda c, r, a: c.store.org,
    ("User", "createdIssueCount"): lambda c, r, a: sum(
        1 for i in c.store.rows("Issue", archived=True) if i["creatorId"] == r["id"]),
    ("Team", "organization"): lambda c, r, a: c.store.org,
    ("Team", "issueCount"): lambda c, r, a: sum(
        1 for i in c.store.rows("Issue", archived=bool(a.get("includeArchived")))
        if i["teamId"] == r["id"]),
    ("Team", "defaultIssueState"): lambda c, r, a: min(
        (s for s in c.store.rows("WorkflowState") if s["teamId"] == r["id"]
         and s["type"] == "backlog"), key=lambda s: s["position"], default=None),
    ("Team", "markedAsDuplicateWorkflowState"): lambda c, r, a: next(
        (s for s in c.store.rows("WorkflowState") if s["teamId"] == r["id"]
         and s["type"] == "duplicate"), None),
    ("Team", "triageIssueState"): lambda c, r, a: None,  # triage is disabled on mock teams
    ("Team", "activeCycle"): lambda c, r, a: None,  # cycles are disabled on mock teams
    ("IssueLabel", "organization"): lambda c, r, a: c.store.org,
    ("Organization", "userCount"): lambda c, r, a: len(c.store.rows("User")),
    ("Organization", "createdIssueCount"): lambda c, r, a: len(
        c.store.rows("Issue", archived=True)),
}


def resolve_field(source: Any, info: graphql.GraphQLResolveInfo, **args: Any) -> Any:
    ctx: Ctx = info.context
    parent = info.parent_type.name
    name = info.field_name
    if source is ROOT:
        fn = ROOTS.get((parent, name))
        if fn is None:
            raise Unmodelled(f"{parent}.{name}")
        if parent != "Mutation":
            return fn(ctx, info, **args)
        # A failed mutation leaves no trace, like Linear's transaction per mutation.
        snapshot, events = copy.deepcopy(ctx.store.data), len(ctx.store.events)
        try:
            return fn(ctx, info, **args)
        except Exception:
            ctx.store.data = snapshot
            del ctx.store.events[events:]
            raise
    if isinstance(source, Conn):
        return source.field(name)
    if not isinstance(source, dict):
        raise Unmodelled(f"{parent}.{name}")
    typename = source.get("__typename")
    if typename is None:  # payloads and value objects built by the resolvers
        if name in source:
            return source[name]
        raise Unmodelled(f"{parent}.{name}")
    return entity_field(ctx, source, name, args, info)


def entity_field(ctx: Ctx, row: dict, name: str, args: dict, info: Any = None) -> Any:
    typename = row["__typename"]
    key = (typename, name)
    if key in COMPUTED:
        return COMPUTED[key](ctx, row, args)
    if key in ONE:
        target, fk = ONE[key]
        return ctx.store.get(target, row[fk])
    if key in MANY:
        rows = MANY[key](ctx.store, row)
        if info is None:  # filter evaluation: the whole live collection
            return [r for r in rows if r.get("archivedAt") is None]
        filter_arg = info.parent_type.fields[name].args.get("filter")
        return connect(ctx, rows, args, filter_arg.type if filter_arg else None)
    if name in row and not name.startswith("_"):
        if args and any(v is not None for v in args.values()):
            raise Unmodelled(f"arguments of {typename}.{name}")
        return row[name]
    raise Unmodelled(f"{typename}.{name}")


def resolve_type(value: Any, info: Any, abstract: Any) -> str | None:
    return value.get("__typename") if isinstance(value, dict) else None


# -- filters -------------------------------------------------------------------------------
_DURATION = re.compile(r"(-)?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def _instant(value: Any) -> datetime:
    text = str(value)
    m = _DURATION.fullmatch(text)
    if m and text not in ("P", "-P"):
        sign = -1 if m.group(1) else 1
        weeks, days, hours, minutes, seconds = (int(g or 0) for g in m.groups()[1:])
        delta = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
        return datetime.now(UTC) + sign * delta
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00Z"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise invalid(f"{value!r} is not an ISO 8601 date or duration") from None


def _fold(text: str, accents: bool = False) -> str:
    text = text.lower()
    if accents:
        text = "".join(c for c in unicodedata.normalize("NFKD", text)
                       if not unicodedata.combining(c))
    return text


def _ref_ids(ctx: Ctx, comparator: str, value: Any) -> Any:
    """IssueIDComparator takes an issue UUID or identifier; EntityIdentifierIDComparator a
    project UUID or slugId."""
    def one(v: Any) -> Any:
        if comparator == "IssueIDComparator":
            row = ctx.store.find_issue(v)
            return row["id"] if row else v
        if comparator == "EntityIdentifierIDComparator":
            row = next((p for p in ctx.store.rows("Project", archived=True)
                        if v in (p["id"], p["slugId"])), None)
            return row["id"] if row else v
        return v
    return [one(v) for v in value] if isinstance(value, list) else one(value)


def compare(ctx: Ctx, value: Any, cond: dict, comparator: str) -> bool:
    known = {"StringComparator", "NullableStringComparator", "IDComparator", "IssueIDComparator",
             "EntityIdentifierIDComparator", "NumberComparator", "NullableNumberComparator",
             "DateComparator", "NullableDateComparator", "BooleanComparator"}
    if comparator not in known:
        raise Unmodelled(comparator)
    dates = comparator in ("DateComparator", "NullableDateComparator")
    for op, arg in cond.items():
        if op == "null":
            if arg is not None and (value is None) != arg:
                return False
            continue
        if arg is None:
            continue
        if value is None:  # SQL semantics: a comparison with NULL never matches
            return False
        arg = _ref_ids(ctx, comparator, arg)
        v = _instant(value) if dates else value
        a = ([_instant(x) for x in arg] if isinstance(arg, list) else _instant(arg)) if dates else arg
        if op == "eq":
            ok = v == a
        elif op == "neq":
            ok = v != a
        elif op == "in":
            ok = v in a
        elif op == "nin":
            ok = v not in a
        elif op in ("gt", "gte", "lt", "lte"):
            ok = {"gt": v > a, "gte": v >= a, "lt": v < a, "lte": v <= a}[op]
        elif op in _STRING_OPS:
            ok = _STRING_OPS[op](str(v), str(a))
        else:
            raise Unmodelled(f"{comparator}.{op}")
        if not ok:
            return False
    return True


_STRING_OPS: dict[str, Callable[[str, str], bool]] = {
    "eqIgnoreCase": lambda v, a: _fold(v) == _fold(a),
    "neqIgnoreCase": lambda v, a: _fold(v) != _fold(a),
    "contains": lambda v, a: a in v,
    "containsIgnoreCase": lambda v, a: _fold(a) in _fold(v),
    "containsIgnoreCaseAndAccent": lambda v, a: _fold(a, True) in _fold(v, True),
    "notContains": lambda v, a: a not in v,
    "notContainsIgnoreCase": lambda v, a: _fold(a) not in _fold(v),
    "startsWith": lambda v, a: v.startswith(a),
    "startsWithIgnoreCase": lambda v, a: _fold(v).startswith(_fold(a)),
    "notStartsWith": lambda v, a: not v.startswith(a),
    "endsWith": lambda v, a: v.endswith(a),
    "notEndsWith": lambda v, a: not v.endswith(a),
}


def match(ctx: Ctx, row: dict, flt: dict, ftype: Any) -> bool:
    ftype = get_named_type(ftype)
    obj = schema().type_map[row["__typename"]]
    for key, cond in flt.items():
        if cond is None:
            continue
        if key == "and":
            if not all(match(ctx, row, c, ftype) for c in cond):
                return False
            continue
        if key == "or":
            if not any(match(ctx, row, c, ftype) for c in cond):
                return False
            continue
        if key == "null":  # handled by the relation that holds this filter
            continue
        sub = get_named_type(ftype.fields[key].type)
        if key not in obj.fields:
            raise Unmodelled(f"{ftype.name}.{key} filter")
        value = entity_field(ctx, row, key, {})
        if sub.name.endswith("Comparator"):
            if not compare(ctx, value, cond, sub.name):
                return False
        elif isinstance(value, list):
            if not match_collection(ctx, value, cond, sub):
                return False
        elif value is None:
            if cond.get("null") is not True:
                return False
        elif cond.get("null") is True or not match(ctx, value, cond, sub):
            return False
    return True


def match_collection(ctx: Ctx, rows: list[dict], cond: dict, ctype: GraphQLInputObjectType) -> bool:
    item_type = ctype.fields["some"].type
    for key, value in cond.items():
        if value is None:
            continue
        if key == "some":
            ok = any(match(ctx, r, value, item_type) for r in rows)
        elif key == "every":
            ok = all(match(ctx, r, value, item_type) for r in rows)
        elif key == "length":
            ok = compare(ctx, len(rows), value, "NumberComparator")
        elif key == "null":
            ok = (not rows) == value
        elif key == "and":
            ok = all(match_collection(ctx, rows, c, ctype) for c in value)
        elif key == "or":
            ok = any(match_collection(ctx, rows, c, ctype) for c in value)
        else:  # a field of the item: some item matches it
            ok = any(match(ctx, r, {key: value}, item_type) for r in rows)
        if not ok:
            return False
    return True


# -- root fields ---------------------------------------------------------------------------
def _list(typename: str, root_field: str) -> Callable[..., Conn]:
    def resolver(ctx: Ctx, info: Any, **args: Any) -> Conn:
        filter_arg = info.parent_type.fields[root_field].args.get("filter")
        return connect(ctx, ctx.store.rows(typename, archived=True), args,
                       filter_arg.type if filter_arg else None)
    return resolver


def _one(typename: str) -> Callable[..., dict]:
    def resolver(ctx: Ctx, info: Any, id: str) -> dict:
        return ctx.store.require(typename, id)
    return resolver


def _issue(ctx: Ctx, info: Any, id: str) -> dict:
    return ctx.store.require_issue(id)


def _team(ctx: Ctx, info: Any, id: str) -> dict:
    return ctx.store.require("Team", id)


def _user(ctx: Ctx, info: Any, id: str) -> dict:
    return ctx.viewer if id == "me" else ctx.store.require("User", id)


def _comment(ctx: Ctx, info: Any, id: str | None = None, hash: str | None = None) -> dict:
    if hash is not None:
        raise Unmodelled("Query.comment(hash)")
    if id is None:
        raise invalid("id is required")
    return ctx.store.require("Comment", id)


def _attachments_for_url(ctx: Ctx, info: Any, url: str, **args: Any) -> Conn:
    rows = [a for a in ctx.store.rows("Attachment", archived=True) if a["url"] == url]
    return connect(ctx, rows, args)


def _payload(ctx: Ctx, key: str, entity: Any) -> dict:
    return {"success": True, "lastSyncId": ctx.store.sync_id(), key: entity}


def _issue_create(ctx: Ctx, info: Any, input: dict) -> dict:
    return _payload(ctx, "issue", ctx.store.create_issue(ctx.viewer, input))


def _issue_update(ctx: Ctx, info: Any, id: str, input: dict) -> dict:
    return _payload(ctx, "issue", ctx.store.update_issue(ctx.viewer, id, input))


def _issue_add_label(ctx: Ctx, info: Any, id: str, labelId: str) -> dict:
    return _payload(ctx, "issue", ctx.store.set_labels(ctx.viewer, id, [labelId], []))


def _issue_remove_label(ctx: Ctx, info: Any, id: str, labelId: str) -> dict:
    return _payload(ctx, "issue", ctx.store.set_labels(ctx.viewer, id, [], [labelId]))


def _issue_delete(ctx: Ctx, info: Any, id: str, permanentlyDelete: bool | None = None) -> dict:
    if permanentlyDelete and not ctx.viewer["admin"]:
        raise LinearError("Forbidden", type="forbidden", code="FORBIDDEN", status=403,
                          presentable="Only admins can permanently delete issues.")
    issue = ctx.store.archive_issue(ctx.viewer, id, trash=True, permanently=bool(permanentlyDelete))
    return _payload(ctx, "entity", None if permanentlyDelete else issue)


def _issue_archive(ctx: Ctx, info: Any, id: str, trash: bool | None = None) -> dict:
    return _payload(ctx, "entity", ctx.store.archive_issue(ctx.viewer, id, trash=bool(trash)))


def _issue_unarchive(ctx: Ctx, info: Any, id: str) -> dict:
    return _payload(ctx, "entity", ctx.store.unarchive_issue(ctx.viewer, id))


def _comment_create(ctx: Ctx, info: Any, input: dict) -> dict:
    return _payload(ctx, "comment", ctx.store.create_comment(ctx.viewer, input))


def _comment_update(ctx: Ctx, info: Any, id: str, input: dict,
                    skipEditedAt: bool | None = None) -> dict:
    return _payload(ctx, "comment", ctx.store.update_comment(ctx.viewer, id, input,
                                                             skip_edited_at=bool(skipEditedAt)))


def _comment_delete(ctx: Ctx, info: Any, id: str) -> dict:
    return _payload(ctx, "entityId", ctx.store.delete_comment(ctx.viewer, id))


def _relation_create(ctx: Ctx, info: Any, input: dict, overrideCreatedAt: Any = None) -> dict:
    if overrideCreatedAt is not None:
        raise Unmodelled("issueRelationCreate(overrideCreatedAt)")
    return _payload(ctx, "issueRelation", ctx.store.create_relation(ctx.viewer, input))


def _relation_delete(ctx: Ctx, info: Any, id: str) -> dict:
    return _payload(ctx, "entityId", ctx.store.delete_relation(id))


def _label_create(ctx: Ctx, info: Any, input: dict, replaceTeamLabels: bool | None = None) -> dict:
    if replaceTeamLabels:
        raise Unmodelled("issueLabelCreate(replaceTeamLabels)")
    allowed = {"id", "name", "teamId", "color", "description", "parentId", "isGroup"}
    extra = sorted(k for k, v in input.items() if k not in allowed and v is not None)
    if extra:
        raise Unmodelled(f"IssueLabelCreateInput.{extra[0]}")
    label = ctx.store.add_label(
        input.get("teamId"), input["name"], actor=ctx.viewer, color=input.get("color"),
        description=input.get("description"), parent_id=input.get("parentId"),
        is_group=bool(input.get("isGroup")), entity_id=input.get("id"))
    return _payload(ctx, "issueLabel", label)


def _attachment_create(ctx: Ctx, info: Any, input: dict) -> dict:
    return _payload(ctx, "attachment", ctx.store.create_attachment(ctx.viewer, input))


def _attachment_update(ctx: Ctx, info: Any, id: str, input: dict) -> dict:
    return _payload(ctx, "attachment", ctx.store.update_attachment(ctx.viewer, id, input))


def _attachment_delete(ctx: Ctx, info: Any, id: str) -> dict:
    return _payload(ctx, "entityId", ctx.store.delete_attachment(ctx.viewer, id))


def _document_create(ctx: Ctx, info: Any, input: dict) -> dict:
    return _payload(ctx, "document", ctx.store.create_document(ctx.viewer, input))


def _document_update(ctx: Ctx, info: Any, id: str, input: dict) -> dict:
    return _payload(ctx, "document", ctx.store.update_document(ctx.viewer, id, input))


def _file_upload(ctx: Ctx, info: Any, contentType: str, filename: str, size: int,
                 makePublic: bool | None = None, metaData: Any = None) -> dict:
    from .server import upload_file  # the signed URLs belong to the HTTP layer

    record = ctx.store.create_upload(ctx.viewer, contentType, filename, size,
                                     bool(makePublic), metaData)
    return _payload(ctx, "uploadFile", upload_file(ctx.store, record))


ROOTS: dict[tuple[str, str], Callable[..., Any]] = {
    ("Query", "issues"): _list("Issue", "issues"),
    ("Query", "issue"): _issue,
    ("Query", "teams"): _list("Team", "teams"),
    ("Query", "team"): _team,
    ("Query", "issueLabels"): _list("IssueLabel", "issueLabels"),
    ("Query", "issueLabel"): _one("IssueLabel"),
    ("Query", "workflowStates"): _list("WorkflowState", "workflowStates"),
    ("Query", "workflowState"): _one("WorkflowState"),
    ("Query", "viewer"): lambda ctx, info: ctx.viewer,
    ("Query", "users"): _list("User", "users"),
    ("Query", "user"): _user,
    ("Query", "comments"): _list("Comment", "comments"),
    ("Query", "comment"): _comment,
    ("Query", "attachments"): _list("Attachment", "attachments"),
    ("Query", "attachment"): _one("Attachment"),
    ("Query", "attachmentsForURL"): _attachments_for_url,
    ("Query", "documents"): _list("Document", "documents"),
    ("Query", "document"): _one("Document"),
    ("Query", "issueRelations"): _list("IssueRelation", "issueRelations"),
    ("Query", "issueRelation"): _one("IssueRelation"),
    ("Query", "projects"): _list("Project", "projects"),
    ("Query", "project"): _one("Project"),
    ("Query", "organization"): lambda ctx, info: ctx.store.org,
    ("Mutation", "issueCreate"): _issue_create,
    ("Mutation", "issueUpdate"): _issue_update,
    ("Mutation", "issueAddLabel"): _issue_add_label,
    ("Mutation", "issueRemoveLabel"): _issue_remove_label,
    ("Mutation", "issueDelete"): _issue_delete,
    ("Mutation", "issueArchive"): _issue_archive,
    ("Mutation", "issueUnarchive"): _issue_unarchive,
    ("Mutation", "commentCreate"): _comment_create,
    ("Mutation", "commentUpdate"): _comment_update,
    ("Mutation", "commentDelete"): _comment_delete,
    ("Mutation", "issueRelationCreate"): _relation_create,
    ("Mutation", "issueRelationDelete"): _relation_delete,
    ("Mutation", "issueLabelCreate"): _label_create,
    ("Mutation", "attachmentCreate"): _attachment_create,
    ("Mutation", "attachmentUpdate"): _attachment_update,
    ("Mutation", "attachmentDelete"): _attachment_delete,
    ("Mutation", "documentCreate"): _document_create,
    ("Mutation", "documentUpdate"): _document_update,
    ("Mutation", "fileUpload"): _file_upload,
}


# -- complexity ----------------------------------------------------------------------------
def complexity(doc: graphql.DocumentNode, op: OperationDefinitionNode, variables: dict) -> int:
    """Linear's documented formula: each property 0.1, each object 1, and a connection multiplies
    its children by `first`/`last` (default 50); rounded up."""
    sch = schema()
    fragments = {d.name.value: d for d in doc.definitions if isinstance(d, FragmentDefinitionNode)}

    def page_size(node: FieldNode) -> int:
        for arg in node.arguments or ():
            if arg.name.value in ("first", "last"):
                value = graphql.value_from_ast(arg.value, GraphQLInt, variables)
                if isinstance(value, int):
                    return value
        return DEFAULT_PAGE

    def cost(selection_set: Any, parent: Any) -> float:
        total = 0.0
        for sel in selection_set.selections if selection_set else ():
            if isinstance(sel, FieldNode):
                name = sel.name.value
                if name.startswith("__") or not hasattr(parent, "fields"):
                    continue
                target = get_named_type(parent.fields[name].type)
                if is_leaf_type(target):
                    total += 0.1
                elif target.name.endswith("Connection"):
                    size = page_size(sel)
                    for inner in sel.selection_set.selections:
                        sub = cost(graphql.SelectionSetNode(selections=(inner,)), target)
                        is_page = isinstance(inner, FieldNode) and inner.name.value == "pageInfo"
                        total += sub if is_page else size * sub
                else:
                    total += 1 + cost(sel.selection_set, target)
            elif isinstance(sel, InlineFragmentNode):
                cond = sch.type_map[sel.type_condition.name.value] if sel.type_condition else parent
                total += cost(sel.selection_set, cond)
            elif isinstance(sel, FragmentSpreadNode):
                frag = fragments[sel.name.value]
                total += cost(frag.selection_set, sch.type_map[frag.type_condition.name.value])
        return total

    root = sch.mutation_type if op.operation.value == "mutation" else sch.query_type
    return math.ceil(round(cost(op.selection_set, root), 6))


# -- request execution ---------------------------------------------------------------------
@dataclass
class Outcome:
    status: int
    body: dict
    complexity: int | None = None
    operation: str | None = None
    mutated: bool = False


def _graphql_error(err: GraphQLError, *, code: str, status: int, kind: str = "graphql error") -> dict:
    out = err.formatted
    out["extensions"] = {"type": kind, "code": code, "statusCode": status, "userError": True,
                         "userPresentableMessage": err.message, "http": {"status": status}}
    return out


def _execution_error(err: GraphQLError) -> tuple[dict, int]:
    original = err.original_error
    out = err.formatted
    if isinstance(original, LinearError):
        out["message"] = original.message
        out["extensions"] = original.extensions()
        return out, original.status
    if original is None or isinstance(original, GraphQLError):  # coercion, operation selection
        return _graphql_error(err, code="BAD_USER_INPUT", status=400), 400
    out["extensions"] = {"type": "internal error", "code": "INTERNAL_SERVER_ERROR",
                         "statusCode": 500, "userError": False,
                         "userPresentableMessage": "An internal error occurred.",
                         "http": {"status": 500}}
    return out, 500


def run(store: Store, viewer: dict, query: Any, variables: Any, operation_name: Any) -> Outcome:
    if not isinstance(query, str) or not query.strip():
        err = GraphQLError("GraphQL operations must contain a non-empty `query`.")
        return Outcome(400, {"errors": [_graphql_error(err, code="BAD_REQUEST", status=400)]})
    if variables is not None and not isinstance(variables, dict):
        err = GraphQLError("`variables` must be an object.")
        return Outcome(400, {"errors": [_graphql_error(err, code="BAD_REQUEST", status=400)]})
    sch = schema()
    try:
        doc = graphql.parse(query)
    except GraphQLError as err:
        return Outcome(400, {"errors": [_graphql_error(err, code="GRAPHQL_PARSE_FAILED", status=400)]})
    errors = graphql.validate(sch, doc)
    if errors:
        return Outcome(400, {"errors": [_graphql_error(e, code="GRAPHQL_VALIDATION_FAILED",
                                                       status=400) for e in errors]})
    ops = [d for d in doc.definitions if isinstance(d, OperationDefinitionNode)]
    if operation_name:
        op = next((o for o in ops if o.name and o.name.value == operation_name), None)
        problem = f'Unknown operation named "{operation_name}".'
    else:
        op = ops[0] if len(ops) == 1 else None
        problem = "Must provide operation name if query contains multiple operations."
    if op is None:
        err = GraphQLError(problem)
        return Outcome(400, {"errors": [_graphql_error(err, code="BAD_USER_INPUT", status=400)]})
    try:
        points = complexity(doc, op, variables or {})
    except (GraphQLError, TypeError, ValueError):
        points = None  # mistyped variables: execution reports them below
    if points is not None and points > MAX_COMPLEXITY:
        err = LinearError(f"Query too complex: complexity {points} exceeds the maximum of "
                          f"{MAX_COMPLEXITY}")
        return Outcome(400, {"errors": [{"message": err.message, "extensions": err.extensions()}]},
                       points)
    mutation = op.operation.value == "mutation"
    with store.lock:
        ctx = Ctx(store, viewer)
        result = graphql.execute_sync(sch, doc, ROOT, ctx, variables or {}, operation_name,
                                      field_resolver=resolve_field, type_resolver=resolve_type)
        body: dict[str, Any] = {}
        status = 200
        if result.errors:
            formatted = [_execution_error(e) for e in result.errors]
            body["errors"] = [f for f, _ in formatted]
            if any(f["extensions"]["code"] == "MOCK_NOT_IMPLEMENTED" for f, _ in formatted):
                status = 501
            elif result.data is None:
                status = formatted[0][1]
        body["data"] = result.data
        return Outcome(status, body, points, op.name.value if op.name else None,
                       mutated=mutation)
