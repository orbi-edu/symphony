--!nonstrict
-- A stub of the `tern` global for running the plugin's modules outside Tern
-- (tests/tern/test_tern_views.py, tests/tern/test_tern_signin.py): view builders return
-- `{k, p, c}` nodes as Tern's do, and `json.encode` writes them as JSON. The tests copy the
-- plugin's modules next to this one and have each start with `local tern = require("./tern_stub")`.

local NULL = setmetatable({}, { __tostring = function()
	return "null"
end })

local function node(k: string, p: any?, c: any?): any
	return { k = k, p = p or {}, c = c }
end

local ui = {
	span = function(t, s)
		return { t = t, s = s }
	end,
	text = function(spans)
		return node("text", { spans = spans })
	end,
	col = function(children)
		return node("col", nil, children)
	end,
	row = function(children)
		return node("row", nil, children)
	end,
	lines = function(children)
		return node("lines", nil, children)
	end,
	card = function(head, children, props)
		local p = props or {}
		p.head = head
		return node("card", p, children)
	end,
	section = function(head, children)
		return node("section", { head = head }, children)
	end,
	badge = function(t, tone)
		return node("badge", { text = t, tone = tone })
	end,
	table = function(cols, rows, opts)
		local p = opts or {}
		p.cols, p.rows = cols, rows
		return node("table", p)
	end,
	list = function(items)
		local c = {}
		for i, it in items do
			c[i] = node("item", it)
		end
		return node("list", {}, c)
	end,
	kv = function(items)
		return node("kv", { items = items })
	end,
	md = function(text)
		return node("md", { text = text })
	end,
	code = function(text, lang)
		return node("code", { text = text, lang = lang })
	end,
	meter = function(value, label)
		return node("meter", { value = value, label = label })
	end,
	progress = function(value, label)
		return node("progress", { value = value, label = label })
	end,
	node = node,
}

local function encode(v: any): string
	local t = type(v)
	if v == nil or v == NULL then
		return "null"
	elseif t == "boolean" then
		return tostring(v)
	elseif t == "number" then
		if v ~= v or v == math.huge or v == -math.huge then
			return "null"
		end
		return if v == math.floor(v) and math.abs(v) < 1e15 then string.format("%d", v) else string.format("%.17g", v)
	elseif t == "string" then
		return '"' .. string.gsub(v, '[%c"\\]', function(ch)
			local named = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\t"] = "\\t", ["\r"] = "\\r" }
			return named[ch] or string.format("\\u%04x", string.byte(ch))
		end) .. '"'
	elseif t == "table" then
		local n = #v
		if n > 0 or next(v) == nil then
			local out = {}
			for i = 1, n do
				out[i] = encode(v[i])
			end
			return "[" .. table.concat(out, ",") .. "]"
		end
		local keys = {}
		for k in v do
			table.insert(keys, tostring(k))
		end
		table.sort(keys)
		local out = {}
		for _, k in keys do
			table.insert(out, encode(k) .. ":" .. encode(v[k]))
		end
		return "{" .. table.concat(out, ",") .. "}"
	end
	error("cannot encode a " .. t)
end

-- `env` is what `getenv` reads; `process.run` records each spawn in `runs` with its callback,
-- which it never calls (a test may). `files` is what `fs.read` reads, `store` the `kv`, and `docs`
-- what `json.decode` returns for a text (the stub has no JSON parser).
local env: { [string]: string } = {}
local runs = {}
local files: { [string]: string } = {}
local store: { [string]: any } = {}
local docs: { [string]: any } = {}

return {
	ui = ui,
	json = { null = NULL, encode = encode, array = function(t)
		return t or {}
	end, decode = function(text)
		local doc = docs[text]
		if doc == nil then
			error("not a known JSON document: " .. text)
		end
		return doc
	end },
	docs = docs,
	now = function()
		return os.clock() * 1000
	end,
	env = env,
	getenv = function(name)
		return env[name]
	end,
	runs = runs,
	process = { run = function(argv, opts, cb)
		table.insert(runs, { argv = argv, opts = opts, cb = cb })
	end },
	files = files,
	fs = { read = function(path)
		return assert(files[path], "no such file: " .. path)
	end },
	store = store,
	kv = { get = function(key)
		return store[key]
	end, set = function(key, value)
		store[key] = value
	end },
	log = { warn = function() end, info = function() end },
}
