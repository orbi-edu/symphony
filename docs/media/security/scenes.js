// The security video: ten scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/security.md, docs/providers.md, docs/mcp.md, docs/assistant.md (Sandbox),
// deploy/networkpolicy.yaml, deploy/rbac.yaml, src/symphony/ghapp.py (WORKSPACE, READ_ONLY) and
// src/symphony/assistant/sandbox.py and share.py (the sandbox pod and share_file's refusals).

const SHOT = (name) => `../../tern/${name}-dark.png`;

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const t = (ref, d) => Engine.at(ref) + d;
const mono = (s, size = 28) => `<span class="mono" style="font-size:${size}px">${s}</span>`;
// A zone frame: an outline with its name on top (dashed amber for the untrusted zone).
const zone = (parent, x, w, ref, color, name) => {
  D.abs(parent, "div", { x, y: 225, w, h: 560, in: ref, kind: "fade", lift: 0 },
    { style: { border: `3px ${color === "var(--amber)" ? "dashed" : "solid"} ${color}`, borderRadius: "22px", background: "rgba(255,255,255,0.02)" } });
  D.abs(parent, "div", { x: x + 20, y: 238, in: ref }, { text: name, style: { fontSize: "28px", fontWeight: "700", color } });
};
// A card whose rows appear at the given refs (or one after the other from the card's ref).
const rows = (parent, o, title, items, refs, size = 28) => {
  const el = card(parent, o, title ? `<h3>${title}</h3>` : "");
  items.forEach((html, i) => {
    const r = Engine.h("p", { html, style: { fontSize: `${size}px`, lineHeight: "1.45", marginTop: "8px" } });
    el.append(r);
    Engine.anim(r, { in: Array.isArray(refs) ? refs[i] : t(refs, 0.2 + i * 0.35), lift: 6 });
  });
  return el;
};

const SCENES = {
  // 1. Trust zones
  s1(sec, sv) {
    zone(sec, 100, 330, "s1.2", "var(--blue)", "1 · People's machines");
    zone(sec, 470, 250, "s1.3", "var(--muted)", "2 · Edge");
    zone(sec, 760, 650, "s1.4", "var(--green)", "3 · Trusted side");
    zone(sec, 1450, 370, "s1.1", "var(--amber)", "4 · Untrusted");
    const ws = D.box(sec, { x: 1475, y: 330, w: 320, h: 190, cls: "ws", in: "s1.1+0.3", kick: "Workspace pod", title: "agent code", sub: "its commands, omp" });
    chip(sec, 1475, 545, "s1.1+0.8", "untrusted", "red");
    const cli = D.box(sec, { x: 125, y: 300, w: 280, h: 150, cls: "people", in: "s1.2", kick: "CLI", title: "API token", sub: "in the keychain" });
    D.box(sec, { x: 125, y: 480, w: 280, h: 140, cls: "people", in: "s1.2+0.5", kick: "Tern plugin", title: "API token", sub: "in memory only" });
    const tun = D.box(sec, { x: 490, y: 330, w: 210, h: 170, cls: "ext", in: "s1.3", kick: "Cloudflare", title: "Tunnel", sub: "outbound only" });
    const names = [["Control API", "trusted"], ["Daemon", "trusted"], ["Runner Jobs", "trusted"], ["Merger", "merger"], ["Redis", "store"], ["Secrets", "store"]];
    const B = names.map(([n, cls], i) => D.box(sec, { x: 785 + (i % 3) * 205, y: 300 + Math.floor(i / 3) * 120, w: 190, h: 100, cls: `center ${cls}`,
      in: t("s1.4", 0.2 + i * 0.25), title: `<span style="font-size:28px">${n}</span>` }));
    D.wire(sec, sv, cli, tun, { in: "s1.3+0.4" });
    D.wire(sec, sv, tun, B[0], { in: "s1.4+0.2" });
    D.abs(sec, "div", { x: 785, y: 540, w: 600, h: 235, in: "s1.5" }, {
      class: "card", html: '<h3>Kept and used here</h3><div class="chips">' +
        ["Linear API key", "GitHub App private key", "tracker App token", "Slack, Discord", "API tokens"].map((k) => `<span class="chip blue" style="font-size:28px;font-family:inherit">${k}</span>`).join("") + "</div>",
    });
    const w = D.wire(sec, sv, B[2], ws, { from: "r", to: "l", in: "s1.6", color: "#f5b452" });
    D.packet(sv, w, "s1.6+0.4", "s1.6+1.2", "#f5b452");
    D.abs(sec, "div", { x: 1475, y: 615, w: 320, in: "s1.6+0.8" }, { text: "only the credentials of its own attempt", style: { fontSize: "28px", color: "var(--amber)", lineHeight: "1.3" } });
  },

  // 2. Model providers
  s2(sec) {
    const A = D.layer(sec, { in: "s2", out: "s2.4" });
    const kinds = [
      ["s2.1", "API key", "one key per provider, stored by an admin"],
      ["s2.2", "Host cloud credentials", "for example Amazon Bedrock through IAM"],
      ["s2.2+3.0", `Custom endpoint, ${mono("auth: none", 30)}`, "an endpoint of your own, with no key"],
    ];
    kinds.forEach(([ref, title, sub], i) => card(A.el, { x: 100, y: 230 + i * 185, w: 900, h: 160, in: ref },
      `<h3><span class="tick">✓</span> ${title}</h3><p>${sub}</p>`));
    card(A.el, { x: 1080, y: 230, w: 740, h: 530, in: "s2.3" },
      `<h3 style="color:var(--red)">Not supported</h3>
       <div class="chips" style="margin-top:14px"><span class="chip red">provider OAuth ✕</span><span class="chip red">subscription login ✕</span></div>
       <p style="margin-top:22px">Login-only providers are not in the catalog, for example ${mono("openai-codex")}, ${mono("github-copilot")} and ${mono("cursor")}.</p>`);

    D.shot(sec, {
      x: 100, y: 200, w: 1720, h: 560, in: "s2.4",
      shots: [{ src: SHOT("admin-providers"), in: "s2.4" }],
      cam: [["s2.4", 1440, 520, 0.6], ["s2.4+0.5", 1000, 520, 0.85], ["s2.5", 1000, 470, 0.85], ["s2.5+1.8", 1850, 470, 0.85]],
      spots: [
        { x: 150, y: 635, w: 460, h: 50, in: "s2.4+1.4", out: "s2.5" },
        { x: 365, y: 338, w: 690, h: 50, in: "s2.5+0.3", out: "s2.5+1.8" },
        { x: 2720, y: 402, w: 135, h: 205, in: "s2.5+2.4" },
      ],
    });
    D.abs(sec, "div", { x: 100, y: 790, in: "s2.4+0.8" }, { html: `<span class="chip">printf '%s' "$KEY" | symphony admin provider key anthropic</span> &nbsp;<span style="font-size:28px;color:var(--muted)">stdin or a hidden prompt</span>` });
  },

  // 3. The workspace pod
  s3(sec, sv) {
    const ws = D.box(sec, { x: 610, y: 400, w: 680, h: 200, cls: "ws", in: "s3.1", kick: "Workspace pod", title: "one attempt" });
    chip(sec, 630, 545, "s3.2", "no ingress", "red");
    chip(sec, 830, 545, "s3.4", "no ServiceAccount token", "red");
    const allow = [["Gateway", "TCP 8090", "trusted"], ["Cluster DNS", "port 53", "trusted"], ["Public addresses", "0.0.0.0/0 minus private", "ext"]];
    const A = allow.map(([n, s, cls], i) => D.box(sec, { x: 1400, y: 230 + i * 165, w: 420, h: 125, cls, in: t("s3.2", 1.4 + i * 0.6), title: `<span style="font-size:30px">${n}</span>`, sub: s }));
    A.forEach((b, i) => D.wire(sec, sv, ws, b, { in: t("s3.2", 1.4 + i * 0.6), color: "#3ddc97", tf: 0.5 }));
    ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8"].forEach((r, i) =>
      chip(sec, 630 + (i % 2) * 300, 630 + Math.floor(i / 2) * 58, t("s3.3", i * 0.2), `✕ ${r}`, "red"));
    ["Control API", "Redis", "The node, other pods", "Metadata 169.254.169.254"].forEach((n, i) => {
      const ref = t("s3.3", 1.6 + i * 0.5);
      const b = D.box(sec, { x: 100, y: 230 + i * 120, w: 430, h: 96, cls: "bad center", in: ref, title: `<span style="font-size:28px">${n}</span>` });
      D.wire(sec, sv, ws, b, { from: "l", to: "r", ff: 0.3 + i * 0.13, in: ref, color: "#ff6b6b", dash: true });
    });
    const runner = D.box(sec, { x: 610, y: 215, w: 680, h: 100, cls: "trusted center", in: "s3.5", title: '<span style="font-size:30px">Runner Job</span>' });
    D.wire(sec, sv, runner, ws, { from: "b", to: "t", in: "s3.5+0.3", color: "#5cb8ff", label: "pods/exec, Kubernetes API", lo: [20, -6] });
    D.callout(sec, { x: 100, y: 815, in: "s3.6" }, "Public egress is open: only the attempt's own credentials could leave.");
  },

  // 4. The four workspace credentials
  s4(sec) {
    const creds = [
      ["s4.2", "Model credential of the role", "/run/symphony/engine.env"],
      ["s4.2+2.4", "GitHub installation token", "/run/symphony/gh-token · the ticket's repositories"],
      ["s4.3", "Telemetry token", "engine environment · one dispatch"],
      ["s4.3+2.4", "MCP token", "the agent's private mcp.json · one session, one role"],
    ];
    D.abs(sec, "div", { x: 100, y: 215, in: "s4.1" }, { html: '<b style="font-size:32px">In the workspace</b>' });
    creds.forEach(([ref, title, path], i) => D.abs(sec, "div", { x: 100, y: 275 + i * 125, w: 980, h: 108, in: ref }, {
      class: "node", html: `<div style="font-size:30px;font-weight:700"><span style="color:var(--amber)">${i + 1}</span> &nbsp;${title}</div><div class="mono" style="font-size:28px;color:var(--muted);margin-top:6px">${path}</div>`,
      style: { padding: "12px 24px", borderColor: "var(--amber)" },
    }));
    chip(sec, 100, 790, "s4.4", "written by the runner into private files", "amber");
    rows(sec, { x: 1160, y: 215, w: 660, h: 640, in: "s4.5" }, '<span style="color:var(--red)">Never in the pod</span>', [
      "✕ Linear API key and webhook secret",
      "✕ GitHub tracker token",
      "✕ GitHub App private key and IDs",
      "✕ GitHub webhook secret",
      "✕ Slack and Discord credentials",
      "✕ API tokens, the bootstrap token too",
      "✕ other providers' keys, with a custom provider",
    ], "s4.5");
  },

  // 5. GitHub App token scopes
  s5(sec) {
    D.term(sec, { x: 100, y: 215, w: 820, in: "s5.1" }, "ghapp.WORKSPACE · a normal ticket", [
      { c: "contents:      write", in: "s5.2" },
      { c: "pull_requests: write", in: "s5.2+0.3" },
      { c: "metadata:      read", in: "s5.2+0.6" },
      { c: "checks:        read", in: "s5.2+0.9" },
      { c: "actions:       read", in: "s5.2+1.2" },
      { c: "statuses:      read", in: "s5.2+1.5" },
    ]);
    D.abs(sec, "div", { x: 100, y: 592, in: "s5.3" }, { html: '<span class="chip red">no issues</span> <span class="chip red">no projects</span> <span class="chip red">no workflows</span>' });
    D.abs(sec, "div", { x: 100, y: 652, w: 820, in: "s5.3+1.0" }, { text: "Tickets go through the tracker host tool. A push that changes .github/workflows/* is rejected.", style: { fontSize: "28px", color: "var(--muted)" } });
    chip(sec, 1000, 215, "s5.1+1.2", "scoped to the ticket's repositories", "blue");
    D.term(sec, { x: 1000, y: 300, w: 820, in: "s5.4" }, "ghapp.READ_ONLY · an investigation", [
      { c: "contents:      read", in: "s5.4+0.3" },
      { c: "pull_requests: read", in: "s5.4+0.6" },
      { c: "metadata:      read", in: "s5.4+0.9" },
    ]);
    D.abs(sec, "div", { x: 1000, y: 560, in: "s5.4+2.4" }, { html: '<span class="chip red">push ✕</span> <span class="chip red">new pull request ✕</span> &nbsp;<span style="font-size:28px;color:var(--muted)">a report is the deliverable</span>' });
    // Lifetime: one attempt, a renewal between turns.
    D.abs(sec, "div", { x: 100, y: 800, w: 1720, h: 8, in: "s5.5", kind: "wipe", dur: 0.8 }, { style: { background: "#3a4a62", borderRadius: "4px" } });
    ["turn 1", "turn 2", "turn 3"].forEach((n, i) => {
      D.abs(sec, "div", { x: 160 + i * 560, y: 822, in: t("s5.5", 0.3 + i * 0.4) }, { class: "mono", text: n, style: { fontSize: "28px", color: "var(--muted)" } });
      if (i) D.abs(sec, "div", { x: i * 560 - 100, y: 745, in: t("s5.5", 1.6 + i * 0.4), kind: "pop" }, { html: '<span class="chip green">renew</span>' });
    });
    D.abs(sec, "div", { x: 1170, y: 745, in: "s5.5" }, { html: '<span class="chip amber">≤ 1 h · renewed 10 min before expiry</span>' });
  },

  // 6. MCP
  s6(sec) {
    D.shot(sec, {
      x: 100, y: 200, w: 1720, h: 580, in: "s6", out: "s6.3",
      shots: [{ src: SHOT("admin-mcp"), in: "s6" }],
      cam: [["s6", 1440, 560, 0.6], ["s6+0.5", 1000, 520, 0.85], ["s6.2", 1850, 640, 0.85]],
      spots: [
        { x: 970, y: 450, w: 420, h: 140, in: "s6.1+0.4", out: "s6.2" },
        { x: 2040, y: 618, w: 720, h: 44, in: "s6.2" },
      ],
    });
    const L = D.layer(sec, { in: "s6.3" });
    const p = L.el;
    const agent = D.box(p, { x: 100, y: 240, w: 420, h: 160, cls: "ws", in: "s6.3", kick: "Agent session", title: "researcher", sub: '<span class="mono">mcp.json: MCP token</span>' });
    const gw = D.box(p, { x: 720, y: 240, w: 440, h: 160, cls: "trusted", in: "s6.3", kick: "Gateway", title: '<span class="mono" style="font-size:30px">/v1/mcp/search</span>', sub: "checks role and server" });
    const srv = D.box(p, { x: 1400, y: 240, w: 420, h: 160, cls: "ext", in: "s6.3+2.4", kick: "Remote MCP server", title: "search", sub: "the real URL" });
    const secret = D.box(p, { x: 720, y: 470, w: 440, h: 110, cls: "store", in: "s6.3", kick: "Secret", title: '<span class="mono" style="font-size:28px">symphony-mcp</span>' });
    const w1 = D.wire(p, L.sv, agent, gw, { in: "s6.3+0.3", color: "#5cb8ff", label: "MCP token", lo: [0, -34] });
    D.packet(L.sv, w1, "s6.3+0.6", "s6.3+1.2");
    D.wire(p, L.sv, secret, gw, { from: "t", to: "b", in: "s6.3+0.2", color: "#5b6b82", label: "mounted", lo: [60, 0] });
    const w2 = D.wire(p, L.sv, gw, srv, { in: "s6.3+2.4", color: "#3ddc97", label: "stored headers added", lo: [0, -34] });
    D.packet(L.sv, w2, "s6.3+2.8", "s6.3+3.4", "#3ddc97");
    chip(p, 100, 420, "s6.3+1.4", "agent's Authorization dropped", "amber");
    rows(p, { x: 100, y: 610, w: 1060, h: 270, in: "s6.4" }, "", [
      "One token per session and role, a SHA-256 hash in Redis",
      'Another role or a disabled server: <span class="chip red">403 mcp_forbidden</span>',
      "Revoked at the session end; expires after 4 hours",
    ], ["s6.4", "s6.5", "s6.6"]);
    D.term(p, { x: 1240, y: 470, w: 580, in: "s6.7" }, "the agent's settings", [
      { c: "mcp:", in: "s6.7+0.2" },
      { c: "  enableProjectConfig: false", in: "s6.7+0.4" },
      { c: "tools:", in: "s6.7+2.2" },
      { c: "  xdev: false", in: "s6.7+2.4" },
    ]);
    chip(p, 1240, 760, "s6.7+1.0", "✕ the repository's .omp/mcp.json", "red");
    D.abs(p, "div", { x: 1240, y: 830, w: 580, in: "s6.7+2.8" }, { text: "MCP tools add no write tool.", style: { fontSize: "28px", color: "var(--muted)" } });
  },

  // 7. Setup secrets, RBAC, webhooks, fencing
  s7(sec) {
    const A = D.layer(sec, { in: "s7", out: "s7.3" });
    const adm = D.box(A.el, { x: 100, y: 300, w: 380, h: 150, cls: "people", in: "s7.1", kick: "admin token", title: '<span class="mono" style="font-size:28px">PUT /v1/setup/*</span>' });
    const api = D.box(A.el, { x: 620, y: 300, w: 380, h: 150, cls: "trusted", in: "s7.1+0.3", kick: "Control API", title: "verify, then save" });
    const ven = D.box(A.el, { x: 1140, y: 220, w: 680, h: 130, cls: "ext", in: "s7.1+1.0", kick: "Real service", title: "Linear, GitHub, Slack, Discord", sub: "a value that fails is not saved" });
    const sec2 = D.box(A.el, { x: 1140, y: 420, w: 680, h: 130, cls: "store", in: "s7.1+2.6", kick: "Named Secrets", title: '<span class="mono" style="font-size:28px">linear-api-key, github-app, …</span>' });
    D.wire(A.el, A.sv, adm, api, { in: "s7.1+0.3" });
    D.wire(A.el, A.sv, api, ven, { in: "s7.1+1.0", color: "#5cb8ff", tf: 0.5 });
    D.wire(A.el, A.sv, api, sec2, { in: "s7.1+2.6", color: "#3ddc97", tf: 0.5 });
    chip(A.el, 100, 480, "s7.2", "only admin can write", "amber");
    D.abs(A.el, "div", { x: 100, y: 620, in: "s7.2+1.0" }, { html: '<b style="font-size:30px">Write-only:</b> &nbsp;<span class="chip">no response</span> <span class="chip">no log</span> <span class="chip">no audit entry</span> <span class="chip">no command line</span>' });
    D.abs(A.el, "div", { x: 100, y: 700, w: 1720, in: "s7.2+2.0" }, { text: "The checklist shows only whether a value is set.", style: { fontSize: "28px", color: "var(--muted)" } });

    const B = D.layer(sec, { in: "s7.3", out: "s7.4" });
    rows(B.el, { x: 100, y: 220, w: 1720, h: 330, in: "s7.3" }, "Namespaced Roles · no ClusterRole", [
      `${mono("symphony-api")} &nbsp; get, patch on named Secrets that exist before setup · <span style="color:var(--red)">no create, no delete</span>`,
      `${mono("symphony-runner")} &nbsp; workspace pods, pods/exec, get on the provider and MCP Secrets`,
      `${mono("symphony-daemon")} &nbsp; Jobs, pods, pods/exec, the workflow ConfigMap`,
      `${mono("symphony-merger")}, ${mono("symphony-assistant")} &nbsp; get on the provider and MCP Secrets`,
      `${mono("symphony-gateway")} &nbsp; nothing; no token mounted`,
    ], "s7.3");

    const C = D.layer(sec, { in: "s7.4" });
    rows(C.el, { x: 100, y: 220, w: 1100, h: 290, in: "s7.4" }, "Webhook signatures", [
      `Linear &nbsp;${mono("Linear-Signature")} · HMAC-SHA256 · 5-minute window`,
      `GitHub &nbsp;${mono("X-Hub-Signature-256")} · HMAC-SHA256`,
      '<span class="chip red">401 invalid_signature</span> <span class="chip red">401 stale_webhook</span>',
    ], "s7.4");
    card(C.el, { x: 1260, y: 220, w: 560, h: 290, in: "s7.4+2.4" },
      "<h3>Only a wake-up</h3><p>The woken loop reads the source of truth again. A forged delivery can cost a poll, not change state.</p>");
    D.abs(C.el, "div", { x: 100, y: 570, w: 1720, in: "s7.5" }, { html: '<b style="font-size:30px">Fencing:</b> &nbsp;<span class="chip violet">dispatch:&lt;issue&gt;:&lt;seq&gt;</span> <span class="chip violet">new gateway token per dispatch</span> <span class="chip">an old runner exits: fenced</span>' });
  },

  // 8. API tokens
  s8(sec) {
    const A = D.layer(sec, { in: "s8", out: "s8.6" });
    const roles = [
      ["s8.1+1.6", "viewer", "blue", "reads every view and uses the read MCP tools"],
      ["s8.2", "operator", "green", "+ messages and interrupts, pause, resume, hold, release, merger bypass and requeue"],
      ["s8.3", "admin", "amber", "+ API tokens, provider keys, team configuration, WORKFLOW.md, first-run setup"],
    ];
    roles.forEach(([ref, n, c, text], i) => D.abs(A.el, "div", { x: 100, y: 220 + i * 175, w: 1000, h: 155, in: ref }, {
      class: "node", html: `<div><span class="chip ${c}">${n}</span></div><div style="font-size:28px;margin-top:10px;line-height:1.3">${text}</div>`,
      style: { padding: "14px 24px" },
    }));
    D.abs(A.el, "div", { x: 100, y: 760, in: "s8.1" }, { html: '<span style="font-size:28px;color:var(--muted)">format</span> &nbsp;<span class="chip">sym_&lt;8 hex&gt;_&lt;43 characters&gt;</span>' });
    card(A.el, { x: 1180, y: 220, w: 640, h: 250, in: "s8.4" },
      '<h3>Stored</h3><p>token ID, SHA-256 of the secret, role, created, expiry, last use</p><p style="color:var(--muted);margin-top:8px">compared in constant time</p>');
    card(A.el, { x: 1180, y: 500, w: 640, h: 330, in: "s8.5" },
      "<h3>Rate limits</h3><p><b>600</b> requests a minute per token</p><p><b>30</b> failed authentications a minute per address</p>");
    chip(A.el, 1205, 760, "s8.5+2.6", "429 + Retry-After", "red");

    D.shot(sec, {
      x: 100, y: 200, w: 1720, h: 580, in: "s8.6",
      shots: [{ src: SHOT("audit"), in: "s8.6" }],
      cam: [["s8.6", 1440, 620, 0.6], ["s8.6+0.5", 980, 560, 0.9]],
      spots: [{ x: 20, y: 318, w: 2000, h: 146, in: "s8.6+0.6" }],
    });
  },

  // 9. The assistant
  s9(sec, sv) {
    const msg = D.box(sec, { x: 100, y: 230, w: 400, h: 170, cls: "ext", in: "s9.1", kick: "Slack or Discord", title: "a message", sub: "untrusted input" });
    const proc = D.box(sec, { x: 620, y: 230, w: 520, h: 170, cls: "trusted", in: "s9.3", kick: "Assistant process", title: "holds the credentials", sub: "chat, tracker, GitHub App" });
    const ag = D.box(sec, { x: 1260, y: 230, w: 560, h: 170, cls: "ws", in: "s9.2", kick: "Assistant agent", title: '<span class="mono" style="font-size:28px">read grep glob web_search</span>', sub: "and the role's MCP servers" });
    D.wire(sec, sv, msg, proc, { in: "s9.3+0.2" });
    D.wire(sec, sv, proc, ag, { in: "s9.3+0.4", color: "#f5b452" });
    chip(sec, 620, 420, "s9.3+0.8", "the agent's env: no credentials", "amber");
    chip(sec, 1260, 420, "s9.2+2.0", "default: no bash, edit, write", "red");
    D.box(sec, { x: 1260, y: 500, w: 560, h: 110, cls: "store", in: "s9.3+2.6", out: "s9.4", kick: "Worktrees at the default branch", title: '<span class="mono" style="font-size:28px">ghapp.READ_ONLY</span>' });

    // The optional sandbox: a workspace pod per thread, and what share_file refuses.
    const S = D.layer(sec, { in: "s9.4", out: "s9.8" });
    const pod = rows(S.el, { x: 100, y: 490, w: 860, h: 390, in: "s9.4" }, "Sandbox pod · one per thread", [
      `${mono("component: workspace", 26)} · the workspace NetworkPolicy`,
      "no Redis, API, other pods or metadata endpoint",
      mono("automountServiceAccountToken: false", 26),
      `clone token on stdin: one repository, ${mono("contents: read", 26)}`,
      "no chat, tracker, model or MCP credential",
    ], ["s9.4+0.8", "s9.4+2.8", "s9.5+0.2", "s9.5+2.6", "s9.5+4.0"]);
    pod.style.border = "3px dashed var(--amber)";
    chip(S.el, 1010, 490, "s9.6", "mapped people only: sandbox_run · share_file", "blue");
    rows(S.el, { x: 1010, y: 570, w: 810, h: 310, in: "s9.7" }, `${mono("share_file", 30)} refuses`, [
      "✕ anything inside a .git directory",
      `✕ ${mono(".git-credentials .netrc .npmrc .ssh .aws .kube", 24)}`,
      "✕ a file whose bytes hold a credential",
      "✕ a file over 25 MB",
    ], "s9.7");

    const P = D.layer(sec, { in: "s9.8" });
    card(P.el, { x: 100, y: 490, w: 1040, h: 200, in: "s9.8" },
      '<h3>Proposed tickets: 2</h3><p>ENG · Add the CSV export &nbsp;·&nbsp; ENG · Fix the date filter</p><div class="chips" style="margin-top:10px"><span class="chip green">Create</span><span class="chip blue">Create and start</span><span class="chip">Dismiss</span></div>');
    D.abs(P.el, "div", { x: 100, y: 720, w: 1720, in: "s9.9" }, { html: '<span class="chip green">alice (in notify.users) clicks Create → tickets created</span> &nbsp;<span class="chip">audit: assistant.tickets_created · alice</span>' });
    D.abs(P.el, "div", { x: 100, y: 790, w: 1720, in: "s9.9+1.4" }, { html: '<span class="chip red">bob (not mapped) clicks → private refusal</span>' });
  },

  // 10. Local mode
  s10(sec) {
    D.abs(sec, "div", { x: 100, y: 215, w: 1720, h: 230, in: "s10.1", kind: "fade", lift: 0 },
      { style: { border: "3px dashed var(--amber)", borderRadius: "22px" } });
    D.abs(sec, "div", { x: 125, y: 228, in: "s10.1" }, { html: '<b style="font-size:28px;color:var(--amber)">One host user · no pod, no container</b>' });
    D.box(sec, { x: 130, y: 280, w: 420, h: 140, cls: "trusted", in: "s10.1+0.3", kick: "Symphony process", title: "API, daemon, runner" });
    D.box(sec, { x: 600, y: 280, w: 420, h: 140, cls: "ws", in: "s10.1+0.6", kick: "Agent", title: "same user" });
    D.abs(sec, "div", { x: 1070, y: 285, w: 730, in: "s10.1+2.4" }, { html: '<div class="chips"><span class="chip amber">files</span><span class="chip amber">network</span><span class="chip amber">git credentials</span><span class="chip amber">~/.omp</span></div>' });
    D.abs(sec, "div", { x: 1070, y: 365, w: 730, in: "s10.2" }, { html: '<span class="chip">LINEAR_API_KEY ✕</span> <span class="chip">GITHUB_APP_* ✕</span>' });
    D.callout(sec, { x: 600, y: 458, in: "s10.2+2.6" }, "Hygiene, not a boundary: the same user can read the Symphony process.");
    rows(sec, { x: 100, y: 540, w: 840, h: 240, in: "s10.3" }, '<span style="color:var(--green)">Still holds</span>', [
      `✓ a scoped App token in ${mono("credentials/&lt;workspace&gt;/")}`,
      "✓ host git credential helpers cleared",
      "✓ repository MCP configs ignored",
    ], "s10.3");
    rows(sec, { x: 980, y: 540, w: 840, h: 240, in: "s10.4" }, '<span style="color:var(--red)">Does not hold</span>', [
      "✕ NetworkPolicy, pod limits",
      `✕ ${mono("~/.omp/agent/mcp.json")} still loads`,
      "✕ no App: the host user's git credentials",
    ], "s10.4");
    D.abs(sec, "div", { x: 100, y: 810, w: 1720, in: "s10.5" }, { html: '<span class="chip blue">a dedicated Unix user, no sudo</span> <span class="chip blue">tickets only from trusted people</span> <span class="chip blue">API on 127.0.0.1</span>' });
  },
};
