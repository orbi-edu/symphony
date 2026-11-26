--!nonstrict
-- Runs the plugin's self-update (lib/update.luau) for tests/tern/test_tern_self_update.py, from
-- the temporary copy the test makes (`lib/` holds the plugin's modules and `tern_stub.luau`). The
-- argument is a Luau table literal, `N` standing for JSON null:
--   {older = {{a, b}, …}}                  → {older = {bool, …}}
--   {decide = {own, info, server, tried}}  → {decision}
--   {own, server, kv, answers, polls}      → {runs, notice, kv, renders}
-- The last form calls `on_server(server)` `polls` times (default 2). After each call, the CLI
-- commands it started get the next `answers` in order, `{status, doc}`, as Tern would deliver
-- them; a command without an answer keeps running. `runs` lists each command's arguments.

local tern = require("./lib/tern_stub")
local U = require("./lib/update")

local case = assert(loadstring("return function(N) return " .. (...) .. " end"))()(tern.json.null)

if case.older then
	local out = {}
	for i, pair in case.older do
		out[i] = U.older(pair[1], pair[2])
	end
	print(tern.json.encode({ older = out }))
	return
end

if case.decide then
	local d = case.decide
	print(tern.json.encode(U.decide(d.own, d.info, d.server, d.tried or {})))
	return
end

tern.files["plugin.toml"] = 'schema = 1\nid = "symphony"\nname = "Symphony"\nversion = "' .. case.own .. '"\n'
for k, v in case.kv or {} do
	tern.store[k] = v
end
local renders = 0
local answered = 0
local function pump()
	while answered < #tern.runs do
		local a = (case.answers or {})[answered + 1]
		if a == nil then
			return
		end
		answered += 1
		local key = "answer " .. answered
		tern.docs[key] = a.doc
		tern.runs[answered].cb({ status = a.status or 0, stdout = key, stderr = "", timed_out = false })
	end
end
for _ = 1, case.polls or 2 do
	U.on_server(case.server, function()
		renders += 1
	end)
	pump()
end

local runs = {}
for i, r in tern.runs do
	-- `cli.argv` without SYMPHONY_BIN: `/bin/sh -c LOOKUP symphony ARGS...`.
	runs[i] = { table.unpack(r.argv, 5) }
end
print(tern.json.encode({ runs = runs, notice = U.notice() or tern.json.null, kv = tern.store, renders = renders }))
