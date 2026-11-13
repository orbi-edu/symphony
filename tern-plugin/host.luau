--!strict
-- Host half: the block types. Their state lives on the session daemon, so every window
-- attached to the session shows the same panel and it survives windows closing.

local app = require("./lib/app")
local setup = require("./lib/setup")
local signin = require("./lib/signin")

tern.block.define("app", app.def)
tern.block.define("signin", signin.def)
tern.block.define("setup", setup.def)
