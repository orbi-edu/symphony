// MCP servers for agents: eight scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/mcp.md, docs/security.md, src/symphony/mcp_servers.py and src/symphony/gateway_api.py;
// the proof comes from scenario 12 of docs/mocked-certification.md.

const SHOT = (name) => `../../tern/${name}-dark.png`;

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const t = (ref, d) => Engine.at(ref) + d;
// A highlight ring around a box, between two refs.
const ring = (parent, b, ref, out, color = "#5cb8ff") =>
  D.abs(parent, "div", { x: b.x - 8, y: b.y - 8, w: b.w + 16, h: b.h + 16, in: ref, out, lift: 0 },
    { style: { border: `4px solid ${color}`, borderRadius: "22px", boxShadow: `0 0 26px ${color}80` } });
// Rows that appear one by one inside a card: [[html, ref], ...].
const rows = (el, list, style = {}) => list.forEach(([html, ref]) => {
  const r = Engine.h("p", { html, style: { fontSize: "28px", lineHeight: "1.5", ...style } });
  el.append(r);
  Engine.anim(r, { in: ref, lift: 6 });
});

const SCENES = {
  // 1. Why
  s1(sec, sv) {
    const servers = [["search", "searches the web"], ["docs", "reads a wiki"], ["errors", "an error tracker"]].map(([n, s], i) =>
      D.box(sec, { x: 100, y: 230 + i * 190, w: 400, h: 150, cls: "ext", in: `s1.2+${(i * 0.4).toFixed(1)}`, kick: "MCP server",
        title: `<span class="mono">${n}</span>`, sub: s }));
    const admin = D.box(sec, { x: 600, y: 330, w: 540, h: 360, cls: "people", in: "s1.3", kick: "Admin", title: "Servers per role",
      body: Engine.h("div", { html:
        '<p class="mono" style="font-size:26px;line-height:1.6;margin:12px 0 0"><span style="color:var(--blue)">search</span> → researcher, planner<br>' +
        '<span style="color:var(--blue)">docs</span> → implementer, reviewer<br><span style="color:var(--blue)">errors</span> → implementer</p>' }) });
    servers.forEach((s, i) => D.wire(sec, sv, s, admin, { in: `s1.3+${(0.2 + i * 0.2).toFixed(1)}`, color: "#5b6b82" }));

    const body = Engine.h("div");
    const usual = D.chips(["read", "grep", "bash", "edit", "write"].map((n) => [n, ""]));
    body.append(usual);
    const cfg = Engine.h("p", { html: 'private config <span class="chip amber">mcp.json</span>', style: { fontSize: "28px", margin: "26px 0 0", color: "var(--muted)" } });
    body.append(cfg);
    const extra = D.chips([["mcp__docs_search", "green"], ["mcp__errors_get_issue", "green"]]);
    extra.style.marginTop = "26px";
    body.append(extra);
    const agent = D.box(sec, { x: 1260, y: 230, w: 560, h: 520, cls: "ws", in: "s1.1+0.4", kick: "Agent session", title: "implementer", body });
    Engine.anim(cfg, { in: "s1.4+0.6", lift: 6 });
    Engine.anim(extra, { in: "s1.5", lift: 6 });
    const w = D.wire(sec, sv, admin, agent, { in: "s1.4", label: "at start", color: "#f5b452" });
    D.packet(sv, w, "s1.4+0.3", "s1.4+0.9", "#f5b452");
    D.callout(sec, { x: 640, y: 790, in: "s1.5+0.6" }, 'Each tool: <span class="mono">mcp__&lt;server&gt;_&lt;tool&gt;</span>, next to the usual tools');
  },

  // 2. Transports and roles
  s2(sec) {
    const T = [
      ["http", "Streamable HTTP", "a remote server · <span class=\"mono\">https://</span>", [["headers via the gateway", "green"]]],
      ["sse", "HTTP + SSE, older", "a remote server · <span class=\"mono\">https://</span>", [["headers via the gateway", "green"]]],
      ["stdio", "A local process", "next to the agent · command, args, env", [["no secret headers", "red"]]],
    ].map(([k, title, sub, chips], i) => D.box(sec, { x: 100 + i * 590, y: 220, w: 540, h: 260, cls: i < 2 ? "trusted" : "ws",
      in: `s2.1+${(0.2 + i * 0.3).toFixed(1)}`, kick: k, title, sub, body: D.chips(chips) }));
    ring(sec, { x: T[0].x, y: T[0].y, w: T[1].x + T[1].w - T[0].x, h: T[0].h }, "s2.2", "s2.3");
    ring(sec, T[2], "s2.3", "s2.4", "#f5b452");

    D.abs(sec, "div", { x: 100, y: 528, in: "s2.4" }, { text: "Roles (the team roles)", style: { fontSize: "30px", fontWeight: "700" } });
    [["implementer", "the coding agent of a ticket", "the workspace runner"],
      ["reviewer, oracle, arbiter, developer, probe", "the merger's agents", "the merger"],
      ["reflector", "the retrospective", "the reflector Job"],
      ["assistant, researcher, planner", "the chat assistant", "symphony-assistant"]].forEach(([r, who, where], i) =>
      D.abs(sec, "div", { x: 100, y: 584 + i * 74, w: 1720, h: 62, in: t("s2.4", 0.3 + i * 0.35) }, {
        class: "node", style: { padding: "0 22px" },
        html: `<div class="rowc"><b class="mono" style="width:740px;font-size:26px;color:var(--violet)">${r}</b><span style="width:470px">${who}</span><span style="color:var(--muted)">${where}</span></div>`,
      }));
    chip(sec, 1290, 526, "s2.5", "no servers → no MCP tools", "amber");
  },

  // 3. How a request flows
  s3(sec, sv) {
    const agent = D.box(sec, { x: 100, y: 280, w: 400, h: 190, cls: "ws", in: "s3.1", kick: "Agent (omp)", title: "implementer", sub: "one session, one role" });
    const gw = D.box(sec, { x: 760, y: 280, w: 400, h: 190, cls: "trusted", in: "s3.1+0.3", kick: "Sandbox gateway", title: '<span class="mono" style="font-size:30px">/v1/mcp/docs</span>', sub: "checks, then forwards" });
    const srv = D.box(sec, { x: 1420, y: 280, w: 400, h: 190, cls: "ext", in: "s3.1+0.6", kick: "MCP server", title: '<span class="mono">docs</span>', sub: '<span class="mono">docs.example.com</span>' });
    D.term(sec, { x: 100, y: 520, w: 600, in: "s3.1+0.8" }, "agent mcp.json (private, 0600)", [
      { c: "type: http", in: "s3.1+1.0" },
      { c: "url: <gateway>/v1/mcp/docs", in: "s3.1+1.3" },
      { c: "Authorization: Bearer mcp_…", in: "s3.2+0.5" },
    ]);
    chip(sec, 100, 215, "s3.2", "one token per session and role", "amber");
    const redis = D.box(sec, { x: 760, y: 560, w: 400, h: 150, cls: "store", in: "s3.3", kick: "Redis", title: '<span class="mono" style="font-size:30px">sha256(token)</span>', sub: '<span class="mono" style="font-size:24px">→ {role, session, exp}</span>' });
    const wa = D.wire(sec, sv, agent, gw, { in: "s3.3+0.4", label: "Bearer mcp_…", lo: [0, -30], color: "#f5b452" });
    D.packet(sv, wa, "s3.3+0.8", "s3.3+1.4", "#f5b452");
    D.wire(sec, sv, gw, redis, { from: "b", to: "t", in: "s3.4", bi: true, color: "#b197fc" });
    const checks = card(sec, { x: 1220, y: 570, w: 600, h: 280, in: "s3.4" }, "<h3>Every check must pass</h3>");
    rows(checks, [
      ['<span class="tick">✓</span> a live token', "s3.4+0.3"],
      ['<span class="tick">✓</span> the server is enabled', "s3.4+0.6"],
      ['<span class="tick">✓</span> its transport is remote', "s3.4+0.9"],
      ['<span class="tick">✓</span> the token\'s role is in its roles', "s3.4+1.2"],
    ]);
    D.swap(sec, 100, 780, [["s3.5", '403 {"error": "mcp_forbidden"}', "red"], ["s3.7", "revoked at session end · TTL 4 h by default", "amber"]]);
    chip(sec, 760, 215, "s3.6", "agent's Authorization dropped", "red", "s3.7");
    const ws = D.wire(sec, sv, gw, srv, { in: "s3.6", label: "+ stored headers", lo: [0, -30], color: "#3ddc97" });
    D.packet(sv, ws, "s3.6+0.5", "s3.6+1.1", "#3ddc97");
    D.abs(sec, "div", { x: 1420, y: 485, w: 400, in: "s3.6+1.2" }, { text: "The answer streams back unchanged.", style: { fontSize: "26px", color: "var(--muted)" } });
  },

  // 4. Guard rails
  s4(sec) {
    D.term(sec, { x: 100, y: 230, w: 760, in: "s4.2" }, "agent config.yml, written by Symphony", [
      { c: "mcp:", in: "s4.2+0.2" },
      { c: "  enableProjectConfig: false", in: "s4.2+0.4" },
      { c: "tools:", in: "s4.3" },
      { c: "  xdev: false", in: "s4.3+0.2" },
    ]);
    D.callout(sec, { x: 100, y: 515, in: "s4.3+0.8" }, 'no <span class="mono">xd://</span> devices, no <span class="mono">write</span> for a read-only role');
    D.box(sec, { x: 960, y: 230, w: 860, h: 160, cls: "bad", in: "s4.1+0.3", kick: "acme/app · main", title: '<span class="mono">.omp/mcp.json</span>', sub: "the repository's own servers" });
    chip(sec, 1640, 290, "s4.2+1.8", "ignored", "red");
    D.abs(sec, "div", { x: 960, y: 410, in: "s4.1+0.8" }, { html: '<span class="chip red">third-party MCP configs</span> &nbsp;<span style="font-size:28px;color:var(--muted)">ignored too</span>' });
    const stdio = card(sec, { x: 960, y: 500, w: 860, h: 300, in: "s4.4" }, "<h3>stdio servers</h3><p>No secret headers. Symphony refuses an env name that looks like a credential:</p>");
    const env = D.chips([["API_KEY", "red"], ["TOKEN", "red"], ["SECRET", "red"], ["PASSWORD", "red"], ["AUTH", "red"], ["TZ=UTC ✓", "green"]]);
    stdio.append(env);
    Engine.anim(env, { in: "s4.4+0.8", lift: 6 });
    card(sec, { x: 100, y: 600, w: 760, h: 250, in: "s4.5" },
      "<h3>The command must be in the image</h3><p>The workspace image for the implementer. The orchestrator image for the merger, the reflector and the assistant.</p>");
  },

  // 5. Local mode
  s5(sec) {
    D.box(sec, { x: 100, y: 230, w: 520, h: 220, cls: "ext", in: "s5.1", kick: "Host", title: '<span class="mono">~/.omp/agent</span>', sub: "models and logins",
      body: D.chips([["unchanged", "green"]]) });
    D.term(sec, { x: 700, y: 230, w: 1120, in: "s5.2" }, "<root>/credentials/<sandbox>/mcp/  (private)", [
      { c: "symphony/.claude-plugin/plugin.json", in: "s5.2+0.3" },
      { c: "symphony/.mcp.json   # the role's servers", in: "s5.2+0.6" },
      { c: "config.yml           # project configs off, xdev off", in: "s5.2+0.9" },
    ]);
    D.term(sec, { x: 100, y: 520, w: 1720, in: "s5.3" }, "engine command line", [
      { cmd: "omp … --plugin-dir <dir>/mcp/symphony --config <dir>/mcp/config.yml", in: "s5.3+0.2" },
    ]);
    chip(sec, 100, 700, "s5.4", "the overlay wins: .omp/mcp.json stays off", "green");
    chip(sec, 100, 780, "s5.4+1.0", "remote servers → the local gateway · tools mcp__symphony_<server>_<tool>", "blue");
  },

  // 6. Add a server
  s6(sec) {
    D.shot(sec, {
      x: 100, y: 200, w: 1720, h: 600, in: "s6", out: "s6.5",
      shots: [{ src: SHOT("admin-mcp"), in: "s6" }],
      cam: [["s6", 1440, 519, 0.597], ["s6.3", 2060, 668, 1.05]],
      spots: [
        { x: 448, y: 266, w: 112, h: 60, in: "s6.1+0.4", out: "s6.2" },
        { x: 12, y: 402, w: 1956, h: 190, in: "s6.2+0.2", out: "s6.3" },
        { x: 2030, y: 612, w: 720, h: 52, in: "s6.3+1.0", out: "s6.4" },
        { x: 2118, y: 450, w: 86, h: 48, in: "s6.4", out: "s6.5" },
        { x: 2030, y: 742, w: 800, h: 184, in: "s6.4+0.8", out: "s6.5" },
      ],
    });
    D.callout(sec, { x: 100, y: 820, in: "s6.3+1.2", out: "s6.4" }, "header names only: the values are write-only");
    D.callout(sec, { x: 100, y: 820, in: "s6.4+1.4", out: "s6.5" }, 'initialize, tools/list · or <span class="mono">unreachable</span>, <span class="mono">rejected</span>, <span class="mono">protocol_error</span>');

    D.term(sec, { x: 100, y: 220, w: 1720, in: "s6.5" }, "symphony admin mcp (an admin token)", [
      { cmd: "symphony admin mcp set docs --transport http --url https://docs.example.com/mcp \\", in: "s6.5+0.2" },
      { c: "    --role implementer --role reviewer", in: "s6.5+1.0" },
      { cmd: "echo '{\"X-API-Key\": \"<key>\"}' | symphony admin mcp headers docs", in: "s6.5+1.4" },
      { c: "{\"headers_set\":[\"X-API-Key\"]}", in: "s6.6+0.6" },
      { cmd: "symphony admin mcp test docs", in: "s6.5+2.4" },
      { cmd: "symphony admin mcp list", in: "s6.5+3.0" },
    ]);
    chip(sec, 100, 620, "s6.6+1.2", "header values never in arguments or output", "amber");
    chip(sec, 100, 700, "s6.6+1.8", "audit: admin.mcp.<put|remove|headers|test>", "");
  },

  // 7. Limits
  s7(sec) {
    const c1 = card(sec, { x: 100, y: 220, w: 840, h: 300, in: "s7.1" },
      '<h3>Kubernetes: about a minute</h3><p>The gateway reads the mounted Secret <span class="mono">symphony-mcp</span>. The kubelet updates it after about a minute, so a new header or role can take that long.</p>');
    const big = Engine.h("div", { class: "big", text: "≈ 1 min", style: { color: "var(--amber)", marginTop: "14px" } });
    c1.append(big);
    Engine.anim(big, { in: "s7.2", kind: "pop" });
    card(sec, { x: 980, y: 220, w: 840, h: 300, in: "s7.3" },
      "<h3>Running agents</h3><p>An agent reads its MCP config when it starts. A running agent keeps its servers until its session ends.</p>");
    card(sec, { x: 100, y: 550, w: 1720, h: 220, in: "s7.4" },
      '<h3>Local mode: your own <span class="mono">~/.omp</span></h3><p class="mono" style="color:var(--amber)">~/.omp/agent/mcp.json</p><p style="margin-top:10px">still loads in local mode. In a cluster, the agent dir is private, so no user config applies.</p>');
  },

  // 8. Proof
  s8(sec) {
    const board = card(sec, { x: 100, y: 205, w: 1150, h: 680, in: "s8.1" },
      '<h3>Mocked run, Linear and Slack · every check passed</h3>');
    const ok = '<span class="tick">✓</span>';
    rows(board, [
      [`${ok} <span class="mono">PUT /v1/admin/mcp/mock</span> → 200 · http · implementer, assistant`, "s8.2"],
      [`${ok} headers write-only: <span class="mono">Authorization</span>, <span class="mono">X-Acme-Tenant</span>`, "s8.2+0.6"],
      [`${ok} test: ok · tools <span class="mono">echo</span>, <span class="mono">lookup_ticket_context</span>`, "s8.2+1.2"],
      [`${ok} the repository's own server <span class="mono">project</span>: no request`, "s8.3"],
      [`${ok} the staged config names only the gateway`, "s8.3+1.0"],
      [`${ok} <span class="mono">mcp__symphony_mock_lookup_ticket_context</span> → Workpad`, "s8.4"],
      [`${ok} every request at the mock had the stored headers`, "s8.5"],
      [`${ok} revoked bearer: 403 at the gateway, 401 at the mock`, "s8.6"],
      [`${ok} the assistant on Slack: a second call, the same headers`, "s8.7"],
    ], { marginTop: "16px", lineHeight: "1.35" });
    const tile = (y, ref, big, text, color) => D.abs(sec, "div", { x: 1290, y, w: 530, h: 210, in: ref, kind: "pop" }, {
      class: "card", html: `<div class="big" style="color:${color}">${big}</div><p style="margin-top:8px">${text}</p>`,
    });
    tile(205, "s8.4", "16.1 s", "the coding agent's tool result reached the ticket", "var(--green)");
    tile(440, "s8.6", "403", "<span class=\"mono\">mcp_forbidden</span> for the revoked agent token", "var(--red)");
    tile(675, "s8.7", "9.1 s", "the assistant's answer on Slack, through the gateway", "var(--blue)");
  },
};
