--!strict
-- The `symphony` CLI is the only owner of the credential (internals §7): both halves read and
-- write through it. `run` calls `cb(data, err, cx)` with the decoded JSON stdout on exit 0, or a
-- normalized `{code, message}` error; it returns `false, err` instead when the CLI can't be
-- spawned, so a window caller can still use the `cx` it holds.

local M = {}

export type CliError = { code: string, message: string }

-- `SYMPHONY_BIN` names the CLI explicitly; it always wins.
local function explicit(): string?
	local b = tern.getenv("SYMPHONY_BIN")
	return if b and b ~= "" then b else nil
end

-- The command a shell tab types (takeover): the person's shell has their own PATH.
function M.bin(): string
	return explicit() or "symphony"
end

-- A Tern started from the Dock has launchd's PATH, not the login shell's, so its daemon does not
-- see where `uv tool install` and pipx put the CLI (`$UV_TOOL_BIN_DIR`, `$XDG_BIN_HOME` or
-- `~/.local/bin`) or where Homebrew puts it. Without `SYMPHONY_BIN` the CLI is looked up on the
-- daemon's PATH first, then in those directories. `$HOME` expands in the daemon's environment, and
-- the CLI inherits the extended PATH (its `tern install` writes the absolute path it finds).
local LOOKUP = 'export PATH="${PATH:+$PATH:}${UV_TOOL_BIN_DIR:+$UV_TOOL_BIN_DIR:}'
	.. '${XDG_BIN_HOME:+$XDG_BIN_HOME:}$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"; exec symphony "$@"'

-- The argv that runs `symphony ARGS...`; the arguments stay separate words, never shell text.
function M.argv(args: { string }): { string }
	local b = explicit()
	local argv = if b then { b } else { "/bin/sh", "-c", LOOKUP, "symphony" }
	for _, a in args do
		table.insert(argv, a)
	end
	return argv
end

M.MISSING = {
	code = "cli_missing",
	message = "Tern could not find the symphony CLI on its PATH or in ~/.local/bin, /opt/homebrew/bin or /usr/local/bin. Install the Symphony CLI, or set SYMPHONY_BIN to its full path and restart Tern.",
}

local function trim(s: string): string
	return string.match(s, "^%s*(.-)%s*$") or ""
end

local function decode(text: string): any
	local body = trim(text)
	if body == "" then
		return nil
	end
	local ok, value = pcall(tern.json.decode, body)
	if ok then
		return value
	end
	-- Some commands print progress before their JSON line: take the last line.
	local last = string.match(body, "([^\n]+)$")
	if last then
		local ok2, value2 = pcall(tern.json.decode, last)
		if ok2 then
			return value2
		end
	end
	return nil
end

function M.run(
	args: { string },
	opts: { stdin: string?, timeout_ms: number? }?,
	cb: (data: any, err: CliError?, cx: any) -> ()
): (boolean, CliError?)
	local argv = M.argv(args)
	local o = opts or {}
	local ok, spawn_err = pcall(tern.process.run, argv, {
		stdin = o.stdin,
		timeout_ms = o.timeout_ms or 20000,
		env = { NO_COLOR = "1", SYMPHONY_CLIENT = "tern-plugin" },
	}, function(r: any, cx: any)
		local data = decode(r.stdout)
		if r.status == 0 then
			cb(data or {}, nil, cx)
			return
		end
		local err: CliError
		if r.timed_out then
			err = { code = "timeout", message = "The symphony CLI did not respond in time." }
		elseif type(data) == "table" and type(data.error) == "table" then
			err = { code = tostring(data.error.code or "error"), message = tostring(data.error.message or "") }
		elseif r.status == 127 then
			err = M.MISSING
		else
			local text = trim(if r.stderr ~= "" then r.stderr else r.stdout)
			err = { code = "cli_failed", message = if text ~= "" then text else "The CLI exited with status " .. r.status }
		end
		-- A command can fail and still print its result (`setup notifications test` lists every
		-- channel and exits 1 when one failed): hand that result over with the error.
		cb(if type(data) == "table" and data.error == nil then data else nil, err, cx)
	end)
	if not ok then
		tern.log.warn("symphony CLI spawn failed", spawn_err)
		return false, M.MISSING
	end
	return true, nil
end

-- `symphony api METHOD PATH [--header K:V]... [--data -]` with the body on stdin, so secrets
-- (provider keys, new tokens) never appear in a process list.
function M.api(
	method: string,
	path: string,
	body: any,
	headers: { [string]: string }?,
	cb: (data: any, err: CliError?, cx: any) -> ()
): (boolean, CliError?)
	local args = { "api", method, path }
	for k, v in headers or {} do
		table.insert(args, "--header")
		table.insert(args, k .. ":" .. v)
	end
	local stdin = nil
	if body ~= nil then
		table.insert(args, "--data")
		table.insert(args, "-")
		stdin = tern.json.encode(body)
	end
	return M.run(args, { stdin = stdin }, cb)
end

-- English text for every `error.code` the control API returns (src/symphony/api), plus the
-- CLI's own. The API's `message` is English and is never shown; the only detail kept is the
-- field list of an `invalid_request`, which names fields, not prose.
local KNOWN = {
	not_logged_in = "You are not signed in to Symphony yet.",
	cli_missing = M.MISSING.message,
	cli_failed = "The symphony CLI failed.",
	timeout = "Symphony did not respond in time.",
	unreachable = "Could not reach Symphony.",
	bad_response = "Symphony sent a response Tern could not read.",
	unauthorized = "The token was rejected (revoked or expired). Sign in again.",
	invalid_token = "The token was rejected (revoked or expired). Sign in again.",
	forbidden = "Your role does not allow this action.",
	rate_limited = "Too many requests. Wait a few seconds.",
	not_found = "Not found. It may have been removed.",
	method_not_allowed = "This version of Symphony does not support this operation.",
	invalid_request = "Invalid request.",
	invalid_revision = "Invalid version in If-Match. Reload and try again.",
	precondition_required = "The version (If-Match) is missing. Reload and try again.",
	precondition_failed = "Someone changed this first. Reload and try again.",
	conflict = "This conflicts with another change. Reload and try again.",
	session_ended = "The session has already ended.",
	stale_target = "This attempt is no longer the current one.",
	review_mismatch = "The document changed since the review. Review it again.",
	idempotency_conflict = "This request was already made with different data.",
	invalid_idempotency_key = "Invalid idempotency key.",
	invalid_command = "Invalid command.",
	invalid_target = "Invalid target.",
	invalid_cursor = "Invalid page. Reload the list.",
	invalid_limit = "Invalid item limit.",
	invalid_audit = "Invalid audit entry.",
	invalid_admission = "The daemon rejected the request.",
	daemon_unavailable = "The daemon is not responding.",
	queue_full = "The command queue is full. Try again shortly.",
	store_busy = "Symphony is busy. Try again.",
	store_unavailable = "Symphony's storage is unavailable.",
	audit_unavailable = "The audit log is unavailable.",
	lease_lost = "Symphony lost ownership of the operation. Try again.",
	workflow_unavailable = "WORKFLOW.md cannot be reached.",
	workflow_conflict = "WORKFLOW.md changed. Reload and try again.",
	invalid_workflow = "WORKFLOW.md would become invalid.",
	providers_unavailable = "The provider storage is unavailable.",
	providers_conflict = "The providers changed. Reload and try again.",
	providers_not_bootstrapped = "The providers Secret is not installed in the cluster.",
	providers_outcome_unknown = "Could not confirm the save. Reload the providers before you try again.",
	providers_invalid = "The provider storage is corrupted. Ask the host administrator.",
	providers_rejected = "The cluster rejected the provider change.",
	providers_full = "The provider limit is reached.",
	invalid_provider = "Invalid provider definition.",
	unknown_provider = "The omp catalog has no provider with this id.",
	provider_key_required = "Enter a key. This provider has no stored key yet.",
	catalog_unavailable = "The omp model catalog is not installed.",
	merger_unavailable = "The merger is not deployed.",
	repo_required = "Name the repository. More than one merger runs.",
	unknown_repo = "No merger serves this repository.",
	projection_unavailable = "This data is unavailable right now.",
	unavailable = "A Symphony service is unavailable.",
	lease_not_held = "No Symphony session holds this device now.",
	mcp_unreachable = "Symphony could not reach the MCP server.",
	rejected = "The MCP server rejected the stored headers.",
}

function M.describe(err: CliError?): string
	if not err then
		return ""
	end
	local base = KNOWN[err.code]
	if err.code == "cli_failed" and err.message ~= "" then
		-- The CLI's own failures are already English.
		return "The symphony CLI failed: " .. err.message
	end
	if err.code == "invalid_request" then
		local fields = string.match(err.message or "", "^invalid request:%s*(.+)$")
		if fields then
			local FIELD = { reason = "the reason", text = "the text", name = "the name", role = "the role",
				expires_days = "the expiry", models = "the models", apiKey = "the key" }
			local names = {}
			for f in string.gmatch(fields, "[^,%s]+") do
				table.insert(names, FIELD[f] or f)
			end
			return "Invalid request. Check: " .. table.concat(names, ", ") .. "."
		end
	end
	if base then
		return base
	end
	if string.match(err.code or "", "^http_%d+$") then
		return "Symphony returned error " .. string.sub(err.code, 6) .. "."
	end
	-- Verification failures (setup) say what the service rejected; the API writes them in English.
	if type(err.message) == "string" and err.message ~= "" then
		return err.message
	end
	return "Symphony rejected the operation (" .. tostring(err.code) .. ")."
end

return M
