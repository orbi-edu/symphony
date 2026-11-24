--!strict
-- Block `symphony.setup`: the first-run setup (setup contract §4), laid out as Tern's design
-- guide asks: a head row, a progress bar, then the steps beside the selected step's card.
-- The checklist is `GET /v1/setup`, read with `symphony setup --status --json`; every write is
-- a `symphony setup <step> … --json` call with the secret on stdin, never in argv, the block's
-- args or `save()`. The API verifies each value against the real service before it stores it,
-- and the card shows what it found (repositories, missing permissions, board states).
--
-- Tern 0.8.3 does not draw `button`, `stats`, `skeleton`, `empty`, `field`, `select` or
-- `switch`, ignores a card's `meta`/`icon`/`end`, an input's `secret` and a table cell's `dot`,
-- and its tables cannot select rows. So actions are keyed chips, the steps are a pickable
-- `list` with status icons, secrets are masked here, and the card's primary action is the
-- first row of its body (docs/tern.md, "Tern 0.8.3").

local cli = require("./cli")
local fmt = require("./fmt")
local h = require("./ui")

local ui = tern.ui

local M = {}

-- While incomplete, the status is read again this often (contract §4).
local STATUS_MS = 30000
-- Check-mark frames for a step that turns done.
local FRAME_MS = 90
local CHECK = { "·", "∘", "○", "◎", "●", "✓" }
local CHECK_MS = #CHECK * FRAME_MS

-- What each step is for, in one or two sentences.
local WHY = {
	tracker = "Symphony picks up tickets from your board and moves them as the agents work. It needs the board and a credential that can read and update it.",
	github_app = "The GitHub App clones the repositories, opens the pull requests and lets the merger review and merge them.",
	repositories = "Tickets go to the default repository unless they carry a repo: label. Each repository has its own merger.",
	models = "Every agent role needs a model whose provider has a credential. Set provider keys and models in Admin.",
	notifications = "Optional. Symphony tells your team in Slack or Discord when a ticket needs an answer or a PR is merged.",
	assistant = "Optional. A bot in Slack and Discord answers questions about the project, researches and plans tickets. It creates a ticket only after a person mapped in notify.users confirms. It uses the bot token from Notifications.",
	webhooks = "Optional, recommended. Webhooks wake Symphony as soon as a ticket or a PR changes, instead of on the next poll.",
	skills = "Optional. Central skill sources are instructions fetched from other repositories and staged for agents before each run.",
}

-- What the API checks a step against, for the spinner.
local VERIFYING = {
	tracker = "Checking the board and the credential…",
	github_app = "Minting an installation token and reading its repositories…",
	repositories = "Checking the repository…",
	notifications = "Sending a test message to the channel…",
	assistant = "Opening a Socket Mode connection and checking the bot…",
	webhooks = "Creating the webhook secrets…",
	skills = "Resolving the skill sources…",
	test = "Sending a test message to each channel…",
}

type Field = { id: string, label: string, help: string, placeholder: string, secret: boolean?, multiline: boolean?, optional: boolean? }

local F = {
	linear_key = { id = "api_key", label = "Linear API key", help = "From Linear settings › Security & access. It is stored in the cluster and never shown again.", placeholder = "lin_api_…", secret = true },
	team = { id = "team", label = "Team key", help = "The key in your ticket identifiers, for example ENG in ENG-123.", placeholder = "ENG" },
	project = { id = "project", label = "Project slug", help = "The project's slug from its Linear URL.", placeholder = "mobile-app-4f2a" },
	owner = { id = "owner", label = "Owner", help = "The organization or user that owns the Projects board.", placeholder = "acme" },
	project_number = { id = "project_number", label = "Project number", help = "The number in the board's URL: github.com/orgs/acme/projects/7.", placeholder = "7" },
	status_field = { id = "status_field", label = "Status field", help = "The single-select field that holds the ticket state. Leave empty for Status.", placeholder = "Status", optional = true },
	app_id = { id = "app_id", label = "App ID", help = "On the App's settings page, under About.", placeholder = "123456" },
	installation_id = { id = "installation_id", label = "Installation ID", help = "The number at the end of the installation's settings URL.", placeholder = "45678901" },
	private_key = { id = "private_key", label = "Private key", help = "Paste the whole .pem file, including the BEGIN and END lines.", placeholder = "-----BEGIN RSA PRIVATE KEY-----   paste with ⌘V", secret = true, multiline = true },
	default_repo = { id = "default_repo", label = "Default repository", help = "owner/name. Tickets without a repo: label go here.", placeholder = "acme/web" },
	base_branches = { id = "base_branches", label = "Base branches", help = "owner/name=branch, separated by spaces. Each overrides the branch Symphony treats as main. Empty: auto-detect from the remote.", placeholder = "acme/app=develop", optional = true },
	runtimes = { id = "runtimes", label = "Repository runtimes", help = "owner/name=runtime, separated by spaces. Sets the default runtime for each repository.", placeholder = "acme/app=dotnet10", optional = true },
	components = { id = "components", label = "Components", help = "owner/name:component=path@runtime, separated by spaces. Defines repository components for monorepo dispatch.", placeholder = "acme/app:api=api@dotnet10", optional = true },
	slack_channel = { id = "channel", label = "Slack channel", help = "The channel ID or name the bot posts to. Invite the bot to it first.", placeholder = "#eng-symphony" },
	slack_token = { id = "token", label = "Bot token", help = "The Slack app's bot token (xoxb-…).", placeholder = "xoxb-…", secret = true },
	discord_channel = { id = "channel", label = "Discord channel ID", help = "Right-click the channel › Copy Channel ID (developer mode on).", placeholder = "1234567890123456789" },
	discord_token = { id = "token", label = "Bot token", help = "The Discord bot's token. The bot needs Send Messages in the channel.", placeholder = "MTIz…", secret = true },
	webhook_url = { id = "url", label = "Webhook URL", help = "An incoming webhook URL for the channel.", placeholder = "https://…", secret = true },
	slack_channels = { id = "slack_channels", label = "Slack channels", help = "Channel IDs where it answers @mentions, separated by spaces. Direct messages always work.", placeholder = "C0123ABCD", optional = true },
	app_token = { id = "app_token", label = "Slack app token", help = "The app-level token (xapp-…) with connections:write, for Socket Mode. Leave it empty to keep the stored one.", placeholder = "xapp-…", secret = true, optional = true },
	discord_channels = { id = "discord_channels", label = "Discord channels", help = "Channel IDs where it answers @mentions, separated by spaces. The bot needs the Message Content intent.", placeholder = "123456789012345678", optional = true },
	assistant_repos = { id = "repos", label = "Repositories", help = "owner/name, separated by spaces. It reads them at their base branch. Empty: every repository Symphony routes to.", placeholder = "acme/app", optional = true },
	skill_repo = { id = "skill_repo", label = "Source repository", help = "The repository holding the skill directories (owner/name).", placeholder = "acme/agent-skills" },
	skill_path = { id = "skill_path", label = "Path", help = "Directory in the source repo holding skill sub-directories. Default: skills.", placeholder = "skills", optional = true },
	skill_ref = { id = "skill_ref", label = "Ref", help = "Branch or tag. Default: the repo's default branch.", placeholder = "main", optional = true },
	skill_roles = { id = "skill_roles", label = "Roles", help = "Restrict to these roles, separated by commas. Default: every role.", placeholder = "implementer, reviewer", optional = true },
	skill_for_repos = { id = "skill_for_repos", label = "Target repositories", help = "owner/name, separated by spaces. Default: every repository (global).", placeholder = "acme/app", optional = true },
}

-- The fields a step shows in its current mode.
local function fields(state: any, step: string?): { Field }
	local m = state.modes
	if step == "tracker" then
		if m.tracker == "github" then
			return { F.owner, F.project_number, F.status_field }
		end
		return { F.linear_key, if m.scope == "project" then F.project else F.team }
	elseif step == "github_app" then
		return { F.app_id, F.installation_id, F.private_key }
	elseif step == "repositories" then
		return { F.default_repo, F.base_branches, F.runtimes, F.components }
	elseif step == "notifications" then
		local provider = m.notify
		if m[provider] == "webhook" then
			return { F.webhook_url }
		end
		return { if provider == "discord" then F.discord_channel else F.slack_channel, if provider == "discord" then F.discord_token else F.slack_token }
	elseif step == "assistant" then
		return { F.slack_channels, F.app_token, F.discord_channels, F.assistant_repos }
	elseif step == "skills" then
		return { F.skill_repo, F.skill_path, F.skill_ref, F.skill_roles, F.skill_for_repos }
	end
	return {}
end

-- The values typed for a step, per mode (a Linear key and a GitHub owner never mix).
local function bucket(state: any, step: string): { [string]: string }
	local m = state.modes
	local k = step
	if step == "tracker" then
		k = "tracker:" .. m.tracker
	elseif step == "notifications" then
		k = "notifications:" .. m.notify .. ":" .. m[m.notify]
	end
	state.values[k] = state.values[k] or {}
	return state.values[k]
end

local function value(state: any, step: string, id: string): string
	return bucket(state, step)[id] or ""
end

local function trim(s: string): string
	return string.match(s, "^%s*(.-)%s*$") or ""
end

local function steps(state: any): { any }
	local doc = state.doc
	return (type(doc) == "table" and type(doc.steps) == "table") and doc.steps or {}
end

local function find(state: any, id: string?): any
	for _, s in steps(state) do
		if s.id == id then
			return s
		end
	end
	return nil
end

-- The `assistant` step's detail: `{running, heartbeat_at, threads, slack: {connected, channels},
-- discord: {connected, channels, intent}}` (`symphony assistant status`).
local function assistant_detail(state: any): any
	local s = find(state, "assistant")
	return (s and type(s.detail) == "table") and s.detail or {}
end

-- Ids typed separated by spaces or commas.
local function words(s: string): { string }
	local out = {}
	for w in string.gmatch(s, "[^,%s]+") do
		table.insert(out, w)
	end
	return out
end

local function complete(state: any): boolean
	return type(state.doc) == "table" and state.doc.complete == true
end

-- The first required step that is not done, else the first optional one.
local function next_step(state: any): string?
	for _, s in steps(state) do
		if s.required and s.status ~= "done" then
			return s.id
		end
	end
	for _, s in steps(state) do
		if not s.required and s.status ~= "done" and s.status ~= "skipped" then
			return s.id
		end
	end
	return nil
end

local function input_path(id: string): string
	return "main.steps.detail.form." .. id .. ".input"
end

local function focus(state: any, cx: any)
	if state.focus then
		cx:frame({ { "focus", input_path(state.focus) } })
	end
end

local function first_field(state: any, cx: any)
	local list = fields(state, state.sel)
	state.focus = if #list > 0 then list[1].id else nil
	if state.sel == "repositories" then
		local b = bucket(state, "repositories")
		local s = find(state, "repositories")
		local d = (s and type(s.detail) == "table") and s.detail or {}
		if b.base_branches == nil and type(d.base_branches) == "table" then
			local parts = {}
			for repo, branch in d.base_branches do
				table.insert(parts, tostring(repo) .. "=" .. tostring(branch))
			end
			table.sort(parts)
			b.base_branches = table.concat(parts, " ")
		end
		if b.runtimes == nil and type(d.repo_runtimes) == "table" then
			local parts = {}
			for repo, runtime in d.repo_runtimes do
				table.insert(parts, tostring(repo) .. "=" .. tostring(runtime))
			end
			table.sort(parts)
			b.runtimes = table.concat(parts, " ")
		end
		if b.components == nil and type(d.components) == "table" then
			local parts = {}
			for repo, repo_comps in d.components do
				if type(repo_comps) == "table" then
					for cname, cdef in repo_comps do
						local path = if type(cdef) == "table" then tostring(cdef.path or "") else ""
						local crt = if type(cdef) == "table" and cdef.runtime then "@" .. tostring(cdef.runtime) else ""
						table.insert(parts, tostring(repo) .. ":" .. tostring(cname) .. "=" .. path .. crt)
					end
				end
			end
			table.sort(parts)
			b.components = table.concat(parts, " ")
		end
	elseif state.sel == "assistant" then
		-- The channels already set start in the fields, so a change starts from them.
		local b = bucket(state, "assistant")
		local d = assistant_detail(state)
		for _, pair in { { "slack_channels", d.slack }, { "discord_channels", d.discord } } do
			local t = pair[2]
			if b[pair[1]] == nil and type(t) == "table" and type(t.channels) == "table" then
				b[pair[1]] = table.concat(t.channels, " ")
			end
		end
		if b.repos == nil and type(d.repositories) == "table" then
			b.repos = table.concat(d.repositories, " ")
		end
	end
	focus(state, cx)
end

local function animate(state: any, cx: any)
	if state.ticking then
		return
	end
	state.ticking = true
	local function frame()
		if state.stopped then
			return
		end
		local celebrating = false
		for _, at in state.celebrate do
			if tern.now() - at < CHECK_MS then
				celebrating = true
			end
		end
		cx:render()
		if state.busy or celebrating then
			tern.timer(FRAME_MS, frame)
		else
			state.ticking = false
		end
	end
	tern.timer(FRAME_MS, frame)
end

local function toast(cx: any, tone: string, text: string)
	if type(cx.toast) == "function" then
		cx:toast(tone, text)
	end
end

-- Reads the checklist; a step that turned done gets its check mark and a toast.
local function load(state: any, cx: any)
	local ok, err = cli.run({ "setup", "--status", "--json" }, { timeout_ms = 20000 }, function(data, e)
		if state.stopped then
			return
		end
		state.read_at = tern.now()
		if e then
			state.load_error = cli.describe(e)
		else
			state.load_error = nil
			local first = state.doc == nil
			state.doc = type(data) == "table" and data or {}
			for _, s in steps(state) do
				local was = state.prev[s.id]
				if s.status == "done" and was ~= nil and was ~= "done" then
					state.celebrate[s.id] = tern.now()
					toast(cx, "success", tostring(s.title) .. " is set up")
					animate(state, cx)
				end
				state.prev[s.id] = s.status
			end
			if first or not find(state, state.sel) then
				state.sel = next_step(state) or (steps(state)[1] and steps(state)[1].id)
				first_field(state, cx)
			end
			if complete(state) and not state.ready_told then
				state.ready_told = true
				if not first then
					toast(cx, "success", "Symphony is ready")
				end
			end
		end
		cx:render()
	end)
	if not ok then
		state.load_error = cli.describe(err)
	end
end

-- The CLI call that verifies and saves the selected step: `args` and the secret for stdin.
local function command(state: any): ({ string }?, string?, string?)
	local step = state.sel
	local v = function(id: string): string
		return trim(value(state, step, id))
	end
	local m = state.modes
	if step == "tracker" then
		if m.tracker == "github" then
			if v("owner") == "" or not string.match(v("project_number"), "^%d+$") then
				return nil, nil, "Enter the owner and the project number."
			end
			local args = { "setup", "tracker", "--kind", "github", "--owner", v("owner"), "--project-number", v("project_number") }
			if v("status_field") ~= "" then
				table.insert(args, "--status-field")
				table.insert(args, v("status_field"))
			end
			table.insert(args, "--json")
			return args, nil, nil
		end
		local scope = if m.scope == "project" then "project" else "team"
		if v("api_key") == "" or v(scope) == "" then
			return nil, nil, if scope == "project" then "Enter the API key and the project slug." else "Enter the API key and the team key."
		end
		return { "setup", "tracker", "--kind", "linear", "--" .. scope, v(scope), "--json" }, v("api_key") .. "\n", nil
	elseif step == "github_app" then
		if not string.match(v("app_id"), "^%d+$") or not string.match(v("installation_id"), "^%d+$") then
			return nil, nil, "Enter the App ID and the installation ID. Both are numbers."
		end
		if not string.find(v("private_key"), "PRIVATE KEY", 1, true) then
			return nil, nil, "Paste the whole private key file, including the BEGIN and END lines."
		end
		return { "setup", "github-app", "--app-id", v("app_id"), "--installation-id", v("installation_id"), "--json" }, v("private_key") .. "\n", nil
	elseif step == "repositories" then
		local repo = v("default_repo")
		local bb = v("base_branches")
		local rt = v("runtimes")
		local comp = v("components")
		-- At least default_repo, base_branches, runtimes, or components must be given.
		local has_repo = string.match(repo, "^[%w%.%-_]+/[%w%.%-_]+$")
		local has_bb = bb ~= ""
		local has_rt = rt ~= ""
		local has_comp = comp ~= ""
		if not has_repo and not has_bb and not has_rt and not has_comp then
			return nil, nil, "Enter the repository as owner/name, or set a base branch, runtime, or component."
		end
		local args = { "setup", "repositories" }
		if has_repo then
			table.insert(args, "--default-repo")
			table.insert(args, repo)
		end
		local kept = {}
		for _, pair in words(bb) do
			local name = string.match(pair, "^([%w%.%-_]+/[%w%.%-_]+)=.+$")
			if not name then
				return nil, nil, "Enter each base branch as owner/name=branch."
			end
			kept[string.lower(name)] = true
			table.insert(args, "--branch")
			table.insert(args, pair)
		end
		-- A repository removed from the field loses its override (`owner/name=` clears it).
		local s = find(state, "repositories")
		local saved = s and type(s.detail) == "table" and s.detail.base_branches
		if type(saved) == "table" then
			for name in saved do
				if not kept[string.lower(tostring(name))] then
					table.insert(args, "--branch")
					table.insert(args, tostring(name) .. "=")
				end
			end
		end
		-- Runtimes: same merge-then-clear pattern.
		local rt_kept = {}
		for _, pair in words(rt) do
			if not string.find(pair, "=", 1, true) then
				return nil, nil, "Enter each runtime as owner/name=runtime."
			end
			local name = string.match(pair, "^(.-)=")
			if name then rt_kept[string.lower(name)] = true end
			table.insert(args, "--runtime")
			table.insert(args, pair)
		end
		local saved_rt = s and type(s.detail) == "table" and s.detail.repo_runtimes
		if type(saved_rt) == "table" then
			for name in saved_rt do
				if not rt_kept[string.lower(tostring(name))] then
					table.insert(args, "--runtime")
					table.insert(args, tostring(name) .. "=")
				end
			end
		end
		-- Components: same merge-then-clear pattern (`owner/name:component=` removes one).
		local comp_kept = {}
		for _, pair in words(comp) do
			local key = string.match(pair, "^([%w%.%-_]+/[%w%.%-_]+:[%w%-]+)=.+$")
			if not key then
				return nil, nil, "Enter each component as owner/name:component=path@runtime."
			end
			comp_kept[string.lower(key)] = true
			table.insert(args, "--component")
			table.insert(args, pair)
		end
		local saved_comp = s and type(s.detail) == "table" and s.detail.components
		if type(saved_comp) == "table" then
			for name, comps in saved_comp do
				if type(comps) == "table" then
					for cname in comps do
						local key = tostring(name) .. ":" .. tostring(cname)
						if not comp_kept[string.lower(key)] then
							table.insert(args, "--component")
							table.insert(args, key .. "=")
						end
					end
				end
			end
		end
		table.insert(args, "--json")
		return args, nil, nil
	elseif step == "notifications" then
		local provider = m.notify
		if m[provider] == "webhook" then
			if not string.match(v("url"), "^https://") then
				return nil, nil, "Enter the webhook URL. It starts with https://."
			end
			return { "setup", "notifications", provider, "--webhook", "--json" }, v("url") .. "\n", nil
		end
		if v("channel") == "" or v("token") == "" then
			return nil, nil, "Enter the channel and the bot token."
		end
		local flag = if provider == "discord" then "--channel-id" else "--channel"
		return { "setup", "notifications", provider, flag, v("channel"), "--json" }, v("token") .. "\n", nil
	elseif step == "assistant" then
		local slack, discord, repos = words(v("slack_channels")), words(v("discord_channels")), words(v("repos"))
		if #slack == 0 and #discord == 0 then
			return nil, nil, "Enter a Slack or a Discord channel ID."
		end
		for _, r in repos do
			if not string.match(r, "^[%w%.%-_]+/[%w%.%-_]+$") then
				return nil, nil, "Enter each repository as owner/name."
			end
		end
		local token = v("app_token")
		local d = assistant_detail(state)
		local stored = type(d.slack) == "table" and (d.slack.configured == true or d.slack.connected == true)
		if #slack > 0 and token == "" and not stored then
			return nil, nil, "Enter the Slack app token (xapp-…). Slack needs it for Socket Mode."
		end
		local args = { "setup", "assistant" }
		for _, c in slack do
			table.insert(args, "--slack-channel")
			table.insert(args, c)
		end
		for _, c in discord do
			table.insert(args, "--discord-channel")
			table.insert(args, c)
		end
		for _, r in repos do
			table.insert(args, "--repo")
			table.insert(args, r)
		end
		-- The app token is the first stdin line; an empty line keeps the stored one. A transport
		-- without channels is turned off, so both lists always go.
		local stdin = if #slack > 0 then token .. "\n" else nil
		table.insert(args, "--json")
		return args, stdin, nil
	elseif step == "skills" then
		local repo = trim(v("skill_repo"))
		if repo == "" or not string.match(repo, "^[%w%.%-_]+/[%w%.%-_]+$") then
			return nil, nil, "Enter the source repository as owner/name."
		end
		local args = { "setup", "skills", "--repo", repo }
		local path = trim(v("skill_path"))
		if path ~= "" then
			table.insert(args, "--path")
			table.insert(args, path)
		end
		local ref = trim(v("skill_ref"))
		if ref ~= "" then
			table.insert(args, "--ref")
			table.insert(args, ref)
		end
		for r in string.gmatch(trim(v("skill_roles")), "[^,%s]+") do
			table.insert(args, "--role")
			table.insert(args, r)
		end
		for r in string.gmatch(trim(v("skill_for_repos")), "%S+") do
			if not string.match(r, "^[%w%.%-_]+/[%w%.%-_]+$") then
				return nil, nil, "Enter each target repository as owner/name."
			end
			table.insert(args, "--for-repo")
			table.insert(args, r)
		end
		table.insert(args, "--json")
		return args, nil, nil
	end
	return nil, nil, nil
end

local function run(state: any, cx: any, key: string, args: { string }, stdin: string?, done: (data: any) -> ())
	if state.busy then
		return
	end
	state.busy = key
	state.errors[key] = nil
	animate(state, cx)
	local ok, err = cli.run(args, { stdin = stdin, timeout_ms = 60000 }, function(data, e)
		state.busy = nil
		if state.stopped then
			return
		end
		-- `setup notifications test` prints every channel's result and exits 1 when one failed:
		-- those results are the answer, shown per channel, not a generic failure.
		local partial = type(data) == "table" and type(data.results) == "table"
		if e and not partial then
			state.errors[key] = cli.describe(e)
			cx:render()
			focus(state, cx)
			return
		end
		done(data)
		load(state, cx)
	end)
	if not ok then
		state.busy = nil
		state.errors[key] = cli.describe(err)
	end
	cx:render()
end

local function save(state: any, cx: any)
	local step = state.sel
	local args, stdin, problem = command(state)
	if problem then
		state.errors[step] = problem
		cx:render()
		return
	end
	if not args then
		return
	end
	state.results[step] = nil
	run(state, cx, step, args, stdin, function(data)
		state.results[step] = type(data) == "table" and data or {}
		-- Secrets leave memory once saved; the API never returns them.
		for _, f in fields(state, step) do
			if f.secret then
				bucket(state, step)[f.id] = nil
			end
		end
	end)
end

local function test_notifications(state: any, cx: any)
	state.results.test = nil
	run(state, cx, "test", { "setup", "notifications", "test", "--json" }, nil, function(data)
		state.results.test = type(data) == "table" and data or {}
	end)
end

-- Webhook secrets exist already: a new call must rotate them (409 `webhooks_exist` otherwise).
local function webhooks_exist(state: any): boolean
	local s = find(state, "webhooks")
	local d = s and type(s.detail) == "table" and s.detail or {}
	return (type(d.linear) == "table" and d.linear.secret == true) or (type(d.github) == "table" and d.github.secret == true)
end

local function make_webhooks(state: any, cx: any)
	local args = { "setup", "webhooks" }
	if webhooks_exist(state) then
		table.insert(args, "--rotate")
	end
	table.insert(args, "--json")
	run(state, cx, "webhooks", args, nil, function(data)
		-- Shown once, like a new API token: kept only in memory until dismissed.
		state.webhooks = type(data) == "table" and data or {}
	end)
end

-- ── rendering ──────────────────────────────────────────────────────────────

local function names(list: any): string
	local out = {}
	for _, x in (type(list) == "table" and list) or {} do
		table.insert(out, if type(x) == "table" then tostring(x.name or x.full_name or x.id or "?") else tostring(x))
	end
	return table.concat(out, ", ")
end

local PROVIDER_WORD = { slack = "Slack", discord = "Discord", linear = "Linear", github = "GitHub" }
local TEST_ERROR = { rejected = "the service rejected the message", unreachable = "the service did not answer" }

-- Step status as the guide maps tones: done success, to do pending, failed error, skipped muted.
local STATUS = {
	done = { "done", "success", "check" }, todo = { "to do", "pending", "clock" },
	error = { "needs attention", "error", "alert" }, skipped = { "skipped", "muted", "minus" },
}

local function status_of(s: any): { string }
	if not s.required and s.status == "todo" then
		return { "to do", "muted", "clock" }
	end
	return STATUS[s.status or ""] or { tostring(s.status or "?"), "muted", "clock" }
end

-- `last_test` of a channel: `{ok, error, at}` or nil.
local function last_test(t: any): Spans
	if type(t) ~= "table" then
		return { ui.span("no test message yet", "muted") }
	elseif t.ok then
		return { ui.span("✓ test message sent", "success"), ui.span(" " .. fmt.ago(t.at), "muted") }
	end
	-- `error` is a code from the API's status, or `{code, message}` from the test command.
	local why = if type(t.error) == "table" then (t.error.message or t.error.code) else (TEST_ERROR[t.error or ""] or t.error)
	return { ui.span("test failed: " .. tostring(why or "no reason given"), "error"), ui.span(" " .. fmt.ago(t.at), "muted") }
end

-- The assistant's transports and process, from the step's detail or from what the save returned.
local INTENT = { full = "message content on", limited = "message content on (limited)" }

local function assistant_rows(d: any, rows: { { Spans } }, status: boolean)
	for _, p in { "slack", "discord" } do
		local t = d[p]
		if type(t) == "table" and (t.configured or t.connected) then
			local spans: Spans = { if t.connected then ui.span("connected", "success") else ui.span("not connected", "error") }
			if type(t.channels) == "table" and #t.channels > 0 then
				table.insert(spans, ui.span("  " .. table.concat(t.channels, ", "), "mono"))
			else
				table.insert(spans, ui.span("  direct messages only", "muted"))
			end
			table.insert(rows, { PROVIDER_WORD[p], spans })
			if p == "discord" then
				table.insert(rows, { "Intent", if INTENT[t.intent or ""] then { ui.span(INTENT[t.intent], "success") }
					else { ui.span("Message Content intent is off. Turn it on in the Developer Portal › Bot › Privileged Gateway Intents.", "warning") } })
			end
		end
	end
	if type(d.repositories) == "table" and #d.repositories > 0 then
		table.insert(rows, { "Reads", { ui.span(fmt.cut(table.concat(d.repositories, ", "), 90), "mono") } })
	end
	if status then
		if #rows == 0 then
			table.insert(rows, { "Transports", { ui.span("none yet", "muted") } })
		end
		table.insert(rows, { "Process", if d.running
			then { ui.span("running", "success"), ui.span("  heartbeat " .. fmt.ago(d.heartbeat_at) .. " · " .. fmt.plural(tonumber(d.threads) or 0, "active thread", "active threads"), "muted") }
			else { ui.span("not running", "muted") } })
	end
end

-- What `GET /v1/setup` says about a step right now (`detail` is an object per step).
local function detail_rows(s: any): { { Spans } }
	local d = type(s.detail) == "table" and s.detail or {}
	local rows: { { Spans } } = {}
	if s.id == "tracker" then
		if d.kind == "linear" then
			table.insert(rows, { "Board", { ui.span("Linear  "), ui.span(if d.team_key then "team " .. d.team_key else "project " .. tostring(d.project_slug or "?"), "mono") } })
		elseif d.kind == "github" then
			table.insert(rows, { "Board", { ui.span("GitHub Projects  "), ui.span(tostring(d.owner) .. " #" .. tostring(d.project_number) .. " · " .. tostring(d.status_field or "Status"), "mono") } })
		else
			table.insert(rows, { "Board", { ui.span("not chosen yet", "muted") } })
		end
		if d.kind then
			table.insert(rows, { "Credential", if d.credential then { ui.span("works", "success") } else { ui.span("missing or rejected", "error") } })
		end
		if type(d.states) == "table" and #d.states > 0 then
			table.insert(rows, { "Board states", { ui.span(fmt.cut(names(d.states), 90), "mono") } })
		end
	elseif s.id == "github_app" then
		if d.configured then
			table.insert(rows, { "Repositories", { ui.span(tostring(#(d.repositories or {})) .. "  ", "mono"), ui.span(fmt.cut(names(d.repositories), 80), "mono") } })
		else
			table.insert(rows, { "App", { ui.span("not configured yet", "muted") } })
		end
		if type(d.missing_permissions) == "table" and #d.missing_permissions > 0 then
			table.insert(rows, { "Missing permissions", { ui.span(names(d.missing_permissions), "error") } })
		end
	elseif s.id == "repositories" then
		table.insert(rows, { "Default", if d.default_repo then { ui.span(d.default_repo, "mono") } else { ui.span("none: every ticket needs a repo: label", "muted") } })
		if type(d.base_branches) == "table" then
			for repo, branch in d.base_branches do
				table.insert(rows, { "Base branch", { ui.span(tostring(repo) .. " → " .. tostring(branch), "mono") } })
			end
		end
		if type(d.mergers) == "table" and #d.mergers > 0 then
			table.insert(rows, { "Mergers running", { ui.span(names(d.mergers), "mono") } })
		end
		if type(d.without_merger) == "table" and #d.without_merger > 0 then
			table.insert(rows, { "No merger yet", { ui.span(names(d.without_merger), "warning") } })
		end
	elseif s.id == "models" then
		local ok, missing = 0, {}
		for _, r in (type(d.roles) == "table" and d.roles) or {} do
			if r.credential then
				ok += 1
			else
				table.insert(missing, tostring(r.role) .. " (" .. tostring(r.provider) .. ")")
			end
		end
		table.insert(rows, { "Roles", { ui.span(ok .. " of " .. #(d.roles or {}), "mono"), ui.span(" have a model with a credential", "muted") } })
		if #missing > 0 then
			table.insert(rows, { "No credential", { ui.span(table.concat(missing, ", "), "error") } })
		end
	elseif s.id == "notifications" then
		for _, p in { "slack", "discord" } do
			local c = d[p]
			if type(c) == "table" and c.configured then
				table.insert(rows, { PROVIDER_WORD[p], last_test(c.last_test) })
			end
		end
		if #rows == 0 then
			table.insert(rows, { "Channels", { ui.span("none yet", "muted") } })
		end
	elseif s.id == "assistant" then
		assistant_rows(d, rows, true)
	elseif s.id == "webhooks" then
		for _, p in { "linear", "github" } do
			local w = d[p]
			if type(w) == "table" and w.in_use ~= false then
				local spans: Spans = if not w.secret then { ui.span("no secret yet", "muted") }
					elseif w.last_delivery_at then { ui.span("last signed delivery ", "success"), ui.span(fmt.ago(w.last_delivery_at), "mono") }
					else { ui.span("secret set · no delivery yet", "pending") }
				table.insert(rows, { PROVIDER_WORD[p], spans })
			end
		end
	elseif s.id == "skills" then
		local sources = if type(d.sources) == "table" then d.sources else {}
		if #sources == 0 then
			table.insert(rows, { "Sources", { ui.span("none yet", "muted") } })
		else
			for _, src in sources do
				local scope = if type(src.repos) == "table" and #src.repos > 0 then "for " .. table.concat(src.repos, ", ") else "global"
				table.insert(rows, { tostring(src.repo or "?"), { ui.span(tostring(src.path or "skills") .. "  ", "mono"), ui.span(scope, "muted") } })
			end
		end
	end
	return rows
end

-- What the API found when it verified and saved the step (inside the step's card: no card).
local function result_nodes(state: any, step: string): { Node }
	local r = state.results[step]
	if type(r) ~= "table" then
		return {}
	end
	local rows: { { Spans } } = {}
	if type(r.repositories) == "table" and step ~= "assistant" then
		table.insert(rows, { "Repositories", { ui.span(#r.repositories .. " found  ", "success"), ui.span(fmt.cut(names(r.repositories), 80), "mono") } })
	end
	local missing = r.missing_permissions
	if type(missing) == "table" and #missing > 0 then
		table.insert(rows, { "Missing permissions", { ui.span(names(missing), "error") } })
	end
	if type(r.states) == "table" then
		table.insert(rows, { "Board states", { ui.span(fmt.cut(names(r.states), 90), "mono") } })
	end
	for _, w in (type(r.warnings) == "table" and r.warnings) or {} do
		local text = if type(w) == "table" and w.code == "state_missing"
			then "The WORKFLOW names the state " .. tostring(w.state) .. ", which is not on the board. Add it to the board, or remove it from tracker.states in the WORKFLOW."
			else tostring(if type(w) == "table" then (w.message or w.code) else w)
		table.insert(rows, { "Warning", { ui.span(text, "warning") } })
	end
	if step == "repositories" then
		table.insert(rows, { "Default", { ui.span(if type(r.default_repo) == "string" then r.default_repo else "cleared", "mono") } })
		if type(r.without_merger) == "table" and #r.without_merger > 0 then
			table.insert(rows, { "No merger yet", { ui.span(names(r.without_merger), "warning") } })
		end
	end
	if step == "notifications" then
		for _, p in { "slack", "discord" } do
			if type(r[p]) == "table" and r[p].configured then
				table.insert(rows, { PROVIDER_WORD[p], last_test(r[p].last_test) })
			end
		end
	end
	if step == "assistant" then
		assistant_rows(r, rows, false)
	end
	if step == "skills" then
		local sources = type(r.sources) == "table" and r.sources or {}
		table.insert(rows, { "Sources", { ui.span(tostring(#sources), "mono") } })
	end
	if type(r.restarted) == "table" and #r.restarted > 0 then
		table.insert(rows, { "Restarted", { ui.span(names(r.restarted), "mono") } })
	end
	local bad = type(missing) == "table" and #missing > 0
	local out: { Node } = {
		ui.text(if bad then { ui.span("Saved, with missing permissions", "strong warning") } else { ui.span("✓ Verified and saved", "strong success") }),
	}
	if #rows > 0 then
		table.insert(out, h.with(ui.kv(rows), { key = "found" }))
	end
	return out
end

local function test_nodes(state: any): { Node }
	local r = state.results.test
	if type(r) ~= "table" then
		return {}
	end
	local rows: { { Spans } } = {}
	local results = type(r.results) == "table" and r.results or {}
	for _, p in { "slack", "discord" } do
		-- The CLI lists `[{channel, ok, error, at}]`; the API keys them by channel.
		local c = results[p]
		for _, x in results do
			if type(x) == "table" and x.channel == p then
				c = x
			end
		end
		if type(c) == "table" then
			table.insert(rows, { PROVIDER_WORD[p], last_test(c) })
		end
	end
	return { h.with(ui.kv(if #rows > 0 then rows else { { "Test", { ui.span("no channel is configured yet", "muted") } } }), { key = "tested" }) }
end

local function webhooks_nodes(state: any): { Node }
	local w = state.webhooks
	if type(w) ~= "table" then
		return {}
	end
	local out: { Node } = { ui.text({ ui.span("Copy these now. The secrets are not shown again.", "warning") }) }
	local rows: { { Spans } } = {}
	for _, src in { "linear", "github" } do
		local x = w[src]
		if type(x) == "table" then
			table.insert(rows, { PROVIDER_WORD[src] .. " URL", { ui.span(tostring(x.url or "—"), "mono") } })
			table.insert(rows, { PROVIDER_WORD[src] .. " secret", { ui.span(tostring(x.secret or "—"), "mono") } })
			if type(x.events) == "table" then
				table.insert(rows, { "Events", { ui.span(names(x.events), "mono") } })
			end
		end
	end
	table.insert(out, h.with(ui.kv(rows), { key = "secrets" }))
	table.insert(out, h.row("copy", {
		h.action_key("Copy GitHub", "copy=github:secret", "⌃G"),
		h.action_key("Copy Linear", "copy=linear:secret", "⌃L"),
		h.action_key("Done", "webhooks_dismiss", "esc", "muted"),
	}, { wrap = true }))
	return out
end

local function field_node(state: any, f: Field): Node
	local raw = value(state, state.sel, f.id)
	local text = raw
	-- 0.8.3 draws an `input`'s text as is (no `secret`): mask it here.
	if f.secret and raw ~= "" then
		local lines = select(2, string.gsub(raw, "\n", "\n")) + 1
		text = string.rep("•", math.min(utf8.len(raw) or #raw, 24)) .. (if lines > 1 then "  " .. lines .. " lines" else "")
	end
	local focused = state.focus == f.id
	return h.col(f.id, {
		ui.text({ ui.span(f.label, "strong"), ui.span(if f.optional and f.secret then "  optional, hidden, write-only" elseif f.optional then "  optional" elseif f.secret then "  hidden, write-only" else "", "muted") }),
		ui.node("input", {
			key = "input",
			text = text,
			cursor = if focused then (utf8.len(text) or #text) else nil,
			placeholder = f.placeholder,
			mono = true,
			actions = { click = "focus=" .. f.id },
		}),
		h.muted(f.help),
	}, { gap = "xs" })
end

-- A step's mode as a segmented `tabs` (the guide's control for a view's mode).
local function modes(state: any): { Node }
	local m = state.modes
	local function seg(key: string, active: string, items: { any }): Node
		return ui.node("tabs", { key = key, variant = "segmented", active = active, items = items })
	end
	if state.sel == "tracker" then
		local out = { seg("mode_tracker", m.tracker, { { id = "linear", label = "Linear" }, { id = "github", label = "GitHub Projects" } }) }
		if m.tracker == "linear" then
			table.insert(out, seg("mode_scope", m.scope, { { id = "team", label = "By team" }, { id = "project", label = "By project" } }))
		end
		return out
	elseif state.sel == "notifications" then
		return {
			seg("mode_notify", m.notify, { { id = "slack", label = "Slack" }, { id = "discord", label = "Discord" } }),
			seg("mode_" .. m.notify, m[m.notify], { { id = "bot", label = "Bot token" }, { id = "webhook", label = "Webhook" } }),
		}
	end
	return {}
end

-- The step's one primary action (accent unless the finish screen holds the primary).
local function primary_chip(state: any, s: any, primary: boolean): Node?
	local tone = if primary then "accent" else "neutral"
	if #fields(state, s.id) > 0 then
		local label = if state.busy == s.id then "Verifying…" else "Verify and save"
		return h.action_key(label, "save", "⏎", tone)
	elseif s.id == "models" then
		return h.action_key("Open Admin", "open_admin", "⏎", tone)
	elseif s.id == "webhooks" then
		local label = if state.busy == "webhooks" then "Creating…" elseif webhooks_exist(state) then "Rotate the secrets" else "Create webhook secrets"
		return h.action_key(label, "webhooks", "⏎", tone)
	end
	return nil
end

local function detail_card(state: any): Node
	local s = find(state, state.sel)
	if not s then
		return h.with(ui.card({ ui.span("No step selected", "strong") }, { h.muted("Select a step.") }), { key = "detail", min = { w = "44ch" }, max = { w = "56ch" } })
	end
	local st = status_of(s)
	local body: { Node } = {}
	local acts: { Node } = {}
	local chip = primary_chip(state, s, not complete(state))
	if chip then
		table.insert(acts, chip)
	end
	if s.id == "notifications" then
		table.insert(acts, h.action_key(if state.busy == "test" then "Sending…" else "Send a test", "test", "⌃T"))
	elseif s.id == "repositories" then
		table.insert(acts, h.action_key("Clear the default", "clear_repo", "⌃D", "muted"))
	elseif s.id == "skills" then
		local d = s.detail
		for _, src in (type(d) == "table" and type(d.sources) == "table" and d.sources) or {} do
			local repo = tostring(src.repo or "?")
			table.insert(acts, h.action("Remove " .. repo, "remove_skill=" .. repo, "muted"))
		end
	end
	if #acts > 0 then
		-- 0.8.3 ignores a card's `end`: the actions open the body, at its right.
		table.insert(body, ui.node("row", { key = "acts", gap = "sm", justify = "end", align = "center" }, acts))
	end
	table.insert(body, h.muted(WHY[s.id] or ""))
	local rows = detail_rows(s)
	if #rows > 0 then
		table.insert(body, h.with(ui.kv(rows), { key = "facts" }))
	end
	if type(s.error) == "table" and s.error.message then
		table.insert(body, ui.text({ ui.span(tostring(s.error.message), "error") }))
	end
	for _, n in modes(state) do
		table.insert(body, n)
	end
	local list = fields(state, s.id)
	if #list > 0 then
		local form: { Node } = {}
		for _, f in list do
			table.insert(form, field_node(state, f))
		end
		table.insert(body, h.col("form", form, { gap = "md" }))
	end
	if state.busy then
		table.insert(body, ui.node("spinner", { key = "busy", text = VERIFYING[state.busy] or "Working…" }))
	end
	local err = state.errors[s.id] or (s.id == "notifications" and state.errors.test) or (s.id == "webhooks" and state.errors.webhooks)
	if err then
		table.insert(body, ui.text({ ui.span("Not saved", "strong error") }))
		table.insert(body, h.with(ui.code(err), { key = "why" }))
	end
	for _, n in result_nodes(state, s.id) do
		table.insert(body, n)
	end
	if s.id == "notifications" then
		for _, n in test_nodes(state) do
			table.insert(body, n)
		end
	elseif s.id == "webhooks" then
		for _, n in webhooks_nodes(state) do
			table.insert(body, n)
		end
	end
	local idx = table.find(steps(state), s) or 0
	local card = ui.card({
		ui.span(idx .. "  ", "muted mono"), ui.span(tostring(s.title), "strong"), ui.span("  " .. st[1], st[2]),
	}, body)
	return h.with(card, { key = "detail", min = { w = "44ch" }, max = { w = "56ch" }, tone = if err or s.status == "error" then "error" else nil })
end

local function step_list(state: any): Node
	local rows: { Item } = {}
	local list_steps = steps(state)
	for i, s in list_steps do
		local st = status_of(s)
		local at = state.celebrate[s.id]
		local mark = nil
		if at and tern.now() - at < CHECK_MS then
			mark = CHECK[math.clamp(math.floor((tern.now() - at) / FRAME_MS) + 1, 1, #CHECK)]
		end
		rows[i] = {
			label = { ui.span(tostring(s.title)) },
			detail = if s.required then nil else { ui.span("  optional", "muted") },
			value = { ui.span(if mark then mark .. " " .. st[1] else st[1], st[2]) },
			icon = st[3],
			tone = st[2],
		}
	end
	local list = ui.list(rows)
	local sel_node = nil
	for i, child in (list.c or {}) do
		local id = list_steps[i].id
		h.with(child, { key = "s_" .. id, actions = { click = "step=" .. id } })
		if id == state.sel then
			sel_node = "main.steps.list.items.s_" .. id
		end
	end
	list.p = { key = "items", selected = sel_node }
	return ui.node("col", { key = "list", grow = 1, basis = 0, gap = "sm" }, {
		h.muted("Required steps first. Each one is verified against the real service before it is saved."),
		list,
	})
end

local function ready_card(state: any): Node
	local optional = {}
	for _, s in steps(state) do
		if not s.required and s.status ~= "done" then
			table.insert(optional, tostring(s.title))
		end
	end
	local rows: { { Spans } } = {
		{ "1", "Create your first ticket: put it in an active state on the board. An agent picks it up within a minute." },
		{ "2", "Watch it on the Dashboard and answer when it asks." },
		{ "3", { ui.span("Invite your team: "), ui.span("symphony token create <name> --role operator", "mono") } },
	}
	if #optional > 0 then
		table.insert(rows, { "Later", "Optional steps left: " .. table.concat(optional, ", ") .. "." })
	end
	return h.with(ui.card({ ui.span("✓ Symphony is ready", "strong success") }, {
		ui.node("row", { key = "acts", gap = "sm", justify = "end", align = "center" }, {
			h.action_key("Open the dashboard", "open_dashboard", "⌃O", "accent"),
		}),
		h.muted("Every required step is done and verified."),
		h.with(ui.kv(rows), { key = "next" }),
	}), { key = "ready", tone = "success" })
end

local function head(state: any): Node
	local mode = state.doc and state.doc.mode
	local meta = if mode == "local" then "local stack" elseif mode == "kubernetes" then "Kubernetes" else "first run"
	-- The progress bar under the head carries the count; the badge says the state.
	local badge = if not state.doc then ui.badge("Checking…", "accent")
		elseif complete(state) then ui.badge("Ready", "success")
		else ui.badge("Not set up", "pending")
	return ui.node("row", { key = "head", justify = "between", align = "center" }, {
		ui.text({ ui.span("Setup", "title strong"), ui.span("  Symphony · " .. meta, "muted") }),
		h.row("end", { badge, h.action_key("Refresh", "refresh", "⌃R") }),
	})
end

local function progress(state: any): Node?
	local done, total = 0, 0
	for _, s in steps(state) do
		if s.required then
			total += 1
			if s.status == "done" then
				done += 1
			end
		end
	end
	if total == 0 then
		return nil
	end
	return h.with(ui.progress(done / total, { ui.span(done .. " of " .. total .. " required steps", "mono") }), {
		key = "progress", tone = if done == total then "success" else "accent",
	})
end

local function legend(state: any): { { string } }
	local out = { { "↑↓", "step" } }
	if #fields(state, state.sel) > 0 then
		table.insert(out, { "tab", "field" })
		table.insert(out, { "⌘V", "paste" })
		table.insert(out, { "⏎", "verify and save" })
	elseif state.sel == "webhooks" then
		table.insert(out, { "⏎", if webhooks_exist(state) then "rotate" else "create secrets" })
	elseif state.sel == "models" then
		table.insert(out, { "⏎", "open Admin" })
	end
	if state.sel == "notifications" then
		table.insert(out, { "⌃T", "send a test" })
	end
	if complete(state) then
		table.insert(out, { "⌃O", "dashboard" })
	end
	table.insert(out, { "⌃R", "refresh" })
	table.insert(out, { "esc", "back" })
	return out
end

-- ── input ──────────────────────────────────────────────────────────────────

local function text_key(state: any, k: Key): boolean
	local id = state.focus
	if not id then
		return false
	end
	local f = nil
	for _, x in fields(state, state.sel) do
		if x.id == id then
			f = x
		end
	end
	if not f then
		return false
	end
	local b = bucket(state, state.sel)
	local cur = b[id] or ""
	if k.name == "paste" then
		local t = k.text or ""
		if not f.multiline then
			t = string.gsub(t, "[\r\n]+", "")
		end
		b[id] = cur .. t
	elseif k.name == "backspace" then
		if k.alt then
			b[id] = ""
		else
			local n = utf8.len(cur) or 0
			b[id] = if n > 0 then string.sub(cur, 1, (utf8.offset(cur, n) or 1) - 1) else ""
		end
	elseif k.text and k.text ~= "" and not k.ctrl and not k.meta then
		b[id] = cur .. k.text
	else
		return false
	end
	state.errors[state.sel] = nil
	return true
end

local function select_step(state: any, cx: any, id: string)
	if find(state, id) then
		state.sel = id
		first_field(state, cx)
	end
end

local function move_step(state: any, cx: any, by: number)
	local list = steps(state)
	local at = 1
	for i, s in list do
		if s.id == state.sel then
			at = i
		end
	end
	local nxt = list[math.clamp(at + by, 1, math.max(#list, 1))]
	if nxt then
		select_step(state, cx, nxt.id)
	end
end

local function open_link(cx: any, req: { [string]: any })
	req.pane = cx.pane
	cx:open(fmt.ui_link(req))
end

local function primary(state: any, cx: any)
	local s = state.sel
	if #fields(state, s) > 0 then
		save(state, cx)
	elseif s == "webhooks" then
		make_webhooks(state, cx)
	elseif s == "models" then
		open_link(cx, { kind = "open", view = "admin" })
	elseif complete(state) then
		open_link(cx, { kind = "open", view = "dashboard" })
	end
end

-- Escape backs out one layer: the one-time secrets, then an error or a result.
local function back(state: any): boolean
	if state.webhooks then
		state.webhooks = nil
		return true
	end
	local s = state.sel
	if s and (state.errors[s] or state.results[s] or state.results.test or state.errors.test) then
		state.errors[s], state.results[s] = nil, nil
		state.errors.test, state.results.test = nil, nil
		return true
	end
	return false
end

local function on_action(state: any, act: string, v: string?, cx: any)
	if act == "step" and v then
		select_step(state, cx, v)
	elseif act == "focus" and v then
		state.focus = v
		focus(state, cx)
	elseif act == "save" then
		save(state, cx)
	elseif act == "clear_repo" then
		run(state, cx, "repositories", { "setup", "repositories", "--clear-default-repo", "--json" }, nil, function(data)
			state.results.repositories = type(data) == "table" and data or {}
		end)
	elseif act == "test" then
		test_notifications(state, cx)
	elseif act == "webhooks" then
		make_webhooks(state, cx)
	elseif act == "webhooks_dismiss" then
		state.webhooks = nil
	elseif act == "copy" and v and type(state.webhooks) == "table" then
		local src, what = string.match(v, "^(%w+):(%w+)$")
		local x = src and state.webhooks[src]
		if type(x) == "table" and type(x[what]) == "string" then
			cx:copy(x[what])
			toast(cx, "success", PROVIDER_WORD[src] .. " secret copied")
		end
	elseif act == "open_admin" then
		open_link(cx, { kind = "open", view = "admin" })
	elseif act == "open_dashboard" then
		open_link(cx, { kind = "open", view = "dashboard" })
	elseif act == "refresh" then
		load(state, cx)
	elseif act == "remove_skill" and v then
		run(state, cx, "skills", { "setup", "skills", "--repo", v, "--remove", "--json" }, nil, function(data)
			state.results.skills = type(data) == "table" and data or {}
		end)
	end
	cx:render()
end

local function ctrl(k: Key, letter: string): boolean
	return k.ctrl == true and (k.text == letter or k.name == letter)
end

M.def = {
	font = "sans",
	init = function(cx: any, _args: { string }, _saved: any)
		local state = {
			doc = nil,
			load_error = nil,
			sel = nil,
			focus = nil,
			values = {},
			modes = { tracker = "linear", scope = "team", notify = "slack", slack = "bot", discord = "bot" },
			busy = nil,
			ticking = false,
			results = {},
			errors = {},
			celebrate = {},
			prev = {},
			webhooks = nil,
			ready_told = false,
			read_at = 0,
			stopped = false,
		}
		load(state, cx)
		local function tick()
			if state.stopped then
				return
			end
			if not complete(state) and not state.busy and tern.now() - state.read_at >= STATUS_MS then
				load(state, cx)
			end
			tern.timer(STATUS_MS, tick)
		end
		tern.timer(STATUS_MS, tick)
		return state
	end,
	title = function(state: any)
		return if complete(state) then "Symphony · Ready" else "Symphony · Setup"
	end,
	view = function(state: any, _cx: any)
		local parts: { Node } = { head(state) }
		local bar = progress(state)
		if bar then
			table.insert(parts, bar)
		end
		if not state.doc then
			if state.load_error then
				table.insert(parts, h.with(ui.card({ ui.span("Could not read the setup status", "strong error") }, {
					ui.node("row", { key = "acts", gap = "sm", justify = "end" }, { h.action_key("Try again", "refresh", "⌃R") }),
					h.with(ui.code(state.load_error), { key = "why" }),
				}), { key = "steps", tone = "error" }))
			else
				table.insert(parts, h.loading("steps", "Reading what is set up…"))
			end
		elseif #steps(state) == 0 then
			table.insert(parts, h.with(ui.card({ ui.span("Nothing to set up", "strong") }, {
				h.muted("This Symphony reports no setup steps. Refresh after upgrading the API."),
			}), { key = "steps" }))
		else
			if complete(state) then
				table.insert(parts, ready_card(state))
			end
			if state.load_error then
				table.insert(parts, ui.text({ ui.span("Could not refresh: " .. state.load_error, "error") }))
			end
			table.insert(parts, ui.node("row", { key = "steps", gap = "lg", align = "stretch" }, {
				step_list(state),
				detail_card(state),
			}))
		end
		return {
			main = ui.node("col", { gap = "lg" }, parts),
			dock = ui.col({ h.legend(legend(state)) }),
		}
	end,
	key = function(state: any, k: Key, cx: any): boolean?
		if k.name == "up" then
			move_step(state, cx, -1)
			return true
		elseif k.name == "down" then
			move_step(state, cx, 1)
			return true
		elseif k.name == "tab" then
			local list = fields(state, state.sel)
			if #list > 0 then
				local at = 1
				for i, f in list do
					if f.id == state.focus then
						at = i
					end
				end
				state.focus = list[((at - 1 + (if k.shift then -1 else 1)) % #list) + 1].id
				focus(state, cx)
			end
			return true
		elseif k.name == "escape" then
			return back(state)
		elseif k.name == "enter" then
			if not state.busy then
				primary(state, cx)
			end
			return true
		elseif ctrl(k, "r") then
			load(state, cx)
			return true
		elseif ctrl(k, "t") and state.sel == "notifications" then
			test_notifications(state, cx)
			return true
		elseif ctrl(k, "d") and state.sel == "repositories" then
			on_action(state, "clear_repo", nil, cx)
			return true
		elseif ctrl(k, "o") and complete(state) then
			open_link(cx, { kind = "open", view = "dashboard" })
			return true
		elseif (ctrl(k, "g") or ctrl(k, "l")) and state.webhooks then
			on_action(state, "copy", (if ctrl(k, "g") then "github" else "linear") .. ":secret", cx)
			return true
		end
		return text_key(state, k)
	end,
	event = function(state: any, ev: UiEvent, cx: any)
		if ev.ev == "focus" and type(ev.id) == "string" then
			local f = string.match(ev.id, "%.form%.([%w_]+)%.input$")
			if f then
				state.focus = f
				focus(state, cx)
			end
		elseif ev.ev == "select" and type(ev.id) == "string" and type(ev.item) == "string" then
			-- A segmented mode strip: `mode_<group>`.
			local group = string.match(ev.id, "%.mode_([%w_]+)$")
			if group then
				state.modes[group] = ev.item
				first_field(state, cx)
			end
		elseif ev.ev == "action" and type(ev.act) == "string" then
			local v = ev.value
			on_action(state, ev.act, if type(v) == "string" then v elseif v ~= nil then tostring(v) else nil, cx)
		end
	end,
	save = function(_state: any)
		-- Nothing typed is kept: secrets must not reach `save()`, and the rest is cheap to retype.
		return {}
	end,
}

return M
