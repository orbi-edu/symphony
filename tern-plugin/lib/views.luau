--!strict
-- Rendering of the `symphony.app` block: one page with a head row, a tab strip and the active
-- view (Dashboard, Sessions, Tickets, Merger, Repositories, Infrastructure, Telemetry, Learnings,
-- Audit, Admin). Pure functions of the state built in app.luau.

local fmt = require("./fmt")
local h = require("./ui")
local apimod = require("./api")
local P = require("./projections")

local ui = tern.ui

local V = {}

V.VIEWS = { "dashboard", "sessions", "tickets", "merger", "repositories", "infrastructure", "telemetry", "learnings", "audit", "admin" }
V.ADMIN_TABS = { "models", "providers", "tokens", "mcp", "skills", "omp", "assistant" }
V.FILTERS = { "active", "input", "ended", "all" }

-- Stable node keys from ids that may hold ":" or "." (session ids, consumers).
function V.key(id: string): string
	return "k" .. (string.gsub(id, "[^%w]", "_"))
end

local function host_of(url: string): string
	return string.match(url, "^%a+://([^/]+)") or url
end

local function sessions(state: any): { any }
	local doc = state.data.sessions
	return if doc and type(doc.items) == "table" then doc.items else {}
end

local function active(s: any): boolean
	return s.state ~= "ended"
end

local ORDER = { waiting_input = 1, running = 2, starting = 3, idle = 4, ended = 5 }

-- Active sessions: waiting first, then running, newest first within a state.
function V.active_sessions(state: any): { any }
	local out = {}
	for _, s in sessions(state) do
		if active(s) then
			table.insert(out, s)
		end
	end
	table.sort(out, function(a, b)
		local ra, rb = ORDER[a.state] or 9, ORDER[b.state] or 9
		if ra ~= rb then
			return ra < rb
		end
		return (fmt.to_ms(a.updated_at) or 0) > (fmt.to_ms(b.updated_at) or 0)
	end)
	return out
end

function V.filtered_sessions(state: any): { any }
	local f = state.filter
	if f == "active" then
		return V.active_sessions(state)
	end
	local out = {}
	for _, s in sessions(state) do
		if f == "all" or (f == "input" and s.state == "waiting_input") or (f == "ended" and s.state == "ended") then
			table.insert(out, s)
		end
	end
	return out
end

function V.find_session(state: any, sid: string?): any
	if not sid then
		return nil
	end
	for _, s in sessions(state) do
		if s.sid == sid then
			return s
		end
	end
	return nil
end

local function label_of(s: any): string
	return s.identifier or s.sid or "?"
end

-- ── head and navigation ────────────────────────────────────────────────────

local function daemon_badge(state: any): Node
	local ov = state.data.overview
	if state.api.auth ~= "ok" then
		return ui.badge("Disconnected", "muted")
	end
	if state.offline then
		return ui.badge("Offline", "error")
	end
	if not ov or type(ov.daemon) ~= "table" then
		return ui.badge("Connecting…", "pending")
	end
	if not ov.daemon.alive then
		return ui.badge("Daemon stopped", "error")
	elseif ov.daemon.paused then
		return ui.badge("Daemon paused", "warning")
	end
	return ui.badge("Daemon running", "success")
end

local function head(state: any): Node
	local meta = "  not connected"
	local c = state.api.creds
	if c then
		local who = (c.name or "?") .. " (" .. tostring(c.role or "?") .. ")"
		local when = if state.updated_ms then "updated " .. fmt.ago(state.updated_ms) else "connecting"
		meta = "  " .. host_of(c.url) .. " · " .. who .. " · " .. when
	end
	local right: { Node } = { daemon_badge(state) }
	local ov = state.data.overview
	if c and ov and type(ov.daemon) == "table" and apimod.can(state.api, "operator") then
		if ov.daemon.paused then
			table.insert(right, h.action_key("Resume", "resume", "⌃P"))
		else
			table.insert(right, h.action_key("Pause", "pause", "⌃P"))
		end
	end
	table.insert(right, h.action("Refresh", "refresh", "neutral"))
	return ui.node("row", { key = "head", justify = "between", align = "center" }, {
		ui.text({ ui.span("Symphony", "title strong"), ui.span(meta, "muted") }),
		h.row("end", right),
	})
end

-- The terminal width (columns) from which the tab strip has room for full labels and counts.
local WIDE_COLS = 120

local function nav(state: any): Node
	local ov = state.data.overview
	local counts = ov and ov.counts or {}
	local active_n = #V.active_sessions(state)
	local issues = state.data.issues
	local merger_n = 0
	if ov and type(ov.merger) == "table" then
		merger_n = fmt.size(ov.merger.reviewing) + fmt.size(ov.merger.parked) + fmt.size(ov.merger.train)
	end
	-- Below WIDE_COLS the strip drops its counts and takes short labels, so all ten tabs fit at
	-- 80 columns (the palette keeps the full names).
	local wide = (state.cols or 200) >= WIDE_COLS
	local function lab(text: string, short: string, n: number?): Spans
		if not wide then
			return short
		end
		if n and n > 0 then
			return { ui.span(text .. " "), ui.span(tostring(n), "muted") }
		end
		return text
	end
	local waiting = counts.waiting_input or 0
	local items: { any } = {
		{ id = "dashboard", label = if not wide then "Home"
			elseif waiting > 0 then { ui.span("Dashboard "), ui.span(tostring(waiting), "pending") }
			else "Dashboard" },
		{ id = "sessions", label = lab("Sessions", "Sessions", active_n) },
		{ id = "tickets", label = lab("Tickets", "Tickets", issues and type(issues.items) == "table" and #issues.items or nil) },
		{ id = "merger", label = lab("Merger", "Merger", merger_n) },
		{ id = "repositories", label = lab("Repositories", "Repos") },
		{ id = "infrastructure", label = lab("Infrastructure", "Infra") },
		{ id = "telemetry", label = lab("Telemetry", "Usage") },
		{ id = "learnings", label = lab("Learnings", "Learn") },
		{ id = "audit", label = "Audit" },
	}
	if apimod.can(state.api, "admin") then
		table.insert(items, { id = "admin", label = "Admin" })
	end
	return ui.node("tabs", { key = "nav", active = state.view, items = items })
end

-- ── shared pieces ──────────────────────────────────────────────────────────

-- A pickable list at node id `path` (its parent's id plus its key). Items are
-- `{id, label, detail?, value?, icon?, tone?}`; a click selects, a double click opens.
local function pick_list(path: string, key: string, items: { any }, selected: string?, empty: string): Node
	local rows: { Item } = {}
	for i, it in items do
		rows[i] = { label = it.label, detail = it.detail, value = it.value, icon = it.icon, tone = it.tone }
	end
	local list = ui.list(rows)
	local children: { Node } = list.c or {}
	local sel_node = nil
	for i, child in children do
		local id = items[i].id
		local k = V.key(id)
		h.with(child, { key = k, actions = { click = "sel=" .. id, dblclick = "open=" .. id } })
		if id == selected then
			sel_node = path .. "." .. k
		end
	end
	list.p = { key = key, selected = sel_node, empty = { ui.span(empty, "muted") } }
	return list
end

-- Where an `assist` session talks: "Slack · C0123ABCD · thread 1731.42" or "Discord · DM".
-- The engine writes `identifier` "<transport> DM" for a direct message.
function V.assist_where(s: any): string
	local parts = { fmt.TRANSPORT[s.transport or ""] or tostring(s.transport or "chat") }
	local dm = type(s.identifier) == "string" and string.find(s.identifier, " DM$") ~= nil
	table.insert(parts, if dm then "DM" else tostring(s.channel or "?"))
	-- A DM's thread is the DM itself: no need to repeat its id.
	if s.thread and not (dm and tostring(s.thread) == tostring(s.channel)) then
		table.insert(parts, "thread " .. tostring(s.thread))
	end
	return table.concat(parts, " · ")
end

local function session_item(s: any): any
	local word, tone = fmt.state(s.state)
	local value: { any } = { ui.span(word, tone) }
	if s.turn then
		table.insert(value, ui.span(" · turn " .. tostring(s.turn), "muted"))
	end
	table.insert(value, ui.span(" · " .. fmt.ago(s.updated_at), "muted"))
	local detail = fmt.KIND[s.kind or "implement"] or s.kind or ""
	if s.kind == "assist" then
		detail = detail .. " · " .. (fmt.ASSIST_ROLE[s.role or ""] or tostring(s.role or "assistant")) .. " · " .. V.assist_where(s)
	end
	return {
		id = s.sid,
		label = { ui.span(label_of(s), "strong"), ui.span("  " .. fmt.cut(s.title, 70)) },
		detail = { ui.span("  " .. detail, "muted") },
		value = value,
		icon = fmt.STATE_ICON[s.state or ""] or "activity",
		tone = tone,
	}
end

-- "provider/model" → "model" and "provider" (the Dashboard's detail card has a short measure).
local function model_id(m: any): string
	if type(m) ~= "string" then
		return "—"
	end
	return string.match(m, "^[^/]+/(.+)$") or m
end

local function model_provider(m: any): string
	return if type(m) == "string" then string.match(m, "^([^/]+)/") or "—" else "—"
end

-- The tracked issue behind a session, for `held` (internals §12).
function V.issue_of(state: any, s: any): any
	local doc = state.data.issues
	for _, it in (doc and doc.items) or {} do
		if (s.issue_id and it.issue_id == s.issue_id) or (it.session_id and it.session_id == s.sid) then
			return it
		end
	end
	return nil
end

-- The devices a session holds through an MCP server with a lease cleanup (Seiji): session
-- items carry `mcp_leases: [{server, id, model?, platform?}]` while the gateway records them.
function V.session_leases(s: any): { any }
	local out = {}
	if type(s) ~= "table" or s.state == "ended" then
		return out
	end
	for _, l in (type(s.mcp_leases) == "table" and s.mcp_leases) or {} do
		if type(l) == "table" and type(l.server) == "string" and type(l.id) == "string" then
			table.insert(out, l)
		end
	end
	return out
end

-- "iphone-16", else the lease id.
function V.lease_label(l: any): string
	return if type(l.model) == "string" and l.model ~= "" then l.model else l.id
end

local function held_text(held: any): string
	return "held by " .. tostring(held.by or "?") .. " " .. fmt.ago(held.at)
end

local function session_actions(state: any, s: any): Node
	local acts: { Node } = { h.action("Open chat", "chat=" .. s.sid, "accent") }
	if apimod.can(state.api, "operator") and #V.session_leases(s) > 0 then
		table.insert(acts, h.action("Watch device", "watch=" .. s.sid, "neutral"))
		table.insert(acts, h.action("Watch with control", "watch_control=" .. s.sid, "neutral"))
	end
	local issue = V.issue_of(state, s)
	local held = issue and type(issue.held) == "table" and issue.held or nil
	if apimod.can(state.api, "operator") then
		if held then
			table.insert(acts, h.action("Return to Symphony", "release_issue=" .. tostring(issue.issue_id), "neutral"))
		end
		if s.state ~= "ended" then
			if s.state == "waiting_input" then
				table.insert(acts, h.action("Answer", "answer=" .. s.sid, "neutral"))
			end
			table.insert(acts, h.action("Send message", "say=" .. s.sid))
			if s.state == "running" or s.state == "starting" or s.state == "waiting_input" then
				table.insert(acts, h.action("Interrupt", "interrupt=" .. s.sid, "error"))
			end
			if not held and (s.kind == nil or s.kind == "implement" or s.kind == "answer") then
				table.insert(acts, h.action("Take over", "takeover=" .. s.sid))
			end
		end
	end
	if held then
		return h.col("acts", {
			ui.text({ ui.span("Ticket " .. held_text(held), "warning"), ui.span(if held.reason then " · " .. fmt.cut(held.reason, 60) else "", "muted") }),
			h.row("chips", acts, { wrap = true }),
		}, { gap = "xs" })
	end
	return h.row("acts", acts, { wrap = true })
end

local function session_detail(state: any, s: any): Node
	if not s then
		return h.card("detail", { ui.span("No session selected", "strong") }, {
			h.muted("Pick a session in the list to see details and act on it."),
		})
	end
	local word, tone = fmt.state(s.state)
	local facts: { { Spans } } = {
		{ "State", { ui.span(word, tone) } },
		{ "Kind", fmt.KIND[s.kind or "implement"] or (s.kind or "—") },
	}
	if s.kind == "assist" then
		local dm = type(s.identifier) == "string" and string.find(s.identifier, " DM$") ~= nil
		table.insert(facts, { "Role", fmt.role_info(tostring(s.role or "assistant"))[1] })
		table.insert(facts, { "Transport", fmt.TRANSPORT[s.transport or ""] or tostring(s.transport or "—") })
		table.insert(facts, { "Channel", if dm then "direct message" else { ui.span(tostring(s.channel or "—"), "mono") } })
		if s.thread then
			table.insert(facts, { "Thread", { ui.span(tostring(s.thread), "mono") } })
		end
		if s.asker then
			table.insert(facts, { "Asked by", { ui.span(tostring(s.asker), "mono") } })
		end
	end
	for _, f in {
		{ "Model", { ui.span(model_id(s.model), "mono") } },
		{ "Provider", { ui.span(model_provider(s.model), "muted") } },
		{ "Turn", { ui.span(tostring(s.turn or "—"), "mono") } },
	} do
		table.insert(facts, f)
	end
	local toks = fmt.tokens(s.tokens)
	if toks then
		local cost = type(s.tokens) == "table" and s.tokens.cost or nil
		table.insert(facts, { "Tokens", { ui.span(fmt.count(toks), "mono"), ui.span(if cost then "  " .. fmt.money(cost) else "", "muted") } })
	end
	if s.repo then
		table.insert(facts, { "Repository", { ui.span(s.repo, "mono"), ui.span(if s.branch then "  " .. s.branch else "", "muted") } })
	end
	if type(s.pr_url) == "string" then
		local n = string.match(s.pr_url, "/pull/(%d+)")
		table.insert(facts, { "PR", { ui.link(if n then "#" .. n else s.pr_url, s.pr_url) } })
	end
	for _, l in V.session_leases(s) do
		local spans: Spans = { ui.span(V.lease_label(l), "mono"), ui.span("  on " .. l.server .. " · ", "muted"), ui.span(l.id, "mono muted") }
		if l.platform == "android" then
			table.insert(spans, ui.span("  no live screen yet", "warning"))
		end
		table.insert(facts, { "Device", spans })
	end
	table.insert(facts, { "Updated", fmt.ago(s.updated_at) })
	local body: { Node } = { session_actions(state, s), ui.kv(facts) }
	if type(s.input_request) == "table" and state.view ~= "dashboard" then
		-- The question is part of this session's card, not a card of its own.
		table.insert(body, 1, ui.section({ ui.span("Agent question", "strong pending") }, {
			ui.text({ ui.span(fmt.cut(s.input_request.question, 400)) }),
		}))
	end
	local evidence = P.evidence(s)
	if evidence then
		table.insert(body, evidence)
	end
	if type(s.last_message) == "string" and s.last_message ~= "" then
		table.insert(body, ui.section("Last message", { ui.text({ ui.span(fmt.cut(s.last_message, 280), "muted") }) }))
	end
	return h.card("detail", { ui.span(label_of(s), "strong"), ui.span("  " .. fmt.cut(s.title, 60), "muted") }, body)
end

-- List and detail side by side: the list takes the spare width, the card a readable measure.
local function master_detail(key: string, list: Node, detail: Node): Node
	h.with(detail, { min = { w = "44ch" }, max = { w = "56ch" }, basis = "content" })
	return ui.node("row", { key = key, gap = "lg", align = "start" }, {
		ui.node("col", { key = "list", grow = 1, basis = 0 }, { list }),
		detail,
	})
end

-- ── Dashboard ──────────────────────────────────────────────────────────────

local function n_span(v: any, tone_when_positive: string?): Spans
	local n = if type(v) == "number" then v else 0
	return { ui.span(tostring(n), if n > 0 and tone_when_positive then "strong " .. tone_when_positive else "strong") }
end

-- At most four tiles (0.8.3 draws no `stats`: a one-row table); the rest rides along muted.
local function counts_strip(ov: any): Node
	local c = ov.counts or {}
	local function plus(n: any, what: string): any
		return ui.span(if type(n) == "number" and n > 0 then " · " .. n .. " " .. what else "", "muted")
	end
	local running = n_span(c.running, "accent")
	table.insert(running, plus(c.queued, "queued"))
	local waiting = n_span(c.waiting_input, "pending")
	table.insert(waiting, plus(c.held, "held"))
	local review = n_span(c.in_review)
	table.insert(review, plus(c.merging, "merging"))
	return h.strip("counts", {
		{ "Running", running },
		{ "Needs input", waiting },
		{ "In review", review },
		{ "Cost today", { ui.span(fmt.money(ov.cost_today), "strong mono"), ui.span("  " .. fmt.count(ov.tokens_today) .. " tokens", "muted") } },
	})
end

local function waiting_cards(state: any): Node?
	local waiting = {}
	for _, s in V.active_sessions(state) do
		if s.state == "waiting_input" then
			table.insert(waiting, s)
		end
	end
	if #waiting == 0 then
		return nil
	end
	local cards: { Node } = {}
	for i, s in waiting do
		if i > 3 then
			table.insert(cards, h.muted("… " .. (#waiting - 3) .. " more need input in the Sessions tab"))
			break
		end
		local req = type(s.input_request) == "table" and s.input_request or {}
		local body: { Node } = {
			ui.text({ ui.span(fmt.cut(req.question or s.last_message or "The agent is waiting for an answer.", 300)) }),
		}
		if type(req.options) == "table" and #req.options > 0 then
			table.insert(body, ui.text({ ui.span("Options: ", "muted"), ui.span(table.concat(req.options, " · ")) }))
		end
		local acts: { Node } = {}
		if apimod.can(state.api, "operator") then
			table.insert(acts, h.action("Answer", "answer=" .. s.sid, "neutral"))
		end
		table.insert(acts, h.action("Open chat", "chat=" .. s.sid, "neutral"))
		table.insert(acts, h.muted("asked " .. fmt.ago(req.at or s.updated_at)))
		table.insert(body, h.row("acts", acts))
		table.insert(cards, h.card(V.key(s.sid), {
			ui.span(label_of(s), "strong"),
			ui.span("  " .. fmt.cut(s.title, 80)),
		}, body, "pending"))
	end
	return h.col("waiting", {
		ui.text({ ui.span("Needs input", "strong pending"), ui.span("  answer so the agent can continue", "muted") }),
		h.col("cards", cards, { gap = "sm" }),
	}, { gap = "sm" })
end

local function merger_line(state: any): Node
	local ov = state.data.overview
	local m = ov and type(ov.merger) == "table" and ov.merger or {}
	return h.row("merger", {
		ui.text({
			ui.span("Merger  ", "strong"),
			ui.span(fmt.plural(fmt.size(m.reviewing), "PR in review", "PRs in review"), "muted"),
			ui.span(" · ", "dim"),
			ui.span(fmt.plural(fmt.size(m.parked), "parked", "parked"), "muted"),
			ui.span(" · ", "dim"),
			ui.span(fmt.plural(fmt.size(m.train), "in the train", "in the train"), "muted"),
		}),
		h.action("Open merger", "view=merger", "neutral"),
	})
end

-- The backend is not set up yet (`GET /v1/setup`): admins get the way in, others whom to ask.
local function setup_banner(state: any): Node?
	local doc = state.data.setup
	if type(doc) ~= "table" or doc.complete ~= false then
		return nil
	end
	local done, total = 0, 0
	for _, s in doc.steps or {} do
		if s.required then
			total += 1
			if s.status == "done" then
				done += 1
			end
		end
	end
	local progress = { ui.span("  " .. done .. " of " .. total .. " required steps done", "muted") }
	if apimod.can(state.api, "admin") then
		return h.card("setup", { ui.span("Symphony is not set up yet", "strong warning"), table.unpack(progress) }, {
			h.muted("Agents do not start until the required steps are done."),
			h.row("acts", { h.action_key("Open setup", "setup", "⌃S") }),
		}, "warning")
	end
	return h.card("setup", { ui.span("Symphony is not set up yet", "strong warning"), table.unpack(progress) }, {
		h.muted("Ask an admin to finish setup."),
	}, "warning")
end

local function dashboard(state: any): Node
	local ov = state.data.overview
	local parts: { Node } = {}
	local banner = setup_banner(state)
	if banner then
		table.insert(parts, banner)
	end
	table.insert(parts, counts_strip(ov))
	local limits = P.rate_limits(ov)
	if limits then
		table.insert(parts, limits)
	end
	local w = waiting_cards(state)
	if w then
		table.insert(parts, w)
	end
	local list_items = {}
	for _, s in V.active_sessions(state) do
		table.insert(list_items, session_item(s))
	end
	local sel = state.sel.dashboard
	table.insert(parts, ui.text({ ui.span("Active sessions", "strong"), ui.span("  " .. #list_items, "muted") }))
	if #list_items == 0 then
		table.insert(parts, h.empty("none", "No active sessions", "Agents start on their own when an eligible ticket reaches an active state on the board.", nil))
	else
		table.insert(parts, master_detail("active",
			pick_list("main.dashboard.active.list.sessions", "sessions", list_items, sel, "No active sessions"),
			session_detail(state, V.find_session(state, sel))))
	end
	table.insert(parts, merger_line(state))
	table.insert(parts, P.trends(state))
	return h.col("dashboard", parts, { gap = "lg" })
end

-- ── Sessions ───────────────────────────────────────────────────────────────

local function sessions_view(state: any): Node
	local all = sessions(state)
	local n = { active = 0, input = 0, ended = 0, all = #all }
	for _, s in all do
		if s.state == "ended" then
			n.ended += 1
		else
			n.active += 1
		end
		if s.state == "waiting_input" then
			n.input += 1
		end
	end
	local labels = { active = "Active", input = "Needs input", ended = "Ended", all = "All" }
	local items: { any } = {}
	for _, f in V.FILTERS do
		table.insert(items, { id = f, label = { ui.span(labels[f] .. " "), ui.span(tostring(n[f]), if f == "input" and n.input > 0 then "warning" else "muted") } })
	end
	local list_items = {}
	for _, s in V.filtered_sessions(state) do
		table.insert(list_items, session_item(s))
	end
	local empty = ({
		active = "No active sessions right now",
		input = "No agent is waiting for an answer",
		ended = "No sessions ended in the last 7 days",
		all = "No sessions yet",
	})[state.filter] or ""
	return h.col("sessions", {
		ui.node("row", { key = "bar", justify = "between", align = "center" }, {
			h.muted("Agent sessions, from the one that needs you to the oldest"),
			ui.node("tabs", { key = "filter", variant = "segmented", active = state.filter, items = items }),
		}),
		master_detail("md",
			pick_list("main.sessions.md.list.sessions", "sessions", list_items, state.sel.sessions, empty),
			session_detail(state, V.find_session(state, state.sel.sessions))),
	}, { gap = "md" })
end

-- ── Tickets ────────────────────────────────────────────────────────────────

local function tickets(state: any): Node
	local doc = state.data.issues
	if not doc then
		if state.errs.issues then
			return h.error_card("tickets", "Could not load the tickets", state.errs.issues, "refresh")
		end
		return h.loading("tickets", "Loading tickets…")
	end
	local rows: { any } = {}
	for _, it in doc.items or {} do
		local word, tone = fmt.phase(it.phase)
		if type(it.held) == "table" then
			word, tone = held_text(it.held), "warning"
		elseif fmt.ISSUE_REASON[it.reason or ""] then
			word, tone = "blocked: " .. fmt.ISSUE_REASON[it.reason], "error"
		elseif type(it.blocked) == "table" and type(it.blocked.reason) == "string" then
			word = word .. ": " .. fmt.cut(it.blocked.reason, 60)
		end
		local pr: Spans = { ui.span("—", "dim") }
		if type(it.pr_url) == "string" then
			local n = string.match(it.pr_url, "/pull/(%d+)")
			pr = { ui.link(if n then "#" .. n else "PR", it.pr_url) }
		end
		local cost = it.cost
		if cost == nil and type(it.tokens) == "table" then
			cost = it.tokens.cost
		end
		table.insert(rows, {
			id = it.issue_id or it.identifier,
			cells = {
				id = { ui.span(it.identifier or "?", "strong") },
				title = fmt.cut(it.title, 90),
				phase = { ui.span(word, tone) },
				tracker = { ui.span(it.tracker_state or "—", "muted") },
				attempt = { ui.span(tostring(it.attempt or it.seq or "—"), "mono") },
				cost = { ui.span(fmt.money(cost), "mono") },
				pr = pr,
				at = { ui.span(fmt.ago(it.updated_at), "muted") },
			},
		})
	end
	local tbl = h.data_table({
		key = "table",
		cols = {
			{ id = "id", head = "Ticket", mono = true, priority = 9 },
			{ id = "title", head = "Title", grow = 1, priority = 10 },
			{ id = "phase", head = "In Symphony", priority = 7 },
			{ id = "tracker", head = "On the board", priority = 5 },
			{ id = "attempt", head = "Attempt", align = "end", mono = true, priority = 3 },
			{ id = "cost", head = "Cost", align = "end", mono = true, priority = 4 },
			{ id = "pr", head = "PR", mono = true, priority = 6 },
			{ id = "at", head = "Updated", align = "end", mono = true, priority = 2 },
		},
		rows = rows,
		empty = "No tracked tickets. Move a ticket to an active state on the board.",
	})
	local held_items = {}
	for _, it in doc.items or {} do
		if type(it.held) == "table" then
			table.insert(held_items, it)
		end
	end
	local parts: { Node } = { h.muted("Tickets that Symphony tracks: their state here and on the tracker board"), tbl }
	if #held_items > 0 then
		local cards: { Node } = {}
		for _, it in held_items do
			local acts: { Node } = {}
			if apimod.can(state.api, "operator") then
				table.insert(acts, h.action("Return to Symphony", "release_issue=" .. tostring(it.issue_id), "success"))
			end
			table.insert(acts, h.muted(held_text(it.held) .. (if it.held.reason then " · " .. fmt.cut(it.held.reason, 80) else "")))
			table.insert(cards, h.with(h.row(V.key(tostring(it.issue_id)), {
				ui.text({ ui.span(it.identifier or "?", "strong"), ui.span("  " .. fmt.cut(it.title, 60)) }),
				h.row("acts", acts),
			}), {}))
		end
		table.insert(parts, h.col("held", {
			ui.text({ ui.span("Held by a person", "strong warning"), ui.span("  Symphony does not dispatch these tickets until someone returns them", "muted") }),
			h.col("rows", cards, { gap = "xs" }),
		}, { gap = "sm" }))
	end
	return h.col("tickets", parts, { gap = "md" })
end

-- ── Merger ─────────────────────────────────────────────────────────────────

local function pr_span(pr: any): Spans
	return { ui.span("#" .. tostring(pr), "strong mono") }
end

-- Review phases the merger writes (`set_reviewing(phase=…)` in merger/review.py, resolve.py).
V.MERGER_PHASE = {
	pin = { "preparing the review", "pending" },
	review = { "reviewing", "accent" },
	verify = { "verifying findings", "accent" },
	arbitrate = { "arbitrating", "info" },
	ci = { "waiting for CI", "warning" },
	resolve = { "resolving a conflict", "warning" },
}

-- Train record states (`REC_STATES` in merger/store.py).
V.TRAIN_STATE = {
	publishing = { "publishing the review", "pending" },
	reviewed = { "reviewed, waiting for the train", "pending" },
	train = { "in the train", "accent" },
	resolve = { "resolving a conflict", "warning" },
	finalizing = { "finishing the merge", "success" },
}
V.IN_TRAIN = { train = true, resolve = true, finalizing = true }

-- Parked reviews (`park(kind=…)` in merger/review.py); a clean one says what it waits for
-- (`waiting`: `ci`, or `approval` once CI passed under `require_approval`).
V.PARKED = {
	clean = { "clean review", "waiting for CI", "warning" },
	report = { "will not merge", "report published", "error" },
}
V.PARKED_WAITING = {
	ci = "waiting for CI",
	approval = "waiting for approval",
}

-- A parked row's word: {detail, value, tone}.
function V.parked_word(p: any): { string }
	local word = V.PARKED[p.kind or ""] or { p.kind or "—", "parked", "warning" }
	if p.kind == "clean" then
		return { word[1], V.PARKED_WAITING[p.waiting or "ci"] or tostring(p.waiting), word[3] }
	end
	return word
end

-- `GET /v1/merger` lists each repository the merger serves (`{repos: [{repo, alive, …snapshot}]}`).
function V.merger_repos(state: any): { any }
	local m = state.data.merger
	return (type(m) == "table" and type(m.repos) == "table") and m.repos or {}
end

-- Actionable ids carry the repository: `q<pr>@<repo>` (queued) and `p<pr>@<repo>` (parked).
function V.merger_actionables(state: any): { any }
	local out = {}
	for _, r in V.merger_repos(state) do
		local repo = tostring(r.repo)
		for _, q in r.queue or {} do
			local kind = ({ bypass = "bypass", rereview = "re-review", first = "first review" })[q.kind or ""] or (q.kind or "")
			table.insert(out, {
				id = "q" .. tostring(q.pr) .. "@" .. repo,
				pr = q.pr,
				repo = repo,
				where = "queue",
				label = { ui.span("#" .. tostring(q.pr), "strong"), ui.span("  " .. fmt.cut(q.title, 70)) },
				detail = { ui.span("  " .. repo .. " · " .. (q.author or "?") .. " · " .. kind, "muted") },
				value = { ui.span("queued", "pending"), ui.span(" · ready " .. fmt.ago(q.ready_at), "muted") },
				icon = "clock",
				tone = "pending",
			})
		end
		for _, p in r.parked or {} do
			local word = V.parked_word(p)
			-- The PR's title; an older record has none: `PR #n`, never the report comment's URL.
			local label = if type(p.title) == "string" and p.title ~= ""
				then { ui.span("#" .. tostring(p.pr), "strong"), ui.span("  " .. fmt.cut(p.title, 70)) }
				else { ui.span("PR #" .. tostring(p.pr), "strong") }
			table.insert(out, {
				id = "p" .. tostring(p.pr) .. "@" .. repo,
				pr = p.pr,
				repo = repo,
				where = "parked",
				label = label,
				detail = { ui.span("  " .. repo .. " · " .. word[1], "muted") },
				value = { ui.span(word[2], word[3]), ui.span(" · since " .. fmt.ago(p.parked_at), "muted") },
				icon = "timer",
				tone = "warning",
			})
		end
	end
	return out
end

local function merger(state: any): Node
	local m = state.data.merger
	if not m then
		if state.errs.merger then
			return h.error_card("merger", "Could not load the merger", state.errs.merger, "refresh")
		end
		return h.loading("merger", "Loading the merger…")
	end
	local repos = V.merger_repos(state)
	local n_review, n_parked, n_train, n_queue = 0, 0, 0, 0
	local status_rows, rev_rows, train_rows = {}, {}, {}
	for _, r in repos do
		local repo = tostring(r.repo)
		local train = type(r.train) == "table" and r.train or {}
		local records = type(train.records) == "table" and train.records or {}
		n_review += #(r.reviewing or {})
		n_parked += #(r.parked or {})
		n_queue += #(r.queue or {})
		local lease = type(train.lease) == "table" and train.lease or nil
		local batch = type(r.batch) == "table" and r.batch or {}
		local names = {}
		for _, b in (type(batch.members) == "table" and batch.members) or {} do
			table.insert(names, "#" .. tostring(b.pr) .. (if b.merged then " ✓" else ""))
		end
		-- `label_filter` (merger status): the PRs this merger takes in.
		local lf = type(r.label_filter) == "table" and r.label_filter or nil
		local lf_labels = lf and type(lf.labels) == "table" and lf.labels or {}
		table.insert(status_rows, {
			id = "s" .. repo,
			cells = {
				repo = { ui.span(repo, "strong") },
				alive = if r.alive then { ui.span("running", "success") } else { ui.span("not running", "error") },
				lease = if lease
					then { ui.span(tostring(lease.owner), "mono"), ui.span(" · renews in " .. math.max(0, math.floor((lease.expires_in_ms or 0) / 1000)) .. " s", "muted") }
					else { ui.span("no lease: the train is stopped", "muted") },
				batch = { ui.span(if #names > 0 then table.concat(names, "  ") else "—", "mono") },
				filter = if lf == nil
					then { ui.span("—", "muted") }
					elseif lf.active
					then { ui.span("labeled: ", "muted"), ui.span(table.concat(lf_labels, ", "), "mono") }
					else { ui.span("every PR", "muted") },
			},
		})
		for _, rv in r.reviewing or {} do
			local ph = V.MERGER_PHASE[rv.phase or ""] or { rv.phase or "—", "muted" }
			table.insert(rev_rows, {
				id = "r" .. tostring(rv.pr) .. "@" .. repo,
				cells = {
					repo = { ui.span(repo, "muted") },
					pr = pr_span(rv.pr),
					phase = { ui.span(ph[1], ph[2]) },
					worker = { ui.span(rv.worker or "—", "muted") },
					sha = { ui.span(string.sub(rv.head_sha or "", 1, 8), "mono") },
					agents = { ui.span(tostring(#(rv.sessions or {})), "mono") },
					at = { ui.span(fmt.ago(rv.started_at), "muted") },
				},
			})
		end
		for _, rec in records do
			if V.IN_TRAIN[rec.state or ""] then
				n_train += 1
			end
			local sw = V.TRAIN_STATE[rec.state or ""] or { rec.state or "—", "muted" }
			if rec.conflict then
				sw = { "conflict", "warning" }
			end
			table.insert(train_rows, {
				id = "t" .. tostring(rec.pr) .. "@" .. repo,
				cells = {
					repo = { ui.span(repo, "muted") },
					pr = pr_span(rec.pr),
					state = { ui.span(sw[1], sw[2]) },
					branch = { ui.span(rec.branch or "—", "mono") },
					author = rec.author or "—",
					at = { ui.span(fmt.ago(rec.reviewed_at), "muted") },
				},
			})
		end
	end
	local parts: { Node } = {
		h.strip("counts", {
			{ "Reviewing", n_span(n_review, "accent") },
			{ "Parked", n_span(n_parked, "warning") },
			{ "In the train", n_span(n_train, "accent") },
			{ "Queued", n_span(n_queue) },
		}),
		ui.section({ ui.span("Mergers", "strong"), ui.span("  one merger, a train per repository", "muted") }, {
			h.data_table({
				key = "t",
				cols = {
					{ id = "repo", head = "Repository", mono = true, priority = 9 },
					{ id = "alive", head = "Merger", priority = 8 },
					{ id = "lease", head = "Train lease", grow = 1, priority = 10 },
					{ id = "batch", head = "Current batch", align = "end", mono = true, priority = 5 },
					{ id = "filter", head = "Takes", priority = 4 },
				},
				rows = status_rows,
				empty = "No merger has started yet",
			}),
		}),
		ui.section({ ui.span("Reviewing", "strong") }, {
			h.data_table({
				key = "t",
				cols = {
					{ id = "repo", head = "Repository", mono = true, priority = 7 },
					{ id = "pr", head = "PR", mono = true, priority = 9 },
					{ id = "phase", head = "Stage", grow = 1, priority = 10 },
					{ id = "worker", head = "Worker", mono = true, priority = 3 },
					{ id = "sha", head = "Commit", mono = true, priority = 4 },
					{ id = "agents", head = "Agents", align = "end", mono = true, priority = 5 },
					{ id = "at", head = "Since", align = "end", mono = true, priority = 6 },
				},
				rows = rev_rows,
				empty = "No PRs in review",
			}),
		}),
		ui.section({ ui.span("Merge train", "strong") }, {
			h.data_table({
				key = "t",
				cols = {
					{ id = "repo", head = "Repository", mono = true, priority = 6 },
					{ id = "pr", head = "PR", mono = true, priority = 9 },
					-- The row's state ("finishing the merge") outlasts every secondary column.
					{ id = "state", head = "State", priority = 9 },
					{ id = "branch", head = "Branch", grow = 1, mono = true, truncate = "middle", priority = 10 },
					{ id = "author", head = "Author", priority = 4 },
					{ id = "at", head = "Reviewed", align = "end", mono = true, priority = 3 },
				},
				rows = train_rows,
				empty = "The train is empty",
			}),
		}),
	}

	local acts = V.merger_actionables(state)
	local sel = state.sel.merger
	local selected = nil
	for _, a in acts do
		if a.id == sel then
			selected = a
		end
	end
	local qchildren: { Node } = { pick_list("main.merger.queue.list", "list", acts, sel, "Nothing queued or parked") }
	if selected and apimod.can(state.api, "operator") then
		local ref = tostring(selected.pr) .. "@" .. selected.repo
		table.insert(qchildren, h.row("acts", {
			h.muted(selected.repo .. " #" .. tostring(selected.pr) .. ":"),
			h.action_key("Bypass", "bypass=" .. ref, "b", "warning"),
			h.action_key("Requeue", "requeue=" .. ref, "q"),
			h.action_key("Release", "release=" .. ref, "l"),
		}))
	end
	table.insert(parts, h.col("queue", {
		ui.text({ ui.span("Queue and parked", "strong"), ui.span("  " .. #acts, "muted") }),
		table.unpack(qchildren),
	}, { gap = "sm" }))
	return h.col("merger", parts, { gap = "lg" })
end

-- ── Admin ──────────────────────────────────────────────────────────────────

local function consumer_kind(name: string): string
	local prefix = string.match(name, "^([%w]+)") or name
	return ({ daemon = "daemon", runner = "runner", merger = "merger", member = "person" })[prefix] or prefix
end

function V.model_ref(state: any, role: string): any
	local d = state.draft[role]
	if d then
		return d, true
	end
	local cfg = state.data.config
	local ref = cfg and type(cfg.models) == "table" and cfg.models[role] or nil
	return ref, false
end

local function propagation(state: any): Node
	local cfg = state.data.config or {}
	local gen = cfg.generation or 0
	local obs = state.data.observed
	if not obs then
		if state.errs.observed then
			return h.error_card("prop", "Could not read the rollout", state.errs.observed, "refresh")
		end
		return h.loading("prop", "Reading who has applied the configuration…")
	end
	local names = {}
	for name in obs do
		table.insert(names, name)
	end
	local KO = { daemon = 1, merger = 2, runner = 3, person = 4 }
	table.sort(names, function(a, b)
		local ka, kb = KO[consumer_kind(a)] or 9, KO[consumer_kind(b)] or 9
		if ka ~= kb then
			return ka < kb
		end
		return a < b
	end)
	local rows: { any } = {}
	local applied = 0
	for _, name in names do
		local o = obs[name] or {}
		local g = tonumber(o.generation) or 0
		local ok = g >= gen
		if ok then
			applied += 1
		end
		table.insert(rows, {
			id = V.key(name),
			cells = {
				who = { ui.span(name, "mono") },
				kind = { ui.span(consumer_kind(name), "muted") },
				gen = { ui.span(tostring(g), "mono") },
				status = if ok
					then { ui.span("✓ applied", "success") }
					else { ui.span("pending", "warning") },
				at = { ui.span(fmt.ago(o.at), "muted") },
			},
		})
	end
	local total = #names
	local frac = if total > 0 then applied / total else 0
	local summary = if total == 0
		then "No consumer has read the configuration yet"
		else applied .. " of " .. total .. " consumers applied generation " .. gen
	return h.col("prop", {
		ui.text({ ui.span("Generation " .. gen .. " rollout", "strong"), ui.span("  " .. summary, "muted") }),
		h.with(ui.progress(frac, { ui.span(math.floor(frac * 100 + 0.5) .. "%", "mono") }), { key = "bar", tone = if applied == total then "success" else "accent" }),
		h.data_table({
			key = "t",
			cols = {
				{ id = "who", head = "Consumer", grow = 1, mono = true, priority = 9 },
				{ id = "kind", head = "Kind", priority = 4 },
				{ id = "gen", head = "Generation", align = "end", mono = true, priority = 8 },
				{ id = "status", head = "State", priority = 7 },
				{ id = "at", head = "Seen", align = "end", mono = true, priority = 5 },
			},
			rows = rows,
			empty = "Consumers show up here when they read the configuration",
		}),
	}, { gap = "sm" })
end

-- "was openai/gpt-5.5 · medium": the published value a staged change replaces.
local function was_text(state: any, role: string): string
	local cfg = state.data.config
	local live = cfg and type(cfg.models) == "table" and cfg.models[role] or nil
	if not live then
		return "new"
	end
	return "was " .. (live.provider or "?") .. "/" .. (live.model or "?") .. " · " .. (fmt.THINKING_WORD[live.thinking or "medium"] or "")
end

local function models(state: any): Node
	local cfg = state.data.config
	if not cfg then
		if state.errs.config then
			return h.error_card("models", "Could not load the team configuration", state.errs.config, "refresh")
		end
		return h.loading("models", "Loading the team configuration…")
	end
	local items = {}
	local staged = 0
	for _, role in fmt.roles_of(cfg.models) do
		local ref, is_draft = V.model_ref(state, role)
		if is_draft then
			staged += 1
		end
		local r = fmt.role_info(role)
		local model = if ref then (ref.provider or "?") .. "/" .. (ref.model or "?") else "—"
		local think = if ref then fmt.THINKING_WORD[ref.thinking or "medium"] or ref.thinking else ""
		table.insert(items, {
			id = role,
			label = { ui.span(r[1], "strong"), ui.span("  " .. (if is_draft then was_text(state, role) else r[2]), "muted") },
			value = {
				ui.span(model, if is_draft then "mono warning" else "mono"),
				ui.span("  thinking " .. think, if is_draft then "warning" else "muted"),
			},
			icon = if is_draft then "pen" else "sparkle",
			tone = if is_draft then "warning" else nil,
		})
	end
	local meta: { any } = { ui.span("Generation " .. tostring(cfg.generation or 0), "strong") }
	if cfg.updated_by then
		table.insert(meta, ui.span("  published by " .. tostring(cfg.updated_by) .. " " .. fmt.ago(cfg.updated_at), "muted"))
	elseif (cfg.generation or 0) == 0 then
		table.insert(meta, ui.span("  Symphony defaults: nothing published yet", "muted"))
	end
	if type(cfg.reason) == "string" and cfg.reason ~= "" then
		table.insert(meta, ui.span("  “" .. fmt.cut(cfg.reason, 80) .. "”", "muted"))
	end
	local bar: { Node } = {
		h.action_key("Change model", "model", "enter", "accent"),
		h.action_key("Thinking", "thinking", "t"),
	}
	if staged > 0 then
		table.insert(bar, h.action_key("Publish " .. fmt.plural(staged, "change", "changes"), "publish", "p", "success"))
		table.insert(bar, h.action("Discard", "discard", "muted"))
	end
	return h.col("models", {
		ui.text(meta),
		h.muted("The model and thinking level of each role. Publishing creates a new generation. Each consumer applies it when its next unit of work starts."),
		pick_list("main.admin.models.roles", "roles", items, state.sel.models, ""),
		h.row("bar", bar),
		propagation(state),
	}, { gap = "md" })
end

-- omp built-in providers a team configures first (internals §13), in this order.
V.FEATURED_PROVIDERS = { "anthropic", "openai", "amazon-bedrock", "google" }
V.PROVIDER_NAME = { anthropic = "Anthropic", openai = "OpenAI", ["amazon-bedrock"] = "Amazon Bedrock", google = "Google" }

-- The Providers list: the featured built-ins the catalog has (all of them until it loads), then
-- every other stored item. A row is `{id, name, builtin, item?}`; `item` is the stored record.
function V.provider_rows(state: any): { any }
	local doc = state.data.providers
	local stored: { [string]: any } = {}
	for _, p in (doc and doc.items) or {} do
		stored[p.id] = p
	end
	local in_catalog: { [string]: boolean }? = nil
	if state.data.catalog then
		in_catalog = {}
		for _, p in state.data.catalog do
			if p.key then
				(in_catalog :: any)[p.id] = true
			end
		end
	end
	local rows, seen = {}, {}
	for _, id in V.FEATURED_PROVIDERS do
		local item = stored[id]
		if item or not in_catalog or (in_catalog :: any)[id] then
			seen[id] = true
			table.insert(rows, { id = id, name = (item and item.name) or V.PROVIDER_NAME[id], builtin = not item or item.builtin == true, item = item })
		end
	end
	for _, p in (doc and doc.items) or {} do
		if not seen[p.id] then
			table.insert(rows, { id = p.id, name = p.name or V.PROVIDER_NAME[p.id] or p.id, builtin = p.builtin == true, item = p })
		end
	end
	return rows
end

-- Whether a key can be set on a row: built-ins always, own providers only with `auth: apiKey`.
function V.takes_key(row: any): boolean
	return row.builtin or (row.item and row.item.auth == "apiKey")
end

local function providers(state: any): Node
	local doc = state.data.providers
	if not doc then
		if state.errs.providers then
			return h.error_card("providers", "Could not load the providers", state.errs.providers, "refresh")
		end
		return h.loading("providers", "Loading providers…")
	end
	local parts: { Node } = {}
	if doc.storageReady == false then
		table.insert(parts, h.card("storage", { ui.span("Provider storage is not installed", "strong warning") }, {
			h.muted("Ask the host administrator to install the providers Secret before you save keys."),
		}, "warning"))
	end
	local count: { [string]: number } = {}
	for _, p in state.data.catalog or {} do
		count[p.id] = p.n
	end
	local rows = V.provider_rows(state)
	local items = {}
	for _, r in rows do
		local p = r.item
		local value: Spans
		if V.takes_key(r) then
			value = if p and p.credentialConfigured then { ui.span("key set", "success") } else { ui.span("no key", if r.builtin then "muted" else "warning") }
		else
			value = { ui.span("no credential", "muted") }
		end
		local detail: string
		if r.builtin then
			detail = "built into omp" .. (if count[r.id] then " · " .. fmt.plural(count[r.id], "model", "models") else "")
		else
			detail = "custom · " .. tostring(p.api or "?") .. " · " .. fmt.plural(#(p.models or {}), "model", "models")
		end
		table.insert(items, {
			id = r.id,
			label = { ui.span(r.name or r.id, "strong"), ui.span("  " .. r.id, "muted mono") },
			detail = { ui.span("  " .. detail, "muted") },
			value = value,
			icon = "server",
			tone = if not r.builtin and V.takes_key(r) and not p.credentialConfigured then "warning" else nil,
		})
	end
	table.insert(parts, h.muted("Every model uses an API key. The key stays in the cluster and the API never returns it. It applies on each agent's next attempt. Without a key here, the provider uses the host credential (Secret model-provider)."))
	table.insert(parts, pick_list("main.admin.providers.list", "list", items, state.sel.providers, "No providers"))
	local sel = nil
	for _, r in rows do
		if r.id == (state.sel.providers or (rows[1] and rows[1].id)) then
			sel = r
		end
	end
	local acts: { Node } = {}
	if sel and V.takes_key(sel) then
		table.insert(acts, h.muted(sel.id .. ":"))
		table.insert(acts, h.action_key(if sel.item and sel.item.credentialConfigured then "Replace key" else "Set key", "apikey=" .. sel.id, "c", "accent"))
	end
	if state.data.catalog then
		table.insert(acts, h.action("Other omp provider…", "builtin_other", "neutral"))
	end
	if #acts > 0 then
		table.insert(parts, h.row("acts", acts))
	end
	return h.col("providers", parts, { gap = "md" })
end

local function created_card(state: any): Node?
	local c = state.created
	if not c then
		return nil
	end
	return h.card("created", { ui.span("Token created: " .. c.name, "strong") }, {
		ui.text({ ui.span("Copy it now. It will not be shown again. The recipient signs in with ", "muted"), ui.span("symphony login", "code"), ui.span(".", "muted") }),
		ui.code(c.token),
		h.row("acts", {
			h.action("Copy token", "token_copy", "accent"),
			h.action("I saved it", "token_dismiss", "muted"),
		}),
	}, "success")
end

local function tokens(state: any): Node
	local doc = state.data.tokens
	local parts: { Node } = {}
	local cc = created_card(state)
	if cc then
		table.insert(parts, cc)
	end
	if not doc then
		if state.errs.tokens then
			table.insert(parts, h.error_card("err", "Could not load the tokens", state.errs.tokens, "refresh"))
		else
			table.insert(parts, h.loading("loading", "Loading tokens…"))
		end
		return h.col("tokens", parts, { gap = "md" })
	end
	local items = {}
	local live = {}
	for _, t in doc.items or {} do
		local revoked = t.revoked_at ~= nil
		local value: { any }
		if revoked then
			value = { ui.span("revoked " .. fmt.ago(t.revoked_at), "muted") }
		else
			value = {
				ui.span(if t.last_used_at then "used " .. fmt.ago(t.last_used_at) else "never used", "muted"),
				ui.span(" · expires " .. fmt.until_(t.expires_at), "muted"),
			}
		end
		table.insert(items, {
			id = t.id,
			label = { ui.span(t.name or t.id, if revoked then "muted del" else "strong"), ui.span("  " .. tostring(t.role or "?"), if t.role == "admin" then "warning" else "muted") },
			detail = { ui.span("  " .. t.id .. " · created by " .. tostring(t.created_by or "?") .. " " .. fmt.ago(t.created_at), "muted mono") },
			value = value,
			icon = if revoked then "slash-circle" else "key-round",
			tone = if revoked then "muted" else nil,
		})
		live[t.id] = if revoked then nil else (t.name or t.id)
	end
	table.insert(parts, ui.node("row", { key = "bar", justify = "between", align = "center" }, {
		h.muted("Team API tokens: viewer watches, operator also acts, admin administers."),
		-- While a new token is shown, its Copy is the screen's one primary.
		h.action_key("Create token", "token_new", "n", if state.created then "neutral" else "accent"),
	}))
	table.insert(parts, pick_list("main.admin.tokens.list", "list", items, state.sel.tokens, "No tokens created"))
	local sel = state.sel.tokens
	if sel and live[sel] then
		table.insert(parts, h.row("acts", { h.muted(live[sel] .. ":"), h.action_key("Revoke", "revoke=" .. sel, "x", "error") }))
	end
	return h.col("tokens", parts, { gap = "md" })
end

local function omp(state: any): Node
	local cfg = state.data.config
	if not cfg then
		if state.errs.config then
			return h.error_card("omp", "Could not load the team configuration", state.errs.config, "refresh")
		end
		return h.loading("omp", "Loading the team configuration…")
	end
	local o = type(cfg.omp) == "table" and cfg.omp or {}
	local settings = type(o.settings) == "table" and o.settings or {}
	local n = 0
	for _ in settings do
		n += 1
	end
	local parts: { Node } = {
		h.muted("omp defaults that every worker uses. Each person applies them to their own omp, in a symphony profile kept apart from theirs."),
		ui.kv({
			{ "omp version", { ui.span(tostring(o.version or "—"), "mono") } },
			{ "Generation", { ui.span(tostring(cfg.generation or 0), "mono") } },
			{ "Settings", fmt.plural(n, "key", "keys") },
		}),
	}
	if n > 0 then
		table.insert(parts, ui.code(tern.json.encode(settings, true), "json"))
	end
	table.insert(parts, h.row("acts", {
		h.action_key(if state.busy.omp_sync then "Applying…" else "Apply to my omp", "omp_sync", "s", "accent"),
	}))
	local res = state.omp_sync
	if res then
		-- `symphony omp sync --json` fields (cli/local.py omp_sync), labelled for people.
		local LABELS = { { "profile", "Profile" }, { "config", "File" }, { "generation", "Generation" },
			{ "model", "Default model" } }
		local facts: { { Spans } } = {}
		local seen = {}
		for _, pair in LABELS do
			local v = res[pair[1]]
			if v ~= nil and type(v) ~= "table" and not seen[pair[2]] then
				seen[pair[2]] = true
				table.insert(facts, { pair[2], { ui.span(tostring(v), "mono") } })
			end
		end
		-- Same warnings as `symphony omp sync`: no omp binary, or not the team's version.
		local omp = if type(res.omp) == "table" then res.omp else nil
		if omp and not omp.path then
			table.insert(facts, { "Warning", { ui.span("omp not found (install it from Tern or set SYMPHONY_OMP)", "warning") } })
		elseif omp and omp.team_version and not omp.matches then
			table.insert(facts, { "Warning", { ui.span(
				tostring(omp.path) .. " is " .. tostring(omp.version or "an unknown version")
					.. "; the team uses " .. tostring(omp.team_version), "warning") } })
		end
		table.insert(parts, h.card("result", { ui.span("Applied to your omp", "strong success") }, {
			if #facts > 0 then ui.kv(facts) else h.muted("Your omp was already up to date."),
		}, "success"))
	end
	return h.col("omp", parts, { gap = "md" })
end

-- ── Admin › MCP ────────────────────────────────────────────────────────────

-- `GET /v1/admin/mcp` lists `{servers: [{name, transport, url?, command?, args?, env?,
-- headers_set, roles, enabled, timeout_ms?, instructions?}], roles}`; header values never come back.
function V.mcp_servers(state: any): { any }
	local doc = state.data.mcp
	local out = {}
	for _, s in (type(doc) == "table" and type(doc.servers) == "table" and doc.servers) or {} do
		if type(s) == "table" and type(s.name) == "string" then
			table.insert(out, s)
		end
	end
	table.sort(out, function(a, b)
		return a.name < b.name
	end)
	return out
end

function V.mcp_find(state: any, name: string?): any
	for _, s in V.mcp_servers(state) do
		if s.name == name then
			return s
		end
	end
	return nil
end

-- The roles a server can be given: the API's list, else the plugin's own.
function V.mcp_roles(state: any): { string }
	local doc = state.data.mcp
	if type(doc) == "table" and type(doc.roles) == "table" and #doc.roles > 0 then
		return doc.roles
	end
	return fmt.ROLES
end

function V.is_remote(transport: string?): boolean
	return transport == "http" or transport == "sse"
end

V.MCP_TRANSPORT = { http = "Streamable HTTP", sse = "SSE", stdio = "stdio" }

type McpField = { id: string, label: string, help: string, placeholder: string?, secret: boolean?, optional: boolean?, choice: { { string } }? }

local MF: { [string]: McpField } = {
	name = { id = "name", label = "Name", help = "Lowercase letters, digits, - and _. Agents see the tools under this name.", placeholder = "linear-docs" },
	transport = { id = "transport", label = "Transport", help = "Remote servers go through the gateway, which adds the secret headers. stdio servers run next to the agent.",
		choice = { { "http", "Streamable HTTP" }, { "sse", "SSE" }, { "stdio", "stdio" } } },
	url = { id = "url", label = "URL", help = "The server's MCP endpoint. It must use https://.", placeholder = "https://mcp.example.com/mcp" },
	command = { id = "command", label = "Command", help = "The program to run where the agent runs.", placeholder = "npx" },
	args = { id = "args", label = "Arguments", help = "Separated by spaces.", placeholder = "-y @acme/mcp-server", optional = true },
	env = { id = "env", label = "Environment", help = "KEY=value pairs, separated by spaces. Not secret: agents can read them.", placeholder = "LOG_LEVEL=warn", optional = true },
	roles = { id = "roles", label = "Roles", help = "Separated by commas, or click a role below.", placeholder = "researcher, planner" },
	enabled = { id = "enabled", label = "State", help = "A disabled server stays stored, and no agent gets it.", choice = { { "on", "Enabled" }, { "off", "Disabled" } } },
	header = { id = "header", label = "Secret header", help = "Its name, for example Authorization.", placeholder = "Authorization", optional = true },
	value = { id = "value", label = "Header value", help = "Write-only. The gateway adds it to every call. The API never returns it.", placeholder = "Bearer …", secret = true, optional = true },
	remove = { id = "value", label = "Header value", help = "Write-only. Leave it empty to remove the header.", placeholder = "Bearer …", secret = true, optional = true },
	preset = { id = "preset", label = "Preset", help = "Seiji lends iOS simulators to agents. Its preset adds the HTTP API, a 65 min call timeout, SEIJI_TOKEN and SEIJI_URL for the agent, and the release of held devices when the session ends.",
		choice = { { "custom", "Custom" }, { "seiji", "Seiji" } } },
	seiji_url = { id = "url", label = "URL", help = "Seiji's MCP endpoint. Its HTTP API is the same address without /mcp.", placeholder = "https://seiji.example.com/mcp" },
	seiji_value = { id = "value", label = "Header value", help = "Bearer and the Seiji token of the user agents act as. Write-only: the gateway adds it, and agents get only their session token.", placeholder = "Bearer sj_…", secret = true },
}

-- The presets the add form offers (`symphony admin mcp set … --preset NAME`).
V.MCP_PRESETS = { seiji = true }

-- The fields of the MCP card's form, in tab order. `form.mode` is add, edit or headers.
function V.mcp_fields(form: any): { McpField }
	if form.mode == "headers" then
		return { MF.header, MF.remove }
	end
	local out: { McpField } = {}
	if form.mode == "add" then
		table.insert(out, MF.preset)
		table.insert(out, MF.name)
	end
	if form.mode == "add" and V.MCP_PRESETS[form.values.preset or ""] then
		-- The preset sets the transport (Streamable HTTP) and the header's name.
		table.insert(out, MF.seiji_url)
		table.insert(out, MF.roles)
		table.insert(out, MF.enabled)
		table.insert(out, MF.header)
		table.insert(out, MF.seiji_value)
		return out
	end
	table.insert(out, MF.transport)
	if V.is_remote(form.values.transport) then
		table.insert(out, MF.url)
	else
		table.insert(out, MF.command)
		table.insert(out, MF.args)
		table.insert(out, MF.env)
	end
	table.insert(out, MF.roles)
	table.insert(out, MF.enabled)
	if form.mode == "add" and V.is_remote(form.values.transport) then
		table.insert(out, MF.header)
		table.insert(out, MF.value)
	end
	return out
end

-- Role names typed in the Roles field.
function V.split_roles(text: string?): { string }
	local out = {}
	for r in string.gmatch(text or "", "[^,%s]+") do
		if not table.find(out, r) then
			table.insert(out, r)
		end
	end
	return out
end

local function mask(raw: string): string
	return if raw == "" then "" else string.rep("•", math.min(utf8.len(raw) or #raw, 24))
end

local function mcp_field(state: any, f: McpField): Node
	local form = state.mcp_form
	local raw = tostring(form.values[f.id] or "")
	local note = if f.optional then "  optional" elseif f.secret then "  hidden, write-only" else ""
	if f.secret and f.optional then
		note = "  optional, hidden, write-only"
	end
	local head: Spans = { ui.span(f.label, "strong"), ui.span(note, "muted") }
	if f.choice and form.focus == f.id then
		table.insert(head, ui.span("  ←→", "key"))
	end
	local children: { Node } = { ui.text(head) }
	if f.choice then
		local items = {}
		for _, c in f.choice do
			table.insert(items, { id = c[1], label = c[2] })
		end
		table.insert(children, ui.node("tabs", { key = "seg", variant = "segmented", active = raw, items = items }))
	else
		-- 0.8.3 draws an `input`'s text as is (no `secret`): the plugin masks it.
		local text = if f.secret then mask(raw) else raw
		table.insert(children, ui.node("input", {
			key = "input",
			text = text,
			cursor = if form.focus == f.id then (utf8.len(text) or #text) else nil,
			placeholder = f.placeholder,
			mono = true,
			actions = { click = "mcp_focus=" .. f.id },
		}))
	end
	if f.id == "roles" then
		local picked = V.split_roles(raw)
		local chips: { Node } = {}
		for _, r in V.mcp_roles(state) do
			local on = table.find(picked, r) ~= nil
			table.insert(chips, h.action(if on then "✓ " .. r else r, "mcp_role=" .. r, if on then "accent" else "muted", "r_" .. r))
		end
		table.insert(children, h.row("chips", chips, { wrap = true, gap = "xs" }))
	end
	table.insert(children, h.muted(f.help))
	return h.col(f.id, children, { gap = "xs" })
end

local function mcp_form_card(state: any): Node
	local form = state.mcp_form
	local head: Spans = if form.mode == "add" then { ui.span("New MCP server", "strong") }
		elseif form.mode == "headers" then { ui.span("Secret headers", "strong"), ui.span("  " .. tostring(form.name), "muted mono") }
		else { ui.span("Edit ", "strong"), ui.span(tostring(form.name), "strong mono") }
	local busy = state.busy.mcp_save
	local body: { Node } = {
		-- 0.8.3 ignores a card's `end`: the actions open the body, at its right.
		ui.node("row", { key = "acts", gap = "sm", justify = "end", align = "center" }, {
			h.action_key("Cancel", "mcp_cancel", "esc", "muted"),
			h.action_key(if busy then "Saving…" else "Save", "mcp_save", "⏎", "accent"),
		}),
	}
	-- The outcome sits under the actions: the Seiji form runs past the fold.
	if busy then
		table.insert(body, ui.node("spinner", { key = "busy", text = "Saving through symphony admin mcp…" }))
	end
	if form.error then
		table.insert(body, ui.text({ ui.span("Not saved", "strong error") }))
		table.insert(body, h.with(ui.code(form.error), { key = "why" }))
	end
	if form.mode == "headers" then
		local s = V.mcp_find(state, form.name)
		local set = (s and s.headers_set) or {}
		table.insert(body, ui.kv({ { "Stored", if #set > 0 then { ui.span(table.concat(set, ", "), "mono") } else { ui.span("none", "muted") } } }))
	end
	local list: { Node } = {}
	for _, f in V.mcp_fields(form) do
		table.insert(list, mcp_field(state, f))
	end
	table.insert(body, h.col("form", list, { gap = "md" }))
	return h.card("detail", head, body, if form.error then "error" else nil)
end

-- `me`: the server's own `GET {http_base}/v1/me` (Seiji: `{user, quota}`), read with the stored header.
local QUOTA = {
	{ "monthlyMinutes", function(n: number): string return math.floor(n) .. " min a month" end },
	{ "maxConcurrent", function(n: number): string return fmt.plural(n, "device", "devices") .. " at once" end },
	{ "maxLeaseMinutes", function(n: number): string return n .. " min a lease" end },
	{ "idleMinutes", function(n: number): string return "idle release after " .. n .. " min" end },
}

local function mcp_me_spans(me: any): Spans?
	if type(me) ~= "table" or me.user == nil then
		return nil
	end
	local spans: Spans = { ui.span("Signed in as ", "muted"), ui.span(tostring(me.user), "mono strong") }
	local q = type(me.quota) == "table" and me.quota or {}
	for _, entry in QUOTA do
		local n = tonumber(q[entry[1]])
		if n then
			table.insert(spans, ui.span(" · " .. entry[2](n), "muted"))
		end
	end
	return spans
end

-- The test's error codes (`POST /v1/admin/mcp/{name}/test`), for people.
local MCP_TEST_ERROR = {
	unreachable = "The gateway could not reach the server.",
	rejected = "The server rejected the stored headers (401 or 403).",
	protocol_error = "The server did not answer as an MCP server.",
}

local function mcp_test_nodes(state: any, name: string): { Node }
	if state.busy["mcp_test:" .. name] then
		return { ui.node("spinner", { key = "testing", text = "Calling initialize and tools/list through the gateway…" }) }
	end
	local t = state.mcp_tests[name]
	if type(t) ~= "table" then
		return { h.muted("Not tested yet. Test calls initialize and tools/list through the gateway, with the stored headers.") }
	end
	if t.ok == true then
		local tools = type(t.tools) == "table" and t.tools or {}
		local shown = {}
		for i, tool in tools do
			if i > 12 then
				break
			end
			table.insert(shown, tostring(if type(tool) == "table" then tool.name else tool))
		end
		local spans: Spans = { ui.span("✓ Reached", "strong success"), ui.span("  " .. fmt.plural(#tools, "tool", "tools"), "muted") }
		if t.protocol_version then
			table.insert(spans, ui.span(" · protocol ", "muted"))
			table.insert(spans, ui.span(tostring(t.protocol_version), "mono"))
		end
		table.insert(spans, ui.span(" · " .. fmt.ago(t.at), "muted"))
		local out: { Node } = { h.with(ui.text(spans), { key = "tested" }) }
		if #shown > 0 then
			local line = table.concat(shown, "  ")
			if #tools > #shown then
				line ..= "  and " .. (#tools - #shown) .. " more"
			end
			table.insert(out, h.with(ui.text({ ui.span(line, "mono") }), { key = "tools" }))
		end
		local me = mcp_me_spans(t.me)
		if me then
			table.insert(out, h.with(ui.text(me), { key = "me" }))
		end
		return out
	elseif t.ok == false then
		local e = type(t.error) == "table" and t.error or {}
		local why = MCP_TEST_ERROR[e.code or ""] or (if type(e.message) == "string" and e.message ~= "" then e.message else "The test did not pass.")
		local out: { Node } = {
			h.with(ui.text({ ui.span("Test failed", "strong error"), ui.span("  " .. tostring(e.code or "error") .. " · " .. fmt.ago(t.at), "muted") }), { key = "tested" }),
			h.with(ui.text({ ui.span(why, "error") }), { key = "why" }),
		}
		if type(e.message) == "string" and e.message ~= "" and MCP_TEST_ERROR[e.code or ""] then
			table.insert(out, h.with(ui.code(fmt.cut(e.message, 400)), { key = "detail" }))
		end
		return out
	end
	return { h.with(ui.text({ ui.span(tostring(t.note or "Not tested."), "muted") }), { key = "tested" }) }
end

local function mcp_card(state: any, s: any): Node
	if state.mcp_form then
		return mcp_form_card(state)
	end
	if not s then
		return h.card("detail", { ui.span("No server selected", "strong") }, {
			h.muted("Pick a server in the table to see it, test it and change it."),
		})
	end
	local remote = V.is_remote(s.transport)
	local acts: { Node } = {
		h.action_key(if state.busy["mcp_test:" .. s.name] then "Testing…" else "Test", "mcp_test=" .. s.name, "⏎", "accent"),
		h.action_key("Edit", "mcp_edit=" .. s.name, "e"),
	}
	if remote then
		table.insert(acts, h.action_key("Headers", "mcp_headers=" .. s.name, "h"))
	end
	table.insert(acts, h.action_key(if s.enabled == false then "Enable" else "Disable", "mcp_toggle=" .. s.name, "d"))
	table.insert(acts, h.action_key("Remove", "mcp_remove=" .. s.name, "x", "error"))
	local facts: { { Spans } } = {
		{ "Transport", V.MCP_TRANSPORT[s.transport or ""] or tostring(s.transport or "—") },
	}
	if remote then
		table.insert(facts, { "URL", { ui.span(fmt.cut(tostring(s.url or "—"), 80), "mono") } })
		table.insert(facts, { "Agents call", { ui.span("the gateway, /v1/mcp/" .. s.name, "mono") } })
		local set = type(s.headers_set) == "table" and s.headers_set or {}
		table.insert(facts, { "Secret headers", if #set > 0 then { ui.span(table.concat(set, ", "), "mono"), ui.span("  values write-only", "muted") } else { ui.span("none", "muted") } })
	else
		local cmd = tostring(s.command or "—")
		for _, a in (type(s.args) == "table" and s.args) or {} do
			cmd ..= " " .. tostring(a)
		end
		table.insert(facts, { "Command", { ui.span(fmt.cut(cmd, 80), "mono") } })
		local env = {}
		for k, v in (type(s.env) == "table" and s.env) or {} do
			table.insert(env, tostring(k) .. "=" .. tostring(v))
		end
		table.sort(env)
		table.insert(facts, { "Environment", if #env > 0 then { ui.span(fmt.cut(table.concat(env, " "), 80), "mono") } else { ui.span("none", "muted") } })
	end
	local roles = type(s.roles) == "table" and s.roles or {}
	table.insert(facts, { "Roles", if #roles > 0 then table.concat(roles, ", ") else { ui.span("none: no agent gets it", "warning") } })
	table.insert(facts, { "State", if s.enabled == false then { ui.span("disabled", "muted") } else { ui.span("enabled", "success") } })
	if s.timeout_ms then
		table.insert(facts, { "Timeout", { ui.span(tostring(s.timeout_ms) .. " ms", "mono") } })
	end
	if type(s.http_base) == "string" then
		table.insert(facts, { "HTTP API", { ui.span(fmt.cut(s.http_base, 60), "mono"), ui.span("  through /v1/mcp/" .. s.name .. "/http", "muted") } })
	end
	local agent_env = {}
	for k, v in (type(s.agent_env) == "table" and s.agent_env) or {} do
		table.insert(agent_env, tostring(k) .. "=" .. tostring(v))
	end
	table.sort(agent_env)
	if #agent_env > 0 then
		table.insert(facts, { "Agent env", { ui.span(fmt.cut(table.concat(agent_env, " "), 80), "mono") } })
	end
	if type(s.rewrite_origins) == "table" and #s.rewrite_origins > 0 then
		table.insert(facts, { "Rewrites", { ui.span(fmt.cut(table.concat(s.rewrite_origins, " "), 80), "mono") } })
	end
	local c = type(s.cleanup) == "table" and s.cleanup or nil
	if c then
		table.insert(facts, { "Cleanup", { ui.span(tostring(c.release_tool), "mono"), ui.span(" for each ", "muted"), ui.span(tostring(c.acquire_tool), "mono"), ui.span(" lease when the session ends", "muted") } })
	end
	local body: { Node } = {
		ui.node("row", { key = "acts", gap = "sm", justify = "end", align = "center", wrap = true }, acts),
		h.with(ui.kv(facts), { key = "facts" }),
		ui.section({ ui.span("Last test", "strong") }, mcp_test_nodes(state, s.name)),
	}
	return h.card("detail", {
		ui.span(s.name, "strong mono"),
		ui.span("  " .. (V.MCP_TRANSPORT[s.transport or ""] or tostring(s.transport or "")), "muted"),
		ui.span(if s.enabled == false then "  disabled" else "  enabled", if s.enabled == false then "muted" else "success"),
	}, body)
end

local function mcp(state: any): Node
	local doc = state.data.mcp
	if not doc then
		if state.errs.mcp then
			return h.error_card("mcp", "Could not load the MCP servers", state.errs.mcp, "refresh")
		end
		return h.loading("mcp", "Loading the MCP servers…")
	end
	local servers = V.mcp_servers(state)
	-- While the card holds a form or a test runs, its Save or Test is the one primary.
	local add = h.action_key("Add server", "mcp_new", "n", "neutral")
	local bar = ui.node("row", { key = "bar", justify = "between", align = "center" }, {
		h.muted("MCP servers give agents more tools. Each role gets the servers enabled for it. Secret headers stay in the gateway."),
		add,
	})
	if #servers == 0 and not state.mcp_form then
		return h.col("mcp", {
			bar,
			h.empty("md", "No MCP servers yet", "Add a server, pick the roles that use it, then test it. Without servers, agents use their built-in tools only.",
				h.action_key("Add server", "mcp_new", "n", "accent")),
		}, { gap = "md" })
	end
	local sel = state.sel.mcp
	local rows: { any } = {}
	for _, s in servers do
		local picked = s.name == sel
		local set = type(s.headers_set) == "table" and s.headers_set or {}
		table.insert(rows, {
			id = V.key(s.name),
			cells = {
				name = { ui.span(if picked then "▸ " else "  ", "accent"), ui.span(s.name, if picked then "strong accent" else "strong") },
				transport = { ui.span(V.MCP_TRANSPORT[s.transport or ""] or tostring(s.transport or "—"), "muted") },
				roles = { ui.span(if type(s.roles) == "table" and #s.roles > 0 then table.concat(s.roles, ", ") else "none", if type(s.roles) == "table" and #s.roles > 0 then "" else "warning") },
				headers = if not V.is_remote(s.transport) then { ui.span("—", "dim") }
					elseif #set > 0 then { ui.span(fmt.plural(#set, "header", "headers") .. " set", "success") }
					else { ui.span("none", "muted") },
				enabled = if s.enabled == false then { ui.span("disabled", "muted") } else { ui.span("enabled", "success") },
			},
		})
	end
	local tbl = h.data_table({
		key = "servers",
		cols = {
			{ id = "name", head = "Name", grow = 1, mono = true, priority = 10 },
			{ id = "transport", head = "Transport", priority = 5 },
			{ id = "roles", head = "Roles", priority = 8 },
			{ id = "headers", head = "Headers", priority = 4 },
			{ id = "enabled", head = "State", priority = 9 },
		},
		rows = rows,
		empty = "No servers yet",
	})
	return h.col("mcp", {
		bar,
		master_detail("md", tbl, mcp_card(state, V.mcp_find(state, sel))),
	}, { gap = "md" })
end

-- ── Admin › Assistant ──────────────────────────────────────────────────────

-- `GET /v1/assistant/memory` is `{soul: {repo, path, sha} | null, people: [{login, count,
-- updated_at}]}`; `GET /v1/assistant/memory/{login}` is `{login, items: [{id, text, kind, at}]}`.
function V.memory_people(state: any): { any }
	local doc = state.data.assistant
	return (type(doc) == "table" and type(doc.people) == "table" and doc.people) or {}
end

local function soul_facts(soul: any): Node
	local facts: { { Spans } }
	if type(soul) ~= "table" then
		facts = { { "Soul", { ui.span("not reported: the assistant is not running, or no soul repository is set", "muted") } } }
	else
		facts = { { "Soul", { ui.span(tostring(soul.repo) .. ":" .. tostring(soul.path), "mono") } } }
		if soul.sha then
			table.insert(facts, { "Version", { ui.span(string.sub(tostring(soul.sha), 1, 12), "mono"), ui.span("  blob on origin/main", "muted") } })
		else
			table.insert(facts, { "Version", { ui.span("the built-in text: the file is missing", "warning") } })
		end
	end
	return h.with(ui.kv(facts), { key = "soul" })
end

local function person_card(state: any, login: string?): Node
	if not login then
		return h.card("detail", { ui.span("No person selected", "strong") }, {
			h.muted("Pick a person to see what the assistant remembers about them."),
		})
	end
	local doc = state.data.assistant_person
	local acts = ui.node("row", { key = "acts", gap = "sm", justify = "end", align = "center" }, {
		h.action_key(if state.busy["memory_wipe:" .. login] then "Wiping…" else "Wipe", "memory_wipe=" .. login, "x", "error"),
	})
	local body: { Node } = { acts }
	if type(doc) ~= "table" or doc.login ~= login then
		if state.errs.assistant_person then
			table.insert(body, ui.text({ ui.span(state.errs.assistant_person, "error") }))
		else
			table.insert(body, h.loading("items", "Reading the memory of " .. login .. "…"))
		end
	else
		local rows: { Item } = {}
		for _, it in doc.items or {} do
			table.insert(rows, {
				label = tostring(it.text or ""),
				detail = tostring(it.kind or "explicit") .. " · " .. fmt.ago(it.at) .. " · " .. tostring(it.id or ""),
			})
		end
		local list = ui.list(rows)
		list.p = { key = "items", empty = { ui.span("The assistant remembers nothing about " .. login .. ".", "muted") } }
		table.insert(body, list)
	end
	local n = if type(doc) == "table" and doc.login == login then #(doc.items or {}) else nil
	return h.card("detail", {
		ui.span(login, "strong mono"),
		ui.span(if n then "  " .. fmt.plural(n, "item", "items") else "", "muted"),
	}, body)
end

local function assistant(state: any): Node
	local doc = state.data.assistant
	if not doc then
		if state.errs.assistant then
			return h.error_card("assistant", "Could not load the assistant's memory", state.errs.assistant, "refresh")
		end
		return h.loading("assistant", "Loading the assistant's soul and memory…")
	end
	local parts: { Node } = {
		h.muted("The soul file in Git sets the assistant's persona and team rules. It remembers facts about each person mapped in notify.users. Every read here is audited."),
		soul_facts(doc.soul),
	}
	local people = V.memory_people(state)
	if #people == 0 then
		table.insert(parts, h.empty("md", "No memory yet",
			"The assistant remembers what a mapped person asks it to remember in Slack or Discord. Nothing is stored for anyone yet.", nil))
		return h.col("assistant", parts, { gap = "md" })
	end
	local items = {}
	for _, p in people do
		table.insert(items, {
			id = p.login,
			label = { ui.span(p.login, "strong mono") },
			detail = { ui.span("  updated " .. fmt.ago(p.updated_at), "muted") },
			value = { ui.span(fmt.plural(p.count or 0, "item", "items"), "muted") },
			icon = "user",
		})
	end
	local sel = state.sel.assistant
	local list = pick_list("main.admin.assistant.md.list.list", "list", items, sel, "Nobody")
	table.insert(parts, master_detail("md", list, person_card(state, sel)))
	return h.col("assistant", parts, { gap = "md" })
end

-- ── Admin › Skills ─────────────────────────────────────────────────────────

-- `GET /v1/admin/skills` lists `{sources: [{repo, path, ref, roles, repos, scope, sha, skills, error}]}`.
function V.skill_sources(state: any): { any }
	local doc = state.data.skills
	if type(doc) ~= "table" or type(doc.sources) ~= "table" then
		return {}
	end
	return doc.sources
end

local function skills(state: any): Node
	local items = V.skill_sources(state)
	if #items == 0 then
		return h.empty("skills", "Skills", "No central skill sources configured. Add one in WORKFLOW.md under skills.sources or with symphony setup skills.", nil)
	end
	local rows: { Node } = {}
	for _, src in items do
		local repo = tostring(src.repo or "?")
		local path = tostring(src.path or "skills")
		local scope = if src.repos then "repos" else "global"
		local sha = if src.sha then string.sub(tostring(src.sha), 1, 8) else "—"
		local skill_names = src.skills
		local count = if type(skill_names) == "table" then #skill_names else 0
		local err = src.error
		local tone = if err then "error" elseif count > 0 then "positive" else "muted"
		local detail_text = if err then tostring(err) else tostring(count) .. " skills"
		local spans: Spans = { ui.span(repo, "strong"), ui.span("  " .. path .. "  ", "muted"), ui.span(scope, "muted"), ui.span("  " .. sha, "muted"), ui.span("  " .. detail_text, tone) }
		table.insert(rows, ui.text(spans))
	end
	return h.col("skills", rows, { gap = "sm" })
end

local function admin(state: any): Node
	if not apimod.can(state.api, "admin") then
		local role = tostring(state.api.creds and state.api.creds.role or "?")
		return h.empty("admin", "Administration", "Models, providers, tokens, MCP servers and the assistant's memory need an admin token. Your role: " .. role .. ".", nil)
	end
	local labels = { models = "Models", providers = "Providers", tokens = "Tokens", mcp = "MCP", skills = "Skills", omp = "Team omp", assistant = "Assistant" }
	local items: { any } = {}
	for _, t in V.ADMIN_TABS do
		table.insert(items, { id = t, label = labels[t] })
	end
	local body = ({ models = models, providers = providers, tokens = tokens, mcp = mcp, skills = skills, omp = omp, assistant = assistant })[state.admin_tab] or models
	return h.col("admin", {
		ui.node("tabs", { key = "sub", variant = "segmented", active = state.admin_tab, items = items }),
		body(state),
	}, { gap = "md" })
end

-- ── page ───────────────────────────────────────────────────────────────────

local function connection(state: any): Node?
	local a = state.api
	if a.auth == "checking" then
		return h.loading(state.view, "Connecting to Symphony…")
	elseif a.auth == "logged_out" then
		return h.empty(state.view, "You are not signed in to Symphony",
			"Sign in with your team's Symphony address and an API token. The token stays in the keychain, never in Tern.",
			h.action_key("Sign in", "login", "enter", "accent"))
	elseif a.auth == "cli_missing" then
		return h.error_card(state.view, "symphony CLI not found", (a.auth_error and a.auth_error.message) or "", "refresh")
	elseif a.auth == "error" then
		return h.error_card(state.view, "Could not read the credential", (a.auth_error and a.auth_error.message) or "", "refresh")
	end
	if not state.data.overview then
		if state.errs.overview then
			return h.error_card(state.view, "Could not reach Symphony", state.errs.overview, "refresh")
		end
		return h.loading(state.view, "Loading the dashboard…")
	end
	return nil
end

local VIEW_FN = {
	dashboard = dashboard, sessions = sessions_view, tickets = tickets, merger = merger, admin = admin,
	repositories = P.repos, infrastructure = P.infra, telemetry = P.telemetry, learnings = P.learning, audit = P.audit,
}

-- Keys everyone has, then the ones that act (operator and up), per view.
local LEGENDS = {
	dashboard = { { "tab", "tabs" }, { "↑↓", "session" }, { "enter", "chat" }, { "r", "refresh" } },
	sessions = { { "tab", "tabs" }, { "[ ]", "filter" }, { "↑↓", "session" }, { "enter", "chat" }, { "r", "refresh" } },
	tickets = { { "tab", "tabs" }, { "r", "refresh" } },
	merger = { { "tab", "tabs" }, { "↑↓", "PR" }, { "r", "refresh" } },
	repositories = { { "tab", "tabs" }, { "↑↓", "repository" }, { "r", "refresh" } },
	infrastructure = { { "tab", "tabs" }, { "r", "refresh" } },
	telemetry = { { "tab", "tabs" }, { "[ ]", "period" }, { "r", "refresh" } },
	learnings = { { "tab", "tabs" }, { "r", "refresh" } },
	audit = { { "tab", "tabs" }, { "m", "load more" }, { "r", "refresh" } },
	admin = { { "tab", "tabs" }, { "r", "refresh" } },
}

local ACTION_LEGENDS = {
	dashboard = { { "a", "answer" }, { "m", "message" }, { "i", "interrupt" }, { "t", "take over" } },
	sessions = { { "a", "answer" }, { "m", "message" }, { "i", "interrupt" }, { "t", "take over" } },
	merger = { { "b", "bypass" }, { "q", "requeue" }, { "l", "release" } },
}

local ADMIN_LEGENDS = {
	models = { { "[ ]", "section" }, { "↑↓", "role" }, { "enter", "change model" }, { "t", "thinking" }, { "p", "publish" } },
	providers = { { "[ ]", "section" }, { "↑↓", "provider" }, { "c", "set key" } },
	tokens = { { "[ ]", "section" }, { "↑↓", "token" }, { "n", "new token" }, { "x", "revoke" } },
	mcp = { { "[ ]", "section" }, { "↑↓", "server" }, { "enter", "test" }, { "n", "add" }, { "e", "edit" }, { "h", "headers" }, { "d", "enable or disable" }, { "x", "remove" } },
	omp = { { "[ ]", "section" }, { "s", "apply to my omp" } },
	assistant = { { "[ ]", "section" }, { "↑↓", "person" }, { "x", "wipe" } },
}

-- While the MCP card holds a form, every letter goes to its fields.
local MCP_FORM_LEGEND = { { "tab", "field" }, { "←→", "choice" }, { "⌘V", "paste" }, { "enter", "save" }, { "esc", "cancel" } }
local REPO_FORM_LEGEND = { { "tab", "field" }, { "←→", "runtime" }, { "⌘V", "paste" }, { "enter", "save" }, { "esc", "cancel" } }


local function legend_for(state: any): { { string } }
	if state.api.auth == "logged_out" then
		return { { "enter", "sign in" }, { "r", "check again" } }
	elseif state.api.auth ~= "ok" then
		return { { "r", "try again" } }
	end
	if state.view == "admin" and state.admin_tab == "mcp" and state.mcp_form then
		return table.clone(MCP_FORM_LEGEND)
	end
	if state.view == "repositories" and state.repo_form then
		return table.clone(REPO_FORM_LEGEND)
	end
	local out = table.clone(LEGENDS[state.view] or LEGENDS.dashboard)
	local extra = nil
	if state.view == "admin" then
		extra = if apimod.can(state.api, "admin") then ADMIN_LEGENDS[state.admin_tab] else nil
	elseif apimod.can(state.api, "operator") then
		extra = ACTION_LEGENDS[state.view]
	end
	for _, e in extra or {} do
		table.insert(out, e)
	end
	if state.view == "repositories" and apimod.can(state.api, "admin") then
		table.insert(out, { "e", "configure" })
	end
	if (state.view == "dashboard" or state.view == "sessions") and apimod.can(state.api, "operator")
		and #V.session_leases(V.find_session(state, state.sel[state.view])) > 0 then
		table.insert(out, { "w", "watch device" })
		table.insert(out, { "⇧W", "with control" })
	end
	if apimod.can(state.api, "operator") then
		local ov = state.data.overview
		local paused = ov and type(ov.daemon) == "table" and ov.daemon.paused
		table.insert(out, { "⌃P", if paused then "resume" else "pause" })
	end
	local setup = state.data.setup
	if type(setup) == "table" and setup.complete == false and apimod.can(state.api, "admin") then
		table.insert(out, { "⌃S", "setup" })
	end
	return out
end

function V.render(state: any): BlockView
	local body = connection(state)
	if not body then
		body = (VIEW_FN[state.view] or dashboard)(state)
	end
	local main = ui.node("col", { gap = "lg" }, { head(state), nav(state), body :: Node })
	local dock: { Node } = {}
	if state.flash then
		table.insert(dock, h.with(ui.text({ ui.span(state.flash.text, state.flash.tone) }), { key = "flash" }))
	end
	if state.offline and state.data.overview then
		table.insert(dock, h.with(ui.text({ ui.span("No connection to Symphony. Showing the last data. " .. state.offline, "error") }), { key = "offline" }))
	end
	if state.confirm then
		-- The confirm strip sits above the key legend, in sight while the page scrolls.
		table.insert(dock, h.confirm(state.confirm.title, state.confirm.line, state.confirm.ok, state.confirm.tone))
	end
	table.insert(dock, h.legend(legend_for(state)))
	return { main = main, dock = ui.col(dock) }
end

return V
