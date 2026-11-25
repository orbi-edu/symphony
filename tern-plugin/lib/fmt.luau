--!strict
-- Wording and formatting shared by both halves: state words and tones, relative times,
-- money, token counts, UTF-8 safe truncation and the `symphony://ui?req=` link encoding.

local M = {}

-- Session states (internals §2) → word and tone.
M.STATE = {
	starting = { "starting", "pending" },
	running = { "running", "accent" },
	waiting_input = { "needs input", "pending" },
	idle = { "idle", "muted" },
	ended = { "ended", "muted" },
}

M.STATE_ICON = {
	starting = "clock",
	running = "activity",
	waiting_input = "help",
	idle = "pause",
	ended = "check",
}

-- Session kinds (internals §2).
M.KIND = {
	implement = "implementation",
	answer = "answer",
	review = "review",
	oracle = "oracle",
	arbiter = "arbiter",
	resolve = "resolution",
	reflect = "reflection",
	assist = "assistant",
}

-- The roles an `assist` session runs as (assistant/engine.py modes).
M.ASSIST_ROLE = { assistant = "assistant", researcher = "research", planner = "planning" }

-- Chat transports of the assistant.
M.TRANSPORT = { slack = "Slack", discord = "Discord" }

-- Issue phases (GET /v1/issues).
M.PHASE = {
	running = { "running", "accent" },
	waiting_input = { "needs input", "pending" },
	queued = { "queued", "pending" },
	retrying = { "retrying", "warning" },
	in_review = { "in review", "info" },
	merging = { "merging", "accent" },
	held = { "held by a person", "pending" },
	blocked_on_person = { "waiting for a person", "warning" },
}

-- Why an issue can't be dispatched (`/v1/issues` `reason`).
M.ISSUE_REASON = {
	no_repo_designation = "no repository set",
	malformed_repo_designation = "malformed repository",
	mixed_owner_repo_designation = "repositories of more than one owner",
	repo_not_authorized = "repository not authorized",
}

-- Agent roles of the team config (teamconfig.ROLES), in display order. The Models tab lists
-- every role the API's configuration names, so a role added later shows up without a change
-- here (it takes its id as its name until it gets one below).
M.ROLES = { "implementer", "reviewer", "oracle", "arbiter", "developer", "reflector", "probe", "assistant", "researcher", "planner" }
M.ROLE = {
	implementer = { "Implementer", "writes the code for each ticket" },
	reviewer = { "Reviewer", "reviews the PRs in the merger" },
	oracle = { "Oracle", "confirms the reviewer's findings" },
	arbiter = { "Arbiter", "breaks ties between reviewer and oracle" },
	developer = { "Developer", "fixes the PR after review" },
	reflector = { "Reflector", "retrospective and learnings" },
	probe = { "Probe", "quick, cheap checks" },
	assistant = { "Assistant", "answers questions in Slack and Discord" },
	researcher = { "Researcher", "research and discovery for the assistant" },
	planner = { "Planner", "plans tickets for a person to confirm" },
}

-- The roles of a configuration: the known ones in display order, then any other role it names.
function M.roles_of(models: any): { string }
	if type(models) ~= "table" then
		return table.clone(M.ROLES)
	end
	local out, seen = {}, {}
	for _, r in M.ROLES do
		if models[r] ~= nil then
			table.insert(out, r)
			seen[r] = true
		end
	end
	local extra = {}
	for r in models do
		if type(r) == "string" and not seen[r] then
			table.insert(extra, r)
		end
	end
	table.sort(extra)
	for _, r in extra do
		table.insert(out, r)
	end
	return out
end

-- A role's name and line, for a role this plugin does not know yet too.
function M.role_info(role: string): { string }
	return M.ROLE[role] or { (string.upper(string.sub(role, 1, 1)) .. string.sub(role, 2)), "agent role" }
end

M.THINKING = { "off", "minimal", "low", "medium", "high", "xhigh", "max" }
M.THINKING_WORD = {
	off = "off",
	minimal = "minimal",
	low = "low",
	medium = "medium",
	high = "high",
	xhigh = "very high",
	max = "max",
}

-- API token roles (apitokens.py).
M.TOKEN_ROLES = { "viewer", "operator", "admin" }
M.TOKEN_ROLE = {
	viewer = "viewer: watches",
	operator = "operator: also chats, interrupts and pauses",
	admin = "admin: also manages tokens, providers and models",
}

function M.state(s: string?): (string, string)
	local e = M.STATE[s or ""]
	if e then
		return e[1], e[2]
	end
	return s or "—", "muted"
end

function M.phase(p: string?): (string, string)
	local e = M.PHASE[p or ""]
	if e then
		return e[1], e[2]
	end
	return p or "—", "muted"
end

-- Wall clock in milliseconds.
function M.now_ms(): number
	return os.time() * 1000
end

local function two(n: number): string
	return string.format("%02d", n)
end

-- Days from civil date (Howard Hinnant's algorithm), for ISO timestamps without os.time(table) UTC.
local function days_from_civil(y: number, m: number, d: number): number
	y = if m <= 2 then y - 1 else y
	local era = (if y >= 0 then y else y - 399) // 400
	local yoe = y - era * 400
	local mp = (m + 9) % 12
	local doy = (153 * mp + 2) // 5 + d - 1
	local doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
	return era * 146097 + doe - 719468
end

-- Milliseconds since the epoch from a number (ms, or s when small) or an ISO-8601 string.
function M.to_ms(v: any): number?
	if type(v) == "number" then
		return if v < 1e11 then v * 1000 else v
	end
	if type(v) ~= "string" then
		return nil
	end
	local y, mo, d, h, mi, s = string.match(v, "^(%d%d%d%d)-(%d%d)-(%d%d)[T ](%d%d):(%d%d):(%d%d)")
	if not y then
		return nil
	end
	local secs = days_from_civil(tonumber(y) :: number, tonumber(mo) :: number, tonumber(d) :: number) * 86400
		+ (tonumber(h) :: number) * 3600
		+ (tonumber(mi) :: number) * 60
		+ (tonumber(s) :: number)
	local sign, oh, om = string.match(v, "([+-])(%d%d):?(%d%d)$")
	if sign then
		local off = (tonumber(oh) :: number) * 3600 + (tonumber(om) :: number) * 60
		secs = if sign == "+" then secs - off else secs + off
	end
	return secs * 1000
end

-- "now", "12 s ago", "3 min ago", "2 h ago", "4 d ago".
function M.ago(v: any): string
	local t = M.to_ms(v)
	if not t then
		return "—"
	end
	local s = math.floor((M.now_ms() - t) / 1000)
	if s < 5 then
		return "now"
	elseif s < 60 then
		return s .. " s ago"
	elseif s < 3600 then
		return s // 60 .. " min ago"
	elseif s < 86400 then
		return s // 3600 .. " h ago"
	end
	return s // 86400 .. " d ago"
end

-- "in 3 d", "in 5 h"; "expired" in the past.
function M.until_(v: any): string
	local t = M.to_ms(v)
	if not t then
		return "never"
	end
	local s = math.floor((t - M.now_ms()) / 1000)
	if s <= 0 then
		return "expired"
	elseif s < 3600 then
		return "in " .. math.max(1, s // 60) .. " min"
	elseif s < 86400 then
		return "in " .. s // 3600 .. " h"
	end
	return "in " .. s // 86400 .. " d"
end

-- 12.5 → "$12.50".
function M.money(v: any): string
	if type(v) ~= "number" then
		return "—"
	end
	return "$" .. string.format("%.2f", v)
end

-- 45210 → "45.2k"; 1200000 → "1.2M".
function M.count(v: any): string
	if type(v) ~= "number" then
		return "—"
	end
	if v >= 1e6 then
		return string.format("%.1f", v / 1e6) .. "M"
	elseif v >= 1e3 then
		return string.format("%.1f", v / 1e3) .. "k"
	end
	return tostring(math.floor(v))
end

-- Total tokens of a session meta `tokens` field (a table or a number).
function M.tokens(t: any): number?
	if type(t) == "number" then
		return t
	elseif type(t) == "table" then
		return t.total or ((t.input or 0) + (t.output or 0))
	end
	return nil
end

-- Cuts `s` to at most `n` characters (not bytes), adding "…"; collapses whitespace.
function M.cut(s: any, n: number): string
	if type(s) ~= "string" then
		return ""
	end
	s = string.gsub(s, "%s+", " ")
	s = string.match(s, "^%s*(.-)%s*$") or ""
	local len = utf8.len(s)
	if not len or len <= n then
		return s
	end
	local stop = utf8.offset(s, n) or #s
	return string.sub(s, 1, stop - 1) .. "…"
end

-- "3" → "3 PR", plural-aware ("1 session", "2 sessions").
function M.plural(n: number, one: string, many: string): string
	return tostring(n) .. " " .. (if n == 1 then one else many)
end

-- Length of a list-or-number field (merger summaries come either way).
function M.size(v: any): number
	if type(v) == "number" then
		return v
	elseif type(v) == "table" then
		return #v
	end
	return 0
end

function M.urlencode(s: string): string
	return (string.gsub(s, "[^%w%-%._~]", function(c)
		return string.format("%%%02X", string.byte(c))
	end))
end

function M.urldecode(s: string): string
	s = string.gsub(s, "+", " ")
	return (string.gsub(s, "%%(%x%x)", function(h)
		return string.char(tonumber(h, 16) :: number)
	end))
end

-- The link a block opens to ask its window for UI only the window has (pickers, prompts, tabs).
M.UI_PREFIX = "symphony://ui?req="

function M.ui_link(req: { [string]: any }): string
	return M.UI_PREFIX .. M.urlencode(tern.json.encode(req))
end

function M.parse_ui_link(url: string): { [string]: any }?
	if string.sub(url, 1, #M.UI_PREFIX) ~= M.UI_PREFIX then
		return nil
	end
	local ok, req = pcall(tern.json.decode, M.urldecode(string.sub(url, #M.UI_PREFIX + 1)))
	if ok and type(req) == "table" then
		return req
	end
	return nil
end

-- POSIX single-quoting for command lines typed into a new tab.
function M.shq(s: string): string
	return "'" .. string.gsub(s, "'", "'\\''") .. "'"
end

-- Clock time "14:05" of a ms timestamp in local time.
function M.clock(v: any): string
	local t = M.to_ms(v)
	if not t then
		return "—"
	end
	local d = os.date("*t", math.floor(t / 1000)) :: any
	return two(d.hour) .. ":" .. two(d.min)
end

-- A merger action target `<pr>@<repo>` as people read it: `acme/web #821`.
function M.merger_ref(ref: any): string
	local pr, repo = string.match(tostring(ref or ""), "^(%d+)@(.+)$")
	return if pr then repo .. " #" .. pr else "#" .. tostring(ref)
end

return M
