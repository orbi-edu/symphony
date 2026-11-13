// "Symphony and Seiji": seven scenes. Every claim comes from docs/seiji.md, docs/mcp.md, docs/tern.md,
// docs/mocked-certification.md (scenario 13), tests/test_seiji_real.py and Seiji's README.
// Time references point at narration cues (see ../lib/engine.js `at`).

const SHOT = (name) => `../../tern/${name}-dark.png`;
const t = (ref, d) => Engine.at(ref) + d;
const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const label = (parent, x, y, ref, text, out = null, style = {}) =>
  D.abs(parent, "div", { x, y, in: ref, out }, { html: text, style: { fontSize: "30px", color: "var(--muted)", ...style } });
// Rows that appear one by one inside a card.
const rows = (el, items, style = {}) => items.forEach(([ref, html]) => {
  const r = Engine.h("p", { html, style: { fontSize: "28px", lineHeight: "1.4", marginTop: "14px", ...style } });
  el.append(r);
  Engine.anim(r, { in: ref, lift: 6 });
});

const SCENES = {
  // 1. What Seiji is
  s1(sec, sv) {
    const agents = ["ENG-163", "ENG-151", "ENG-139"].map((id, i) =>
      D.box(sec, { x: 100, y: 240 + i * 150, w: 330, h: 136, cls: "ws", in: t("s1.1", 0.2 + i * 0.2), kick: "agent", title: id, sub: '<span class="mono">omp</span>' }));
    const mac = D.box(sec, { x: 660, y: 250, w: 560, h: 420, cls: "ext", in: "s1.1+0.6", kick: "Seiji", title: "one shared Mac", style: { justifyContent: "flex-start" } });
    ["iPhone 16", "iPhone 16", "iPad"].forEach((m, i) =>
      D.abs(sec, "div", { x: 700 + i * 170, y: 380, w: 140, h: 230, in: t("s1.1", 1.0 + i * 0.2), kind: "pop" }, {
        html: `<div style="font-size:26px;text-align:center;margin-top:84px">${m}</div>`,
        style: { border: "3px solid #3a4b63", borderRadius: "26px", background: "#111a27" },
      }));
    agents.forEach((a, i) => D.wire(sec, sv, a, mac, { in: t("s1.1", 1.2 + i * 0.15), label: i === 0 ? "MCP" : "", lo: [0, -26] }));
    chip(sec, 660, 690, "s1.2", "a fresh simulator for each lease", "blue");
    chip(sec, 660, 745, "s1.2+1.4", "deleted at release", "amber");
    const ev = card(sec, { x: 1330, y: 250, w: 490, h: 420, in: "s1.3" }, "<h3>Evidence of each run</h3>");
    rows(ev, [
      ["s1.3+0.3", "🎬 video.mp4"], ["s1.3+0.8", "📄 app.log"], ["s1.3+1.3", "🖼 a screenshot per step"],
      ["s1.3+1.9", '🔒 manifest, <span class="mono">SHA-256</span> per file'],
    ]);
    D.wire(sec, sv, mac, { x: 1330, y: 250, w: 490, h: 420 }, { in: "s1.3", color: "#3ddc97" });
    label(sec, 100, 800, "s1.4", "<b style=\"color:var(--text)\">ENG-163</b> Sign-in screen crashes on iOS 18 when the password is empty");
    chip(sec, 100, 850, "s1.5", "the agent runs the app", "green");
    chip(sec, 560, 850, "s1.5+1.5", "reviewers see what happened", "green");
  },

  // 2. Connect it
  s2(sec) {
    D.term(sec, { x: 100, y: 205, w: 1720, in: "s2.1", out: "s2.5" }, "admin token", [
      { cmd: "symphony admin mcp set seiji --preset seiji \\", in: "s2.1+0.3" },
      { c: "    --url https://seiji.example.com/mcp --role implementer", in: "s2.1+1.4" },
      { cmd: "echo '{\"Authorization\": \"Bearer sj_…\"}' | symphony admin mcp headers seiji", in: "s2.2" },
      { cmd: "symphony admin mcp test seiji", in: "s2.4-1.4" },
      { c: "seiji works (protocol 2025-11-25). Tools: …. Account: symphony.", in: "s2.4-0.4" },
    ]);
    chip(sec, 100, 560, "s2.2+1.6", "headers are write-only: the API never returns them", "amber", "s2.5");
    const pre = card(sec, { x: 100, y: 615, w: 1720, h: 270, in: "s2.3", out: "s2.5" }, "<h3>The preset fills</h3>");
    const grid = Engine.h("div", { style: { display: "grid", gridTemplateColumns: "200px 1fr 200px 1fr", gap: "8px 18px", fontSize: "27px", marginTop: "10px" } });
    pre.append(grid);
    [["transport", "http"], ["http_base", "https://seiji.example.com"], ["timeout_ms", "3900000 (65 min)"],
     ["agent_env", "SEIJI_TOKEN, SEIJI_URL"], ["cleanup", "acquire_device → release_device"]].forEach(([k, v], i) => {
      const a = Engine.h("span", { class: "mono", text: k, style: { color: "var(--blue)" } });
      const b = Engine.h("span", { class: "mono", text: v });
      grid.append(a, b);
      Engine.anim(a, { in: t("s2.3", 0.3 + i * 0.6) });
      Engine.anim(b, { in: t("s2.3", 0.3 + i * 0.6) });
    });
    chip(sec, 1180, 830, "s2.4", "stored as a plain definition · flags win", "blue", "s2.5");

    D.shot(sec, {
      x: 100, y: 205, w: 1720, h: 640, in: "s2.5",
      shots: [{ src: SHOT("admin-mcp-seiji"), in: "s2.5" }],
      cam: [["s2.5", 1940, 740, 0.95], ["s2.6", 1940, 1120, 0.95]],
      spots: [
        { x: 2040, y: 770, w: 805, h: 260, in: "s2.5+0.8", out: "s2.6" },
        { x: 2040, y: 1050, w: 805, h: 300, in: "s2.6+0.9", out: "s2.7" },
        { x: 2040, y: 1340, w: 805, h: 80, in: "s2.7+0.4" },
      ],
    });
    D.callout(sec, { x: 130, y: 770, in: "s2.5+0.5", out: "s2.6" }, "Tern › Admin › MCP · Preset: Seiji");
    D.callout(sec, { x: 130, y: 770, in: "s2.6+0.3", out: "s2.7" }, "Test: 23 tools · protocol 2025-11-25");
    D.callout(sec, { x: 130, y: 770, in: "s2.7+0.3" }, 'Seiji\'s <span class="mono">/v1/me</span>: the user and the quota');
  },

  // 3. The token never reaches the agent
  s3(sec, sv) {
    const ag = D.box(sec, { x: 100, y: 260, w: 560, h: 300, cls: "ws", in: "s3.2", kick: "workspace pod", title: "agent", style: { justifyContent: "flex-start" } });
    const gw = D.box(sec, { x: 820, y: 260, w: 440, h: 300, cls: "trusted", in: "s3.1+0.3", kick: "trusted", title: "Sandbox gateway", sub: "checks the session token" });
    const sj = D.box(sec, { x: 1420, y: 260, w: 400, h: 300, cls: "ext", in: "s3.1+0.3", kick: "Seiji", title: "MCP + HTTP API" });
    const sec_ = D.box(sec, { x: 820, y: 620, w: 440, h: 140, cls: "store", in: "s3.1", kick: "Secret symphony-mcp", title: '<span class="mono">Bearer sj_…</span>' });
    chip(sec, 125, 380, "s3.2+0.4", "SEIJI_TOKEN = mcp_…", "amber");
    chip(sec, 125, 440, "s3.3", "SEIJI_URL = {gateway}/v1/mcp/seiji/http", "amber");
    label(sec, 125, 500, "s3.2+1.8", "its own session token, not Seiji's", null, { fontSize: "28px" });
    const w1 = D.wire(sec, sv, ag, gw, { in: "s3.4", label: "Bearer mcp_…", lo: [0, -30] });
    const w2 = D.wire(sec, sv, gw, sj, { in: "s3.4+1.0", label: "Bearer sj_…", lo: [0, -30], color: "#3ddc97" });
    D.wire(sec, sv, sec_, gw, { from: "t", to: "b", in: "s3.4+0.8", color: "#3ddc97" });
    D.packet(sv, w1, t("s3.4", 0.2), t("s3.4", 0.9));
    D.packet(sv, w2, t("s3.4", 1.1), t("s3.4", 1.8), "#3ddc97");
    const routes = card(sec, { x: 1340, y: 620, w: 480, h: 265, in: "s3.5" }, "<h3>Through the gateway</h3>");
    rows(routes, [["s3.5+0.3", '<span class="mono">/v1/mcp/seiji</span> MCP'], ["s3.5+1.0", '<span class="mono">…/http/v1/apps</span> upload'],
      ["s3.5+1.6", '<span class="mono">…/http/v1/runs/…</span> evidence'], ["s3.5+2.2", '<span class="mono">…/http/v1/me</span>']], { fontSize: "26px", marginTop: "8px" });
    const rw = card(sec, { x: 100, y: 620, w: 680, h: 265, in: "s3.6", out: "s3.7" }, "<h3>Links in tool results</h3>");
    rows(rw, [["s3.6+0.3", '<span class="mono" style="color:var(--muted)">https://seiji.example.com/v1/runs/r_…</span>'],
      ["s3.6+1.3", '<span class="mono" style="color:var(--green)">→ $SEIJI_URL/v1/runs/r_…</span>']], { fontSize: "25px" });
    const nv = card(sec, { x: 100, y: 620, w: 680, h: 265, in: "s3.7" }, "<h3>Viewer pages</h3>");
    rows(nv, [["s3.7+0.3", '<span class="mono">/view/…</span> takes no token'], ["s3.7+1.2", '<span class="chip red">404 mcp_not_proxied</span>']]);
  },

  // 4. One attempt with a device
  s4(sec, sv) {
    label(sec, 100, 205, "s4.1", '<b style="color:var(--text)">ENG-163</b> · acme/mobile · Sign-in screen crashes on iOS 18 when the password is empty', null, { fontSize: "28px" });
    const steps = [
      ["s4.2", "acquire_device", "iphone-16 · l_3f9c2a7b1e04"],
      ["s4.3", "upload", "POST $SEIJI_URL/v1/apps"],
      ["s4.4", "run_actions", "the sign-in flow, recorded"],
      ["s4.5", "export_evidence", "a Markdown report"],
      ["s4.6", "attach", "the files, on the ticket"],
    ];
    const boxes = steps.map(([ref, k, s], i) => D.box(sec, { x: 100 + i * 349, y: 270, w: 324, h: 150, cls: i === 4 ? "good" : "ws", in: ref, title: `<span class="mono" style="font-size:26px">${k}</span>`, sub: s }));
    boxes.slice(1).forEach((b, i) => D.wire(sec, sv, boxes[i], b, { in: steps[i + 1][0], straight: true }));
    D.abs(sec, "div", { x: 100, y: 450, w: 1720, in: "s4.3+0.8", out: "s4.5" }, {
      class: "mono", text: 'curl -H "Authorization: Bearer $SEIJI_TOKEN" -H "X-Filename: MyApp.zip" --data-binary @MyApp.zip "$SEIJI_URL/v1/apps"',
      style: { fontSize: "24px", color: "var(--amber)", background: "#0f1622", padding: "14px 18px", borderRadius: "12px", border: "2px solid #2b3648" },
    });
    chip(sec, 100, 540, "s4.3+2.0", "streams both ways: up to 1 GiB, never held in memory", "blue", "s4.5");
    const pr = card(sec, { x: 100, y: 520, w: 1000, h: 365, in: "s4.5" }, '<h3>Pull request body · Seiji evidence</h3>');
    rows(pr, [
      ["s4.5+0.4", "### ✅ Sign-in with an empty password"],
      ["s4.5+0.9", '<span style="color:var(--muted)">run r_… · iphone-16 · a step table</span>'],
      ["s4.5+1.4", '<span class="mono" style="font-size:24px;color:var(--muted)">$SEIJI_URL/v1/runs/r_…/files/video.mp4</span>'],
    ], { fontSize: "27px" });
    chip(sec, 130, 820, "s4.6", "reviewers cannot open these links", "red");
    const tk = card(sec, { x: 1160, y: 520, w: 660, h: 365, in: "s4.6+0.6" }, "<h3>Ticket evidence</h3>");
    rows(tk, [["s4.7", '<b>Linear</b>: <span class="mono">evidence.zip</span>'], ["s4.7+1.4", '<b>GitHub issue</b>: <span class="mono">report.md</span> (text only)'],
      ["s4.7+2.6", '<span style="color:var(--muted)">downloaded through $SEIJI_URL</span>']]);
  },

  // 5. Cleanup
  s5(sec, sv) {
    D.abs(sec, "div", { x: 100, y: 300, w: 1720, h: 6, in: "s5.1" }, { style: { background: "#2b3648", borderRadius: "3px" } });
    D.abs(sec, "div", { x: 300, y: 285, w: 900, h: 36, in: "s5.1+0.4" }, { style: { background: "rgba(245,180,82,0.35)", border: "2px solid var(--amber)", borderRadius: "10px" } });
    label(sec, 300, 230, "s5.1+0.4", "lease · costs budget", null, { color: "var(--amber)" });
    label(sec, 1220, 230, "s5.3", "attempt ends", null, { color: "var(--text)" });
    D.abs(sec, "div", { x: 1200, y: 260, w: 4, h: 90, in: "s5.3" }, { style: { background: "var(--text)" } });
    const c1 = card(sec, { x: 100, y: 400, w: 540, h: 480, in: "s5.2" }, "<h3>1 · Record</h3>");
    rows(c1, [["s5.2+0.3", '<span class="mono">acquire_device</span> → the gateway records the lease under the session token'],
      ["s5.2+1.6", '<span class="mono">release_device</span> removes it']]);
    const c2 = card(sec, { x: 690, y: 400, w: 540, h: 480, in: "s5.3" }, "<h3>2 · Release at the end</h3>");
    rows(c2, [["s5.3+0.4", '<span class="mono">release_device</span> for every lease still held, with the stored header'],
      ["s5.3+1.8", '<span class="mono" style="font-size:24px;color:var(--green)">mcp_cleanup held=1 released=1 failed=0</span>']]);
    const c3 = card(sec, { x: 1280, y: 400, w: 540, h: 480, in: "s5.4" }, "<h3>3 · Crash sweep</h3>");
    rows(c3, [["s5.4+0.3", "Every 60 s: the leases of expired sessions"], ["s5.5", "Keeps trying for 24 h, then gives up"],
      ["s5.6", '<span style="color:var(--muted)">Otherwise: Seiji\'s idle reaper, 15 min by default</span>']]);
  },

  // 6. Watching from Tern
  s6(sec) {
    D.shot(sec, {
      x: 100, y: 205, w: 1720, h: 640, in: "s6.1", out: "s6.5",
      shots: [{ src: SHOT("sessions-seiji"), in: "s6.1", out: "s6.3" }, { src: SHOT("sessions-seiji-control"), in: "s6.3" }],
      cam: [["s6.1", 1900, 690, 0.9], ["s6.3", 935, 1640, 0.95], ["s6.4", 1890, 1640, 0.95]],
      spots: [
        { x: 2030, y: 796, w: 810, h: 42, in: "s6.1+0.6", out: "s6.2" },
        { x: 2200, y: 430, w: 168, h: 40, in: "s6.2+0.2", out: "s6.3" },
        { x: 40, y: 1594, w: 2805, h: 190, in: "s6.3+1.0" },
      ],
    });
    D.callout(sec, { x: 130, y: 770, in: "s6.1+0.3", out: "s6.2" }, 'Device: <span class="mono">iphone-16 on seiji · l_3f9c2a7b1e04</span>');
    D.callout(sec, { x: 130, y: 770, in: "s6.2+0.2", out: "s6.3" }, "<b>w</b> Watch device · view only");
    D.callout(sec, { x: 130, y: 770, in: "s6.3+0.4", out: "s6.5" }, "<b>⇧W</b> Watch with control · asks first");
    D.term(sec, { x: 100, y: 205, w: 1720, in: "s6.5", out: "s6.7" }, "operator token", [
      { cmd: "symphony mcp viewer seiji l_3f9c2a7b1e04 --json", in: "s6.5+0.2" },
      { c: '{"url": "https://seiji.example.com/view/open/…", "expires_in": 60, "control": false}', in: "s6.5+1.4" },
    ]);
    chip(sec, 100, 420, "s6.5+2.4", "opens once, within 60 s", "blue", "s6.7");
    chip(sec, 100, 480, "s6.6", "a lease no live session holds → 404 lease_not_held", "red", "s6.7");
    chip(sec, 100, 540, "s6.6+1.5", "operator role · audited as mcp.viewer", "", "s6.7");
    const tb = card(sec, { x: 100, y: 205, w: 1720, h: 600, in: "s6.7" }, "<h3>Two plugins in one Tern</h3>");
    const grid = Engine.h("div", { style: { display: "grid", gridTemplateColumns: "300px 1fr 1fr", gap: "12px 24px", fontSize: "28px", marginTop: "14px" } });
    tb.append(grid);
    [["", "Symphony plugin", "Seiji plugin"], ["Plugin id", "symphony", "seiji"], ["Panel keys", "w, ⇧W (watch)", "o, f (screen, float)"],
     ["Commands", "plugin.symphony.*", "plugin.seiji.*"], ["Devices", "the agents' (Symphony's user)", "your own"]].forEach((r, i) => r.forEach((c, j) => {
      const el = Engine.h("span", { class: i && j ? "mono" : "", text: c, style: { color: i === 0 || j === 0 ? "var(--muted)" : "var(--text)" } });
      grid.append(el);
      Engine.anim(el, { in: t("s6.7", 0.2 + i * 0.4) });
    }));
  },

  // 7. Proof and limits
  s7(sec) {
    const m = card(sec, { x: 100, y: 205, w: 560, h: 590, in: "s7.1" }, "<h3>Mocked scenario 13</h3>");
    rows(m, [
      ["s7.1+0.3", '<span class="big" style="color:var(--green);font-size:56px">55.9 s</span> every check passed'],
      ["s7.2", '<span class="mono" style="font-size:24px">mcp_cleanup held=1 released=1</span><br>0.02 s after the attempt'],
      ["s7.3", "13 requests at the mock, all with the stored token. No other bearer."],
    ]);
    const r = card(sec, { x: 690, y: 205, w: 560, h: 590, in: "s7.4" }, "<h3>Real Seiji (opt-in test)</h3>");
    rows(r, [
      ["s7.4+0.3", "Through the gateway: protocol <span class=\"mono\">2025-11-25</span>, 23 tools"],
      ["s7.4+1.3", "omp 18.20.2 lists the 23 <span class=\"mono\">mcp__seiji_</span> tools"],
      ["s7.4+2.3", '<span class="mono">budget_status</span>, <span class="mono">/v1/me</span>, an upload'],
      ["s7.5", 'Direct at Seiji: <span class="chip red">mcp_… → 401</span> <span class="chip green">sj_… → 200</span>'],
    ]);
    const l = card(sec, { x: 1280, y: 205, w: 540, h: 590, in: "s7.6" }, "<h3 style=\"color:var(--amber)\">Limits</h3>", { borderColor: "var(--amber)" });
    rows(l, [
      ["s7.6+0.2", "No real device booted yet: the test Mac has no simulator runtime"],
      ["s7.7", "One Seiji user for all agents: one budget, one <span class=\"mono\">maxConcurrent</span>"],
      ["s7.8", "Kubernetes: a public TLS name in <span class=\"mono\">listen.allowedHosts</span>"],
    ]);
    D.callout(sec, { x: 100, y: 815, in: "s7.9", style: { fontSize: "32px" } }, 'Guide: <span class="mono">docs/seiji.md</span>');
  },
};
