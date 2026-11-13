--!strict
-- Window half: palette commands and keys, the status-line segment, and the window-only UI the
-- blocks ask for through `symphony://ui?req=…` links (prompts, pickers, chat and takeover tabs,
-- device screens in Tern's browser).
-- Like the host half it never stores the token: it reads it from the CLI into memory.

local cli = require("./lib/cli")
local fmt = require("./lib/fmt")

local APP = "symphony.app"
local SIGNIN = "symphony.signin"
local SETUP = "symphony.setup"
local POLL_MS = 2000
local AUTH_RETRY_MS = 15000

-- ── opening things ─────────────────────────────────────────────────────────

local function find_block(cx: any, kind: string): any
	for _, p in cx.session:panes() do
		if p.block == kind then
			return p
		end
	end
	return nil
end

local function send(cx: any, pane: number, act: string, payload: { [string]: any })
	cx.session:event(pane, { ev = "action", id = "main", act = act, value = tern.json.encode(payload) })
end

local function reveal(cx: any, p: any)
	if p.parked then
		cx.layout:unpark(p.pane)
	else
		cx.layout:focus(p.pane)
	end
end

local function open_view(cx: any, view: string)
	local p = find_block(cx, APP)
	if p then
		reveal(cx, p)
		send(cx, p.pane, "w_view", { view = view })
		return
	end
	cx:new_block(APP, { view }, "tab")
end

local function open_signin(cx: any)
	local p = find_block(cx, SIGNIN)
	if p then
		reveal(cx, p)
		return
	end
	cx:new_block(SIGNIN, nil, "tab")
end

local function open_setup(cx: any)
	local p = find_block(cx, SETUP)
	if p then
		reveal(cx, p)
		return
	end
	cx:new_block(SETUP, nil, "tab")
end

-- An automatic Setup request that arrived while the sign-in tab was open (see `handle`).
local setup_after_signin = false

local SAFE_ID = "^[%w%._:@/%-]+$"

-- Opens a shell tab and types `command` into it. A `launch.command` tab blocks the window hook
-- past its 50 ms budget while the program spawns; a shell tab opens in a few ms and the shell
-- reads the queued line once it is up (and stays when the program exits).
local function new_tab(cx: any, command: string, name: string)
	local pane = cx.layout:new_tab(nil)
	if not pane then
		cx:toast("error", "Could not open a new tab")
		return
	end
	cx:run(pane, command .. "\r")
	local tab = cx.session:tab_of(pane)
	if tab then
		cx.layout:name_tab(tab, name)
	end
end

local function lists_symphony(custom: any): boolean
	for _, a in (type(custom) == "table" and custom) or {} do
		if type(a) == "table" and a.id == "symphony" then
			return true
		end
	end
	return false
end

-- Where `tern acp` reads its agents: `$TERN_CONFIG_DIR/settings.json`, else the macOS default.
local function settings_path(): string?
	local dir = tern.getenv("TERN_CONFIG_DIR")
	if dir and dir ~= "" then
		return dir .. "/settings.json"
	end
	local home = tern.getenv("HOME")
	if home and tern.runtime.os == "macos" then
		return home .. "/Library/Application Support/Tern/settings.json"
	end
	return nil
end

-- Whether `symphony tern install` added the ACP agent. The settings file is what `tern acp`
-- resolves the agent from (a headless window reports default settings); the window's own
-- settings are the fallback when the file can't be read.
local function has_agent(cx: any): boolean
	local path = settings_path()
	if path then
		local ok, doc = pcall(function()
			return tern.json.decode(tern.fs.read(path, 1024 * 1024))
		end)
		if ok and type(doc) == "table" then
			return lists_symphony(type(doc.agents) == "table" and doc.agents.custom or nil)
		end
	end
	local ok, custom = pcall(function()
		return cx.settings:get("agents.custom")
	end)
	return ok and lists_symphony(custom)
end

-- The live session as a native Tern chat (the `symphony` ACP agent from `symphony tern install`).
local function open_chat(cx: any, sid: string, label: string?)
	if not string.match(sid, SAFE_ID) then
		cx:toast("error", "Invalid session", sid)
		return
	end
	if not has_agent(cx) then
		cx:toast("error", "Live chat is not installed in this Tern yet", "Install it from Symphony: Sign in → Install in Tern (symphony tern install)")
		open_signin(cx)
		return
	end
	new_tab(cx, "tern acp symphony --resume " .. fmt.shq(sid), (label or sid) .. " · chat")
end

local function open_takeover(cx: any, ticket: string)
	if not string.match(ticket, SAFE_ID) then
		cx:toast("error", "Invalid ticket", ticket)
		return
	end
	new_tab(cx, cli.bin() .. " takeover " .. fmt.shq(ticket), ticket .. " · takeover")
end

-- ── status line ────────────────────────────────────────────────────────────

local status = {
	creds = nil :: any,
	auth = "checking", -- checking | ok | logged_out | error
	auth_at = -1e9,
	authing = false,
	etag = nil :: string?,
	overview = nil :: any,
	offline = false,
	segment = nil :: any,
	key = "",
}

local function segment(): any
	if status.auth == "logged_out" then
		return { text = "Symphony · sign in", icon = "log-in", tone = "muted", command = "plugin.symphony.signin" }
	elseif status.auth ~= "ok" then
		return nil
	end
	if status.offline then
		return { text = "Symphony · offline", icon = "broadcast", tone = "error", command = "plugin.symphony.dashboard" }
	end
	local ov = status.overview
	if not ov then
		return nil
	end
	local d = type(ov.daemon) == "table" and ov.daemon or {}
	if d.alive == false then
		return { text = "Symphony · daemon stopped", icon = "broadcast", tone = "error", command = "plugin.symphony.dashboard" }
	end
	local c = type(ov.counts) == "table" and ov.counts or {}
	local parts = { "Symphony", (c.running or 0) .. " running" }
	local tone = "muted"
	if (c.waiting_input or 0) > 0 then
		table.insert(parts, c.waiting_input .. (if c.waiting_input == 1 then " needs input" else " need input"))
		tone = "warning"
	end
	if d.paused then
		table.insert(parts, "paused")
		tone = "warning"
	end
	return { text = table.concat(parts, " · "), icon = "broadcast", tone = tone, command = "plugin.symphony.dashboard" }
end

local function publish_status()
	local seg = segment()
	local k = if seg then seg.text .. "|" .. seg.tone else ""
	if k ~= status.key then
		status.key = k
		status.segment = seg
		tern.chrome.refresh()
	end
end

local status_poll: () -> ()

local function status_auth()
	if status.authing then
		return
	end
	status.authing = true
	status.auth_at = tern.now()
	local ok = cli.run({ "token", "print", "--json" }, { timeout_ms = 10000 }, function(data, e)
		status.authing = false
		if not e and type(data) == "table" and type(data.url) == "string" and type(data.token) == "string" then
			status.creds = { url = string.gsub(data.url, "/+$", ""), token = data.token }
			status.auth = "ok"
			status.etag = nil
			status_poll()
		else
			status.creds = nil
			status.auth = if e and e.code == "not_logged_in" then "logged_out" else "error"
		end
		publish_status()
	end)
	if not ok then
		status.authing = false
		status.auth = "error"
	end
end

status_poll = function()
	local creds = status.creds
	if status.auth ~= "ok" or not creds then
		if tern.now() - status.auth_at >= AUTH_RETRY_MS then
			status_auth()
		end
		return
	end
	local headers: { [string]: string } = { Authorization = "Bearer " .. creds.token, Accept = "application/json" }
	if status.etag then
		headers["If-None-Match"] = status.etag
	end
	tern.fetch(creds.url .. "/v1/overview", { headers = headers, timeout_ms = 5000 }, function(r)
		if r.error then
			status.offline = true
		elseif r.status == 304 then
			status.offline = false
		elseif r.status == 200 then
			local ok, doc = pcall(tern.json.decode, r.body)
			if ok then
				status.overview = doc
				status.etag = r.headers["etag"]
				status.offline = false
			end
		elseif r.status == 401 then
			status.creds = nil
			status.auth = "checking"
			status.auth_at = -1e9
		else
			status.offline = true
		end
		publish_status()
	end)
end

local function status_loop()
	status_poll()
	tern.timer(POLL_MS, status_loop)
end

tern.chrome.status(function(_pane)
	if status.segment then
		return { status.segment }
	end
	return nil
end)

status_loop()

-- Re-reads the credential everywhere after a login or logout.
local function auth_changed(cx: any)
	status.creds, status.auth, status.auth_at, status.overview, status.etag = nil, "checking", -1e9, nil, nil
	status_auth()
	for _, p in cx.session:panes() do
		if p.block == APP then
			send(cx, p.pane, "w_reauth", {})
		end
	end
end

-- ── pickers and prompts the blocks ask for ─────────────────────────────────

local function ctx_with(ctx: any, extra: { [string]: any }): { [string]: any }
	local out: { [string]: any } = {}
	for k, v in (type(ctx) == "table" and ctx or {}) do
		out[k] = v
	end
	for k, v in extra do
		out[k] = v
	end
	return out
end

-- `req.min`: the fewest characters the API accepts (8 for audited reasons); a shorter answer
-- reopens the field with what was typed and says why, instead of a round trip to a 422.
local function prompt(cx: any, req: any, pane: number)
	cx:prompt({
		placeholder = req.placeholder,
		value = req.value,
		icon = req.icon or "message",
		action = req.action or "send",
	}):next(function(text, _err, cx2)
		if not text or not cx2 or not string.match(text, "%S") then
			return
		end
		local trimmed = string.match(text, "^%s*(.-)%s*$") or ""
		local min = tonumber(req.min) or 1
		if (utf8.len(trimmed) or #trimmed) < min then
			cx2:toast("error", "The reason needs at least " .. min .. " characters.", "It goes to the audit log.")
			prompt(cx2, ctx_with(req, { value = trimmed }), pane)
			return
		end
		send(cx2, pane, req.act, ctx_with(req.ctx, { text = trimmed }))
	end)
end

-- The picker opens on its first row, so the current choice goes first: Enter keeps it.
local function current_first(items: { any }): { any }
	local out = {}
	for _, it in items do
		if it.current then
			table.insert(out, it)
		end
	end
	for _, it in items do
		if not it.current then
			table.insert(out, it)
		end
	end
	return out
end

local function pick(cx: any, req: any, pane: number)
	local items = {}
	for _, it in req.items or {} do
		table.insert(items, { label = it.label, detail = it.detail, current = it.current, value = it.value or it.label })
	end
	if req.other then
		table.insert(items, { label = req.other, other = true, icon = "pen" })
	end
	if #items == 0 then
		return
	end
	cx:pick({ items = current_first(items), placeholder = req.placeholder, icon = req.icon, action = req.action }):next(function(item, _err, cx2)
		if not item or not cx2 then
			return
		end
		if item.other then
			prompt(cx2, { act = req.act, ctx = req.ctx, placeholder = req.placeholder, icon = req.icon, action = req.action }, pane)
		else
			send(cx2, pane, req.act, ctx_with(req.ctx, { value = item.value, text = item.value }))
		end
	end)
end

local function pick_thinking(cx: any, pane: number, role: string, chosen: any)
	local items = {}
	for _, t in fmt.THINKING do
		table.insert(items, { label = fmt.THINKING_WORD[t], detail = t, value = t, current = t == (chosen.thinking or "medium") })
	end
	cx:pick({ items = current_first(items), placeholder = "Thinking level for " .. fmt.role_info(role)[1] .. " with " .. chosen.model, icon = "sparkle", action = "use" })
		:next(function(item, _err, cx2)
			if item and cx2 then
				send(cx2, pane, "w_stage", { role = role, provider = chosen.provider, model = chosen.model, thinking = item.value })
			end
		end)
end

-- Provider logos Tern draws well at picker size; other providers get the generic mark (some
-- brand logos in Tern 0.8.3 draw oversized in a picker row).
local LOGOS = {
	["amazon-bedrock"] = true, anthropic = true, openai = true, google = true,
	["google-vertex"] = true, azure = true, groq = true, mistral = true, deepseek = true, xai = true,
	openrouter = true, ["github-copilot"] = true,
}

local function logo(provider: string): string
	return if LOGOS[provider] then "provider:" .. provider else "server"
end

-- Step 2, asked by the block once it read the provider's models (the catalog is megabytes:
-- the block decodes it in the host half, whose budget is 2 s; a window hook has 50 ms).
local function pick_model_items(cx: any, req: any, pane: number)
	local role = req.ctx and req.ctx.role
	if type(role) ~= "string" or type(req.provider) ~= "string" then
		return
	end
	local cur = type(req.current) == "table" and req.current or {}
	local items = {}
	for _, m in (type(req.items) == "table" and req.items) or {} do
		table.insert(items, {
			label = m.id,
			detail = m.d,
			icon = logo(req.provider),
			current = req.provider == cur.provider and m.id == cur.model,
			model = m.id,
		})
	end
	if #items == 0 then
		cx:toast("error", "The catalog has no models for " .. req.provider)
		return
	end
	cx:pick({ items = current_first(items), placeholder = req.provider .. " model for " .. fmt.role_info(role)[1] .. " (type to filter)", icon = "sparkle", action = "choose" })
		:next(function(item, _err, cx2)
			if item and cx2 then
				pick_thinking(cx2, pane, role, { provider = req.provider, model = item.model, thinking = cur.thinking })
			end
		end)
end

-- Step 1: the provider; the block then loads its models and asks for step 2.
local function pick_model(cx: any, req: any, pane: number)
	local role = req.ctx and req.ctx.role
	if type(role) ~= "string" then
		return
	end
	local cur = type(req.current) == "table" and req.current or {}
	local items = {}
	for _, p in (type(req.providers) == "table" and req.providers) or {} do
		if type(p) == "table" and type(p.id) == "string" then
			table.insert(items, {
				label = p.id,
				detail = fmt.plural(tonumber(p.n) or 0, "model", "models") .. (if p.used then " · used by the team" else ""),
				icon = logo(p.id),
				current = p.id == cur.provider,
				value = p.id,
			})
		end
	end
	if #items == 0 then
		cx:toast("error", "The model catalog has not loaded yet", "Try again in a few seconds.")
		return
	end
	cx:pick({ items = current_first(items), placeholder = "Provider for " .. fmt.role_info(role)[1], icon = "server", action = "choose" })
		:next(function(item, _err, cx2)
			if item and cx2 then
				send(cx2, pane, "w_provider", { role = role, provider = item.value })
			end
		end)
end

local EXPIRY = {
	{ label = "90 days", detail = "recommended", value = "90" },
	{ label = "30 days", value = "30" },
	{ label = "1 year", value = "365" },
	{ label = "No expiry", detail = "for automation only", value = "" },
}

local function token_new(cx: any, pane: number)
	cx:prompt({ placeholder = "Token name: who or what will use it (e.g. carol, ci-bot)", icon = "key-round", action = "continue" })
		:next(function(name, _err, cx2)
			if not name or not cx2 or not string.match(name, "%S") then
				return
			end
			local roles = {}
			for _, r in fmt.TOKEN_ROLES do
				table.insert(roles, { label = r, detail = fmt.TOKEN_ROLE[r], value = r, current = r == "operator" })
			end
			cx2:pick({ items = current_first(roles), placeholder = "Role for " .. name, icon = "shield", action = "continue" }):next(function(role, _e2, cx3)
				if not role or not cx3 then
					return
				end
				cx3:pick({ items = EXPIRY, placeholder = "Expiry for the token of " .. name, icon = "clock", action = "create" }):next(function(exp, _e3, cx4)
					if exp and cx4 then
						send(cx4, pane, "w_token", { name = name, role = role.value, expires_days = exp.value })
					end
				end)
			end)
		end)
end

local function handle(cx: any, req: any, pane: number)
	local kind = req.kind
	if kind == "prompt" and type(req.act) == "string" then
		prompt(cx, req, pane)
	elseif kind == "pick" and type(req.act) == "string" then
		pick(cx, req, pane)
	elseif kind == "pick_model" then
		pick_model(cx, req, pane)
	elseif kind == "pick_model_items" then
		pick_model_items(cx, req, pane)
	elseif kind == "token_new" then
		token_new(cx, pane)
	elseif kind == "chat" and type(req.sid) == "string" then
		open_chat(cx, req.sid, req.label)
	elseif kind == "watch" and type(req.url) == "string" and string.match(req.url, "^https?://[^%s]+$") then
		-- A device's viewer in Tern's browser beside the panel. A plugin's `cx:open` skips the link
		-- routes, so the Seiji plugin, when installed too, does not open it a second time.
		cx:open(req.url, "beside")
	elseif kind == "takeover" and type(req.ticket) == "string" then
		open_takeover(cx, req.ticket)
	elseif kind == "signin" then
		open_signin(cx)
	elseif kind == "setup" then
		-- The panel asks by itself (`auto`) as soon as a new sign-in reads an unconfigured backend;
		-- while the sign-in tab is still open, wait until the person leaves it for the panel.
		if req.auto and find_block(cx, SIGNIN) then
			setup_after_signin = true
		else
			open_setup(cx)
		end
	elseif kind == "open" and type(req.view) == "string" then
		open_view(cx, req.view)
		if setup_after_signin then
			setup_after_signin = false
			open_setup(cx)
		end
	elseif kind == "auth_changed" then
		auth_changed(cx)
	end
end

-- Only our own blocks may ask: a `symphony://` link printed by some other program in a terminal
-- pane is ignored, and the request must name the pane it came from.
tern.route.link(function(link, cx)
	if string.sub(link.url, 1, 11) ~= "symphony://" then
		return nil
	end
	local req = fmt.parse_ui_link(link.url)
	local info = cx.session:pane(link.pane)
	local block = info and info.block
	if not req or req.pane ~= link.pane or (block ~= APP and block ~= SIGNIN and block ~= SETUP) then
		tern.log.warn("symphony: ignored link from pane", link.pane, block)
		return { handled = true }
	end
	-- A route hook has a 50 ms budget and opening a tab takes longer: answer now, act on the
	-- next tick with the fresh `cx` the timer gives.
	local pane = link.pane
	tern.timer(0, function(cx2)
		if cx2 then
			handle(cx2, req, pane)
		end
	end)
	return { handled = true }
end)

-- ── palette ────────────────────────────────────────────────────────────────

local function view_command(id: string, title: string, view: string, keys: { string }?, icon: string)
	tern.command({
		id = id,
		title = "Symphony: " .. title,
		icon = icon,
		keys = keys,
		run = function(cx)
			open_view(cx, view)
		end,
	})
end

view_command("dashboard", "Dashboard", "dashboard", { "ctrl+alt+s" }, "broadcast")
view_command("sessions", "Sessions", "sessions", nil, "history")
view_command("tickets", "Tickets", "tickets", nil, "kanban")
view_command("merger", "Merger", "merger", { "ctrl+alt+m" }, "merge")
view_command("admin", "Admin", "admin", nil, "sliders")
view_command("repositories", "Repositories", "repositories", nil, "branch")
view_command("infrastructure", "Infrastructure", "infrastructure", nil, "server")
view_command("telemetry", "Telemetry", "telemetry", nil, "chart")
view_command("learnings", "Learnings", "learnings", nil, "lightbulb")
view_command("audit", "Audit", "audit", nil, "list-checks")

tern.command({
	id = "signin",
	title = "Symphony: Sign in",
	icon = "log-in",
	run = function(cx)
		open_signin(cx)
	end,
})

tern.command({
	id = "setup",
	title = "Symphony: Setup",
	icon = "list-checks",
	run = function(cx)
		open_setup(cx)
	end,
})

local SECTION = { waiting_input = "Needs input", running = "Running", starting = "Running", idle = "Idle", ended = "Ended" }
local RANK = { waiting_input = 1, running = 2, starting = 2, idle = 3, ended = 4 }

tern.command({
	id = "chat",
	title = "Symphony: Chat with an agent",
	icon = "message",
	keys = { "ctrl+alt+a" },
	run = function(cx)
		local ok, err = cli.api("GET", "/v1/sessions?limit=100", nil, nil, function(doc, e, cx2)
			if not cx2 then
				return
			end
			if e then
				if e.code == "not_logged_in" then
					cx2:toast("info", "Sign in to Symphony first", "Symphony: Sign in")
					open_signin(cx2)
				else
					cx2:toast("error", "Could not list the sessions", cli.describe(e))
				end
				return
			end
			local list = {}
			for _, s in (type(doc) == "table" and doc.items) or {} do
				table.insert(list, s)
			end
			table.sort(list, function(a, b)
				local ra, rb = RANK[a.state] or 9, RANK[b.state] or 9
				if ra ~= rb then
					return ra < rb
				end
				return (fmt.to_ms(a.updated_at) or 0) > (fmt.to_ms(b.updated_at) or 0)
			end)
			local items = {}
			for _, s in list do
				local word = fmt.state(s.state)
				table.insert(items, {
					label = (s.identifier or s.sid) .. " · " .. fmt.cut(s.title, 60),
					detail = word .. " · " .. (fmt.KIND[s.kind or "implement"] or "") .. " · " .. fmt.ago(s.updated_at),
					group = SECTION[s.state] or "Other",
					icon = fmt.STATE_ICON[s.state or ""] or "activity",
					sid = s.sid,
					ident = s.identifier,
				})
			end
			if #items == 0 then
				cx2:toast("info", "No agent sessions right now", "They appear when a ticket starts running.")
				return
			end
			cx2:pick({ items = items, placeholder = "Chat with which agent?", icon = "message", action = "open chat" })
				:next(function(item, _e, cx3)
					if item and cx3 then
						open_chat(cx3, item.sid, item.ident)
					end
				end)
		end)
		if not ok then
			cx:toast("error", "symphony CLI unavailable", cli.describe(err))
		end
	end,
})
