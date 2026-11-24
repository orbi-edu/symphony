--!strict
-- Block `symphony.app`: Dashboard, Sessions, Tickets, Merger and Admin in one page.
--
-- Reads: the host keeps the token in memory (api.luau) and polls `GET /v1/overview` with
-- `If-None-Match` every 2 s, plus the endpoints the visible view needs. Writes: always through
-- the CLI (`symphony say|answer|interrupt|api …`). What only a window can do (prompts, pickers,
-- new tabs) is asked for by opening a `symphony://ui?req=…` link that the window half routes;
-- the window answers by sending an `action` event `w_<name>` back to this pane.

local apimod = require("./api")
local cli = require("./cli")
local fmt = require("./fmt")
local V = require("./views")
local P = require("./projections")

local M = {}

local POLL_MS = 2000
local AUTH_RETRY_MS = 5000
local FLASH_MS = 6000
-- Relative times ("12 s ago") are text: re-render this often even when no poll changed anything.
local CLOCK_MS = 10000
-- Audited reasons (pause, bypass, publication): the API's rule for commands is 8–1000 characters.
local REASON_MIN = 8

-- Which data each view polls, besides the overview.
local PATHS = {
	overview = "/v1/overview",
	sessions = "/v1/sessions?limit=100",
	issues = "/v1/issues",
	merger = "/v1/merger",
	config = "/v1/admin/config",
	observed = "/v1/admin/config/observed",
	providers = "/v1/admin/providers",
	tokens = "/v1/admin/tokens",
	catalog = "/v1/admin/catalog",
	mcp = "/v1/admin/mcp",
	skills = "/v1/admin/skills",
	repos = "/v1/repos",
	infra = "/v1/infra",
	telemetry = "/v1/telemetry?limit=50",
	learning = "/v1/learning",
	audit = "/v1/audit?limit=50",
	setup = "/v1/setup",
	assistant = "/v1/assistant/memory",
}

-- Projections without an ETag embed `observedAt`, so every read differs: these are read every
-- PROJECTION_MS instead of every poll (and right away when their view opens).
local PROJECTION_MS = 10000
local PROJECTIONS = { repos = true, infra = true, telemetry = true, learning = true, audit = true }
local VIEW_DATA = { repositories = "repos", infrastructure = "infra", telemetry = "telemetry", learnings = "learning", audit = "audit" }
-- `GET /v1/setup` is read every SETUP_MS until it says complete (setup contract §4).
local SETUP_MS = 30000
-- Admin › Assistant: every read of memory is audited, so these are read once when the tab opens,
-- when the selected person changes, after a wipe and on Refresh, never on the poll.
local READ_ONCE = { assistant = true, assistant_person = true }

local function read_path(state: any, name: string): string?
	if name == "assistant_person" then
		local login = state.sel.assistant
		return if login then "/v1/assistant/memory/" .. fmt.urlencode(login) else nil
	end
	return PATHS[name] or ("/v1/" .. name)
end

local function running(pane: number): boolean
	for _, p in tern.pane.list() do
		if p.pane == pane then
			return true
		end
	end
	return false
end

local function needs(state: any): { string }
	local v = state.view
	if v == "dashboard" or v == "sessions" then
		-- Issues carry `held` (internals §12): the session detail offers "Return to Symphony" from it.
		return { "issues" }
	elseif v == "tickets" then
		return { "issues" }
	elseif v == "merger" then
		return { "merger" }
	elseif VIEW_DATA[v] then
		if v == "audit" and state.audit_paged then
			return {}
		end
		return { VIEW_DATA[v] }
	elseif v == "admin" then
		if not apimod.can(state.api, "admin") then
			return {}
		end
		local t = state.admin_tab
		if t == "models" then
			return { "config", "observed", "catalog" }
		elseif t == "providers" then
			-- The catalog says which built-in providers this omp has (internals §13).
			return { "providers", "catalog" }
		elseif t == "tokens" then
			return { "tokens" }
		elseif t == "mcp" then
			return { "mcp" }
		elseif t == "assistant" then
			return { "assistant", "assistant_person" }
		elseif t == "skills" then
			return { "skills" }
		end
		return { "config" }
	end
	return {}
end

local function flash(state: any, tone: string, text: string)
	state.flash = { tone = tone, text = text, at = tern.now() }
end

local function fail(state: any, what: string, err: cli.CliError?)
	flash(state, "error", what .. ": " .. cli.describe(err))
end

local poll: (state: any) -> ()

local function fetch(state: any, name: string)
	local cx = state.cx
	if PROJECTIONS[name] or name == "setup" then
		local last = state.read_at[name]
		local every = if name == "setup" then SETUP_MS else PROJECTION_MS
		if last and state.data[name] and tern.now() - last < every then
			return
		end
		state.read_at[name] = tern.now()
	end
	local path = read_path(state, name)
	if not path then
		return
	end
	if READ_ONCE[name] then
		if state.read_at[name] == path then
			return
		end
		state.read_at[name] = path
		apimod.invalidate(state.api, path)
	end
	apimod.get(state.api, path, function(changed, data, err)
		if state.stopped then
			return
		end
		if err then
			local text = cli.describe(err)
			local was = state.errs[name]
			state.errs[name] = text
			if READ_ONCE[name] then
				state.read_at[name] = nil -- try again on the next poll
			end
			if name == "overview" and (err.code == "unreachable" or err.code == "timeout") then
				state.offline = text
			end
			if err.code == "unauthorized" then
				state.api.auth = "checking"
			end
			if was ~= text then
				cx:render()
			end
			return
		end
		local recovered = state.errs[name] ~= nil or (name == "overview" and state.offline ~= nil)
		state.errs[name] = nil
		if name == "overview" then
			state.offline = nil
			state.updated_ms = fmt.now_ms()
		end
		if name == "audit" and changed and type(data) == "table" then
			state.audit_items = data.items or {}
			state.audit_next = data.nextCursor
		end
		if changed then
			state.data[name] = data
			if name == "overview" and type(data) == "table" then
				local cfg = state.data.config
				if cfg and data.config_generation and data.config_generation ~= cfg.generation then
					apimod.invalidate(state.api, PATHS.config)
					apimod.invalidate(state.api, PATHS.observed)
				end
			end
			if name == "mcp" then
				state.mcp_pending = nil
			end
			if name == "tokens" and type(data) == "table" and type(data.items) == "table" then
				-- Live tokens first, newest first; the API keeps creation order.
				table.sort(data.items, function(a, b)
					local ra, rb = a.revoked_at ~= nil, b.revoked_at ~= nil
					if ra ~= rb then
						return not ra
					end
					return (fmt.to_ms(a.created_at) or 0) > (fmt.to_ms(b.created_at) or 0)
				end)
			end
			if name == "catalog" and type(data) == "table" then
				-- Keep only what the picker needs; the decoded catalog is megabytes.
				local summary = {}
				for _, p in data.providers or {} do
					table.insert(summary, { id = p.id, n = #(p.models or {}), key = p.credential == "apiKey" })
				end
				state.data.catalog = summary
				state.api.cache[PATHS.catalog] = summary
			end
			-- An admin who signs in to a backend that is not set up gets the Setup view once per
			-- panel; everyone else sees the banner on the Dashboard.
			if name == "setup" and type(data) == "table" and data.complete == false
				and apimod.can(state.api, "admin") and not state.setup_opened then
				state.setup_opened = true
				cx:open(fmt.ui_link({ kind = "setup", auto = true, pane = cx.pane }))
			end
		end
		if changed or recovered then
			cx:render()
		end
	end)
end

poll = function(state: any)
	local api = state.api
	if api.auth ~= "ok" then
		local now = tern.now()
		if api.auth == "checking" or now - (state.auth_at or 0) >= AUTH_RETRY_MS then
			state.auth_at = now
			apimod.authenticate(api, function()
				if state.stopped then
					return
				end
				state.cx:render()
				if api.auth == "ok" then
					poll(state)
				end
			end)
		end
		return
	end
	-- Sessions back the tab counts and the Painel, so they follow the overview everywhere.
	fetch(state, "overview")
	fetch(state, "sessions")
	-- Until the backend says it is set up (cheap; throttled to SETUP_MS).
	if not (type(state.data.setup) == "table" and state.data.setup.complete == true) then
		fetch(state, "setup")
	end
	-- Opening Admin › Assistant reads its data again (READ_ONCE).
	local on_assistant = state.view == "admin" and state.admin_tab == "assistant"
	if on_assistant and not state.assistant_open then
		state.read_at.assistant, state.read_at.assistant_person = nil, nil
	end
	state.assistant_open = on_assistant
	for _, name in needs(state) do
		fetch(state, name)
	end
end

local function refresh_now(state: any, names: { string }?)
	for _, name in names or {} do
		apimod.invalidate(state.api, PATHS[name])
	end
	poll(state)
end

-- Opens a window-only UI (prompt, picker, chat or takeover tab) for this pane.
local function ask(state: any, req: { [string]: any })
	req.pane = state.cx.pane
	state.cx:open(fmt.ui_link(req))
end

local function cli_action(state: any, key: string, args: { string }, done_text: string, refresh: { string }?, after: ((data: any) -> ())?)
	if state.busy[key] then
		return
	end
	state.busy[key] = true
	local ok, err = cli.run(args, nil, function(data, e)
		state.busy[key] = nil
		if state.stopped then
			return
		end
		if e then
			fail(state, "Failed", e)
		else
			flash(state, "success", done_text)
			if after then
				after(data)
			end
		end
		state.cx:render()
		refresh_now(state, refresh)
	end)
	if not ok then
		state.busy[key] = nil
		fail(state, "Failed", err)
	end
end

local function api_action(state: any, key: string, method: string, path: string, body: any, headers: { [string]: string }?, done_text: string, refresh: { string }?, after: ((data: any) -> ())?)
	if state.busy[key] then
		return
	end
	state.busy[key] = true
	local ok, err = cli.api(method, path, body, headers, function(data, e)
		state.busy[key] = nil
		if state.stopped then
			return
		end
		if e then
			fail(state, "Failed", e)
		else
			if done_text ~= "" then
				flash(state, "success", done_text)
			end
			if after then
				after(data)
			end
		end
		state.cx:render()
		refresh_now(state, refresh)
	end)
	if not ok then
		state.busy[key] = nil
		fail(state, "Failed", err)
	end
end

-- ── selection ──────────────────────────────────────────────────────────────

local function slot(state: any): string?
	local v = state.view
	if v == "dashboard" or v == "sessions" or v == "merger" then
		return v
	elseif v == "repositories" then
		return "repositories"
	elseif v == "admin" then
		local t = state.admin_tab
		if t == "models" or t == "providers" or t == "tokens" or t == "mcp" or t == "assistant" then
			return t
		end
	end
	return nil
end

local function slot_ids(state: any, s: string): { string }
	local ids = {}
	if s == "dashboard" then
		for _, x in V.active_sessions(state) do
			table.insert(ids, x.sid)
		end
	elseif s == "sessions" then
		for _, x in V.filtered_sessions(state) do
			table.insert(ids, x.sid)
		end
	elseif s == "merger" then
		for _, x in V.merger_actionables(state) do
			table.insert(ids, x.id)
		end
	elseif s == "repositories" then
		for _, r in P.repo_items(state) do
			table.insert(ids, tostring(r.name))
		end
	elseif s == "models" then
		local cfg = state.data.config
		for _, r in fmt.roles_of(cfg and cfg.models) do
			table.insert(ids, r)
		end
	elseif s == "providers" then
		for _, r in V.provider_rows(state) do
			table.insert(ids, r.id)
		end
	elseif s == "tokens" then
		local doc = state.data.tokens
		for _, t in (doc and doc.items) or {} do
			table.insert(ids, t.id)
		end
	elseif s == "mcp" then
		for _, x in V.mcp_servers(state) do
			table.insert(ids, x.name)
		end
	elseif s == "assistant" then
		for _, p in V.memory_people(state) do
			table.insert(ids, p.login)
		end
	end
	return ids
end

-- The selected id of the current list, defaulting to its first row.
local function current(state: any): string?
	local s = slot(state)
	if not s then
		return nil
	end
	local ids = slot_ids(state, s)
	local sel = state.sel[s]
	for _, id in ids do
		if id == sel then
			return id
		end
	end
	-- A server just saved is selected before the list that holds it is read back.
	if s == "mcp" and sel ~= nil and sel == state.mcp_pending then
		return sel
	end
	if ids[1] then
		state.sel[s] = ids[1]
	end
	return ids[1]
end

local function move(state: any, step: number): boolean
	local s = slot(state)
	if not s then
		return false
	end
	local ids = slot_ids(state, s)
	if #ids == 0 then
		return false
	end
	local at = table.find(ids, current(state) or "") or 1
	state.sel[s] = ids[math.clamp(at + step, 1, #ids)]
	if s == "assistant" then
		poll(state) -- read the newly selected person's memory now
	end
	return true
end

-- ── actions ────────────────────────────────────────────────────────────────

local function session(state: any, sid: string?): any
	return V.find_session(state, sid)
end

local function do_chat(state: any, sid: string)
	local s = session(state, sid)
	ask(state, { kind = "chat", sid = sid, label = s and s.identifier or sid })
end

local function do_say(state: any, sid: string)
	local s = session(state, sid)
	local who = s and s.identifier or sid
	local hint = if s and s.state == "running" then " (joins the current turn)" else " (goes in the next turn)"
	ask(state, { kind = "prompt", act = "w_say", ctx = { sid = sid, label = who }, placeholder = "Message for " .. who .. hint, action = "send", icon = "send" })
end

local function do_answer(state: any, sid: string)
	local s = session(state, sid)
	if not s then
		return
	end
	local req = type(s.input_request) == "table" and s.input_request or {}
	local q = fmt.cut(req.question or ("Answer for " .. (s.identifier or sid)), 120)
	local ctx = { sid = sid, label = s.identifier or sid }
	if type(req.options) == "table" and #req.options > 0 then
		local items = {}
		for _, o in req.options do
			table.insert(items, { label = tostring(o), value = tostring(o) })
		end
		ask(state, { kind = "pick", act = "w_answer", ctx = ctx, items = items, other = "Other answer…", placeholder = q, action = "answer", icon = "help" })
	else
		ask(state, { kind = "prompt", act = "w_answer", ctx = ctx, placeholder = q, action = "answer", icon = "help" })
	end
end

local function do_interrupt(state: any, sid: string)
	local s = session(state, sid)
	local who = s and s.identifier or sid
	state.confirm = {
		title = "Interrupt " .. who .. "?",
		line = "The agent stops the current turn right away. The session stays open, and you can send a message later.",
		ok = "Interrupt",
		tone = "error",
		run = function()
			cli_action(state, "interrupt:" .. sid, { "interrupt", sid, "--reason", "Interrupted from Tern", "--json" }, "Interrupt sent to " .. who, { "sessions" })
		end,
	}
end

local function do_takeover(state: any, sid: string)
	local s = session(state, sid)
	if not s then
		return
	end
	local who = s.identifier or sid
	state.confirm = {
		title = "Take over " .. who .. "?",
		line = "Symphony pauses this attempt, downloads the conversation and the branch, and opens omp in a new tab so you can continue from here.",
		ok = "Take over",
		tone = "warning",
		run = function()
			ask(state, { kind = "takeover", ticket = who })
		end,
	}
end

-- Opens a fresh viewer link (one use, 60 s) for a device the session holds, in Tern's browser
-- beside the panel: `symphony mcp viewer NAME LEASE [--control] --json`.
local function open_viewer(state: any, lease: any, control: boolean)
	local key = "watch:" .. lease.server .. ":" .. lease.id
	if state.busy[key] then
		return
	end
	state.busy[key] = true
	local what = V.lease_label(lease)
	local args = { "mcp", "viewer", lease.server, lease.id }
	if control then
		table.insert(args, "--control")
	end
	table.insert(args, "--json")
	local ok, err = cli.run(args, { timeout_ms = 30000 }, function(data, e)
		state.busy[key] = nil
		if state.stopped then
			return
		end
		local url = type(data) == "table" and data.url or nil
		if e and e.code == "lease_not_held" then
			flash(state, "warning", "No Symphony session holds " .. what .. " now. The agent may have released it.")
			refresh_now(state, { "sessions" })
		elseif e and string.find(e.message or "", "iOS simulators only", 1, true) then
			flash(state, "warning", "The live screen supports iOS simulators only for now. " .. what .. " has no screen to watch.")
		elseif e then
			fail(state, "Could not open the screen of " .. what, e)
		elseif type(url) ~= "string" or not string.match(url, "^https?://[^%s]+$") then
			flash(state, "error", "Could not open the screen of " .. what .. ". The viewer link is not a web address.")
		else
			ask(state, { kind = "watch", url = url })
			flash(state, "success", "Opening the screen of " .. what .. " beside this panel, " .. (if control then "with control." else "view only."))
		end
		state.cx:render()
	end)
	if not ok then
		state.busy[key] = nil
		fail(state, "Could not open the screen of " .. what, err)
	end
end

-- With control, a confirm strip first: the agent keeps using the device and others may watch it.
local function watch_lease(state: any, s: any, lease: any, control: boolean)
	if not control then
		open_viewer(state, lease, false)
		return
	end
	local what = V.lease_label(lease)
	state.confirm = {
		title = "Control " .. what .. " of " .. (s.identifier or s.sid) .. "?",
		line = "Your taps, swipes and typing reach the device at once, while the agent uses it. Others may be watching: people who open it from Symphony or the Seiji plugin, and links the agent shared, see what you do. Seiji holds your input while a run uses the device.",
		ok = "Watch with control",
		tone = "warning",
		run = function()
			open_viewer(state, lease, true)
		end,
	}
end

-- Returns true when it asked the window to pick one of several devices.
local function do_watch(state: any, sid: string, control: boolean): boolean
	local s = session(state, sid)
	local leases = V.session_leases(s)
	if #leases == 0 then
		flash(state, "muted", "This session holds no device.")
		return false
	end
	if #leases == 1 then
		watch_lease(state, s, leases[1], control)
		return false
	end
	local items = {}
	for _, l in leases do
		table.insert(items, { label = V.lease_label(l), detail = l.server .. " · " .. l.id, value = l.server .. " " .. l.id })
	end
	ask(state, { kind = "pick", act = "w_watch", ctx = { sid = sid, control = control }, items = items,
		placeholder = "Watch which device of " .. (s.identifier or sid) .. "?", action = "watch", icon = "monitor" })
	return true
end

local function do_command(state: any, action: string, reason: string)
	api_action(state, "cmd:" .. action, "POST", "/v1/commands", { action = action, reason = reason }, nil,
		if action == "pause" then "Pause requested from the daemon" else "Daemon resumed", { "overview" })
end

local function ref_equal(a: any, b: any): boolean
	return a and b and a.provider == b.provider and a.model == b.model and (a.thinking or "medium") == (b.thinking or "medium")
end

local function stage(state: any, role: string, ref: any)
	local cfg = state.data.config
	local live = cfg and type(cfg.models) == "table" and cfg.models[role] or nil
	if ref_equal(ref, live) then
		state.draft[role] = nil
	else
		state.draft[role] = ref
	end
end

local function draft_count(state: any): number
	local n = 0
	for _ in state.draft do
		n += 1
	end
	return n
end

local function do_publish(state: any, reason: string)
	local cfg = state.data.config
	if not cfg or draft_count(state) == 0 then
		return
	end
	local gen = tostring(cfg.generation or 0)
	local models = {}
	for role, ref in state.draft do
		models[role] = ref
	end
	api_action(state, "publish", "PUT", "/v1/admin/config", { models = models, reason = reason }, { ["If-Match"] = gen }, "", { "config", "observed", "overview" }, function(doc)
		state.draft = {}
		local g = type(doc) == "table" and doc.generation or (tonumber(gen) :: number) + 1
		flash(state, "success", "Generation " .. tostring(g) .. " published. Consumers apply it at their next unit of work.")
	end)
end

-- Saves a key: `{builtin: true, apiKey}` unless `id` is a stored own provider, which is sent
-- whole with its new key (internals §13).
local function do_apikey(state: any, id: string, key: string)
	local doc = state.data.providers
	if not doc then
		return
	end
	local item = nil
	for _, p in doc.items or {} do
		if p.id == id then
			item = p
		end
	end
	local body: { [string]: any }
	if item and not item.builtin then
		body = {}
		for k, v in item do
			if k ~= "id" and k ~= "credentialConfigured" then
				body[k] = v
			end
		end
		body.apiKey = key
	else
		body = { builtin = true, apiKey = key }
	end
	local name = (item and item.name) or V.PROVIDER_NAME[id] or id
	api_action(state, "apikey:" .. id, "PUT", "/v1/admin/providers/" .. id, body, { ["If-Match"] = '"' .. tostring(doc.revision) .. '"' },
		"Key for " .. name .. " saved. It applies from each agent's next attempt.", { "providers" })
end

local function ask_key(state: any, id: string)
	local name = V.PROVIDER_NAME[id] or id
	for _, r in V.provider_rows(state) do
		if r.id == id then
			name = r.name or id
		end
	end
	ask(state, { kind = "prompt", act = "w_apikey", ctx = { provider = id }, placeholder = "API key for " .. name .. " (write-only)", action = "save", icon = "key" })
end

-- Built-in providers of the catalog that the list does not show yet.
local function ask_other_builtin(state: any)
	local shown = {}
	for _, r in V.provider_rows(state) do
		shown[r.id] = true
	end
	local items = {}
	for _, p in state.data.catalog or {} do
		-- Only providers that take an API key (internals §13); host-credential ones can't.
		if p.key and not shown[p.id] then
			table.insert(items, { label = p.id, detail = fmt.plural(p.n or 0, "model", "models"), value = p.id })
		end
	end
	if #items == 0 then
		flash(state, "muted", "Every catalog provider is already in the list")
		state.cx:render()
		return
	end
	table.sort(items, function(a, b)
		return a.label < b.label
	end)
	ask(state, { kind = "pick", act = "w_builtin", ctx = {}, items = items, placeholder = "Provider built into omp (type to filter)", action = "choose", icon = "server" })
end

local function do_token(state: any, req: any)
	local body: { [string]: any } = { name = req.name, role = req.role }
	if tonumber(req.expires_days) then
		body.expires_days = tonumber(req.expires_days)
	end
	api_action(state, "token", "POST", "/v1/admin/tokens", body, nil, "", { "tokens" }, function(doc)
		if type(doc) == "table" and type(doc.token) == "string" then
			state.created = { token = doc.token, name = doc.name or req.name }
			flash(state, "success", "Token " .. tostring(doc.name or req.name) .. " created. Copy it now.")
		end
	end)
end

local function do_revoke(state: any, id: string)
	local name = id
	for _, t in (state.data.tokens and state.data.tokens.items) or {} do
		if t.id == id and t.name then
			name = t.name .. " (" .. id .. ")"
		end
	end
	state.confirm = {
		title = "Revoke the token " .. name .. "?",
		line = "Anyone who uses this token loses access right away, in the CLI, Tern and MCP. You cannot undo this.",
		ok = "Revoke",
		tone = "error",
		run = function()
			api_action(state, "revoke:" .. id, "DELETE", "/v1/admin/tokens/" .. id, nil, nil, "Token " .. id .. " revoked", { "tokens" })
		end,
	}
end

-- Admin › Assistant: wipe one person's memory, after the confirm strip.
local function do_memory_wipe(state: any, login: string)
	local n = 0
	for _, p in V.memory_people(state) do
		if p.login == login then
			n = p.count or 0
		end
	end
	state.confirm = {
		title = "Wipe the memory of " .. login .. "?",
		line = "The assistant forgets " .. fmt.plural(n, "item", "items") .. " about " .. login
			.. " at once. The audit log records it. You cannot undo this.",
		ok = "Wipe",
		tone = "error",
		run = function()
			api_action(state, "memory_wipe:" .. login, "DELETE", "/v1/assistant/memory/" .. fmt.urlencode(login), nil, nil,
				"Memory of " .. login .. " wiped", nil, function()
					state.read_at.assistant, state.read_at.assistant_person = nil, nil
					state.data.assistant_person = nil
				end)
		end,
	}
end

-- `ref` is `<pr>@<repo>`, as the merger view builds it: actions name the repository's merger.
local function do_merger(state: any, action: string, ref: string, reason: string?)
	local pr, repo = string.match(ref, "^(%d+)@(.+)$")
	if not pr then
		return
	end
	local words = { bypass = "Bypassed ", requeue = "Requeued ", release = "Released " }
	api_action(state, action .. ":" .. ref, "POST", "/v1/merger/prs/" .. pr .. "/" .. action,
		{ reason = reason or ("From Tern (" .. action .. ")"), repo = repo }, nil,
		(words[action] or "") .. repo .. " #" .. pr, { "merger" })
end

-- ── Admin › MCP ────────────────────────────────────────────────────────────
-- Every write is `symphony admin mcp … --json`. Header values go to the CLI on stdin, never in
-- an argument, and leave memory once saved.

local MCP_NAME_MAX = 48

local function trim(s: string): string
	return string.match(s, "^%s*(.-)%s*$") or ""
end

local function mcp_input_path(id: string): string
	return "main.admin.mcp.md.detail.form." .. id .. ".input"
end

local function mcp_field(state: any, id: string?): any
	local form = state.mcp_form
	if not form then
		return nil
	end
	for _, f in V.mcp_fields(form) do
		if f.id == id then
			return f
		end
	end
	return nil
end

local function mcp_focus(state: any, cx: any)
	local f = mcp_field(state, state.mcp_form and state.mcp_form.focus)
	if f and not f.choice then
		cx:frame({ { "focus", mcp_input_path(f.id) } })
	end
end

-- The form values of a stored definition (an edit starts from them; a toggle re-sends them,
-- since `admin mcp set` replaces the whole definition).
local function mcp_values(s: any): { [string]: string }
	local env = {}
	for k, v in (type(s.env) == "table" and s.env) or {} do
		table.insert(env, tostring(k) .. "=" .. tostring(v))
	end
	table.sort(env)
	local args = {}
	for _, a in (type(s.args) == "table" and s.args) or {} do
		table.insert(args, tostring(a))
	end
	return {
		name = s.name,
		transport = tostring(s.transport or "http"),
		url = tostring(s.url or ""),
		command = tostring(s.command or ""),
		args = table.concat(args, " "),
		env = table.concat(env, " "),
		roles = table.concat((type(s.roles) == "table" and s.roles) or {}, ", "),
		enabled = if s.enabled == false then "off" else "on",
	}
end

local function mcp_open(state: any, cx: any, mode: string, name: string?)
	local s = name and V.mcp_find(state, name) or nil
	if mode ~= "add" and not s then
		return
	end
	local values: { [string]: string }
	if mode == "add" then
		values = { preset = "custom", name = "", transport = "http", url = "", command = "", args = "", env = "", roles = "", enabled = "on", header = "", value = "" }
	elseif mode == "edit" then
		values = mcp_values(s)
	else
		local set = (s and type(s.headers_set) == "table" and s.headers_set) or {}
		values = { header = tostring(set[1] or "Authorization"), value = "" }
	end
	state.confirm = nil
	state.mcp_form = { mode = mode, name = name, values = values, base = s, error = nil }
	for _, f in V.mcp_fields(state.mcp_form) do
		if not f.choice then
			state.mcp_form.focus = f.id
			break
		end
	end
	mcp_focus(state, cx)
end

-- A choice in the form changed. The Seiji preset fills what it decides: Streamable HTTP, the
-- Authorization header and, when empty, the name.
local function mcp_choice(form: any, field: string, value: string)
	form.values[field] = value
	form.focus = field
	form.error = nil
	if field == "preset" and V.MCP_PRESETS[value] then
		local v = form.values
		v.transport = "http"
		if trim(v.header or "") == "" then
			v.header = "Authorization"
		end
		if trim(v.name or "") == "" then
			v.name = value
		end
	end
end

-- `admin mcp set NAME …`: the full definition. Values that start with "-" go as `--arg=…`. A new
-- server with a preset sends `--preset` and the fields the form shows; the CLI fills the rest.
-- Re-sending a stored server keeps every field the form does not edit.
local function mcp_set_args(name: string, v: { [string]: string }, base: any): { string }
	local args = { "admin", "mcp", "set", name }
	if base == nil and V.MCP_PRESETS[v.preset or ""] then
		for _, a in { "--preset", v.preset, "--url", trim(v.url or "") } do
			table.insert(args, a)
		end
		for _, r in V.split_roles(v.roles) do
			table.insert(args, "--role")
			table.insert(args, r)
		end
		if v.enabled == "off" then
			table.insert(args, "--disabled")
		end
		table.insert(args, "--json")
		return args
	end
	table.insert(args, "--transport")
	table.insert(args, v.transport)
	if V.is_remote(v.transport) then
		table.insert(args, "--url")
		table.insert(args, trim(v.url or ""))
	else
		table.insert(args, "--command")
		table.insert(args, trim(v.command or ""))
		for a in string.gmatch(v.args or "", "%S+") do
			table.insert(args, "--arg=" .. a)
		end
		for e in string.gmatch(v.env or "", "%S+") do
			table.insert(args, "--env=" .. e)
		end
	end
	for _, r in V.split_roles(v.roles) do
		table.insert(args, "--role")
		table.insert(args, r)
	end
	if v.enabled == "off" then
		table.insert(args, "--disabled")
	end
	if type(base) == "table" and tonumber(base.timeout_ms) then
		table.insert(args, "--timeout-ms")
		table.insert(args, tostring(math.floor(tonumber(base.timeout_ms) :: number)))
	end
	if type(base) == "table" and base.instructions == true then
		table.insert(args, "--instructions")
	end
	if type(base) == "table" then
		if type(base.http_base) == "string" then
			table.insert(args, "--http-base=" .. base.http_base)
		end
		local env = {}
		for k, t in (type(base.agent_env) == "table" and base.agent_env) or {} do
			table.insert(env, tostring(k) .. "=" .. tostring(t))
		end
		table.sort(env)
		for _, e in env do
			table.insert(args, "--agent-env=" .. e)
		end
		for _, o in (type(base.rewrite_origins) == "table" and base.rewrite_origins) or {} do
			table.insert(args, "--rewrite-origin=" .. tostring(o))
		end
		local c = base.cleanup
		if type(c) == "table" then
			table.insert(args, "--cleanup=" .. table.concat({ tostring(c.acquire_tool), tostring(c.id_field), tostring(c.release_tool), tostring(c.release_arg) }, ":"))
		end
	end
	table.insert(args, "--json")
	return args
end

-- What is wrong with the form, in one sentence, or nil.
local function mcp_problem(state: any, form: any): string?
	local v = form.values
	if form.mode == "headers" or (form.mode == "add" and (trim(v.header or "") ~= "" or (v.value or "") ~= "")) then
		if not string.match(trim(v.header or ""), "^[%w%-]+$") then
			return "Enter the header name, for example Authorization."
		end
		if form.mode == "add" and (v.value or "") == "" then
			return if V.MCP_PRESETS[v.preset or ""]
				then "Enter the header value. It is Bearer and the Seiji token."
				else "Enter the header value, or clear the header name."
		end
	end
	if form.mode == "add" and V.MCP_PRESETS[v.preset or ""] and (v.value or "") == "" then
		return "Enter the header value. It is Bearer and the Seiji token."
	end
	if form.mode == "headers" then
		return nil
	end
	local name = if form.mode == "add" then trim(v.name or "") else form.name
	if #name > MCP_NAME_MAX or not string.match(name, "^[a-z0-9][a-z0-9_%-]*$") then
		return "Enter a name of lowercase letters, digits, - and _ (up to 48), starting with a letter or a digit."
	end
	if form.mode == "add" and V.mcp_find(state, name) then
		return "A server named " .. name .. " exists. Pick another name, or edit that server."
	end
	if V.is_remote(v.transport) then
		if not string.match(trim(v.url or ""), "^https?://[^%s]+$") then
			return "Enter the URL. It starts with https://."
		end
	elseif trim(v.command or "") == "" then
		return "Enter the command that starts the server."
	end
	local roles = V.split_roles(v.roles)
	if #roles == 0 then
		return "Pick at least one role."
	end
	local known = V.mcp_roles(state)
	for _, r in roles do
		if not table.find(known, r) then
			return "There is no role " .. r .. ". Roles: " .. table.concat(known, ", ") .. "."
		end
	end
	return nil
end

-- `{"headers": {name: value | null}}`: an empty value removes the header.
local function mcp_headers_stdin(header: string, value: string): string
	return '{"headers":{' .. tern.json.encode(header) .. ":" .. (if value == "" then "null" else tern.json.encode(value)) .. "}}"
end

-- Runs a form's CLI call; an error stays in the card, with what was typed.
local function mcp_run(state: any, args: { string }, stdin: string?, done: (data: any) -> ())
	state.busy.mcp_save = true
	local function failed(e: cli.CliError?)
		if state.mcp_form then
			state.mcp_form.error = cli.describe(e)
		else
			fail(state, "Failed", e)
		end
	end
	local ok, err = cli.run(args, { stdin = stdin, timeout_ms = 30000 }, function(data, e)
		state.busy.mcp_save = nil
		if state.stopped then
			return
		end
		if e then
			failed(e)
		else
			done(data)
		end
		state.cx:render()
		refresh_now(state, { "mcp" })
	end)
	if not ok then
		state.busy.mcp_save = nil
		failed(err)
	end
	state.cx:render()
end

local function mcp_save(state: any)
	local form = state.mcp_form
	if not form or state.busy.mcp_save then
		return
	end
	local problem = mcp_problem(state, form)
	if problem then
		form.error = problem
		state.cx:render()
		return
	end
	form.error = nil
	local v = form.values
	if form.mode == "headers" then
		local header, value = trim(v.header), v.value or ""
		mcp_run(state, { "admin", "mcp", "headers", form.name, "--json" }, mcp_headers_stdin(header, value), function()
			state.mcp_form = nil
			flash(state, "success", if value == ""
				then "Header " .. header .. " removed from " .. form.name
				else "Header " .. header .. " saved for " .. form.name .. ". The gateway sends it from the next call.")
		end)
		return
	end
	local name = if form.mode == "add" then trim(v.name) else form.name
	local header = trim(v.header or "")
	mcp_run(state, mcp_set_args(name, v, form.base), nil, function()
		state.sel.mcp = name
		state.mcp_pending = name
		state.mcp_tests[name] = nil
		local added = if form.mode == "add" then "Server " .. name .. " added" else "Server " .. name .. " saved"
		if form.mode == "add" and header ~= "" then
			-- The definition is stored: the form now only holds the header, until it is saved too.
			form.mode, form.name, form.values = "headers", name, { header = header, value = v.value }
			mcp_run(state, { "admin", "mcp", "headers", name, "--json" }, mcp_headers_stdin(header, v.value), function()
				state.mcp_form = nil
				flash(state, "success", added .. " with the header " .. header .. ". Press enter to test it.")
			end)
			return
		end
		state.mcp_form = nil
		flash(state, "success", added .. ". Agents get it when they next start. Press enter to test it.")
	end)
end

local function mcp_test(state: any, name: string)
	local key = "mcp_test:" .. name
	if state.busy[key] then
		return
	end
	state.busy[key] = true
	local ok, err = cli.run({ "admin", "mcp", "test", name, "--json" }, { timeout_ms = 60000 }, function(data, e)
		state.busy[key] = nil
		if state.stopped then
			return
		end
		-- A failed test exits 1 and prints `{ok: false, error: {code, message}}`.
		local r: any = if e then { ok = false, error = { code = e.code, message = e.message } } else (data or {})
		r.at = fmt.now_ms()
		state.mcp_tests[name] = r
		state.cx:render()
	end)
	if not ok then
		state.busy[key] = nil
		state.mcp_tests[name] = { ok = false, error = err, at = fmt.now_ms() }
	end
	state.cx:render()
end

local function mcp_toggle(state: any, name: string)
	local s = V.mcp_find(state, name)
	if not s then
		return
	end
	local v = mcp_values(s)
	local on = s.enabled == false
	v.enabled = if on then "on" else "off"
	cli_action(state, "mcp_toggle:" .. name, mcp_set_args(name, v, s),
		if on then "Server " .. name .. " enabled. Agents get it when they next start."
			else "Server " .. name .. " disabled. Agents stop getting it when they next start.", { "mcp" })
end

local function mcp_remove(state: any, name: string)
	if not V.mcp_find(state, name) then
		return
	end
	state.mcp_form = nil
	state.confirm = {
		title = "Remove the MCP server " .. name .. "?",
		line = "Agents stop getting its tools when they next start, and its stored headers are deleted. You cannot undo this.",
		ok = "Remove",
		tone = "error",
		run = function()
			cli_action(state, "mcp_remove:" .. name, { "admin", "mcp", "remove", name, "--json" }, "Server " .. name .. " removed", { "mcp" }, function()
				state.mcp_tests[name] = nil
				state.sel.mcp = nil
			end)
		end,
	}
end

-- Clicking a role chip adds it to the Roles field or takes it out.
local function mcp_toggle_role(state: any, role: string)
	local form = state.mcp_form
	if not form then
		return
	end
	local roles = V.split_roles(form.values.roles)
	local at = table.find(roles, role)
	if at then
		table.remove(roles, at)
	else
		table.insert(roles, role)
	end
	form.values.roles = table.concat(roles, ", ")
	form.error = nil
end

-- Keys while the form is open: every letter is text, tab moves, ←→ picks a choice.
local function mcp_form_key(state: any, k: Key, cx: any): boolean
	local form = state.mcp_form
	local n = k.name
	if n == "escape" then
		state.mcp_form = nil
		return true
	elseif n == "enter" then
		mcp_save(state)
		return true
	elseif n == "tab" then
		local list = V.mcp_fields(form)
		local at = 1
		for i, f in list do
			if f.id == form.focus then
				at = i
			end
		end
		form.focus = list[((at - 1 + (if k.shift then -1 else 1)) % #list) + 1].id
		mcp_focus(state, cx)
		return true
	end
	local f = mcp_field(state, form.focus)
	if not f then
		return false
	end
	if f.choice then
		if n == "left" or n == "right" or k.text == " " then
			local ids = {}
			for _, c in f.choice do
				table.insert(ids, c[1])
			end
			local at = table.find(ids, form.values[f.id]) or 1
			mcp_choice(form, f.id, ids[((at - 1 + (if n == "left" then -1 else 1)) % #ids) + 1])
		end
		return true
	end
	local cur = form.values[f.id] or ""
	if n == "paste" then
		form.values[f.id] = cur .. (string.gsub(k.text or "", "[\r\n]+", ""))
	elseif n == "backspace" then
		if k.alt then
			form.values[f.id] = ""
		else
			local len = utf8.len(cur) or 0
			form.values[f.id] = if len > 0 then string.sub(cur, 1, (utf8.offset(cur, len) or 1) - 1) else ""
		end
	elseif k.text and k.text ~= "" and not k.ctrl and not k.meta then
		form.values[f.id] = cur .. k.text
	else
		return false
	end
	form.error = nil
	return true
end

-- ── Repositories ──────────────────────────────────────────────────────────
-- Per-repository configuration: runtime, base branch and components. Saved with
-- `PUT /v1/setup/repositories` which takes the full maps for every repository.

local REPO_FIELDS = { "runtime", "base_branch", "components" }

local function repo_input_path(id: string): string
	return "main.repositories.md.detail." .. id .. ".input"
end

local function repo_focus(state: any, cx: any)
	local form = state.repo_form
	if form and form.focus ~= "runtime" then
		cx:frame({ { "focus", repo_input_path(form.focus) } })
	end
end

local function repo_open(state: any, cx: any, name: string)
	local r = P.find_repo(state, name)
	if not r or P.repo_readonly(state, r) then
		return
	end
	state.confirm = nil
	local values = P.repo_form_values(r)
	state.repo_form = { name = name, values = values, focus = "runtime", error = nil }
	repo_focus(state, cx)
end

-- Build the body for PUT /v1/setup/repositories: full maps with this repo's change merged.
local function repo_save_body(state: any): any
	local form = state.repo_form
	if not form then
		return nil
	end
	local name = form.name
	local v = form.values
	-- Start from the current maps across all repositories.
	local bb_map: { [string]: string? } = {}
	local rt_map: { [string]: string? } = {}
	local comp_map: { [string]: any } = {}
	for _, r in P.repo_items(state) do
		local rn = tostring(r.name)
		if r.base_branch then
			bb_map[rn] = r.base_branch
		end
		if r.configured_runtime then
			rt_map[rn] = r.configured_runtime
		end
		if r.components_source == "configured" and type(r.components) == "table" then
			comp_map[rn] = r.components
		end
	end
	-- Apply this repository's changes.
	local runtime = trim(v.runtime or "")
	if runtime ~= "" then
		rt_map[name] = runtime
	else
		rt_map[name] = nil
	end
	local base = trim(v.base_branch or "")
	if base ~= "" then
		bb_map[name] = base
	else
		bb_map[name] = nil
	end
	-- Parse components: "name=path@runtime" space-separated.
	local comp_text = trim(v.components or "")
	if comp_text ~= "" then
		local repo_comps: { [string]: any } = {}
		for entry in string.gmatch(comp_text, "%S+") do
			local cname, rest = string.match(entry, "^([%w%-]+)=(.+)$")
			if not cname then
				return nil, "Enter each component as name=path@runtime."
			end
			local cpath, crt = string.match(rest, "^(.-)@(.+)$")
			if not cpath then
				cpath = rest
			end
			repo_comps[cname] = { path = cpath, runtime = crt }
		end
		comp_map[name] = repo_comps
	else
		comp_map[name] = nil
	end
	return {
		runtimes = rt_map,
		base_branches = bb_map,
		components = comp_map,
	}, nil
end

local function repo_save(state: any)
	local form = state.repo_form
	if not form or state.busy.repo_save then
		return
	end
	local body, problem = repo_save_body(state)
	if problem then
		form.error = problem
		state.cx:render()
		return
	end
	if not body then
		return
	end
	form.error = nil
	api_action(state, "repo_save", "PUT", "/v1/setup/repositories", body, nil, "", { "repos" }, function()
		local saved_name = form.name
		state.repo_form = nil
		flash(state, "success", "Configuration saved for " .. saved_name)
	end)
end

-- Keys while the repo form is open: tab moves fields, ←→ picks runtime, text for inputs.
local function repo_form_key(state: any, k: Key, cx: any): boolean
	local form = state.repo_form
	if not form then
		return false
	end
	local n = k.name
	if n == "escape" then
		state.repo_form = nil
		return true
	elseif n == "enter" then
		repo_save(state)
		return true
	elseif n == "tab" then
		local at = table.find(REPO_FIELDS, form.focus) or 1
		form.focus = REPO_FIELDS[((at - 1 + (if k.shift then -1 else 1)) % #REPO_FIELDS) + 1]
		repo_focus(state, cx)
		return true
	end
	-- Runtime is a segmented choice.
	if form.focus == "runtime" then
		if n == "left" or n == "right" or k.text == " " then
			local ids = {}
			for _, known in P.KNOWN_RUNTIMES do
				table.insert(ids, known)
			end
			local cur = form.values.runtime or ""
			-- If current value isn't in the list, add it.
			if cur ~= "" and not table.find(ids, cur) then
				table.insert(ids, 1, cur)
			end
			table.insert(ids, "")  -- auto-detect
			local at = table.find(ids, cur) or 1
			form.values.runtime = ids[((at - 1 + (if n == "left" then -1 else 1)) % #ids) + 1]
		end
		return true
	end
	-- Text input for base_branch and components.
	local cur = form.values[form.focus] or ""
	if n == "paste" then
		form.values[form.focus] = cur .. (string.gsub(k.text or "", "[\r\n]+", " "))
	elseif n == "backspace" then
		if k.alt then
			form.values[form.focus] = ""
		else
			local len = utf8.len(cur) or 0
			form.values[form.focus] = if len > 0 then string.sub(cur, 1, (utf8.offset(cur, len) or 1) - 1) else ""
		end
	elseif k.text and k.text ~= "" and not k.ctrl and not k.meta then
		form.values[form.focus] = cur .. k.text
	else
		return false
	end
	form.error = nil
	return true
end

local function set_view(state: any, view: string)
	if view == "admin" and not apimod.can(state.api, "admin") and state.api.auth == "ok" then
		flash(state, "warning", "Admin needs a token with the admin role")
	end
	if view ~= state.view then
		state.audit_paged = false
	end
	state.view = view
	poll(state)
end

-- The catalog's providers for the model picker: the ones the team already uses first.
local function catalog_providers(state: any): { any }
	local used = {}
	local cfg = state.data.config
	for _, ref in (cfg and type(cfg.models) == "table" and cfg.models) or {} do
		if type(ref) == "table" and ref.provider then
			used[ref.provider] = true
		end
	end
	local list = {}
	for _, p in state.data.catalog or {} do
		table.insert(list, { id = p.id, n = p.n, used = used[p.id] or nil })
	end
	table.sort(list, function(a, b)
		if (a.used or false) ~= (b.used or false) then
			return a.used == true
		end
		return a.id < b.id
	end)
	return list
end

local function primary(state: any)
	local s = slot(state)
	local id = current(state)
	if not s or not id then
		return
	end
	if s == "dashboard" or s == "sessions" then
		do_chat(state, id)
	elseif s == "models" then
		local ref = V.model_ref(state, id)
		if not state.data.catalog then
			flash(state, if state.errs.catalog then "error" else "muted",
				if state.errs.catalog then "Model catalog unavailable: " .. state.errs.catalog else "Loading the model catalog…")
			state.cx:render()
			return
		end
		ask(state, { kind = "pick_model", act = "w_stage", ctx = { role = id }, current = ref, providers = catalog_providers(state) })
	elseif s == "providers" then
		for _, r in V.provider_rows(state) do
			if r.id == id and V.takes_key(r) then
				ask_key(state, id)
			end
		end
	elseif s == "mcp" then
		mcp_test(state, id)
	end
end

local function context_k(n: any): string
	if type(n) ~= "number" then
		return ""
	end
	if n >= 1e6 then
		return string.format("%g", math.floor(n / 1e5) / 10) .. "M"
	end
	return math.floor(n / 1000) .. "k"
end

-- Step 2 of the model picker: this provider's models, compacted here (host half, 2 s budget)
-- so the window only draws a list.
local function ask_models(state: any, role: string, provider: string)
	local path = "/v1/admin/catalog?provider=" .. fmt.urlencode(provider)
	apimod.invalidate(state.api, path)
	apimod.get(state.api, path, function(_changed, doc, err)
		if state.stopped then
			return
		end
		if err then
			fail(state, "Could not read the models of " .. provider, err)
			state.cx:render()
			return
		end
		local items = {}
		for _, p in (type(doc) == "table" and doc.providers) or {} do
			for _, m in p.models or {} do
				local facts = {}
				if m.name and m.name ~= m.id then
					table.insert(facts, m.name)
				end
				if m.contextWindow then
					table.insert(facts, context_k(m.contextWindow) .. " context")
				end
				if m.reasoning then
					table.insert(facts, "reasoning")
				end
				table.insert(items, { id = m.id, d = table.concat(facts, " · ") })
			end
		end
		state.api.cache[path] = nil
		ask(state, { kind = "pick_model_items", ctx = { role = role }, provider = provider, current = V.model_ref(state, role), items = items })
	end)
end

-- The next audit page (`nextCursor` → `cursor`). Paging stops the first page's refresh until
-- `r` or another view, so the rows read don't jump while someone reads them.
local function do_audit_more(state: any)
	local cursor = state.audit_next
	if not cursor or state.busy.audit then
		return
	end
	state.busy.audit = true
	state.audit_paged = true
	local path = "/v1/audit?limit=50&cursor=" .. fmt.urlencode(tostring(cursor))
	apimod.get(state.api, path, function(_changed, doc, err)
		state.busy.audit = nil
		state.api.cache[path] = nil
		if state.stopped then
			return
		end
		if err then
			fail(state, "Could not load more audit entries", err)
		elseif type(doc) == "table" then
			for _, it in doc.items or {} do
				table.insert(state.audit_items, it)
			end
			state.audit_next = doc.nextCursor
		end
		state.cx:render()
	end)
end

-- Window answers: `w_<name>` with a JSON payload `{ctx…, text|value…}`.
local function on_window(state: any, act: string, payload: any)
	local p = type(payload) == "table" and payload or {}
	local text = if type(p.text) == "string" then string.match(p.text, "^%s*(.-)%s*$") or "" else ""
	if (act == "w_pause" or act == "w_bypass" or act == "w_publish") and (utf8.len(text) or #text) < REASON_MIN then
		flash(state, "error", "The reason needs at least " .. REASON_MIN .. " characters. It goes to the audit log.")
		return
	end
	if act == "w_say" and text ~= "" then
		cli_action(state, "say:" .. p.sid, { "say", p.sid, text, "--json" }, "Message sent to " .. tostring(p.label or p.sid), { "sessions" })
	elseif act == "w_answer" and text ~= "" then
		cli_action(state, "answer:" .. p.sid, { "answer", p.sid, text, "--json" }, "Answer sent to " .. tostring(p.label or p.sid), { "sessions", "overview" })
	elseif act == "w_pause" and text ~= "" then
		do_command(state, "pause", text)
	elseif act == "w_stage" and p.role and p.provider and p.model then
		local cur = V.model_ref(state, p.role) or {}
		stage(state, p.role, { provider = p.provider, model = p.model, thinking = p.thinking or cur.thinking or "medium" })
	elseif act == "w_provider" and type(p.role) == "string" and type(p.provider) == "string" then
		ask_models(state, p.role, p.provider)
	elseif act == "w_think" and p.role and type(p.value) == "string" then
		local cur = V.model_ref(state, p.role)
		if cur then
			stage(state, p.role, { provider = cur.provider, model = cur.model, thinking = p.value })
		end
	elseif act == "w_publish" and text ~= "" then
		do_publish(state, text)
	elseif act == "w_builtin" and type(p.value) == "string" and p.value ~= "" then
		ask_key(state, p.value)
	elseif act == "w_apikey" and text ~= "" and p.provider then
		do_apikey(state, p.provider, text)
	elseif act == "w_token" and type(p.name) == "string" and p.name ~= "" and type(p.role) == "string" then
		do_token(state, p)
	elseif act == "w_bypass" and text ~= "" and type(p.ref) == "string" then
		do_merger(state, "bypass", p.ref, text)
	elseif act == "w_reauth" then
		state.api.auth = "checking"
		state.data, state.errs = {}, {}
		poll(state)
	elseif act == "w_view" and type(p.view) == "string" then
		set_view(state, p.view)
	elseif act == "w_watch" and type(p.sid) == "string" and type(p.value) == "string" then
		local server, id = string.match(p.value, "^(%S+) (.+)$")
		local s = session(state, p.sid)
		for _, l in V.session_leases(s) do
			if l.server == server and l.id == id then
				watch_lease(state, s, l, p.control == true)
			end
		end
	end
end

local function on_action(state: any, act: string, value: string?, cx: any)
	local v = value or ""
	if act == "sel" then
		local s = slot(state)
		if s then
			state.sel[s] = v
			if s == "assistant" then
				poll(state)
			end
		end
	elseif act == "open" then
		local s = slot(state)
		if s then
			state.sel[s] = v
			primary(state)
		end
	elseif act == "chat" then
		do_chat(state, v)
	elseif act == "say" then
		do_say(state, v)
	elseif act == "answer" then
		do_answer(state, v)
	elseif act == "interrupt" then
		do_interrupt(state, v)
	elseif act == "takeover" then
		do_takeover(state, v)
	elseif act == "watch" or act == "watch_control" then
		do_watch(state, v, act == "watch_control")
	elseif act == "refresh" then
		state.errs = {}
		state.read_at = {}
		state.audit_paged = false
		if state.api.auth ~= "ok" then
			state.api.auth = "checking"
		end
		refresh_now(state, { "overview", "sessions", "issues", "merger", "config", "observed", "providers", "tokens", "mcp" })
	elseif act == "audit_more" then
		do_audit_more(state)
	elseif act == "pause" then
		ask(state, { kind = "prompt", act = "w_pause", ctx = {}, min = REASON_MIN, placeholder = "Reason for the pause (at least 8 characters). It goes to the audit log.", action = "pause", icon = "pause" })
	elseif act == "resume" then
		do_command(state, "resume", "Resumed from Tern")
	elseif act == "login" then
		ask(state, { kind = "signin" })
	elseif act == "setup" then
		ask(state, { kind = "setup" })
	elseif act == "view" then
		set_view(state, v)
	elseif act == "model" then
		state.sel.models = state.sel.models or "implementer"
		primary(state)
	elseif act == "thinking" then
		local role = current(state) or "implementer"
		local cur = V.model_ref(state, role) or {}
		local items = {}
		for _, t in fmt.THINKING do
			table.insert(items, { label = fmt.THINKING_WORD[t], detail = t, value = t, current = t == (cur.thinking or "medium") })
		end
		ask(state, { kind = "pick", act = "w_think", ctx = { role = role }, items = items, placeholder = "Thinking level for " .. fmt.role_info(role)[1], action = "use", icon = "sparkle" })
	elseif act == "publish" then
		if draft_count(state) > 0 then
			ask(state, { kind = "prompt", act = "w_publish", ctx = {}, min = REASON_MIN, placeholder = "Reason for publishing (at least 8 characters). It goes to the history.", action = "publish", icon = "send" })
		end
	elseif act == "discard" then
		state.draft = {}
	elseif act == "apikey" then
		state.sel.providers = v
		primary(state)
	elseif act == "builtin_other" then
		ask_other_builtin(state)
	elseif act == "token_new" then
		ask(state, { kind = "token_new", act = "w_token" })
	elseif act == "token_copy" then
		if state.created then
			cx:copy(state.created.token)
			flash(state, "success", "Token copied to the clipboard")
		end
	elseif act == "token_dismiss" then
		state.created = nil
	elseif act == "revoke" then
		do_revoke(state, v)
	elseif act == "memory_wipe" then
		do_memory_wipe(state, v)
	elseif act == "omp_sync" then
		cli_action(state, "omp_sync", { "omp", "sync", "--json" }, "Team configuration applied to your omp", nil, function(data)
			state.omp_sync = if type(data) == "table" then data else {}
		end)
	elseif act == "bypass" then
		ask(state, { kind = "prompt", act = "w_bypass", ctx = { ref = v }, min = REASON_MIN, placeholder = "Reason to bypass " .. fmt.merger_ref(v) .. " (at least 8 characters). It goes to the audit log.", action = "bypass", icon = "forward" })
	elseif act == "requeue" or act == "release" then
		state.confirm = {
			title = (if act == "requeue" then "Requeue " else "Release ") .. fmt.merger_ref(v) .. "?",
			line = if act == "requeue"
				then "The PR goes back to the queue and gets a new review."
				else "The merger lets go of the PR. It leaves the train and the queue until a new push.",
			ok = if act == "requeue" then "Requeue" else "Release",
			tone = "warning",
			run = function()
				do_merger(state, act, v, nil)
			end,
		}
	elseif act == "release_issue" then
		local ident = v
		for _, it in (state.data.issues and state.data.issues.items) or {} do
			if it.issue_id == v then
				ident = it.identifier or v
			end
		end
		state.confirm = {
			title = "Return " .. ident .. " to Symphony?",
			line = "The ticket becomes eligible again. Symphony can dispatch a new attempt from what is on the branch.",
			ok = "Return to Symphony",
			tone = "success",
			run = function()
				cli_action(state, "release:" .. v, { "release", ident, "--reason", "Returned to Symphony from Tern", "--json" },
					ident .. " returned to Symphony", { "issues", "overview" })
			end,
		}
	elseif act == "mcp_new" then
		mcp_open(state, cx, "add", nil)
	elseif act == "mcp_edit" or act == "mcp_headers" then
		state.sel.mcp = v
		mcp_open(state, cx, if act == "mcp_edit" then "edit" else "headers", v)
	elseif act == "mcp_test" then
		state.sel.mcp = v
		mcp_test(state, v)
	elseif act == "mcp_toggle" then
		mcp_toggle(state, v)
	elseif act == "mcp_remove" then
		mcp_remove(state, v)
	elseif act == "mcp_save" then
		mcp_save(state)
	elseif act == "mcp_cancel" then
		state.mcp_form = nil
	elseif act == "mcp_role" then
		mcp_toggle_role(state, v)
	elseif act == "mcp_focus" and state.mcp_form then
		state.mcp_form.focus = v
		mcp_focus(state, cx)
	elseif act == "repo_sel" then
		state.sel.repositories = v
	elseif act == "repo_edit" then
		state.sel.repositories = v
		repo_open(state, cx, v)
	elseif act == "repo_save" then
		repo_save(state)
	elseif act == "repo_cancel" then
		state.repo_form = nil
	elseif act == "repo_focus" and state.repo_form then
		state.repo_form.focus = v
		repo_focus(state, cx)
	elseif act == "confirm_ok" then
		local c = state.confirm
		state.confirm = nil
		if c then
			c.run()
		end
	elseif act == "confirm_cancel" then
		state.confirm = nil
	elseif string.sub(act, 1, 2) == "w_" then
		local ok, payload = pcall(tern.json.decode, v)
		on_window(state, act, if ok then payload else nil)
	end
end

local function visible_views(state: any): { string }
	local out = {}
	for _, v in V.VIEWS do
		if v ~= "admin" or apimod.can(state.api, "admin") then
			table.insert(out, v)
		end
	end
	return out
end

local function cycle(list: { string }, cur: string, step: number): string
	local at = table.find(list, cur) or 1
	return list[(at - 1 + step) % #list + 1]
end

local function key(state: any, k: Key, cx: any): boolean?
	if k.ctrl and (k.text == "p" or k.name == "p") and apimod.can(state.api, "operator") then
		local ov = state.data.overview
		on_action(state, if ov and type(ov.daemon) == "table" and ov.daemon.paused then "resume" else "pause", nil, cx)
		return true
	end
	if k.ctrl and (k.text == "s" or k.name == "s") and apimod.can(state.api, "admin") then
		ask(state, { kind = "setup" })
		return true
	end
	if state.mcp_form and state.view == "admin" and state.admin_tab == "mcp" and (k.name == "paste" or not (k.meta or k.ctrl)) then
		return mcp_form_key(state, k, cx)
	end
	if state.repo_form and state.view == "repositories" and (k.name == "paste" or not (k.meta or k.ctrl)) then
		return repo_form_key(state, k, cx)
	end
	if k.meta or k.ctrl then
		return false
	end
	local n = k.name
	if state.confirm then
		if n == "enter" then
			on_action(state, "confirm_ok", nil, cx)
		elseif n == "escape" then
			state.confirm = nil
		else
			return false
		end
		return true
	end
	if n == "tab" then
		set_view(state, cycle(visible_views(state), state.view, if k.shift then -1 else 1))
		return true
	elseif n == "[" or n == "]" then
		local step = if n == "[" then -1 else 1
		if state.view == "sessions" then
			state.filter = cycle(V.FILTERS, state.filter, step)
		elseif state.view == "admin" then
			state.admin_tab = cycle(V.ADMIN_TABS, state.admin_tab, step)
			poll(state)
		else
			return false
		end
		return true
	elseif tonumber(n) and #n == 1 then
		local views = visible_views(state)
		local v = views[if n == "0" then 10 else tonumber(n) :: number]
		if v then
			set_view(state, v)
			return true
		end
		return false
	elseif n == "up" or n == "k" then
		return move(state, -1)
	elseif n == "down" or n == "j" then
		return move(state, 1)
	elseif n == "enter" then
		if state.api.auth == "logged_out" then
			ask(state, { kind = "signin" })
			return false
		end
		primary(state)
		return false
	elseif n == "r" then
		on_action(state, "refresh", nil, cx)
		return true
	end
	if state.view == "audit" and n == "m" and state.audit_next then
		do_audit_more(state)
		return true
	end
	-- Keys that don't need a selected row (an empty token list still creates one).
	if state.view == "admin" and apimod.can(state.api, "admin") then
		if state.admin_tab == "omp" and n == "s" then
			on_action(state, "omp_sync", nil, cx)
			return true
		elseif state.admin_tab == "tokens" and n == "n" then
			on_action(state, "token_new", nil, cx)
			return false
		elseif state.admin_tab == "mcp" and n == "n" and state.data.mcp then
			mcp_open(state, cx, "add", nil)
			return true
		end
	end
	local s = slot(state)
	local id = current(state)
	if not s or not id then
		return false
	end
	local sessions_view = s == "dashboard" or s == "sessions"
	local can_op = apimod.can(state.api, "operator")
	if sessions_view and can_op then
		if n == "m" then
			do_say(state, id)
			return false
		elseif n == "a" then
			do_answer(state, id)
			return false
		elseif n == "i" then
			do_interrupt(state, id)
			return true
		elseif n == "t" then
			do_takeover(state, id)
			return true
		elseif n == "w" or n == "W" then
			-- `W` (shift) watches with control, after the confirm strip.
			return not do_watch(state, id, n == "W" or k.shift == true or k.text == "W")
		end
	elseif s == "merger" and can_op then
		local ref = string.sub(id, 2) -- `<pr>@<repo>`
		if n == "b" then
			on_action(state, "bypass", ref, cx)
			return false
		elseif n == "q" then
			on_action(state, "requeue", ref, cx)
			return true
		elseif n == "l" then
			on_action(state, "release", ref, cx)
			return true
		end
	elseif s == "models" then
		if n == "t" then
			on_action(state, "thinking", nil, cx)
			return false
		elseif n == "p" then
			on_action(state, "publish", nil, cx)
			return false
		end
	elseif s == "providers" and n == "c" then
		primary(state)
		return false
	elseif s == "tokens" and n == "x" then
		for _, t in (state.data.tokens and state.data.tokens.items) or {} do
			if t.id == id and t.revoked_at == nil then
				do_revoke(state, id)
				return true
			end
		end
	elseif s == "assistant" and n == "x" then
		do_memory_wipe(state, id)
		return true
	elseif s == "mcp" then
		local srv = V.mcp_find(state, id)
		if n == "e" then
			mcp_open(state, cx, "edit", id)
			return true
		elseif n == "h" and srv and V.is_remote(srv.transport) then
			mcp_open(state, cx, "headers", id)
			return true
		elseif n == "d" then
			mcp_toggle(state, id)
			return true
		elseif n == "x" then
			mcp_remove(state, id)
			return true
		end
	elseif s == "repositories" and n == "e" and apimod.can(state.api, "admin") then
		local r = P.find_repo(state, id)
		if r and not P.repo_readonly(state, r) then
			repo_open(state, cx, id)
			return true
		end
	end
	return false
end

local TITLES = {
	dashboard = "Dashboard", sessions = "Sessions", tickets = "Tickets", merger = "Merger", admin = "Admin",
	repositories = "Repositories", infrastructure = "Infrastructure", telemetry = "Telemetry",
	learnings = "Learnings", audit = "Audit",
}

M.def = {
	font = "sans",
	init = function(cx: any, args: { string }, saved: any)
		local s = type(saved) == "table" and saved or {}
		local view = args[1] or s.view or "dashboard"
		if not TITLES[view] then
			view = "dashboard"
		end
		local state = {
			cx = cx,
			api = apimod.new(),
			view = view,
			admin_tab = s.admin_tab or args[2] or "models",
			filter = s.filter or "active",
			sel = {},
			data = {},
			errs = {},
			draft = {},
			busy = {},
			read_at = {},
			audit_items = nil,
			audit_next = nil,
			audit_paged = false,
			flash = nil,
			created = nil,
			confirm = nil,
			omp_sync = nil,
			-- Admin › MCP: the card's open form (add, edit or headers) and each server's last test.
			mcp_form = nil,
			mcp_tests = {},
			-- Repositories: the open edit form for a single repository's config.
			repo_form = nil,
			offline = nil,
			stopped = false,
		}
		local function tick()
			if state.stopped or not running(cx.pane) then
				state.stopped = true
				return
			end
			if state.flash and tern.now() - state.flash.at > FLASH_MS then
				state.flash = nil
				cx:render()
			elseif tern.now() - (state.rendered_at or 0) > CLOCK_MS then
				cx:render()
			end
			poll(state)
			tern.timer(POLL_MS, tick)
		end
		tick()
		return state
	end,
	view = function(state: any, cx: any)
		-- The visible list always has a selection (its first row by default), so the detail card
		-- and the keys act on something without a click first.
		current(state)
		state.rendered_at = tern.now()
		state.cols = cx.cols
		return V.render(state)
	end,
	title = function(state: any)
		local ov = state.data.overview
		local waiting = ov and ov.counts and ov.counts.waiting_input or 0
		if waiting > 0 then
			return "Symphony · " .. fmt.plural(waiting, "needs input", "need input")
		end
		return "Symphony · " .. (TITLES[state.view] or "Dashboard")
	end,
	key = key,
	event = function(state: any, ev: UiEvent, cx: any)
		if ev.ev == "select" then
			if ev.id == "main.nav" and type(ev.item) == "string" then
				set_view(state, ev.item)
			elseif ev.id == "main.sessions.bar.filter" and type(ev.item) == "string" then
				state.filter = ev.item
			elseif ev.id == "main.admin.sub" and type(ev.item) == "string" then
				state.admin_tab = ev.item
				poll(state)
			elseif state.mcp_form and type(ev.item) == "string" then
				-- A choice in the MCP card's form: `…detail.form.<field>.seg`.
				local f = string.match(ev.id or "", "^main%.admin%.mcp%.md%.detail%.form%.([%w_]+)%.seg$")
				if f then
					mcp_choice(state.mcp_form, f, ev.item)
				end
			elseif state.repo_form and type(ev.item) == "string" then
				-- A choice in the repo config form: `…detail.<field>.seg`.
				local f = string.match(ev.id or "", "^main%.repositories%.md%.detail%.([%w_]+)%.seg$")
				if f then
					state.repo_form.values[f] = ev.item
					state.repo_form.focus = f
					state.repo_form.error = nil
				end
			end
		elseif ev.ev == "focus" and type(ev.id) == "string" then
			if state.mcp_form then
				local f = string.match(ev.id, "^main%.admin%.mcp%.md%.detail%.form%.([%w_]+)%.input$")
				if f then
					state.mcp_form.focus = f
				end
			end
			if state.repo_form then
				local f = string.match(ev.id, "^main%.repositories%.md%.detail%.([%w_]+)%.input$")
				if f then
					state.repo_form.focus = f
				end
			end
		elseif ev.ev == "action" and type(ev.act) == "string" then
			local value = ev.value
			on_action(state, ev.act, if type(value) == "string" then value elseif value ~= nil then tostring(value) else nil, cx)
		end
	end,
	save = function(state: any)
		return { view = state.view, admin_tab = state.admin_tab, filter = state.filter }
	end,
}

return M
