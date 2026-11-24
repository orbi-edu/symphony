--!strict
-- The read projections: Repositories, Infrastructure, Telemetry, Learnings and Audit
-- (`GET /v1/repos|infra|telemetry|learning|audit`; shapes in src/symphony/api/board.py), plus
-- the Dashboard's provider rate limits. Every projection names its `sources`: one that is not
-- `fresh` means "unknown", so the view says so instead of showing an empty list as "none".
-- The Repositories view also lets admins configure per-repository settings.

local apimod = require("./api")
local fmt = require("./fmt")
local h = require("./ui")

local ui = tern.ui

local P = {}

local SOURCE = {
	state = "daemon state",
	workflow = "WORKFLOW.md",
	learning = "learnings",
	kubernetes = "Kubernetes",
	telemetry = "telemetry",
	metrics = "metrics",
}

-- A muted line per source that is stale or unavailable. The source's `note` is an English
-- diagnostic and is never shown: the status word says enough.
local function sources(doc: any): Node?
	local spans: { any } = {}
	for name, src in (type(doc.sources) == "table" and doc.sources) or {} do
		if type(src) == "table" and src.status ~= "fresh" then
			local word = if src.status == "stale" then "stale" else "unavailable"
			table.insert(spans, ui.span((SOURCE[name] or name) .. ": " .. word, if src.status == "stale" then "warning" else "error"))
			if src.observedAt then
				table.insert(spans, ui.span(" (" .. fmt.ago(src.observedAt) .. ")", "muted"))
			end
			table.insert(spans, ui.span("   "))
		end
	end
	if #spans == 0 then
		return nil
	end
	table.insert(spans, 1, ui.span("Sources  ", "muted"))
	return h.with(ui.text(spans), { key = "sources" })
end

local function truncated(doc: any): Node?
	if type(doc.limits) == "table" and doc.limits.truncated then
		return h.with(h.muted("List truncated. Symphony shows only the most recent items."), { key = "truncated" })
	end
	return nil
end

local function head(text: string, doc: any): Node
	return ui.text({ ui.span(text, "muted"), ui.span(if doc.observedAt then "  · read " .. fmt.ago(doc.observedAt) else "", "dim") })
end

-- Loading and error states shared by the projections.
local function guard(state: any, name: string, what: string): Node?
	local doc = state.data[name]
	if doc then
		return nil
	end
	if state.errs[name] then
		return h.error_card(name, "Could not load " .. what, state.errs[name], "refresh")
	end
	return h.loading(name, "Loading " .. what .. "…")
end

local function page(key: string, parts: { Node? }): Node
	local out: { Node } = {}
	for _, n in parts do
		if n then
			table.insert(out, n)
		end
	end
	return h.col(key, out, { gap = "md" })
end

local TONE = {
	healthy = "success", ok = "success", running = "success", ready = "success", bound = "success",
	fresh = "success", completed = "success", succeeded = "success", applied = "success",
	degraded = "warning", pending = "pending", stale = "warning", waiting = "pending",
	failed = "error", error = "error", unavailable = "error", down = "error",
	unknown = "muted",
}

local WORD = {
	healthy = "healthy", degraded = "degraded", unknown = "unknown", running = "running",
	pending = "pending", completed = "completed", failed = "failed", succeeded = "completed",
	ready = "ready", bound = "bound", error = "error", down = "down", stale = "stale",
}

-- The projections' codes (src/symphony/api/board.py), each to an English word and a tone. An
-- unknown code (a newer API) shows as itself, muted.
local function code(map: { [string]: { string } }, v: any): Spans
	local e = map[tostring(v or "")]
	if e then
		return { ui.span(e[1], e[2] or "muted") }
	end
	return { ui.span(if v == nil then "—" else tostring(v), "muted") }
end

local ROUTE = { issue = "tickets", retrospective = "retrospective", default = "default repository", installation = "GitHub App", workflow = "workflow" }

local ROLE = {
	redis = { "Redis" }, daemon = { "daemon" }, api = { "API" }, gateway = { "gateway" },
	merger = { "merger" }, runner = { "runner" }, workspace = { "workspace" }, sweep = { "cleanup" },
	other = { "other" },
}

-- Pod `component` labels: the role vocabulary plus the reflector job and session volumes.
local COMPONENT = {
	redis = { "Redis" }, daemon = { "daemon" }, api = { "API" }, gateway = { "gateway" },
	merger = { "merger" }, runner = { "runner" }, reflect = { "reflection runner" },
	workspace = { "workspace" }, ["workspace-sessions"] = { "workspace sessions" },
	sweep = { "cleanup" }, other = { "other" },
}

-- Service `detail`: redis and daemon heartbeat codes, or the pod code.
local DETAIL = {
	ok = { "ok", "success" }, stale = { "late", "warning" }, unreadable = { "unreadable", "error" },
	ready = { "ready", "success" }, not_ready = { "not ready", "warning" }, pending = { "pending", "pending" },
	failed = { "failed", "error" }, succeeded = { "completed", "success" }, unknown = { "unknown", "muted" },
	active = { "active", "accent" }, complete = { "completed", "success" },
	bound = { "bound", "success" }, lost = { "lost", "error" },
}

local function status(s: any): Spans
	local key = string.lower(tostring(s or "unknown"))
	return { ui.span(WORD[key] or tostring(s or "—"), TONE[key] or "muted") }
end

-- ── Repositories ──────────────────────────────────────────────────────────

-- The runtimes the server knows: pulled from the API when available, else these defaults.
P.KNOWN_RUNTIMES = { "node", "dotnet9", "dotnet10", "polyglot" }

-- Find a repo item by name in the repos doc.
function P.repo_items(state: any): { any }
	local doc = state.data.repos
	return (type(doc) == "table" and type(doc.items) == "table") and doc.items or {}
end

function P.find_repo(state: any, name: string?): any
	for _, r in P.repo_items(state) do
		if r.name == name then
			return r
		end
	end
	return nil
end

-- Whether a repo is read-only for configuration: non-admin, or archived.
function P.repo_readonly(state: any, r: any): boolean
	if not apimod.can(state.api, "admin") then
		return true
	end
	return r.archived == true
end

-- The form fields, prefilled from the configured value, else the detected one.
function P.repo_form_values(r: any): { [string]: string }
	local runtime = r.configured_runtime or r.detected_runtime or ""
	local base = r.base_branch or ""
	-- Components: "name=path@runtime" rows, one per component.
	local comp_text = ""
	if type(r.components) == "table" then
		local parts = {}
		for cname, cdef in r.components do
			local path = if type(cdef) == "table" then tostring(cdef.path or "") else ""
			local crt = if type(cdef) == "table" and cdef.runtime then "@" .. tostring(cdef.runtime) else ""
			table.insert(parts, tostring(cname) .. "=" .. path .. crt)
		end
		table.sort(parts)
		comp_text = table.concat(parts, " ")
	end
	return { runtime = tostring(runtime), base_branch = tostring(base), components = comp_text }
end

-- The detail card for a selected repository. `form` is the edit form state (nil when not editing).
local function repo_detail(state: any, r: any): Node
	if not r then
		return h.card("detail", { ui.span("No repository selected", "strong") }, {
			h.muted("Pick a repository in the table to see its configuration."),
		})
	end
	local readonly = P.repo_readonly(state, r)
	local form = state.repo_form
	local editing = form ~= nil and form.name == r.name

	-- Facts always shown.
	local routes = {}
	for _, rt in (type(r.routes) == "table" and r.routes) or {} do
		table.insert(routes, ROUTE[tostring(rt)] or tostring(rt))
	end
	local facts: { { Spans } } = {
		{ "Routes", if #routes > 0 then table.concat(routes, " · ") else { ui.span("none", "muted") } },
		{ "Tickets", { ui.span(if r.issues == nil then "?" else tostring(r.issues), "mono") } },
		{ "Merger", { ui.span(if r.merger_policy then "policy" elseif r.merger_policy == false then "—" else "?", "muted") } },
	}
	if type(r.policy_pr) == "string" and r.policy_pr ~= "" then
		local n = string.match(r.policy_pr, "/pull/(%d+)")
		table.insert(facts, { "Policy PR", { ui.link(if n then "#" .. n else "PR", r.policy_pr) } })
	end
	if type(r.provisioning_error) == "string" and r.provisioning_error ~= "" then
		table.insert(facts, { "Provisioning", { ui.span(r.provisioning_error, "error") } })
	end
	if r.default_branch then
		table.insert(facts, { "Default branch", { ui.span(tostring(r.default_branch), "mono muted") } })
	end
	if r.detected_runtime then
		table.insert(facts, { "Detected runtime", { ui.span(tostring(r.detected_runtime), "mono muted") } })
	end
	if not editing and r.base_branch then
		table.insert(facts, { "Base branch", { ui.span(tostring(r.base_branch), "mono") } })
	end
	if not editing then
		local runtime, runtime_tone = r.configured_runtime, "mono"
		if runtime == nil and r.detected_runtime ~= nil then
			runtime, runtime_tone = tostring(r.detected_runtime) .. " (detected)", "muted"
		elseif runtime == nil then
			runtime, runtime_tone = r.runtime or "—", "muted"
		end
		table.insert(facts, { "Runtime", { ui.span(tostring(runtime), runtime_tone) } })
	end
	if not editing and type(r.components) == "table" then
		for cname, cdef in r.components do
			local path = if type(cdef) == "table" then tostring(cdef.path or "") else ""
			local crt = if type(cdef) == "table" and cdef.runtime then " (" .. tostring(cdef.runtime) .. ")" else ""
			local source = if r.components_source == "detected" then "  detected" else ""
			table.insert(facts, { "Component", { ui.span(tostring(cname) .. " → " .. path .. crt, "mono"), ui.span(source, "muted") } })
		end
	end

	local body: { Node } = {}

	if editing then
		-- Edit form: runtime, base branch, components.
		local busy = state.busy.repo_save
		table.insert(body, ui.node("row", { key = "acts", gap = "sm", justify = "end", align = "center" }, {
			h.action_key("Cancel", "repo_cancel", "esc", "muted"),
			h.action_key(if busy then "Saving…" else "Save", "repo_save", "⏎", "accent"),
		}))
		if busy then
			table.insert(body, ui.node("spinner", { key = "busy", text = "Saving repository configuration…" }))
		end
		if form.error then
			table.insert(body, ui.text({ ui.span("Not saved", "strong error") }))
			table.insert(body, h.with(ui.code(form.error), { key = "why" }))
		end
		-- Runtime field
		local rt_head: Spans = { ui.span("Runtime", "strong") }
		if form.focus == "runtime" then
			table.insert(rt_head, ui.span("  ←→", "key"))
		end
		local rt_choices = {}
		for _, known in P.KNOWN_RUNTIMES do
			table.insert(rt_choices, { id = known, label = known })
		end
		local rt_val = form.values.runtime or ""
		-- If the current value is not in known runtimes, add it.
		local found_rt = false
		for _, c in rt_choices do
			if c.id == rt_val then found_rt = true end
		end
		if rt_val ~= "" and not found_rt then
			table.insert(rt_choices, 1, { id = rt_val, label = rt_val })
		end
		table.insert(rt_choices, { id = "", label = "auto-detect" })
		table.insert(body, h.col("runtime", {
			ui.text(rt_head),
			ui.node("tabs", { key = "seg", variant = "segmented", active = rt_val, items = rt_choices }),
			h.muted("The runtime the agent workspace uses for this repository."),
		}, { gap = "xs" }))
		-- Base branch field
		local bb_head: Spans = { ui.span("Base branch", "strong"), ui.span("  optional", "muted") }
		table.insert(body, h.col("base_branch", {
			ui.text(bb_head),
			ui.node("input", {
				key = "input",
				text = form.values.base_branch or "",
				cursor = if form.focus == "base_branch" then (utf8.len(form.values.base_branch or "") or 0) else nil,
				placeholder = r.default_branch or "main",
				mono = true,
				actions = { click = "repo_focus=base_branch" },
			}),
			h.muted("The branch Symphony treats as main. Empty: auto-detect from the remote."),
		}, { gap = "xs" }))
		-- Components field
		local comp_head: Spans = { ui.span("Components", "strong"), ui.span("  optional", "muted") }
		table.insert(body, h.col("components", {
			ui.text(comp_head),
			ui.node("input", {
				key = "input",
				text = form.values.components or "",
				cursor = if form.focus == "components" then (utf8.len(form.values.components or "") or 0) else nil,
				placeholder = "name=path@runtime, space-separated",
				mono = true,
				actions = { click = "repo_focus=components" },
			}),
			h.muted("Monorepo components: name=path@runtime, separated by spaces."),
		}, { gap = "xs" }))
		table.insert(body, h.with(ui.kv(facts), { key = "facts" }))
	else
		-- Read-only view with edit action.
		if not readonly then
			table.insert(body, ui.node("row", { key = "acts", gap = "sm", justify = "end", align = "center" }, {
				h.action_key("Configure", "repo_edit=" .. r.name, "e", "accent"),
			}))
		end
		table.insert(body, h.with(ui.kv(facts), { key = "facts" }))
	end

	local head_spans: Spans = {
		ui.span(tostring(r.name), "strong mono"),
		ui.span(if r.archived then "  archived" else "", if r.archived then "muted" else ""),
	}
	return h.card("detail", head_spans, body)
end

function P.repos(state: any): Node
	local g = guard(state, "repos", "the repositories")
	if g then
		return g
	end
	local doc = state.data.repos
	local sel = state.sel.repositories
	local rows = {}
	for _, r in doc.items or {} do
		local routes = {}
		for _, rt in (type(r.routes) == "table" and r.routes) or {} do
			table.insert(routes, ROUTE[tostring(rt)] or tostring(rt))
		end
		local comp_parts = {}
		if type(r.components) == "table" then
			for cname, _ in r.components do
				table.insert(comp_parts, tostring(cname))
			end
			table.sort(comp_parts)
		end
		-- What a new ticket gets: the workflow's runtime, else the one detected on GitHub.
		local runtime, runtime_tone = r.configured_runtime, "mono"
		if runtime == nil and r.detected_runtime ~= nil then
			runtime, runtime_tone = tostring(r.detected_runtime) .. " (detected)", "muted"
		elseif runtime == nil then
			runtime, runtime_tone = r.runtime or "—", "muted"
		end
		local comp_text = if #comp_parts > 0 then table.concat(comp_parts, ", ") else "—"
		if #comp_parts > 0 and r.components_source == "detected" then
			comp_text ..= " (detected)"
		end
		local base_text, base_tone = r.base_branch, "mono"
		if base_text == nil then
			base_text, base_tone = r.default_branch or "default", "muted"
		end
		local picked = r.name == sel
		table.insert(rows, {
			id = tostring(r.name),
			cells = {
				name = { ui.span(if picked then "▸ " else "  ", "accent"), ui.span(tostring(r.name) .. (if r.archived then " (archived)" else ""), if r.archived then "muted mono" elseif picked then "strong accent" else "strong mono") },
				base = { ui.span(tostring(base_text), base_tone) },
				runtime = { ui.span(tostring(runtime), runtime_tone) },
				components = { ui.span(comp_text, if #comp_parts > 0 then "mono" else "muted") },
				merger = { ui.span(if r.merger_policy then "policy" elseif r.merger_policy == false then "—" else "?", "muted") },
				route = { ui.span(if #routes > 0 then table.concat(routes, " · ") else "—", "muted") },
				issues = { ui.span(if r.issues == nil then "?" else tostring(r.issues), "mono") },
			},
			actions = { click = "repo_sel=" .. tostring(r.name) },
		})
	end
	local tbl = h.data_table({
		key = "t",
		cols = {
			{ id = "name", head = "Repository", grow = 1, mono = true, priority = 9 },
			{ id = "base", head = "Base branch", mono = true, priority = 6 },
			{ id = "runtime", head = "Runtime", mono = true, priority = 5 },
			{ id = "components", head = "Components", mono = true, priority = 4 },
			{ id = "merger", head = "Merger", priority = 3 },
			{ id = "route", head = "Arrives by", priority = 5 },
			{ id = "issues", head = "Tickets", align = "end", mono = true, priority = 7 },
		},
		rows = rows,
		empty = if doc.sources and doc.sources.state and doc.sources.state.status ~= "fresh"
			then "Repositories unknown: the daemon state is unavailable"
			else "No repositories configured in WORKFLOW.md",
	})
	local detail = repo_detail(state, P.find_repo(state, sel))
	h.with(detail, { min = { w = "44ch" }, max = { w = "56ch" }, basis = "content" })
	return page("repositories", {
		head("Repositories Symphony reaches and dispatches tickets to", doc),
		ui.node("row", { key = "md", gap = "lg", align = "start" }, {
			ui.node("col", { key = "list", grow = 1, basis = 0 }, { tbl }),
			detail,
		}),
		truncated(doc),
		sources(doc),
	})
end

-- ── Infrastructure ────────────────────────────────────────────────────────

local KIND = { Pod = "pod", Job = "job", PersistentVolumeClaim = "volume" }

function P.infra(state: any): Node
	local g = guard(state, "infra", "the infrastructure")
	if g then
		return g
	end
	local doc = state.data.infra
	local svc_rows = {}
	for _, s in doc.services or {} do
		table.insert(svc_rows, {
			id = tostring(s.name),
			cells = {
				name = { ui.span(tostring(s.name), "strong mono") },
				role = code(ROLE, s.role),
				status = status(s.status),
				detail = code(DETAIL, s.detail),
			},
		})
	end
	local res_rows = {}
	for i, r in doc.resources or {} do
		local detail = code(DETAIL, r.detail)
		if r.kind == "Job" and tonumber(r.activePods) then
			table.insert(detail, ui.span(" · " .. fmt.plural(tonumber(r.activePods) :: number, "pod", "pods"), "muted"))
		end
		table.insert(res_rows, {
			id = tostring(r.name or i),
			cells = {
				name = { ui.span(tostring(r.name), "mono") },
				kind = KIND[r.kind or ""] or tostring(r.kind or "—"),
				status = detail,
				component = code(COMPONENT, r.component),
				at = { ui.span(fmt.ago(r.createdAt), "muted") },
			},
		})
	end
	local beat = if doc.heartbeatAt then "last heartbeat " .. fmt.ago(doc.heartbeatAt) else "no daemon heartbeat"
	return page("infrastructure", {
		ui.text({
			ui.span(if doc.paused then "Daemon paused" else "Daemon dispatching", if doc.paused then "warning" else "success"),
			ui.span("  · " .. beat, "muted"),
			ui.span(if doc.observedAt then "  · read " .. fmt.ago(doc.observedAt) else "", "dim"),
		}),
		ui.section({ ui.span("Services", "strong") }, {
			h.data_table({
				key = "t",
				cols = {
					{ id = "name", head = "Service", grow = 1, mono = true, truncate = "middle", priority = 9 },
					{ id = "role", head = "Role", priority = 5 },
					{ id = "status", head = "State", priority = 8 },
					{ id = "detail", head = "Detail", priority = 4 },
				},
				rows = svc_rows,
				empty = "Services unknown",
			}),
		}),
		ui.section({ ui.span("Cluster resources", "strong") }, {
			h.data_table({
				key = "t",
				cols = {
					{ id = "name", head = "Resource", grow = 1, mono = true, truncate = "middle", priority = 9 },
					{ id = "kind", head = "Kind", priority = 7 },
					{ id = "status", head = "State", priority = 8 },
					{ id = "component", head = "Component", priority = 5 },
					{ id = "at", head = "Created", align = "end", mono = true, priority = 4 },
				},
				rows = res_rows,
				empty = if doc.sources and doc.sources.kubernetes and doc.sources.kubernetes.status ~= "fresh"
					then "Resources unknown: Kubernetes did not respond"
					else "No workspaces, jobs or volumes right now",
			}),
		}),
		truncated(doc),
		sources(doc),
	})
end

-- ── Telemetry ─────────────────────────────────────────────────────────────

function P.telemetry(state: any): Node
	local g = guard(state, "telemetry", "the telemetry")
	if g then
		return g
	end
	local doc = state.data.telemetry
	local t = type(doc.totals) == "table" and doc.totals or nil
	local parts: { Node? } = { head("Agent usage, from omp's native telemetry", doc) }
	if t then
		table.insert(parts, h.strip("totals", {
			{ "Input", { ui.span(fmt.count(t.input), "strong mono") } },
			{ "Output", { ui.span(fmt.count(t.output), "strong mono") } },
			{ "Cache (read)", { ui.span(fmt.count(t.cacheRead), "strong mono") } },
			{ "Estimated cost", { ui.span(fmt.money(t.costEstimate), "strong mono"), ui.span("  " .. fmt.count(t.total) .. " tokens", "muted") } },
		}))
	else
		table.insert(parts, h.muted("Totals unknown: telemetry is unavailable."))
	end
	local model_rows = {}
	for _, m in doc.models or {} do
		table.insert(model_rows, {
			id = tostring(m.model),
			cells = {
				model = { ui.span(tostring(m.model), "mono") },
				requests = { ui.span(fmt.count(m.requests), "mono") },
				input = { ui.span(fmt.count(m.input), "mono") },
				output = { ui.span(fmt.count(m.output), "mono") },
			},
		})
	end
	table.insert(parts, ui.section({ ui.span("By model", "strong") }, {
		h.data_table({
			key = "t",
			cols = {
				{ id = "model", head = "Model", grow = 1, mono = true, priority = 9 },
				{ id = "requests", head = "Requests", align = "end", mono = true, priority = 7 },
				{ id = "input", head = "Input", align = "end", mono = true, priority = 8 },
				{ id = "output", head = "Output", align = "end", mono = true, priority = 8 },
			},
			rows = model_rows,
			empty = "No requests recorded",
		}),
	}))
	local month_rows = {}
	for _, m in doc.monthly or {} do
		table.insert(month_rows, {
			id = tostring(m.month),
			cells = {
				month = { ui.span(tostring(m.month), "mono") },
				dispatched = { ui.span(tostring(m.dispatched or 0), "mono") },
				rework = { ui.span(tostring(m.rework or 0), "mono") },
				opened = { ui.span(tostring(m.prsOpened or 0), "mono") },
				merged = { ui.span(tostring(m.prsMerged or 0), "mono") },
				learning = { ui.span(tostring(m.learningPrs or 0), "mono") },
			},
		})
	end
	table.insert(parts, ui.section({ ui.span("By month", "strong") }, {
		h.data_table({
			key = "t",
			cols = {
				{ id = "month", head = "Month", grow = 1, mono = true, priority = 9 },
				{ id = "dispatched", head = "Dispatched", align = "end", mono = true, priority = 8 },
				{ id = "rework", head = "Rework", align = "end", mono = true, priority = 6 },
				{ id = "opened", head = "PRs opened", align = "end", mono = true, priority = 7 },
				{ id = "merged", head = "PRs merged", align = "end", mono = true, priority = 7 },
				{ id = "learning", head = "Learning PRs", align = "end", mono = true, priority = 5 },
			},
			rows = month_rows,
			empty = "No monthly history yet",
		}),
	}))
	local sess_rows = {}
	for i, s in doc.sessions or {} do
		if i > 10 then
			break
		end
		local word, tone = fmt.state(s.state)
		local tk = type(s.tokens) == "table" and s.tokens or {}
		table.insert(sess_rows, {
			id = tostring(s.sid),
			cells = {
				who = { ui.span(tostring(s.identifier or s.sid), "strong") },
				kind = { ui.span(fmt.KIND[s.kind or ""] or tostring(s.kind or "—"), "muted") },
				state = { ui.span(word, tone) },
				tokens = { ui.span(fmt.count(tk.total), "mono") },
				cost = { ui.span(fmt.money(tk.cost), "mono") },
				at = { ui.span(fmt.ago(s.updatedAt), "muted") },
			},
		})
	end
	table.insert(parts, ui.section({ ui.span("Recent sessions", "strong") }, {
		h.data_table({
			key = "t",
			cols = {
				{ id = "who", head = "Session", grow = 1, mono = true, priority = 9 },
				{ id = "kind", head = "Kind", priority = 5 },
				{ id = "state", head = "State", priority = 7 },
				{ id = "tokens", head = "Tokens", align = "end", mono = true, priority = 8 },
				{ id = "cost", head = "Cost", align = "end", mono = true, priority = 8 },
				{ id = "at", head = "Updated", align = "end", mono = true, priority = 4 },
			},
			rows = sess_rows,
			empty = "No sessions recorded",
		}),
	}))
	table.insert(parts, truncated(doc))
	table.insert(parts, sources(doc))
	return page("telemetry", parts)
end

-- ── Learnings ─────────────────────────────────────────────────────────────

local LEARN = { pending = { "pending", "pending" }, completed = { "completed", "success" }, failed = { "failed", "error" } }
local PROVENANCE = { retrospective = { "retrospective" }, retained = { "kept from the ticket" } }
local OUTCOME = {
	nothing_to_learn = { "nothing to learn", "muted" }, pr_opened = { "PR opened", "accent" },
	completed = { "learning applied", "success" }, failed = { "failed", "error" }, pending = { "in progress", "pending" },
}

function P.learning(state: any): Node
	local g = guard(state, "learning", "the learnings")
	if g then
		return g
	end
	local doc = state.data.learning
	local rows = {}
	for i, it in doc.items or {} do
		local w = LEARN[it.status or ""] or { tostring(it.status or "—"), "muted" }
		local pr: Spans = { ui.span("—", "dim") }
		if type(it.prUrl) == "string" then
			local n = string.match(it.prUrl, "/pull/(%d+)")
			pr = { ui.link(if n then "#" .. n else "PR", it.prUrl) }
		end
		table.insert(rows, {
			id = tostring(it.id or i),
			cells = {
				ticket = { ui.span(tostring(it.identifier or "—"), "strong") },
				repo = { ui.span(tostring(it.repo or "—"), "mono") },
				status = { ui.span(w[1], w[2]) },
				provenance = code(PROVENANCE, it.provenance),
				outcome = code(OUTCOME, it.outcome),
				pr = pr,
				at = { ui.span(fmt.ago(it.updatedAt), "muted") },
			},
		})
	end
	return page("learnings", {
		head("Retrospectives and learning PRs that reflection opens after merges", doc),
		h.data_table({
			key = "t",
			cols = {
				{ id = "ticket", head = "Ticket", mono = true, priority = 9 },
				{ id = "repo", head = "Repository", grow = 1, mono = true, priority = 10 },
				{ id = "status", head = "State", priority = 8 },
				{ id = "provenance", head = "Origin", priority = 4 },
				{ id = "outcome", head = "Result", priority = 7 },
				{ id = "pr", head = "PR", mono = true, priority = 7 },
				{ id = "at", head = "Updated", align = "end", mono = true, priority = 5 },
			},
			rows = rows,
			empty = if doc.sources and doc.sources.learning and doc.sources.learning.status ~= "fresh"
				then "Learnings unknown: the source is unavailable"
				else "No learnings yet. They appear after the first merges",
		}),
		truncated(doc),
		sources(doc),
	})
end

-- ── Audit ─────────────────────────────────────────────────────────────────

-- Every audited action (admin_commands.ACTIONS and STANDALONE_ACTIONS, plus provider saves).
local ACTION = {
	pause = "paused the daemon", resume = "resumed the daemon", refresh = "requested a refresh",
	reevaluate = "requested a re-evaluation", workflow = "published WORKFLOW.md",
	interrupt = "interrupted the attempt", hold = "took over the ticket", release = "returned the ticket",
	["teamconfig.publish"] = "published models", ["token.create"] = "created a token",
	["token.revoke"] = "revoked a token", ["merger.bypass"] = "bypassed the merger",
	["merger.requeue"] = "requeued a PR", ["merger.release"] = "released a PR",
	["session.message"] = "sent a message", ["session.interrupt"] = "interrupted",
	["session.answer"] = "answered", ["provider.save"] = "saved a provider",
	["admin.mcp.put"] = "saved an MCP server", ["admin.mcp.remove"] = "removed an MCP server",
	["admin.mcp.headers"] = "changed MCP server headers", ["admin.mcp.test"] = "tested an MCP server",
	["assistant.tickets_created"] = "assistant: tickets created",
	["assistant.team_fact_pr"] = "assistant: opened a team fact PR", ["assistant.memory_read"] = "read the assistant's memory",
	["assistant.memory_forget"] = "removed an assistant memory item", ["assistant.memory_wipe"] = "wiped a person's assistant memory",
	["setup.tracker"] = "set up the tracker", ["setup.github_app"] = "set up the GitHub App",
	["setup.repositories"] = "set the default repository", ["setup.notifications"] = "set up notifications",
	["setup.webhooks"] = "created webhook secrets", ["setup.assistant"] = "set up the assistant",
	["setup.skills"] = "changed skill sources",
}

local RESULT = {
	accepted = { "accepted", "success" }, succeeded = { "ok", "success" }, ok = { "ok", "success" },
	applied = { "applied", "success" }, applying = { "applying", "pending" }, attempted = { "attempted", "pending" },
	pending = { "pending", "pending" },
	failed = { "failed", "error" }, rejected = { "rejected", "error" }, unknown = { "uncertain", "warning" },
}

function P.audit(state: any): Node
	local g = guard(state, "audit", "the audit log")
	if g then
		return g
	end
	local doc = state.data.audit
	local rows = {}
	local items = state.audit_items or doc.items or {}
	for i, it in items do
		local r = RESULT[it.result or ""] or { tostring(it.result or "—"), "muted" }
		table.insert(rows, {
			id = tostring(it.id or i),
			cells = {
				at = { ui.span(fmt.ago(it.time), "muted") },
				actor = { ui.span(tostring(it.actor or "—"), "strong") },
				action = ACTION[it.action or ""] or tostring(it.action or "—"),
				target = { ui.span(if it.target == "namespace" then "all tickets" else tostring(it.target or "—"), "mono") },
				result = { ui.span(r[1], r[2]) },
				reason = fmt.cut(it.reason, 80),
			},
		})
	end
	local parts: { Node? } = {
		h.muted("Everything people and agents asked Symphony to do, newest first"),
		h.data_table({
			key = "t",
			cols = {
				{ id = "at", head = "When", mono = true, priority = 8 },
				{ id = "actor", head = "Who", priority = 9 },
				{ id = "action", head = "Action", priority = 9 },
				{ id = "target", head = "Target", mono = true, truncate = "middle", priority = 6 },
				{ id = "result", head = "Result", priority = 7 },
				{ id = "reason", head = "Reason", grow = 1, priority = 10 },
			},
			rows = rows,
			empty = "Nothing audited yet",
		}),
	}
	if state.audit_next then
		table.insert(parts, h.row("more", { h.action_key("Load more", "audit_more", "m") }))
	end
	return page("audit", parts)
end

-- ── Dashboard: provider rate limits ───────────────────────────────────────

-- Stages the runner and merger record (src/symphony/usage.py): omp's usage-limit stages and
-- provider throttling seen on a retry.
local STAGE = {
	low_priority = { "low priority", "warning" },
	wrap_up = { "wrapping up", "warning" },
	throttled = { "throttled", "error" },
}

function P.rate_limits(ov: any): Node?
	local list = type(ov.rate_limits) == "table" and ov.rate_limits or {}
	if #list == 0 then
		return nil
	end
	local rows = {}
	for i, r in list do
		local left: Spans
		if r.remaining ~= nil and r.limit ~= nil then
			local tone = if (tonumber(r.remaining) or 0) == 0 then "error" else "warning"
			-- usage-limit records carry the allowance left in percent (`limit` 100).
			local text = if tonumber(r.limit) == 100 and r.source == "usage_limit"
				then tostring(r.remaining) .. "% of quota"
				else tostring(r.remaining) .. " of " .. tostring(r.limit)
			left = { ui.span(text, "mono " .. tone) }
		elseif r.remaining ~= nil then
			left = { ui.span(tostring(r.remaining), "mono warning") }
		else
			left = { ui.span("exhausted", "error") }
		end
		local back = "—"
		if r.reset_at then
			back = fmt.clock(r.reset_at)
		elseif r.retry_after_ms then
			back = "in " .. math.max(1, math.floor(r.retry_after_ms / 1000)) .. " s"
		end
		table.insert(rows, {
			id = "r" .. i,
			cells = {
				model = { ui.span(tostring(r.model), "mono") },
				stage = code(STAGE, r.stage),
				left = left,
				back = back,
				seen = { ui.span(fmt.ago(r.observed_at) .. (if r.source == "retry" then " · on retry" else ""), "muted") },
			},
		})
	end
	return h.col("limits", {
		ui.text({ ui.span("Rate limits", "strong warning"), ui.span("  providers refusing or near the limit (last hour)", "muted") }),
		h.data_table({
			key = "t",
			cols = {
				{ id = "model", head = "Model", grow = 1, mono = true, priority = 9 },
				{ id = "stage", head = "Stage", priority = 5 },
				{ id = "left", head = "Remaining", align = "end", mono = true, priority = 8 },
				{ id = "back", head = "Resets", align = "end", mono = true, priority = 7 },
				{ id = "seen", head = "Seen", align = "end", mono = true, priority = 4 },
			},
			rows = rows,
		}),
	}, { gap = "sm" })
end

-- ── Session detail: evidence ───────────────────────────────────────────────

local EVIDENCE = { screenshot = "screenshot", video = "video", log = "log", report = "report" }

function P.evidence(meta: any): Node?
	local list = type(meta.evidence) == "table" and meta.evidence or {}
	if #list == 0 then
		return nil
	end
	local lines: { Node } = {}
	for i, e in list do
		if i > 8 then
			table.insert(lines, h.muted("… " .. (#list - 8) .. " more"))
			break
		end
		local title = fmt.cut(e.title or e.url or "evidence", 60)
		local spans: { any } = {}
		if type(e.url) == "string" and e.url ~= "" then
			table.insert(spans, ui.link(title, e.url))
		else
			table.insert(spans, ui.span(title))
		end
		table.insert(spans, ui.span("  " .. (EVIDENCE[e.kind or ""] or tostring(e.kind or "")), "muted"))
		table.insert(lines, h.with(ui.text(spans), { key = "e" .. i }))
	end
	return ui.section("Evidence", lines)
end

return P
