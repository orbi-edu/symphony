// "Why use Symphony?": five scenes. Every claim comes from README.md, docs/*.md or the certification docs.
// Time references point at narration cues (see ../lib/engine.js `at`).

const SHOT = (name) => `../../tern/${name}-dark.png`;
const BIG = { x: 100, y: 205, w: 1720, h: 560 }; // the visual area of scene 2
const t = (ref, d) => Engine.at(ref) + d;
const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const label = (parent, x, y, ref, text, out = null, style = {}) =>
  D.abs(parent, "div", { x, y, in: ref, out }, { text, style: { fontSize: "30px", color: "var(--muted)", ...style } });

const SCENES = {
  // 1. The problem
  s1(sec) {
    const lap = (x, y, who, ref, cls = "") => D.box(sec, { x, y, w: 380, h: 180, cls, in: ref, kick: who, title: "laptop", sub: '<span class="mono">omp</span> · its own keys' });
    lap(100, 240, "one person", "s1.1");
    chip(sec, 100, 450, "s1.1+1.0", "works well alone", "green");
    D.abs(sec, "div", { x: 560, y: 230, w: 4, h: 600, in: "s1.2" }, { style: { background: "#2b3648" } });
    label(sec, 620, 230, "s1.2", "A team");
    lap(620, 290, "alice", "s1.3");
    lap(1030, 290, "bob", "s1.3+0.3");
    lap(1440, 290, "octocat", "s1.3+0.6");
    chip(sec, 620, 500, "s1.3+1.6", "no shared view", "amber");
    const q = (y, ref, text) => D.abs(sec, "div", { x: 620, y, w: 1200, h: 86, in: ref, kind: "pop" }, {
      class: "node", html: `<div style="font-size:30px">${text}</div>`, style: { borderColor: "var(--amber)" },
    });
    q(570, "s1.4", "Who runs the agents, on which machine, with which keys?");
    q(670, "s1.5", "How do we follow, steer and review them?");
    q(770, "s1.6", "How do we merge safely, with credentials away from agent code?");
  },

  // 2. What you get: one visual per narrated point
  s2(sec) {
    const steps = ["Work orders", "Webhook → PR", "Pod per attempt", "Follow, steer", "AI review, train", "Models and keys", "Learnings", "Slack, Discord", "Assistant", "Checked setup"];
    steps.forEach((s, i) => {
      const ref = `s2.${i + 1}`;
      D.abs(sec, "span", { x: 100 + (i % 5) * 346, y: i < 5 ? 790 : 840, in: ref, kind: "pop", o: [[ref, 1], [`${ref}e+0.3`, 0.45]] },
        { class: "chip blue", text: `${i + 1}. ${s}`, style: { fontSize: "28px" } });
    });
    const L = (n) => D.layer(sec, { in: `s2.${n}`, out: `s2.${n + 1}` });

    // 1. Tickets become work orders.
    let a = L(1);
    D.box(a.el, { x: 100, y: 230, w: 760, h: 260, cls: "ext", in: "s2.1", kick: "Linear", title: "ENG-1 · Add the CSV export", sub: "state Todo", body: D.chips([["symphony", "blue"], ["repo:acme/app", "blue"]]) });
    D.box(a.el, { x: 960, y: 230, w: 860, h: 260, cls: "ext", in: "s2.1+0.8", kick: "GitHub Projects", title: "app-42 · Add the CSV export", sub: "Status column: Todo", body: D.chips([["symphony", "blue"]]) });
    label(a.el, 100, 540, "s2.1+1.6", "A ticket with the symphony label is a work order. Its state shows the progress.");

    // 2. Webhook → agent → ready pull request.
    a = L(2);
    const tk = D.box(a.el, { x: 100, y: 300, w: 380, h: 170, cls: "ext", in: "s2.2", title: "Ticket", sub: "ENG-1" });
    const ag = D.box(a.el, { x: 760, y: 300, w: 380, h: 170, cls: "ws", in: "s2.2+0.5", title: "Agent", sub: '<span class="mono">omp</span> in its pod' });
    const pr = D.box(a.el, { x: 1440, y: 300, w: 380, h: 170, cls: "good", in: "s2.2+1.8", title: "Pull request", sub: "ready for review" });
    const w1 = D.wire(a.el, a.sv, tk, ag, { in: "s2.2+0.3", label: "webhook", lo: [0, -30] });
    const w2 = D.wire(a.el, a.sv, ag, pr, { in: "s2.2+1.6", color: "#3ddc97" });
    D.packet(a.sv, w1, t("s2.2", 0.4), t("s2.2", 1.0));
    D.packet(a.sv, w2, t("s2.2", 1.7), t("s2.2", 2.3), "#3ddc97");
    label(a.el, 100, 540, "s2.2+0.8", "Measured in the mocked run: a session started 44 ms after the ticket.");

    // 3. One pod per attempt; keys stay outside.
    a = L(3);
    D.abs(a.el, "div", { x: 100, y: 230, w: 900, h: 400, in: "s2.3" }, { style: { border: "3px dashed var(--amber)", borderRadius: "22px", background: "rgba(245,180,82,0.05)" } });
    label(a.el, 130, 250, "s2.3", "Workspace pod · one attempt", null, { color: "var(--amber)", fontWeight: "700" });
    [["model credential of its role", 320], ["GitHub token, its repositories, ≤ 1 h", 400], ["telemetry token", 480], ["MCP session token", 560]].forEach(([c, y], i) =>
      chip(a.el, 140, y, t("s2.3", 0.4 + i * 0.3), c, "amber"));
    label(a.el, 1080, 250, "s2.3+1.6", "Stay outside", null, { color: "var(--red)", fontWeight: "700" });
    ["Linear API key", "GitHub App private key", "Slack and Discord tokens", "Symphony API tokens"].forEach((c, i) =>
      chip(a.el, 1080, 320 + i * 80, t("s2.3", 1.8 + i * 0.25), `✕ ${c}`, "red"));

    // 4. Follow and steer: the real live chat.
    D.shot(sec, {
      ...BIG, in: "s2.4", out: "s2.5",
      shots: [{ src: SHOT("real-chat"), in: "s2.4" }],
      cam: [["s2.4", 1375, 600, 0.95]],
      spots: [{ x: 540, y: 640, w: 700, h: 70, in: "s2.4+1.2" }],
    });
    D.callout(sec, { x: 130, y: 680, in: "s2.4+0.6", out: "s2.5" }, 'Live chat in Tern · steer, interrupt, take over · also <span class="mono">symphony</span> CLI and MCP');

    // 5. Review and merge: the real merger view.
    D.shot(sec, {
      ...BIG, in: "s2.5", out: "s2.6",
      shots: [{ src: SHOT("merger"), in: "s2.5" }],
      cam: [["s2.5", 905, 560, 0.95], ["s2.5+2.4", 905, 1080, 0.95]],
      spots: [{ x: 36, y: 446, w: 1750, h: 190, in: "s2.5+0.6", out: "s2.5+2.4" }, { x: 36, y: 990, w: 1750, h: 250, in: "s2.5+3.0" }],
    });
    D.callout(sec, { x: 130, y: 680, in: "s2.5+0.4", out: "s2.6" }, "Reviewer and oracle on different models · a deterministic merge train");

    // 6. Models per role and API keys.
    D.shot(sec, {
      ...BIG, in: "s2.6", out: "s2.7",
      shots: [{ src: SHOT("admin-models"), in: "s2.6", out: "s2.6+2.8" }, { src: SHOT("admin-providers"), in: "s2.6+2.8" }],
      cam: [["s2.6", 930, 600, 0.95], ["s2.6+2.8", 930, 470, 0.95]],
    });
    D.callout(sec, { x: 130, y: 680, in: "s2.6+0.4", out: "s2.6+2.6" }, "Admin › Models: one model and thinking level per role");
    D.callout(sec, { x: 130, y: 680, in: "s2.6+3.0", out: "s2.7" }, "Admin › Providers: API keys are write-only");

    // 7. Learnings pull request.
    a = L(7);
    card(a.el, { x: 100, y: 240, w: 1100, h: 300, in: "s2.7" },
      '<h3>learnings: ENG-1</h3><p class="mono" style="color:var(--muted)">branch symphony/learnings/ENG-1</p><div class="chips"><span class="chip violet">symphony-learnings</span><span class="chip">AGENTS.md</span><span class="chip">skills</span></div><p style="margin-top:16px">The reflector proposes at most three lessons. It never merges.</p>');
    chip(a.el, 1260, 300, "s2.7+2.4", "a person reviews and merges", "green");

    // 8. Notices.
    a = L(8);
    card(a.el, { x: 100, y: 240, w: 820, h: 240, in: "s2.8" }, '<h3>Slack</h3><p>🙋 <b>Agent is waiting for an answer</b><br>ENG-1 · acme/app</p>');
    card(a.el, { x: 1000, y: 240, w: 820, h: 240, in: "s2.8+0.6" }, '<h3>Discord</h3><p>↩️ <b>PR returned to its author</b><br>acme/app #4 · ENG-8</p>');
    label(a.el, 100, 520, "s2.8+1.2", "Questions, rejections, red main and failed runs reach every configured channel.");

    // 9. The assistant.
    a = L(9);
    const th = card(a.el, { x: 100, y: 230, w: 1720, h: 300, in: "s2.9" }, '<p class="mono" style="color:var(--muted)">#eng-help · thread</p>');
    const msg = (ref, who, html) => { const el = Engine.h("p", { html: `<b>${who}</b> &nbsp;${html}`, style: { marginTop: "12px", fontSize: "28px" } }); th.append(el); Engine.anim(el, { in: ref, lift: 8 }); };
    msg("s2.9+0.2", "bob", "@symphony plan add rate limits to the public API of acme/app");
    msg("s2.9+1.2", "symphony", 'Proposal: 3 tickets &nbsp;<span class="chip green">Create</span> <span class="chip blue">Create and start</span> <span class="chip">Dismiss</span>');
    label(a.el, 100, 560, "s2.9+2.0", "Read-only tools. A person mapped in notify.users confirms each proposal.");

    // 10. The first-run setup.
    D.shot(sec, {
      ...BIG, in: "s2.10",
      shots: [{ src: SHOT("setup-verifying"), in: "s2.10" }],
      cam: [["s2.10", 990, 450, 0.88]],
      spots: [{ x: 25, y: 290, w: 1930, h: 318, in: "s2.10+0.8" }],
    });
    D.callout(sec, { x: 130, y: 680, in: "s2.10+0.4" }, 'Tern › Setup, or <span class="mono">symphony setup</span>: each value verified before it is saved');
  },

  // 3. Why trust it
  s3(sec) {
    const col = (x, w, ref, title) => D.abs(sec, "div", { x, y: 205, w, h: 680, in: ref }, { class: "card", html: `<h3>${title}</h3>` });
    const c1 = col(100, 520, "s3.1", "Vendor mocks");
    [["Linear", "GraphQL, webhooks"], ["GitHub", "REST, GraphQL, git, CI"], ["Slack", "Web API"], ["Discord", "webhooks, messages"], ["Model APIs", "streams"]].forEach(([n, s], i) => {
      const r = Engine.h("p", { html: `<b>${n}</b> <span style="color:var(--muted)">${s}</span>`, style: { fontSize: "28px", lineHeight: "1.8" } });
      c1.append(r);
      Engine.anim(r, { in: t("s3.1", 0.3 + i * 0.25), lift: 6 });
    });
    const n1 = Engine.h("p", { text: "A request outside the specification gets the vendor's documented error.", style: { marginTop: "14px", color: "var(--muted)" } });
    c1.append(n1);
    Engine.anim(n1, { in: t("s3.1", 1.8) });

    col(660, 640, "s3.2", "Certified runs");
    const tile = (y, ref, big, text, color) => D.abs(sec, "div", { x: 690, y, w: 580, h: 118, in: ref, kind: "pop" }, {
      html: `<div style="display:flex;gap:20px;align-items:center"><div class="big" style="color:${color};font-size:52px;min-width:170px">${big}</div><p style="font-size:28px">${text}</p></div>`,
    });
    tile(270, "s3.2+0.2", "8 / 8", "Linear and Slack · 278.4 s · real agents", "var(--green)");
    tile(390, "s3.2+1.4", "4 / 4", "GitHub Projects and Discord · 182.9 s", "var(--green)");
    tile(510, "s3.3", "real", "merger on a real GitHub repository, GitHub Actions CI", "var(--blue)");
    tile(630, "s3.4", "15", "defects found and fixed, each with a regression test", "var(--amber)");
    tile(750, "s3.5", "275 s", "kind: empty machine to deleted cluster", "var(--blue)");

    const c3 = col(1340, 480, "s3.6", "Yours to run");
    [["MIT License", "s3.6+0.2"], ["self-hosted", "s3.6+0.8"], ["API keys or cloud credentials", "s3.7"], ["no OAuth, no subscription login", "s3.7+1.2"]].forEach(([s, ref]) => {
      const r = Engine.h("p", { html: `<span class="tick">✓</span> ${s}`, style: { fontSize: "28px", lineHeight: "1.9" } });
      c3.append(r);
      Engine.anim(r, { in: ref, lift: 6 });
    });
  },

  // 4. Who it is for, and when not
  s4(sec) {
    const fits = D.abs(sec, "div", { x: 100, y: 205, w: 820, h: 680, in: "s4.1" }, { class: "card", html: '<h3 style="color:var(--green)">It fits</h3>', style: { borderColor: "var(--green)" } });
    const know = D.abs(sec, "div", { x: 1000, y: 205, w: 820, h: 680, in: "s4.3" }, { class: "card", html: '<h3 style="color:var(--amber)">Know before you start</h3>', style: { borderColor: "var(--amber)" } });
    const row = (parent, ref, html) => { const r = Engine.h("p", { html, style: { fontSize: "30px", lineHeight: "1.4", marginTop: "22px" } }); parent.append(r); Engine.anim(r, { in: ref, lift: 6 }); };
    row(fits, "s4.1+0.2", "A team that plans work in <b>tickets</b> and reviews code in <b>pull requests</b>.");
    row(fits, "s4.1+1.6", "People watch and steer instead of running agents by hand.");
    row(fits, "s4.2", 'Want a person on disputes? <span class="mono">require_human: true</span> sends them to a maintainer.');
    row(know, "s4.3+0.2", "Code on <b>GitHub</b>. Tracker: <b>Linear</b> or <b>GitHub Projects</b>.");
    row(know, "s4.4", "<b>Kubernetes</b> with a NetworkPolicy CNI. One VM with k3s is enough.");
    row(know, "s4.5", "<b>Local mode</b>: one machine, no container. For trying it out.");
    row(know, "s4.6", "Linear, Slack and Discord: certified against <b>mocks</b>, not the live services.");
  },

  // 5. Get started
  s5(sec) {
    D.term(sec, { x: 100, y: 205, w: 1720, in: "s5.1", out: "s5.4" }, "Kubernetes or k3s", [
      { cmd: "kubectl apply -k deploy/", in: "s5.1+0.3" },
      { cmd: "symphony login --url http://127.0.0.1:8080 --token -", in: "s5.1+1.2" },
      { cmd: "symphony setup", in: "s5.2+0.2" },
    ]);
    D.shot(sec, {
      x: 100, y: 460, w: 1720, h: 425, in: "s5.2", out: "s5.4",
      shots: [{ src: SHOT("setup-ready"), in: "s5.2" }],
      cam: [["s5.2", 860, 410, 1.0]],
      spots: [{ x: 25, y: 245, w: 1660, h: 330, in: "s5.3" }],
    });
    D.abs(sec, "div", { x: 0, y: 300, w: 1920, in: "s5.4+0.1" }, { class: "big-word", text: "Symphony", style: { textAlign: "center", fontSize: "150px" } });
    D.abs(sec, "div", { x: 0, y: 520, w: 1920, in: "s5.4+0.4" }, { class: "mono", text: "docs/videos.md · docs/install.md", style: { textAlign: "center", fontSize: "38px", color: "var(--blue)" } });
    D.abs(sec, "div", { x: 0, y: 600, w: 1920, in: "s5.4+0.7" }, { text: "MIT License · self-hosted", style: { textAlign: "center", fontSize: "32px", color: "var(--muted)" } });
  },
};
