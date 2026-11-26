--!strict
-- The plugin's self-update (docs/tern.md). The target is the version the server runs (`version`
-- in `GET /v1/overview`): the CLI updates to it (`symphony update`), and the plugin to the CLI's
-- own copy (`symphony tern install --plugin-only`, which Tern follows with a reload). On each
-- overview the host compares its version (plugin.toml) with the server's; only when it is behind
-- does it read the CLI's (`symphony version --json`, once per session) and decide. A checkout
-- (`editable`) or another kind of install gets a banner with the command, never an automatic
-- action. Each target is tried once: the attempt is kept in `tern.kv` for TRY_MS, so a reload
-- that follows an update does not loop.

local cli = require("./cli")

local M = {}

export type Info = { cli: string?, install: string?, command: string? }
export type Decision = {
	action: string, -- "none" | "update_cli" | "install_plugin" | "banner"
	key: string?,
	version: string?,
	text: string?,
}
export type Notice = { tone: string, text: string, kind: string, at: number }

local TRY_MS = 6 * 3600 * 1000
local TIMED_MS = 60000
local KV_TRIED = "self_update.tried"
local KV_PENDING = "self_update.pending"
local PLUGIN_ONLY = "symphony tern install --plugin-only"

-- A semantic version as `{major, minor, patch, pre}` (`pre` "" for a release), or nil.
function M.parse(v: any): { any }?
	if type(v) ~= "string" then
		return nil
	end
	local major, minor, patch, rest = string.match(v, "^v?(%d+)%.(%d+)%.(%d+)(.*)$")
	if not major then
		return nil
	end
	rest = string.gsub(rest :: string, "%+.*$", "")
	local pre = ""
	if rest ~= "" then
		pre = string.match(rest, "^%-([%w%.%-]+)$") :: string
		if not pre then
			return nil
		end
	end
	return { tonumber(major), tonumber(minor), tonumber(patch), pre }
end

-- SemVer §11 for pre-release identifiers: numeric ones compare as numbers and sort first.
local function pre_less(a: string, b: string): boolean
	if a == b then
		return false
	elseif a == "" or b == "" then
		return b == "" -- a pre-release sorts before its release
	end
	local pa, pb = string.split(a, "."), string.split(b, ".")
	for i = 1, math.max(#pa, #pb) do
		local x, y = pa[i], pb[i]
		if x == nil then
			return true
		elseif y == nil then
			return false
		end
		if x ~= y then
			local nx, ny = tonumber(x), tonumber(y)
			if nx and ny then
				return nx < ny
			elseif nx or ny then
				return nx ~= nil
			end
			return x < y
		end
	end
	return false
end

-- `a` is older than `b`; false when either is not a version, so garbage never triggers an update.
function M.older(a: any, b: any): boolean
	local x, y = M.parse(a), M.parse(b)
	if not x or not y then
		return false
	end
	for i = 1, 3 do
		if x[i] ~= y[i] then
			return x[i] < y[i]
		end
	end
	return pre_less(x[4], y[4])
end

-- What to do, from this plugin's version, the CLI's `symphony version --json`, the server's
-- version and the keys already tried (`tried[key]` true).
function M.decide(own: string?, info: Info?, server: string?, tried: { [string]: boolean }): Decision
	if not server or not info or not info.cli then
		return { action = "none" }
	end
	local cli_old = M.older(info.cli, server)
	local plugin_old = M.older(own, info.cli)
	if info.install == "tool" then
		if cli_old then
			local key = "cli:" .. server
			if tried[key] then
				return { action = "none" }
			end
			return { action = "update_cli", key = key, version = server, text = "Updating Symphony to " .. server .. "…" }
		elseif plugin_old then
			local key = "plugin:" .. info.cli
			if tried[key] then
				return { action = "none" }
			end
			return { action = "install_plugin", key = key, version = info.cli,
				text = "Updating the Symphony plugin to " .. info.cli .. "…" }
		end
		return { action = "none" }
	end
	-- A checkout or another kind of install: the person runs the command.
	if cli_old then
		return { action = "banner", version = server, text = "The server runs Symphony " .. server .. "; this CLI is "
			.. info.cli .. ". Update it: " .. (info.command or "symphony update") }
	elseif plugin_old then
		return { action = "banner", version = info.cli, text = "This Symphony plugin is " .. tostring(own)
			.. "; the CLI is " .. info.cli .. ". Update it: " .. PLUGIN_ONLY }
	end
	return { action = "none" }
end

-- `version` of this plugin's plugin.toml (relative paths resolve against the plugin dir).
function M.own_version(): string?
	local ok, text = pcall(tern.fs.read, "plugin.toml", 65536)
	if not ok or type(text) ~= "string" then
		tern.log.warn("symphony: cannot read plugin.toml", text)
		return nil
	end
	return string.match("\n" .. text, '\nversion%s*=%s*"([^"]+)"')
end

-- ── the session ───────────────────────────────────────────────────────────────────────────

-- One per host VM: `info` is false when `symphony version` failed (no retry this session).
local S: any = { own = nil, info = nil, reading = false, running = false, notice = nil, announced = false }

local function kv_get(key: string): any
	local ok, v = pcall(tern.kv.get, key)
	return if ok then v else nil
end

local function kv_set(key: string, value: any)
	pcall(tern.kv.set, key, value)
end

local function wall_ms(): number
	return os.time() * 1000
end

local function tried(): { [string]: boolean }
	local out = {}
	local now = wall_ms()
	local stored = kv_get(KV_TRIED)
	for k, at in if type(stored) == "table" then stored else {} do
		if type(at) == "number" and now - at < TRY_MS then
			out[k] = true
		end
	end
	return out
end

local function remember(key: string)
	local stored = kv_get(KV_TRIED)
	local keep = {}
	local now = wall_ms()
	for k, at in if type(stored) == "table" then stored else {} do
		if type(at) == "number" and now - at < TRY_MS then
			keep[k] = at
		end
	end
	keep[key] = now
	kv_set(KV_TRIED, keep)
end

local function notify(tone: string, text: string, kind: string)
	S.notice = { tone = tone, text = text, kind = kind, at = tern.now() }
end

-- The notice to show: `progress` until the command ends, a `banner` while its decision holds,
-- a `timed` one (an error, a success) for TIMED_MS.
function M.notice(): Notice?
	local n = S.notice
	if n and n.kind == "timed" and tern.now() - n.at > TIMED_MS then
		S.notice = nil
		return nil
	end
	return n
end

local function act(d: Decision, changed: () -> ())
	local key = d.key :: string
	remember(key)
	kv_set(KV_PENDING, d.version)
	S.running = true
	notify("muted", d.text :: string, "progress")
	local args = if d.action == "update_cli" then { "update", "--json" } else { "tern", "install", "--plugin-only", "--json" }
	local ok, err = cli.run(args, { timeout_ms = 20 * 60000 }, function(data, e)
		S.running = false
		if e then
			kv_set(KV_PENDING, nil)
			notify("error", "Symphony could not update to " .. tostring(d.version) .. ": " .. cli.describe(e), "timed")
		elseif d.action == "update_cli" and type(data) == "table" and data.status == "updated" then
			S.info.cli = data.server
			local plugin = data.plugin
			if type(plugin) == "table" and plugin.status == "installed" then
				-- The new CLI installed its plugin, and Tern reloads this one.
				remember("plugin:" .. tostring(data.server))
				notify("success", "Updated Symphony to " .. tostring(data.server) .. "; reloading", "timed")
			else
				notify("success", "Updated the Symphony CLI to " .. tostring(data.server), "timed")
			end
		elseif d.action == "update_cli" then
			-- `manual` or `current`: nothing changed here; the next overview decides again.
			kv_set(KV_PENDING, nil)
			S.info = nil
			S.notice = nil
		elseif type(data) == "table" and type(data.plugin) == "table" and data.plugin.status == "unchanged" then
			-- Tern has the new copy already; this panel still runs the old one.
			notify("warning", "Reopen the Symphony panel to load the plugin " .. tostring(d.version), "timed")
		else
			notify("success", "Updated the Symphony plugin to " .. tostring(d.version) .. "; reloading", "timed")
		end
		changed()
	end)
	if not ok then
		S.running = false
		notify("error", "Symphony could not update: " .. cli.describe(err), "timed")
	end
	changed()
end

-- Called with the server's version on each overview; `changed()` re-renders the caller.
function M.on_server(server: any, changed: () -> ())
	if type(server) ~= "string" or S.running then
		return
	end
	if S.own == nil then
		S.own = M.own_version() or false
	end
	local own = if S.own then S.own :: string else nil
	if not S.announced then
		S.announced = true
		local pending = kv_get(KV_PENDING)
		if pending ~= nil then
			kv_set(KV_PENDING, nil)
			if own and not M.older(own, pending) then
				notify("success", "Symphony updated to " .. own, "timed")
				changed()
			end
		end
	end
	-- The CLI and this plugin come from one release: when the plugin is not behind the server,
	-- neither is the CLI as a rule, and the panel spends no command on asking it.
	if own and not M.older(own, server) then
		if S.notice and S.notice.kind == "banner" then
			S.notice = nil
			changed()
		end
		return
	end
	if S.info == nil then
		if S.reading then
			return
		end
		S.reading = true
		local ok = cli.run({ "version", "--json" }, { timeout_ms = 60000 }, function(data, e)
			S.reading = false
			if e or type(data) ~= "table" or type(data.cli) ~= "string" then
				tern.log.warn("symphony version failed", if e then e.message else "unexpected output")
				S.info = false
				return
			end
			S.info = { cli = data.cli, install = data.install, command = data.command }
			M.on_server(server, changed)
		end)
		if not ok then
			S.reading = false
			S.info = false
		end
		return
	end
	if S.info == false then
		return
	end
	local d = M.decide(own, S.info, server, tried())
	if d.action == "banner" then
		if not (S.notice and S.notice.text == d.text) then
			notify("warning", d.text :: string, "banner")
			changed()
		end
	elseif d.action == "none" then
		if S.notice and S.notice.kind == "banner" then
			S.notice = nil
			changed()
		end
	else
		act(d, changed)
	end
end

return M
