--!strict
-- Block `symphony.signin`: guided login. The person types (or pastes) the Symphony URL and an
-- API token into two fields drawn by the block; Enter runs `symphony login --url U --token -`
-- with the token on stdin, so it never reaches a process list, the block's args or `save()`.
-- After a login it offers `symphony tern install --no-plugin` (live chat agent + MCP): the plugin
-- that draws this block is already installed, and its source may not exist next to the CLI.

local cli = require("./cli")
local fmt = require("./fmt")
local h = require("./ui")

local ui = tern.ui

local M = {}

local FIELDS = { "url", "token" }

local function masked(token: string): string
	local n = utf8.len(token) or #token
	if n == 0 then
		return ""
	elseif n <= 12 then
		return string.rep("•", n)
	end
	return string.sub(token, 1, 4) .. string.rep("•", math.min(n - 8, 24)) .. string.sub(token, -4)
end

local function focus(state: any, cx: any)
	cx:frame({ { "focus", "main.form." .. state.focus .. ".input" } })
end

-- Tells this window's plugin that the credential changed, so open panels and the status line
-- ask the CLI again instead of waiting for their next retry.
local function announce(cx: any)
	cx:open(fmt.ui_link({ kind = "auth_changed", pane = cx.pane }))
end

local function login(state: any, cx: any)
	local url = string.match(state.url, "^%s*(.-)%s*$") or ""
	local token = string.match(state.token, "^%s*(.-)%s*$") or ""
	if url == "" then
		state.error = "Enter the Symphony address, for example https://symphony.example.com."
		state.focus = "url"
		focus(state, cx)
		return
	end
	if not string.match(url, "^https?://") then
		url = "https://" .. url
		state.url = url
	end
	if token == "" then
		state.error = "Paste the API token your team admin created for you (it starts with sym_)."
		state.focus = "token"
		focus(state, cx)
		return
	end
	state.error = nil
	state.step = "busy"
	local ok, err = cli.run({ "login", "--url", url, "--token", "-", "--json" }, { stdin = token .. "\n", timeout_ms = 20000 }, function(data, e)
		if e then
			state.step = "form"
			state.error = cli.describe(e)
			state.focus = if e.code == "unreachable" then "url" else "token"
		else
			state.token = ""
			state.step = "done"
			state.me = type(data) == "table" and data or {}
			state.me.url = state.me.url or url
			announce(cx)
		end
		cx:render()
		if state.step == "form" then
			focus(state, cx)
		end
	end)
	if not ok then
		state.step = "form"
		state.error = cli.describe(err)
	end
end

local function install(state: any, cx: any)
	state.step = "installing"
	local ok, err = cli.run({ "tern", "install", "--no-plugin", "--json" }, { timeout_ms = 60000 }, function(data, e)
		if e then
			state.step = "done"
			state.error = cli.describe(e)
		else
			state.error = nil
			state.step = "installed"
			state.installed = type(data) == "table" and data or {}
		end
		cx:render()
	end)
	if not ok then
		state.step = "done"
		state.error = cli.describe(err)
	end
end

local function field(state: any, id: string, label: string, help: string, placeholder: string): Node
	local text = if id == "token" then masked(state.token) else state.url
	return h.col(id, {
		ui.text({ ui.span(label, "strong") }),
		ui.node("input", {
			key = "input",
			text = text,
			cursor = utf8.len(text) or #text,
			placeholder = placeholder,
			mono = true,
			actions = { click = "focus=" .. id },
		}),
		h.muted(help),
	}, { gap = "xs" })
end

local function view_form(state: any): { Node }
	local busy = state.step == "busy"
	local parts: { Node } = {
		h.col("form", {
			field(state, "url", "1 · Symphony address", "The same address your team uses in the browser and the CLI.", "https://symphony.example.com"),
			field(state, "token", "2 · API token", "Ask your team admin: symphony token create <your-name> --role operator. The token goes to the keychain, never to Tern.", "sym_…   paste with ⌘V"),
		}, { gap = "lg" }),
		h.row("acts", {
			h.action_key(if busy then "Signing in…" else "Sign in", "login", "enter", "accent"),
			h.muted(if busy then "checking the token with GET /v1/me…" else "tab switches field"),
		}),
	}
	if state.error then
		table.insert(parts, h.error_card("error", "Could not sign in", state.error, nil))
	end
	return parts
end

local function view_done(state: any): { Node }
	local me = state.me or {}
	local where = if me.storage == "file" then "in a protected file (0600), because there is no keychain" else "in the system keychain"
	local parts: { Node } = {
		h.card("ok", { ui.span("Connected", "strong success") }, {
			ui.kv({
				{ "Address", { ui.span(tostring(me.url or "—"), "mono") } },
				{ "You", { ui.span(tostring(me.name or "—"), "strong"), ui.span("  " .. tostring(me.role or "?"), "muted") } },
				{ "Token", "stored " .. where },
			}),
		}, "success"),
	}
	if state.step == "installed" then
		local got = state.installed or {}
		local lines: { { Spans } } = {
			{ "Live chat", "ACP agent “Symphony” in the Tern settings" },
			{ "MCP", "symphony server for your omp and other agents" },
			{ "Plugin", "this panel, already installed in Tern" },
		}
		if type(got.settings) == "string" then
			table.insert(lines, { "Settings", { ui.span(got.settings, "mono") } })
		end
		table.insert(parts, h.card("installed", { ui.span("All set in Tern", "strong success") }, {
			ui.kv(lines),
			h.row("acts", { h.action_key("Open dashboard", "open_panel", "enter", "accent") }),
		}, "success"))
	else
		local installing = state.step == "installing"
		table.insert(parts, h.card("next", { ui.span("Next step: install in Tern", "strong") }, {
			h.muted("Adds live chat with the agents (ACP agent) and the Symphony MCP for your omp. It changes only the Symphony entries in your settings."),
			h.row("acts", {
				h.action_key(if installing then "Installing…" else "Install in Tern", "install", "enter", "accent"),
				h.action("Not now, open dashboard", "open_panel", "muted"),
				h.action("Switch account", "relogin", "muted"),
			}),
		}))
	end
	if state.error then
		table.insert(parts, h.error_card("error", "The install failed", state.error, nil))
	end
	return parts
end

local function text_key(state: any, k: Key): boolean
	local f = state.focus
	local cur = state[f] :: string
	if k.name == "paste" then
		local t = string.gsub(k.text or "", "[\r\n]+", "")
		state[f] = cur .. t
	elseif k.name == "backspace" then
		if k.alt then
			state[f] = ""
		else
			local n = utf8.len(cur) or 0
			state[f] = if n > 0 then string.sub(cur, 1, (utf8.offset(cur, n) or 1) - 1) else ""
		end
	elseif k.text and k.text ~= "" and not k.ctrl and not k.meta then
		state[f] = cur .. k.text
	else
		return false
	end
	state.error = nil
	return true
end

M.def = {
	font = "sans",
	init = function(cx: any, _args: { string }, saved: any)
		local state = {
			step = "checking",
			url = type(saved) == "table" and type(saved.url) == "string" and saved.url or "",
			token = "",
			focus = "url",
			error = nil,
			me = nil,
			installed = nil,
		}
		local ok = cli.run({ "token", "print", "--json" }, { timeout_ms = 10000 }, function(data, e)
			if not e and type(data) == "table" and data.url then
				state.step = "done"
				state.me = { url = data.url, name = data.name, role = data.role }
			else
				state.step = "form"
				if e and e.code ~= "not_logged_in" then
					state.error = cli.describe(e)
				end
				if state.url ~= "" then
					state.focus = "token"
				end
			end
			cx:render()
			if state.step == "form" then
				focus(state, cx)
			end
		end)
		if not ok then
			state.step = "form"
			state.error = cli.missing().message
		end
		return state
	end,
	title = function(_state: any)
		return "Symphony · Sign in"
	end,
	view = function(state: any, _cx: any)
		local parts: { Node } = {
			ui.text({ ui.span("Sign in to Symphony", "title strong"), ui.span("  connect this Tern to your team's agents", "muted") }),
		}
		if state.step == "checking" then
			table.insert(parts, h.loading("checking", "Checking whether you are signed in…"))
		elseif state.step == "form" or state.step == "busy" then
			for _, n in view_form(state) do
				table.insert(parts, n)
			end
		else
			for _, n in view_done(state) do
				table.insert(parts, n)
			end
		end
		local legend = if state.step == "form"
			then { { "tab", "field" }, { "⌘V", "paste" }, { "enter", "sign in" } }
			else { { "enter", "continue" } }
		return {
			main = ui.node("col", { gap = "lg", max = { w = "96ch" } }, parts),
			dock = ui.col({ h.legend(legend) }),
		}
	end,
	key = function(state: any, k: Key, cx: any): boolean?
		if state.step == "form" then
			if k.name == "tab" or k.name == "up" or k.name == "down" then
				local at = table.find(FIELDS, state.focus) or 1
				state.focus = FIELDS[at % #FIELDS + 1]
				focus(state, cx)
				return true
			elseif k.name == "enter" then
				if state.focus == "url" and state.token == "" then
					state.focus = "token"
					focus(state, cx)
				else
					login(state, cx)
				end
				return true
			end
			return text_key(state, k)
		elseif state.step == "done" and k.name == "enter" then
			install(state, cx)
			return true
		elseif state.step == "installed" and k.name == "enter" then
			cx:open(fmt.ui_link({ kind = "open", view = "dashboard", pane = cx.pane }))
			return false
		end
		return false
	end,
	event = function(state: any, ev: UiEvent, cx: any)
		if ev.ev == "focus" and type(ev.id) == "string" then
			local f = string.match(ev.id, "^main%.form%.(%w+)%.input$")
			if f then
				state.focus = f
				focus(state, cx)
			end
		elseif ev.ev == "action" then
			if ev.act == "focus" and (ev.value == "url" or ev.value == "token") then
				state.focus = ev.value
				focus(state, cx)
			elseif ev.act == "login" and state.step == "form" then
				login(state, cx)
			elseif ev.act == "install" and state.step == "done" then
				install(state, cx)
			elseif ev.act == "open_panel" then
				cx:open(fmt.ui_link({ kind = "open", view = "dashboard", pane = cx.pane }))
			elseif ev.act == "relogin" then
				state.step = "form"
				state.focus = "token"
				state.error = nil
				focus(state, cx)
			end
		end
	end,
	save = function(state: any)
		return { url = state.url }
	end,
}

return M
