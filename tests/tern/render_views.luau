--!nonstrict
-- Renders the plugin's views for tests/tern/test_tern_views.py, from the temporary copy the test
-- makes (`lib/` holds the plugin's modules and `tern_stub.luau`). The argument is a Luau table
-- literal `{ [name] = {view, period, data, errs, sel} }`, `N` standing for JSON null; the output is one
-- JSON document `{ [name] = {main, dock} }` on stdout.

local tern = require("./lib/tern_stub")
local V = require("./lib/views")
local apimod = require("./lib/api")

local cases = assert(loadstring("return function(N) return " .. (...) .. " end"))()(tern.json.null)
local out = {}
for name, case in cases do
	local api = apimod.new()
	api.auth = "ok"
	api.creds = { url = "https://symphony.example.com", token = "dummy", name = "alice", role = "admin" }
	local state = {
		api = api, view = case.view, period = case.period or "all", admin_tab = "models", filter = "active",
		sel = case.sel or {}, data = case.data, errs = case.errs or {}, draft = {}, busy = {}, read_at = {},
		mcp_tests = {}, cols = 160,
	}
	out[name] = V.render(state)
end
print(tern.json.encode(out))
