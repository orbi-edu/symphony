// How Symphony is proven: nine scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts and figures come from docs/testing.md, docs/mocked-certification.md, docs/certification.md,
// docs/tern.md (Tests) and scripts/mocks/specs/SOURCES.md; the test counts from `pytest --collect-only`.

const SHOT = (name) => `../../tern/${name}-dark.png`;
// The stylesheet draws every screenshot 2880x1920; a taller one (the Dashboard) keeps its own height.
const natural = (vp) => {
  for (const img of vp.querySelectorAll("img")) img.style.height = "auto";
  return vp;
};

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const note = (parent, x, y, ref, text, o = {}) =>
  D.abs(parent, "div", { x, y, in: ref, w: o.w, out: o.out }, { html: text, style: { fontSize: "28px", color: "var(--muted)", ...(o.style || {}) } });
const t = (ref, d) => Engine.at(ref) + d;

// A list of rows: [label, text, figure]; each row fades in at its own ref.
function table(parent, x, y, w, list, o = {}) {
  const h = o.h ?? 84;
  const gap = o.gap ?? 96;
  list.forEach(([k, text, fig, ref, figCls], i) => D.abs(parent, "div", { x, y: y + i * gap, w, h, in: ref }, {
    class: "node",
    html: `<div style="display:flex;gap:22px;align-items:center;font-size:28px;line-height:1.25"><b class="mono" style="min-width:${o.kw ?? 70}px;color:var(--blue)">${k}</b><span>${text}</span>${fig ? `<span class="chip ${figCls || "green"}" style="margin-left:auto">${fig}</span>` : ""}</div>`,
    style: { padding: "0 22px" },
  }));
}

const tile = (parent, x, y, w, ref, big, text, color = "var(--green)", h = 200) => D.abs(parent, "div", { x, y, w, h, in: ref, kind: "pop" }, {
  class: "card", html: `<div class="big" style="color:${color}">${big}</div><p style="margin-top:8px">${text}</p>`,
});

const SCENES = {
  // 1. Layers
  s1(sec) {
    const layers = [
      ["s1.1+1.6", "Default suite", "2,184 tests · no network · no model call", "trusted"],
      ["s1.1+2.8", "Mocked end to end", "vendor mocks · real agents · scenarios 1–12", "merger"],
      ["s1.1+4.3", "Real infrastructure", "a kind cluster · a real GitHub repository", "good"],
      ["s1.1+5.6", "Tern, headless", "46 plugin tests", "people"],
    ];
    layers.forEach(([ref, title, sub, cls], i) => D.box(sec, { x: 100 + i * 60, y: 230 + i * 160, w: 1600 - i * 120, h: 130, cls, in: ref,
      title: `<span style="font-size:34px">${title}</span> <span style="font-size:28px;color:var(--muted);font-weight:500">&nbsp;${sub}</span>` }));
  },

  // 2. The default suite
  s2(sec) {
    tile(sec, 100, 220, 560, "s2.1", "2,184", "tests collected", "var(--blue)", 274);
    D.term(sec, { x: 720, y: 220, w: 1100, in: "s2.1+0.3" }, "Terminal", [
      { cmd: "uv sync --extra dev", in: "s2.1+0.5" },
      { cmd: "uv run pytest", in: "s2.1+1.2" },
      { cmd: "uv run pytest --collect-only -q -o addopts=\"\" | tail -1", in: "s2.1+1.8" },
      { c: "2184 tests collected", in: "s2.1+2.6" },
    ]);
    D.abs(sec, "div", { x: 100, y: 520, w: 1720, in: "s2.2" }, { html: '<div class="chips"><span class="chip">Python 3.12</span><span class="chip">uv</span><span class="chip">git</span><span class="chip blue">redis-server: a real, disposable Redis</span><span class="chip green">no network</span><span class="chip green">no model call</span></div>' });
    const areas = ["every /v1 route", "daemon", "runner", "trackers", "Slack and Discord", "merger", "deploy/ against the code", "the mocks: 137 tests"];
    areas.forEach((a, i) => D.abs(sec, "div", { x: 100 + (i % 4) * 432, y: 640 + Math.floor(i / 4) * 110, w: 410, h: 90, in: t("s2.3", 0.2 + i * 0.3) }, {
      class: "node center", html: `<div style="font-size:28px">${a}</div>`, style: { padding: "0 12px" },
    }));
  },

  // 3. Vendor mocks
  s3(sec) {
    const A = D.layer(sec, { in: "s3", out: "s3.2" });
    const mocks = [
      ["Linear", "4100", "linear.graphql"], ["GitHub", "4200", "api.github.com OpenAPI · schema.docs.graphql"], ["Slack", "4300", "slack_web_openapi_v2.json"],
      ["Models", "4400", "Anthropic Messages · OpenAI OpenAPI"], ["Discord", "4500", "discord-openapi.json"], ["MCP", "4600", "MCP 2025-11-25 schema"],
    ];
    mocks.forEach(([name, port, spec], i) => D.box(A.el, { x: 100 + (i % 3) * 580, y: 230 + Math.floor(i / 3) * 260, w: 550, h: 220, cls: "ext", in: t("s3.1", 0.2 + i * 0.3),
      kick: `port ${port}`, title: name, body: (() => { const s = Engine.h("div", { class: "s mono", text: spec, style: { fontSize: "26px", color: "var(--amber)" } }); Engine.anim(s, { in: t("s3.1", 2.2 + i * 0.25), lift: 6 }); return s; })() }));
    note(A.el, 100, 770, "s3.1+3.6", "scripts/mocks/specs/ · for tests and certification only", { w: 1720, style: { fontFamily: "var(--mono)" } });

    const B = D.layer(sec, { in: "s3.2", out: "s3.3" });
    D.term(B.el, { x: 100, y: 220, w: 1720, in: "s3.2" }, "scripts/mocks/specs/SOURCES.md", [
      { c: "File                           Commit or fetch date                       sha256", in: "s3.2+0.2" },
      { c: "linear.graphql                 linear/linear@87d638c0…                    20cadd50…", in: "s3.2+0.6" },
      { c: "github-api.github.com.json.gz  github/rest-api-description@2a33d9cc…      f7a3de68…", in: "s3.2+0.9" },
      { c: "slack_web_openapi_v2.json      slackapi/slack-api-specs@dfea73e0…         742a5c97…", in: "s3.2+1.2" },
      { c: "discord-openapi.json.gz        discord/discord-api-spec@3a21a012…         ef338088…", in: "s3.2+1.5" },
      { c: "mcp-2025-11-25-schema.json.gz  modelcontextprotocol@ac00313f…             268a5f82…", in: "s3.2+1.8" },
      { c: "anthropic-messages.md.gz       fetched 2026-11-16                         6a90747d…", in: "s3.2+2.1" },
    ]);
    note(B.el, 100, 630, "s3.2+2.6", "48 vendored files, each with its source URL", { w: 1720 });

    const C = D.layer(sec, { in: "s3.3" });
    const req = D.box(C.el, { x: 100, y: 230, w: 360, h: 120, cls: "trusted center", in: "s3.3", title: "a request" });
    const val = D.box(C.el, { x: 560, y: 230, w: 460, h: 120, cls: "center", in: "s3.3+0.4", title: "checked against the spec" });
    D.wire(C.el, C.sv, req, val, { in: "s3.3+0.3", color: "#5cb8ff" });
    const errs = card(C.el, { x: 1100, y: 210, w: 720, h: 270, in: "s3.3+1.2" }, "<h3>Outside the spec: the documented error</h3>");
    [["Linear", "HTTP 400, errors[].extensions"], ["GitHub", "404 or 422, documentation_url"], ["Slack", "HTTP 200, ok: false"], ["Discord", "4xx {code, message}"]].forEach(([k, v], i) => {
      const r = Engine.h("p", { html: `<b style="display:inline-block;min-width:130px">${k}</b><span class="mono" style="font-size:26px">${v}</span>`, style: { lineHeight: "1.6" } });
      errs.append(r);
      Engine.anim(r, { in: t("s3.3", 1.5 + i * 0.4), lift: 6 });
    });
    D.abs(C.el, "div", { x: 100, y: 400, w: 920, in: "s3.4" }, { html: '<span class="chip amber">501</span> &nbsp;<span style="font-size:28px">Linear · GitHub · Slack · Discord · MCP</span><br><span style="font-size:28px;color:var(--muted)">an operation of the spec that the mock does not serve</span>' });
    const auth = card(C.el, { x: 100, y: 570, w: 1720, h: 230, in: "s3.5" }, "<h3>Authentication and signatures, as documented</h3>");
    ["Linear: <span class=\"mono\">Authorization: &lt;API key&gt;</span>; Bearer refused",
      "GitHub: App JWT → a scoped installation token; a read-only token cannot push (403)",
      "Slack <span class=\"mono\">Bearer xoxb-…</span> · Discord <span class=\"mono\">Bot &lt;token&gt;</span> · <span class=\"mono\">Linear-Signature</span>, <span class=\"mono\">X-Hub-Signature-256</span>"].forEach((h, i) => {
      const r = Engine.h("p", { html: h, style: { lineHeight: "1.6" } });
      auth.append(r);
      Engine.anim(r, { in: t("s3.5", 0.4 + i * 0.5), lift: 6 });
    });
  },

  // 4. The mocked harness
  s4(sec, sv) {
    const A = D.layer(sec, { in: "s4", out: "s4.3" });
    const drv = D.box(A.el, { x: 100, y: 330, w: 460, h: 200, cls: "people", in: "s4.1", kick: "Driver", title: '<span class="mono" style="font-size:26px">scripts/e2e_mocked/run.py</span>', sub: "acts as octocat, through the mocks" });
    const sym = ["Redis", "Symphony local mode", "Merger acme/app"].map((n, i) => D.box(A.el, { x: 660, y: 230 + i * 150, w: 440, h: 120, cls: "trusted center", in: t("s4.1", 0.4 + i * 0.4), title: `<span style="font-size:30px">${n}</span>` }));
    const mocks = ["Linear", "GitHub", "Slack", "Discord", "MCP"].map((n, i) => D.box(A.el, { x: 1240, y: 220 + i * 95, w: 300, h: 80, cls: "ext center", in: t("s4.1", 1.6 + i * 0.25), title: `<span style="font-size:28px">${n} mock</span>` }));
    const bed = D.box(A.el, { x: 1240, y: 720, w: 580, h: 120, cls: "good center", in: "s4.2", title: '<span style="font-size:30px">Amazon Bedrock · real model</span>', sub: "through the pinned omp 18.20.2" });
    D.wire(A.el, A.sv, drv, sym[1], { in: "s4.1+0.8", color: "#f5b452" });
    mocks.forEach((m, i) => D.wire(A.el, A.sv, sym[1], m, { in: t("s4.1", 1.7 + i * 0.25), color: "#5b6b82", width: 2 }));
    D.wire(A.el, A.sv, sym[2], bed, { in: "s4.2+0.3", color: "#3ddc97" });
    note(A.el, 600, 700, "s4.1+2.8", "all on 127.0.0.1, free ports", { w: 480 });
    D.abs(A.el, "div", { x: 100, y: 600, w: 440, in: "s4.2+1.2" }, { html: '<span class="chip blue">checks read /_mock state</span><br><br><span style="font-size:28px;color:var(--muted)">tickets, comments, PRs, tokens, messages</span>' });

    const B = D.layer(sec, { in: "s4.3" });
    table(B.el, 100, 220, 1720, [
      ["", "Default: Linear and Slack", "1 to 8", "s4.3+0.2", "blue"],
      ["", "--tracker github --notify discord", "1, 3, 4, 7", "s4.3+0.6", "blue"],
      ["", "--assistant (Slack, Linear; MCP)", "9, 10, 12", "s4.3+1.0", "blue"],
      ["", "--assistant --tracker github --notify discord", "11", "s4.3+1.4", "blue"],
    ], { kw: 0 });
    D.abs(B.el, "div", { x: 100, y: 640, in: "s4.4" }, { html: '<span class="chip green">every check passed</span> &nbsp;<span class="chip green">0 requests outside the GitHub spec</span>' });
    note(B.el, 100, 720, "s4.4+1.2", "First full run: 2026-11-16, 8 scenarios in 265 s. GitHub and Discord: 181 s.", { w: 1720 });
  },

  // 5. Scenarios 1 to 6
  s5(sec) {
    const A = D.layer(sec, { in: "s5", out: "s5.4" });
    D.abs(A.el, "div", { x: 100, y: 210, in: "s5.1" }, { html: '<b style="font-size:32px">1 · Implement and merge</b> <span style="font-size:28px;color:var(--muted)">&nbsp;ENG-1, Linear</span>' });
    tile(A.el, 100, 290, 400, "s5.1+0.4", "44 ms", "webhook → the session started", "var(--blue)");
    tile(A.el, 540, 290, 400, "s5.2", "18.8 s", "a ready PR, after an answer through the API", "var(--blue)");
    tile(A.el, 980, 290, 400, "s5.3", "64.8 s", "merged, from the merger start (Linear)");
    tile(A.el, 1420, 290, 400, "s5.3+1.6", "55.8 s", "merged, from the merger start (GitHub)");
    card(A.el, { x: 100, y: 540, w: 1720, h: 300, in: "s5.2+1.0" },
      '<p>✓ the token covered only <span class="mono">acme/app</span> · ✓ the Workpad went through <span class="mono">linear_graphql</span></p><p style="margin-top:10px">✓ the mock CI passed · ✓ the merger reviewed, then the train merged with rebase</p><p style="margin-top:10px">✓ the ticket moved to Merging · ✓ no error reached Slack</p><p style="margin-top:10px;color:var(--muted)">1 turn, 75,044 tokens, about US$ 0.13</p>');

    const B = D.layer(sec, { in: "s5.4" });
    table(B.el, 100, 220, 1720, [
      ["2", "One ticket, two repositories: one token, a PR in each", "31.0 s", "s5.4+0.2"],
      ["3", "Investigation: read-only token; push and PR refused with 403", "15.7 s", "s5.4+1.6"],
      ["4", "Sub-issues with a blocks relation, evidence attached; on GitHub, binary refused", "16.1 s", "s5.4+3.4"],
      ["5", "A person's inline comment → an answer run → a reply in the thread", "9.7 s", "s5.5"],
      ["6", "The retrospective opens a learnings PR on AGENTS.md", "7.5 s", "s5.5+2.4"],
    ], { gap: 110 });
  },

  // 6. Scenarios 7 to 12
  s6(sec) {
    const A = D.layer(sec, { in: "s6", out: "s6.7" });
    table(A.el, 100, 210, 1720, [
      ["7", "Red main → one ticket with the log; green → Done", "2.3 s", "s6.1"],
      ["8", "A real bug → draft, review-requested, ticket in Rework", "30.3 s", "s6.2"],
      ["9", "Ask on Slack → an answer with a cited line", "10.1 s", "s6.3"],
      ["11", "Ask on the Discord Gateway, GitHub tracker", "11.1 s", "s6.3+1.0"],
      ["10", "Plan on Slack → a mapped click makes 3 tickets; an unmapped one is refused", "0.51 s", "s6.4"],
      ["12", "MCP through the gateway: stored headers reach the mock; the agent's token does not", "16.1 s", "s6.5"],
    ], { gap: 100 });
    D.abs(A.el, "div", { x: 100, y: 830, in: "s6.6" }, { html: '<span class="chip amber">7 defects found and fixed</span> &nbsp;<span style="font-size:28px">each with a regression test</span>' });

    const B = D.layer(sec, { in: "s6.7" });
    card(B.el, { x: 100, y: 230, w: 1720, h: 340, in: "s6.7" },
      '<h3 style="color:var(--amber)">What the mocked run does not certify</h3><p style="margin-top:14px">• Real vendors: rate limits under real load, webhook retries</p><p style="margin-top:10px">• The Linear–GitHub integration and GitHub\'s closing keywords</p><p style="margin-top:10px">• Kubernetes: pods, Jobs, NetworkPolicies</p><p style="margin-top:10px">• Slack and Discord by incoming webhook; a user-owned Projects board</p><p style="margin-top:10px">• Blocked dispatch: admission was paused while the sub-issues existed</p>',
      { borderColor: "var(--amber)" });
  },

  // 7. Kubernetes on kind
  s7(sec) {
    D.abs(sec, "div", { x: 100, y: 210, in: "s7.1" }, { html: '<span class="chip blue">E2E_TEARDOWN=1 scripts/e2e_kind.sh</span> &nbsp;<span style="font-size:28px;color:var(--muted)">kind v0.33 · omp 18.20.2 · Amazon Bedrock</span>' });
    const parts = [["cluster 34 s", 34, "#5cb8ff"], ["images 68 s", 68, "#b197fc"], ["15 s", 15, "#3fd3e8"], ["attempt 155 s", 155, "#3ddc97"]];
    const W = 1720 / 275;
    let x = 100;
    parts.forEach(([name, s, color], i) => {
      D.abs(sec, "div", { x, y: 300, w: s * W - 6, h: 70, in: t("s7.2", 0.2 + i * 0.4), kind: "wipe", dur: 0.5 }, {
        html: `<div style="font-size:28px;font-weight:700;color:#06110c;padding:16px 12px;white-space:nowrap;overflow:hidden">${name}</div>`,
        style: { background: color, borderRadius: "10px" },
      });
      x += s * W;
    });
    note(sec, 100, 390, "s7.2+1.8", "275 s, exit code 0, from an empty machine to a deleted cluster · rollouts 15 s", { w: 1720 });
    table(sec, 100, 460, 1000, [
      ["", "ticket → runner Job", "7.7 s", "s7.3", "blue"],
      ["", "→ workspace Pod and sessions PVC", "9.0 s", "s7.3+0.6", "blue"],
      ["", "→ agent_start on the API SSE stream", "15.3 s", "s7.3+1.2", "blue"],
      ["", "refresh → second attempt, same Pod and PVC", "2.8 s", "s7.5", "blue"],
    ], { kw: 0, gap: 100 });
    tile(sec, 1160, 460, 660, "s7.4", "3 ms", "event to the host, p50 (p95 6 ms, max 15 ms)", "var(--green)", 180);
    tile(sec, 1160, 660, 660, "s7.4+1.6", "13 ms", "steer → intent applied", "var(--green)", 180);
    D.abs(sec, "div", { x: 100, y: 870, in: "s7.6" }, { html: '<span class="chip amber">memory tracker</span> <span class="chip amber">max_turns: 1</span> <span class="chip amber">no merger by default</span>' });
  },

  // 8. Merger certification
  s8(sec) {
    const board = card(sec, { x: 100, y: 205, w: 1150, h: 420, in: "s8.1" }, "<h3>A private test repository, GitHub Actions CI, real agents</h3>");
    [["a", "Clean PR → merged at the reviewed head", "s8.2", "pass"],
      ["b", "Real bug → reviewer and oracle agree → draft once", "s8.2+0.6", "pass"],
      ["d", "Batch of 3, CI on the top only", "s8.2+1.2", "pass"],
      ["c", "Two conflicting PRs → agents resolve → merged", "s8.2+2.4", "pass, 3rd round"],
      ["e", "Batch on a protected main", "s8.3", "failed: bug 7"],
      ["f", "Batch of 3 on a protected main, after the fix", "s8.3+1.4", "pass"]].forEach(([k, text, ref, res]) => {
      const ok = res.startsWith("pass");
      const r = Engine.h("div", { html: `<span class="mono" style="color:var(--muted);display:inline-block;width:36px">${k}</span>${text} &nbsp;<span style="font-weight:700;color:${ok ? "var(--green)" : "var(--amber)"}">${ok ? "✓ " : ""}${res}</span>`, style: { fontSize: "28px", lineHeight: "1.45", marginTop: "14px" } });
      board.append(r);
      Engine.anim(r, { in: ref, lift: 6 });
    });
    tile(sec, 1290, 205, 530, "s8.4", "10–40 s", "a review with no findings (2 lanes)", "var(--blue)", 200);
    tile(sec, 1290, 425, 530, "s8.4+2.4", "8 bugs", "found and fixed, each with a regression test", "var(--amber)", 200);
    D.abs(sec, "div", { x: 100, y: 670, w: 1720, in: "s8.5" }, { html: '<span class="chip amber">user token, no GitHub App</span> <span class="chip amber">no webhook: poll every 10 s</span> <span class="chip amber">tracker.jsonl</span> <span class="chip amber">notify.jsonl</span>' });
    D.abs(sec, "div", { x: 100, y: 740, w: 1720, in: "s8.6" }, { html: '<span style="font-size:28px;color:var(--muted)">Main watch: main stayed green. Tests cover red → ticket → green.</span>' });
  },

  // 9. Tern
  s9(sec) {
    D.abs(sec, "div", { x: 100, y: 205, w: 1720, in: "s9.1+0.6" }, { html: '<span class="chip blue">tern serve</span> <span class="chip">an isolated TERN_CONFIG_DIR</span> <span class="chip">the plugin as a fixture</span>' });
    natural(D.shot(sec, {
      x: 100, y: 270, w: 1720, h: 420, in: "s9.1",
      shots: [{ src: SHOT("dashboard"), in: "s9.1" }],
      cam: [["s9.1", 1440, 540, 0.597]],
    }));
    card(sec, { x: 100, y: 720, w: 560, h: 140, in: "s9.2" }, '<p class="mono" style="font-size:26px">test_tern_plugin.py · 32</p><p>a fake API and a test CLI</p>');
    card(sec, { x: 690, y: 720, w: 560, h: 140, in: "s9.2+2.2" }, '<p class="mono" style="font-size:26px">test_tern_real_stack.py · 14</p><p>Redis, the real API and CLI</p>');
    chip(sec, 1290, 730, "s9.3", "plugin error → test fails", "red");
    chip(sec, 1290, 800, "s9.4", "scenarios.py → docs/tern/*.png", "green");
  },
};
