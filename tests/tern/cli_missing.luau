--!nonstrict
-- Runs a CLI command that cannot start, for tests/tern/test_tern_signin.py, from the temporary
-- copy the test makes. Arguments: the daemon's environment as a Luau table literal, then how the
-- spawn fails: `exit127` (the shell found no CLI) or `throws` (Tern could not spawn the argv).
-- The output is the text the plugin shows for the error.

local tern = require("./lib/tern_stub")
local cli = require("./lib/cli")

local env, how = ...
for k, v in assert(loadstring("return " .. env))() do
	tern.env[k] = v
end
tern.process.run = function(_argv, _opts, cb)
	if how == "throws" then
		error("no such file or directory")
	end
	cb({ status = 127, stdout = "", stderr = "", timed_out = false }, {})
end

local shown = nil
local ok, err = cli.run({ "token", "print", "--json" }, nil, function(_data, e)
	shown = cli.describe(e)
end)
if not ok then
	shown = cli.describe(err)
end
print(shown)
