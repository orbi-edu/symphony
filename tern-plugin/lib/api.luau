--!strict
-- Host-side read path: the token lives only in this table's memory, obtained from the CLI
-- (`symphony token print --json`), never in a block's args or `save()`. Reads go straight to
-- the API with tern.fetch and `If-None-Match`, so an unchanged poll is a bodiless 304.

local cli = require("./cli")

local M = {}

export type Creds = { url: string, token: string, name: string?, role: string? }
export type Client = {
	creds: Creds?,
	auth: string, -- "checking" | "ok" | "logged_out" | "cli_missing" | "error"
	auth_error: cli.CliError?,
	authing: boolean,
	etags: { [string]: string },
	cache: { [string]: any },
	inflight: { [string]: boolean },
	errors: { [string]: cli.CliError },
	last_ok: number?,
}

local RANK = { viewer = 1, operator = 2, admin = 3 }

function M.new(): Client
	return {
		creds = nil,
		auth = "checking",
		auth_error = nil,
		authing = false,
		etags = {},
		cache = {},
		inflight = {},
		errors = {},
		last_ok = nil,
	}
end

function M.can(c: Client, role: string): boolean
	local mine = c.creds and c.creds.role or "viewer"
	return (RANK[mine or "viewer"] or 1) >= (RANK[role] or 3)
end

-- Asks the CLI for the credential; `done()` runs after `c.auth` settles.
function M.authenticate(c: Client, done: () -> ())
	if c.authing then
		return
	end
	c.authing = true
	local ok, err = cli.run({ "token", "print", "--json" }, { timeout_ms = 10000 }, function(data, e)
		c.authing = false
		if e then
			c.creds = nil
			c.auth = if e.code == "not_logged_in" then "logged_out" elseif e.code == "cli_missing" then "cli_missing" else "error"
			c.auth_error = e
		elseif type(data) == "table" and type(data.url) == "string" and type(data.token) == "string" then
			local url = string.gsub(data.url, "/+$", "")
			c.creds = { url = url, token = data.token, name = data.name, role = data.role }
			c.auth = "ok"
			c.auth_error = nil
			c.etags, c.cache, c.errors = {}, {}, {}
		else
			c.creds = nil
			c.auth = "error"
			c.auth_error = { code = "cli_failed", message = "symphony token print --json returned an unexpected format." }
		end
		done()
	end)
	if not ok then
		c.authing = false
		c.auth = "cli_missing"
		c.auth_error = err
		done()
	end
end

local function api_error(status: number, body: string): cli.CliError
	local ok, doc = pcall(tern.json.decode, body)
	if ok and type(doc) == "table" and type(doc.error) == "table" then
		return { code = tostring(doc.error.code or status), message = tostring(doc.error.message or "") }
	end
	local codes = { [401] = "unauthorized", [403] = "forbidden", [404] = "not_found", [503] = "unavailable" }
	return { code = codes[status] or ("http_" .. status), message = "HTTP " .. status }
end

-- GET `path`; `cb(changed, data, err)`. A 304 is `changed = false` with the cached data.
-- A 401 drops the credential so the next tick asks the CLI again (a re-login takes effect).
function M.get(c: Client, path: string, cb: (changed: boolean, data: any, err: cli.CliError?) -> ())
	local creds = c.creds
	if not creds or c.inflight[path] then
		return
	end
	c.inflight[path] = true
	local headers: { [string]: string } = {
		Authorization = "Bearer " .. creds.token,
		Accept = "application/json",
		["User-Agent"] = "symphony-tern/1",
	}
	local etag = c.etags[path]
	if etag then
		headers["If-None-Match"] = etag
	end
	tern.fetch(creds.url .. path, { headers = headers, timeout_ms = 5000 }, function(r)
		c.inflight[path] = nil
		if r.error then
			local e = { code = if r.timed_out then "timeout" else "unreachable", message = tostring(r.error) }
			c.errors[path] = e
			cb(false, c.cache[path], e)
			return
		end
		if r.status == 304 then
			c.errors[path] = nil
			c.last_ok = tern.now()
			cb(false, c.cache[path], nil)
			return
		end
		if r.status >= 200 and r.status < 300 then
			local ok, doc = pcall(tern.json.decode, r.body)
			if not ok then
				local e = { code = "bad_response", message = "Invalid response from " .. path }
				c.errors[path] = e
				cb(false, c.cache[path], e)
				return
			end
			c.etags[path] = r.headers["etag"]
			c.cache[path] = doc
			c.errors[path] = nil
			c.last_ok = tern.now()
			cb(true, doc, nil)
			return
		end
		local e = api_error(r.status, r.body)
		if r.status == 401 then
			c.creds = nil
			c.auth = "checking"
		end
		c.errors[path] = e
		cb(false, c.cache[path], e)
	end)
end

-- Forgets the cached copy of `path` so the next GET returns the body even when unchanged.
function M.invalidate(c: Client, path: string)
	c.etags[path] = nil
end

return M
