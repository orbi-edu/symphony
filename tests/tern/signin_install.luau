--!nonstrict
-- Presses Enter on the sign-in block's "Install in Tern" step for tests/tern/test_tern_signin.py,
-- from the temporary copy the test makes (`lib/` holds the plugin's modules and
-- `tern_stub.luau`). The argument is a Luau table literal: the daemon's environment as
-- `tern.getenv` sees it. The output is the argv the block spawned, as JSON.

local tern = require("./lib/tern_stub")
local signin = require("./lib/signin")

for k, v in assert(loadstring("return " .. (...)))() do
	tern.env[k] = v
end
local cx = { render = function() end, frame = function() end, open = function() end }
local state = { step = "done", me = { url = "https://symphony.example.com" } }
signin.def.key(state, { name = "enter" }, cx)
assert(state.step == "installing", "the block did not start the install")
print(tern.json.encode(tern.runs[#tern.runs].argv))
