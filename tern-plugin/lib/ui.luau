--!strict
-- View helpers over tern.ui for the kinds Tern 0.8.3 draws. 0.8.3 has no `button`, `stats`,
-- `empty`, `select`, `skeleton` or `scroll` kinds and its tables take only span cells and have no
-- row selection, so actions are clickable badges, pickable rows are `list`s and dialogs are
-- `overlay`s in the layer region.

local ui = tern.ui

local M = {}

function M.with(n: Node, props: { [string]: any }): Node
	local p: { [string]: any } = n.p or {}
	for k, v in props do
		p[k] = v
	end
	n.p = p
	return n
end

-- An action chip: a badge whose click sends `act` (`name=value`).
function M.action(text: string, act: string, tone: string?, key: string?): Node
	return M.with(ui.badge(text, tone or "neutral"), { actions = { click = act }, key = key or act, title = text })
end

-- An action chip followed by its keycap, as a button with `keys` would show it.
function M.action_key(text: string, act: string, key_label: string, tone: string?): Node
	return ui.node("row", { key = act, gap = "xs", align = "center" }, {
		M.action(text, act, tone, "b"),
		ui.text({ ui.span(key_label, "key") }),
	})
end

function M.row(key: string, children: { Node }, props: { [string]: any }?): Node
	local p: { [string]: any } = { key = key, gap = "sm", align = "center" }
	for k, v in props or {} do
		p[k] = v
	end
	return ui.node("row", p, children)
end

function M.col(key: string, children: { Node }, props: { [string]: any }?): Node
	local p: { [string]: any } = { key = key }
	for k, v in props or {} do
		p[k] = v
	end
	return ui.node("col", p, children)
end

function M.muted(text: string): Node
	return ui.text({ ui.span(text, "muted") })
end

function M.card(key: string, head: Spans, children: { Node }, tone: string?): Node
	local props: { [string]: any } = { key = key }
	if tone then
		props.tone = tone
	end
	return M.with(ui.card(head, children), props)
end

-- The error state: what failed in the head, the tool's own message, a retry.
function M.error_card(key: string, title: string, message: string, retry: string?): Node
	local body: { Node } = { ui.text({ ui.span(message) }) }
	if retry then
		table.insert(body, M.row("acts", { M.action_key("Try again", retry, "r") }))
	end
	return M.card(key, { ui.span(title, "strong error") }, body, "error")
end

-- The empty state: what is missing, why, and the one action that fixes it.
function M.empty(key: string, title: string, line: string, action: Node?): Node
	local body: { Node } = { M.muted(line) }
	if action then
		table.insert(body, M.row("acts", { action }))
	end
	return M.card(key, { ui.span(title, "strong") }, body)
end

function M.loading(key: string, text: string): Node
	return M.with(ui.node("shimmer", { text = text }), { key = key })
end

-- A key legend for the dock: { {"tab", "views"}, … }.
function M.legend(entries: { { string } }): Node
	local spans: { any } = {}
	for _, e in entries do
		table.insert(spans, ui.span(e[1], "key"))
		table.insert(spans, ui.span(" " .. e[2] .. "   ", "muted"))
	end
	return M.with(ui.text(spans), { key = "legend" })
end

-- A one-row table used as a headline strip of numbers (0.8.3 has no `stats` kind).
function M.strip(key: string, cells: { { any } }): Node
	local cols: { Col } = {}
	local row: { Cell } = {}
	for i, c in cells do
		cols[i] = { id = "c" .. i, head = c[1], align = "start" }
		row[i] = c[2]
	end
	return M.with(ui.table(cols, { row }), { key = key })
end

-- A data table, or its `empty` line when it has no rows (0.8.3 draws a bare header row and
-- ignores the table's own `empty` prop).
function M.data_table(props: { [string]: any }): Node
	if #(props.rows or {}) == 0 then
		return M.with(M.muted(props.empty or ""), { key = props.key })
	end
	return ui.node("table", props)
end

-- A `chart` node (heatmap, bars, spark, line, area or donut). Series take the theme's chart
-- palette unless a point names a `tone`.
function M.chart(key: string, kind: string, props: { [string]: any }): Node
	local p: { [string]: any } = { key = key, kind = kind }
	for k, v in props do
		p[k] = v
	end
	return ui.node("chart", p)
end

-- A titled tile of a wrapping row of charts: its body, or the `empty` line when there is no data.
function M.chart_card(key: string, title: string, body: { Node }?, empty: string, min_w: string?): Node
	local nodes: { Node } = if body and #body > 0 then body else { M.muted(empty) }
	return M.with(M.card(key, { ui.span(title, "strong") }, nodes), { grow = 1, basis = 0, min = { w = min_w or "44ch" } })
end

-- A confirm strip for the dock (design guide): the consequence in one sentence, Cancel (esc)
-- and the action (enter) at its right. Destructive actions take the error tone, consequential
-- ones the accent tone; while it is open its action is the screen's one primary.
function M.confirm(title: string, line: string, confirm_text: string, tone: string): Node
	local strip = if tone == "error" then "error" else "accent"
	return M.card("confirm", { ui.span(title, "strong") }, {
		ui.node("row", { key = "acts", gap = "sm", justify = "end", align = "center" }, {
			M.action_key("Cancel", "confirm_cancel", "esc", "muted"),
			M.action_key(confirm_text, "confirm_ok", "⏎", strip),
		}),
		ui.text({ ui.span(line, "muted") }),
	}, strip)
end

return M
