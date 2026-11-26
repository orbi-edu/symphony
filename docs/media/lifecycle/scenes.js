// One ticket, end to end: twelve scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/concepts.md, docs/internals.md, docs/architecture.md, docs/providers.md, WORKFLOW.md,
// CHANGELOG.md and src/symphony/{orchestrator,runner,prompt,reflector,telemetry,intake,triage,questions,hosttools}.py.
// The example ticket ENG-1 and its pull request are those of docs/mocked-certification.md (scenario 1); the figures
// are from docs/certification.md and docs/mocked-certification.md.

const SHOT = (name) => `../../tern/${name}-dark.png`;

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const t = (ref, d) => Engine.at(ref) + d;
const muted = (parent, o, text, size = 28) => D.abs(parent, "div", o, { text, style: { fontSize: `${size}px`, color: "var(--muted)" } });
// One line in a node: [ref, html, extra style].
const row = (parent, o, html, style = {}) =>
  D.abs(parent, "div", o, { class: "node", html: `<div class="rowline">${html}</div>`, style: { padding: "0 22px", ...style } });

// A comment as GitHub or Linear renders it (dark). `parts`: [ref, html, out] blocks in order.
function comment(parent, o, header, parts) {
  const el = D.abs(parent, "div", o, { class: "ghc" });
  el.append(Engine.h("div", { class: "hd", html: header }));
  const bd = Engine.h("div", { class: "bd" });
  el.append(bd);
  for (const [ref, html, out] of parts) {
    const p = Engine.h("div", { html });
    bd.append(p);
    Engine.anim(p, { in: ref, out, lift: 8 });
  }
  return el;
}

// Source lines (a file view): [ref, html] each; `title` on top.
function source(parent, o, title, lines) {
  const el = D.abs(parent, "div", o, { class: "term" });
  el.append(Engine.h("div", { class: "bar", text: title }));
  for (const [ref, html] of lines) {
    const ln = Engine.h("div", { class: "ln yaml", html });
    el.append(ln);
    Engine.anim(ln, { in: ref, lift: 6 });
  }
  return el;
}

// A Slack or Discord message.
function message(parent, o, who, html) {
  return D.abs(parent, "div", o, { class: "msg", html: `<span class="who">${who}</span><span class="app">APP</span><span class="when">now</span>${html}` });
}

const SCENES = {
  // 1. The whole path
  s1(sec, sv) {
    const top = 250;
    const bot = 520;
    const tracker = D.box(sec, { x: 100, y: top, w: 380, h: 190, cls: "ext", in: "s1.2", kick: "Tracker", title: "ENG-1",
      sub: "Linear, or GitHub Projects" });
    const daemon = D.box(sec, { x: 560, y: top, w: 360, h: 190, cls: "trusted", in: "s1.3", kick: "Daemon", title: "dispatch", sub: "one tick at a time" });
    const runner = D.box(sec, { x: 1000, y: top, w: 360, h: 190, cls: "trusted", in: "s1.3+1.0", kick: "Runner Job", title: "one attempt", sub: "drives the agent" });
    const ws = D.box(sec, { x: 1440, y: top, w: 380, h: 190, cls: "ws", in: "s1.4", kick: "Workspace pod", title: "omp agent", sub: "clone of acme/app" });
    const pr = D.box(sec, { x: 1440, y: bot, w: 380, h: 190, cls: "ext", in: "s1.4+1.6", kick: "acme/app", title: "PR #1", sub: "ready for review" });
    const merger = D.box(sec, { x: 1000, y: bot, w: 360, h: 190, cls: "merger", in: "s1.5", kick: "Merger", title: "review, train", sub: "AI agents review" });
    const done = D.box(sec, { x: 560, y: bot, w: 360, h: 190, cls: "good", in: "s1.5+1.4", kick: "main", title: "merged", sub: "ticket → Done" });
    const refl = D.box(sec, { x: 100, y: bot, w: 380, h: 190, cls: "store", in: "s1.6", kick: "Reflector", title: "lessons", sub: "a learnings PR" });
    const w = [
      D.wire(sec, sv, tracker, daemon, { in: "s1.3" }),
      D.wire(sec, sv, daemon, runner, { in: "s1.3+0.8" }),
      D.wire(sec, sv, runner, ws, { in: "s1.4", color: "#f5b452" }),
      D.wire(sec, sv, ws, pr, { from: "b", to: "t", in: "s1.4+1.4", color: "#f5b452" }),
      D.wire(sec, sv, pr, merger, { from: "l", to: "r", in: "s1.5", color: "#b197fc" }),
      D.wire(sec, sv, merger, done, { from: "l", to: "r", in: "s1.5+1.2", color: "#3ddc97" }),
      D.wire(sec, sv, done, refl, { from: "l", to: "r", in: "s1.6", color: "#b197fc" }),
    ];
    const at = ["s1.3+0.2", "s1.3+1.0", "s1.4+0.2", "s1.4+1.6", "s1.5+0.2", "s1.5+1.4", "s1.6+0.2"];
    w.forEach((p, i) => D.packet(sv, p, at[i], t(at[i], 0.6), i < 2 ? "#5cb8ff" : i < 4 ? "#f5b452" : "#b197fc"));
    // The tracker states along the way.
    const states = [["Todo", "s1.2+0.6", ""], ["In Progress", "s1.4+0.4", "amber"], ["In Review", "s1.4+2.2", "violet"], ["Merging", "s1.5+0.8", "violet"], ["Done", "s1.5+1.8", "green"]];
    states.forEach(([s, ref, cls], i) => {
      D.abs(sec, "div", { x: 100 + i * 350, y: 790, w: 300, h: 76, in: ref, kind: "pop" }, {
        class: "state", text: s, style: cls ? { borderColor: `var(--${cls})`, color: `var(--${cls})` } : {},
      });
      if (i) D.abs(sec, "div", { x: 100 + i * 350 - 42, y: 806, in: ref }, { text: "→", style: { fontSize: "34px", color: "var(--muted)" } });
    });
    muted(sec, { x: 100, y: 740, in: "s1.2+0.6" }, "ENG-1 on the board");
  },

  // 2. Eligibility
  s2(sec) {
    const tk = card(sec, { x: 100, y: 210, w: 760, h: 300, in: "s2.1" },
      `<div style="display:flex;gap:16px;align-items:baseline"><b class="mono" style="font-size:32px">ENG-1</b><span style="font-size:32px;font-weight:700">Add farewell() to app/greet.py</span></div>`);
    const labels = Engine.h("div", { class: "chips", style: { marginTop: "18px" } });
    tk.append(labels);
    [["symphony", "blue", "s2.2"], ["repo:acme/app", "blue", "s2.4"]].forEach(([text, cls, ref]) => {
      const c = D.chip(text, cls);
      labels.append(c);
      Engine.anim(c, { in: ref, kind: "pop" });
    });
    const st = Engine.h("p", { html: 'State <span class="chip green">Todo</span> &nbsp; Priority <span class="chip">2</span>', style: { marginTop: "22px", fontSize: "28px" } });
    tk.append(st);
    Engine.anim(st, { in: "s2.3", lift: 6 });

    // The state sets, then the repository rules.
    const S = D.layer(sec, { in: "s2.3", out: "s2.4" });
    [["active", "Todo · In Progress · Rework", "green", "the implementer works", "s2.3"],
      ["parked", "In Review · Merging", "amber", "the merger owns the ticket", "s2.3+2.5"],
      ["terminal", "Done · Canceled · Duplicate", "", "nobody works", "s2.3+3.7"]].forEach(([k, v, cls, who, ref], i) =>
      row(S.el, { x: 100, y: 540 + i * 112, w: 760, h: 96, in: ref },
        `<span class="chip ${cls}" style="min-width:150px;text-align:center">${k}</span><div><div>${v}</div><div style="color:var(--muted)">${who}</div></div>`));

    const R = D.layer(sec, { in: "s2.4", out: "s2.6" });
    [["repo:acme/app", "acme/app", "s2.4+0.6", "green"],
      ["no repo: label", "the default repository", "s2.4+2", "green"],
      ["no label, no default", "not dispatched", "s2.4+3.6", "red"],
      ["two owners", "not dispatched", "s2.4+4.2", "red"]].forEach(([k, v, ref, cls], i) =>
      row(R.el, { x: 100, y: 540 + i * 86, w: 760, h: 74, in: ref },
        `<span class="mono" style="min-width:300px">${k}</span><span style="color:var(--${cls})">→ ${v}</span>`));

    const list = card(sec, { x: 940, y: 210, w: 880, h: 500, in: "s2.1" }, "<h3>Dispatched only when</h3>");
    [["it has a title", "s2.1+0.6"], ["it has every required label", "s2.2"], ["its state is active", "s2.3"], ["it has a repository", "s2.4"],
      ["every blocker is terminal", "s2.5"], ["no person holds it", "s2.5+0.8"], ["a concurrency slot is free", "s2.5+1.6"]].forEach(([text, ref]) => {
      const r = Engine.h("p", { html: `<span class="tick">✓</span> ${text}`, style: { fontSize: "30px", lineHeight: "1.75" } });
      list.append(r);
      Engine.anim(r, { in: ref, lift: 6 });
    });
    card(sec, { x: 940, y: 740, w: 880, h: 140, in: "s2.5+2.4" },
      '<h3>Order</h3><p>priority 1 to 4 first · then the oldest · then the identifier</p>');

    // The intake verdict, on a ticket that is not queued.
    comment(sec, { x: 100, y: 540, w: 760, h: 340, in: "s2.6" }, "<b>symphony</b> commented on ENG-2", [
      ["s2.6+0.3", "<p><b>Symphony</b> — ENG-2 is not queued: it is in <b>Backlog</b>, and Symphony only takes tickets in Todo, In Progress, Rework.</p>"],
      ["s2.6+2.6", "<p>Who acts: the ticket's owner.</p>"],
      ["s2.6+3.2", "<p>Next: move it to <b>Todo</b> when it is ready.</p>"],
    ]);
  },

  // 3. Dispatch
  s3(sec, sv) {
    const tr = D.box(sec, { x: 100, y: 215, w: 330, h: 150, cls: "ext", in: "s3.1", kick: "Tracker", title: "ENG-1 changed", sub: "signed webhook" });
    const api = D.box(sec, { x: 530, y: 215, w: 400, h: 150, cls: "trusted", in: "s3.1+0.6", title: "Control API", sub: '<span class="mono">/v1/webhooks/linear</span>' });
    const wk = D.box(sec, { x: 1030, y: 215, w: 330, h: 150, cls: "store", in: "s3.2", title: "Wake list", sub: '<span class="mono">wake:daemon</span>' });
    const dm = D.box(sec, { x: 1460, y: 215, w: 360, h: 150, cls: "trusted", in: "s3.2+0.5", title: "Daemon", sub: "wakes at once" });
    const w1 = D.wire(sec, sv, tr, api, { in: "s3.1+0.4", label: "HMAC", lo: [0, -40] });
    const w2 = D.wire(sec, sv, api, wk, { in: "s3.2" });
    const w3 = D.wire(sec, sv, wk, dm, { in: "s3.2+0.4", color: "#5cb8ff" });
    D.packet(sv, w1, "s3.1+0.6", "s3.1+1.2");
    D.packet(sv, w2, "s3.2+0.1", "s3.2+0.6");
    D.packet(sv, w3, "s3.2+0.5", "s3.2+1.0");
    chip(sec, 1030, 385, "s3.2+1.6", "safety poll · every 15 s", "");

    ["1 · Reconcile", "2 · Due retries", "3 · New dispatch"].forEach((n, i) =>
      D.box(sec, { x: 100 + i * 440, y: 470, w: 400, h: 100, cls: "center", in: t("s3.2e+0.1", 0.3 + i * 0.45), title: `<span style="font-size:32px">${n}</span>` }));
    D.abs(sec, "div", { x: 92 + 880, y: 462, w: 416, h: 116, in: "s3.3" }, { style: { border: "4px solid #5cb8ff", borderRadius: "20px", boxShadow: "0 0 26px rgba(92,184,255,0.5)" } });

    source(sec, { x: 100, y: 620, w: 900, h: 260, in: "s3.3" }, "Redis, put-if-absent", [
      ["s3.3+0.3", '<span class="k">SET NX</span> issue:&lt;id&gt;            <span class="c"># the claim</span>'],
      ["s3.3+0.8", '<span class="k">INCR</span>   generation:&lt;id&gt;       <span class="c"># → dispatch_seq</span>'],
      ["s3.3+1.3", '<span class="k">SET NX</span> dispatch:&lt;id&gt;:&lt;seq&gt;  <span class="c"># the dispatch token</span>'],
    ]);
    chip(sec, 1040, 640, "s3.3+3", "daemon A ✓", "green");
    chip(sec, 1040, 710, "s3.3+3.8", "daemon B ✕", "red");
    chip(sec, 1040, 780, "s3.3+4.6", "repeated tick ✕", "red");
    const job = D.box(sec, { x: 1460, y: 450, w: 360, h: 140, cls: "trusted", in: "s3.4", kick: "Runner Job", title: '<span class="mono" style="font-size:28px">sym-run-eng-1-…</span>' });
    D.wire(sec, sv, dm, job, { from: "b", to: "t", in: "s3.4", color: "#5cb8ff" });
    D.abs(sec, "div", { x: 1460, y: 640, w: 360, h: 230, in: "s3.5", kind: "pop" }, {
      class: "card", html: '<div class="big" style="color:var(--green)">44 ms</div><p style="margin-top:8px">ticket → session start, mocked run</p>',
    });
  },

  // 4. The runner and the workspace pod
  s4(sec, sv) {
    const run = D.box(sec, { x: 100, y: 215, w: 560, h: 300, cls: "trusted", in: "s4.1", kick: "Trusted side", title: "Runner Job",
      sub: "keeps the secrets", body: D.chips([["Linear API key", "blue"], ["GitHub App private key", "blue"], ["Slack, Discord tokens", "blue"]]) });
    const gh = D.box(sec, { x: 100, y: 640, w: 560, h: 150, cls: "ext", in: "s4.3", kick: "GitHub API", title: "installation token", sub: "acme/app only · 1 hour at most" });
    D.wire(sec, sv, gh, run, { from: "t", to: "b", in: "s4.3+0.4", color: "#3ddc97", label: "mint", lo: [40, 0], bi: true });

    D.abs(sec, "div", { x: 780, y: 215, w: 1040, h: 665, in: "s4.2", kind: "fade", lift: 0 }, { class: "node ws", style: { justifyContent: "flex-start" } });
    D.abs(sec, "div", { x: 810, y: 228, in: "s4.2" }, { html: '<b style="font-size:32px">Workspace pod</b> <span class="mono" style="font-size:28px;color:var(--muted)">&nbsp;ws-eng-1 · symphony-agent:&lt;runtime&gt;</span>' });
    const rows = [
      ["s4.2+1.4", '<span class="mono" style="color:var(--amber);min-width:330px">/workspace</span><span>clone of acme/app, made by the hooks</span>'],
      ["s4.2+0.8", '<span class="mono" style="color:var(--amber);min-width:330px">session volume</span><span>the omp sessions, kept between pods</span>'],
      ["s4.3+2.5", '<span class="mono" style="color:var(--amber);min-width:330px">gh-token · 0600</span><span>read by a git credential helper</span>'],
      ["s4.4", '<span class="mono" style="color:var(--amber);min-width:330px">engine/ · 0700</span><span class="mono">models.yml · mcp.json · config.yml</span>'],
      ["s4.6", '<span class="mono" style="color:var(--green);min-width:330px">omp --mode rpc</span><span>the agent, over pods/exec</span>'],
    ];
    const R = rows.map(([ref, html], i) => ({ x: 810, y: 300 + i * 92, w: 980, h: 76, el: row(sec, { x: 810, y: 300 + i * 92, w: 980, h: 76, in: ref }, html) }));
    D.wire(sec, sv, run, [780, 300], { ff: 0.3, in: "s4.2", color: "#f5b452" });
    D.wire(sec, sv, run, [780, 300 + 4 * 92 + 38], { ff: 0.85, in: "s4.6", color: "#3ddc97" });
    muted(sec, { x: 810, y: 762, w: 980, in: "s4.4+1" }, "PI_CODING_AGENT_DIR: deleted and written again at each attempt");
    D.abs(sec, "div", { x: 810, y: 812, in: "s4.5" }, { html: '<span class="chip red">no ServiceAccount token</span> <span class="chip red">✕ API</span> <span class="chip red">✕ Redis</span> <span class="chip red">✕ node, metadata</span>' });
  },

  // 5. The prompt
  s5(sec) {
    source(sec, { x: 100, y: 210, w: 1060, h: 680, in: "s5.1" }, "WORKFLOW.md", [
      ["s5.1+0.2", '<span class="c">---</span>'],
      ["s5.1+0.4", '<span class="k">tracker:</span> {kind: linear, required_labels: [symphony]}'],
      ["s5.1+0.6", '<span class="k">agent:</span>   {max_turns: 20, input_timeout_ms: 900000}'],
      ["s5.1+0.8", '<span class="k">hooks:</span>   {after_create: …, before_run: …}'],
      ["s5.1+1.0", '<span class="c">---</span>'],
      ["s5.1+1.4", 'You are an autonomous software engineer'],
      ["s5.1+1.4", 'working on <span class="v">{{ issue.identifier }}</span><span class="v">{% if attempt %}</span>'],
      ["s5.1+1.4", '(run <span class="v">{{ attempt }}</span>)<span class="v">{% endif %}</span>: <span class="v">{{ issue.title }}</span>.'],
      ["s5.1+1.8", '<span class="v">{{ issue.description }}</span>'],
      ["s5.3", '<span class="g">Deliver the change … as a ready pull</span>'],
      ["s5.3", '<span class="g">request, then move the ticket to In Review.</span>'],
      ["s5.4", '<span class="g">First: move the ticket to In Progress</span>'],
      ["s5.4", '<span class="g">and post the Workpad.</span>'],
    ]);
    D.abs(sec, "div", { x: 1220, y: 215, in: "s5.2" }, { html: '<span class="chip red">strict Liquid</span>' });
    muted(sec, { x: 1220, y: 275, w: 600, in: "s5.2+0.6" }, "An unknown variable or filter fails the render: the attempt fails, with no silent blank.");
    D.abs(sec, "div", { x: 1220, y: 395, in: "s5.2+1.8" }, { html: '<span class="chip amber">issue</span> <span class="chip amber">attempt</span> <span style="font-size:28px;color:var(--muted)">&nbsp;no other values</span>' });

    comment(sec, { x: 1220, y: 470, w: 600, h: 300, in: "s5.4+0.6", out: "s5.5" }, "<b>symphony</b> commented on ENG-1", [
      ["s5.4+2.4", "<h4>## Workpad</h4>"],
      ["s5.4+2.8", "<p><b>Plan</b> · add farewell(), add a test</p>"],
      ["s5.4+3.2", "<p><b>Notes</b> · …</p>"],
      ["s5.4+3.6", "<p><b>Confusions</b> · …</p>"],
    ]);
    muted(sec, { x: 1220, y: 790, w: 600, in: "s5.4+4", out: "s5.5" }, "one comment, edited in place");
    card(sec, { x: 1220, y: 470, w: 600, h: 190, in: "s5.5+0.3" },
      '<h3>Run 2, turn 1</h3><p>+ “Failure summary from the previous attempt (learn from it): …”</p>');
    card(sec, { x: 1220, y: 690, w: 600, h: 190, in: "s5.5+1.5" },
      '<h3>Turns 2 and later</h3><p>“Continue working on the issue from where you left off. Re-read the Workpad …”</p>');
  },

  // 6. One attempt
  s6(sec) {
    const A = D.layer(sec, { in: "s6", out: "s6.5" });
    const a = A.el;
    const turns = ["turn 1", "turn 2", "turn 3", "…", "turn 20"];
    turns.forEach((n, i) => {
      D.abs(a, "div", { x: 100 + i * 250, y: 220, w: 220, h: 70, in: t("s6.1", 0.2 + i * 0.3), kind: "pop" }, {
        class: "node center", html: `<div class="t" style="font-size:30px">${n}</div>`, style: { padding: "0" },
      });
      if (n !== "…") D.abs(a, "div", { x: 100 + i * 250, y: 300, in: t("s6.1+2", 0.2 + i * 0.2) }, { html: '<span class="chip green">state?</span>' });
    });
    muted(a, { x: 1370, y: 226, w: 450, in: "s6.1+1.6" }, "agent.max_turns, default 20");

    const omp = D.box(a, { x: 100, y: 420, w: 360, h: 150, cls: "ws", in: "s6.2", kick: "Workspace pod", title: "omp" });
    const run = D.box(a, { x: 560, y: 420, w: 360, h: 150, cls: "trusted", in: "s6.2+0.3", kick: "Runner", title: "live session" });
    const red = D.box(a, { x: 1020, y: 420, w: 360, h: 150, cls: "store", in: "s6.2+0.6", kick: "Redis", title: '<span class="mono" style="font-size:28px">session:…:events</span>' });
    const tern = D.box(a, { x: 1480, y: 420, w: 340, h: 150, cls: "people", in: "s6.2+0.9", kick: "Tern", title: "live view" });
    const e1 = D.wire(a, A.sv, omp, run, { in: "s6.2+0.3", color: "#f5b452" });
    const e2 = D.wire(a, A.sv, run, red, { in: "s6.2+0.6", color: "#b197fc" });
    const e3 = D.wire(a, A.sv, red, tern, { in: "s6.2+0.9", color: "#b197fc" });
    [0, 1.2, 2.4].forEach((d) => {
      D.packet(A.sv, e1, t("s6.2", 1.0 + d), t("s6.2", 1.4 + d), "#f5b452", 8);
      D.packet(A.sv, e2, t("s6.2", 1.4 + d), t("s6.2", 1.8 + d), "#b197fc", 8);
      D.packet(A.sv, e3, t("s6.2", 1.8 + d), t("s6.2", 2.2 + d), "#b197fc", 8);
    });
    ["message_update", "tool_execution_start", "tool_execution_end", "turn_end"].forEach((e, i) =>
      D.abs(a, "div", { x: 1020, y: 590 + i * 46, in: t("s6.2", 1.2 + i * 0.4) }, { class: "mono", text: e, style: { fontSize: "28px", color: "var(--muted)" } }));
    const trk = D.box(a, { x: 560, y: 680, w: 360, h: 150, cls: "ext", in: "s6.3", kick: "Tracker", title: "GraphQL", sub: "with the key" });
    D.wire(a, A.sv, run, trk, { from: "b", to: "t", in: "s6.3+0.3", color: "#5cb8ff", bi: true });
    D.abs(a, "div", { x: 100, y: 610, w: 400, in: "s6.3" }, { html: '<span class="chip blue">linear_graphql</span><p style="font-size:28px;color:var(--muted);margin:10px 0 0">a host tool: the pod sends the call, the runner runs it</p>' });
    D.abs(a, "div", { x: 1480, y: 600, w: 340, in: "s6.4" }, { html: '<span class="chip amber">steer</span> <span class="chip amber">follow_up</span><br><span class="chip amber" style="margin-top:10px">interrupt</span> <span class="chip amber" style="margin-top:10px">answer</span>' });
    muted(a, { x: 1480, y: 720, w: 340, in: "s6.4+0.8" }, "intents, read in their own thread: applied at once");

    // The hand-off.
    const B = D.layer(sec, { in: "s6.5" });
    comment(B.el, { x: 100, y: 215, w: 1060, h: 300, in: "s6.5" }, '<b>symphony[bot]</b> opened a pull request', [
      ["s6.5", '<h4>ENG-1: Add farewell() to app/greet.py <span class="mono" style="font-size:26px;color:var(--muted)">#1</span></h4>'],
      ["s6.5+0.5", '<p><span class="chip green">Ready for review</span> <span class="chip blue">symphony</span> <code>eng-1-farewell</code></p>'],
      ["s6.5+1", "<p>What changed, why, how it was verified, and what is risky.</p>"],
    ]);
    D.swap(B.el, 1240, 230, [["s6.5", "ENG-1 · In Progress", "amber"], ["s6.5+2", "ENG-1 · In Review", "violet"]]);
    chip(B.el, 1240, 310, "s6.5+3", "outcome: done", "green");
    muted(B.el, { x: 1240, y: 380, w: 580, in: "s6.5+3.4" }, "The runner sees the state change before the next turn and stops.");
    comment(B.el, { x: 100, y: 550, w: 1060, h: 330, in: "s6.6" }, "<b>symphony</b> commented on ENG-1", [
      ["s6.6", "<p><b>Symphony — session report</b></p>"],
      ["s6.6+0.4", "<p>• Runs: <b>1</b> · elapsed <b>…</b></p>"],
      ["s6.6+0.7", "<p>• Tokens: <b>…</b> (… in · … out)</p>"],
      ["s6.6+1", "<p>• Cost: <b>~US$ …</b> — <i>estimate</i>, computed locally</p>"],
    ]);
    card(B.el, { x: 1240, y: 550, w: 580, h: 330, in: "s6.6+0.6" },
      "<h3>Parked</h3><p>The record stays, with the workspace. It uses no slot. The merger owns the ticket now.</p>");
  },

  // 7. Rework
  s7(sec, sv) {
    const st = [["In Review", "violet", "s7.1"], ["Rework", "red", "s7.1+1.0"], ["agent run", "amber", "s7.1+2.2"], ["In Review", "violet", "s7.3"]];
    const B = st.map(([s, c, ref], i) => D.box(sec, { x: 100 + i * 440, y: 215, w: 380, h: 110, cls: "center", in: ref,
      title: `<span style="font-size:32px;color:var(--${c})">${s}</span>`, style: { borderColor: `var(--${c})` } }));
    B.slice(0, -1).forEach((b, i) => D.wire(sec, sv, b, B[i + 1], { in: st[i + 1][2] }));
    muted(sec, { x: 100, y: 340, w: 380, in: "s7.1+0.4" }, "the merger blocks", 28);
    muted(sec, { x: 540, y: 340, w: 380, in: "s7.1+2.2" }, "an active state", 28);
    muted(sec, { x: 980, y: 340, w: 380, in: "s7.1+2.8" }, "dispatched again", 28);
    muted(sec, { x: 1420, y: 340, w: 400, in: "s7.4" }, "a delta re-review", 28);

    source(sec, { x: 100, y: 410, w: 1080, h: 470, in: "s7.2" }, "The prompt, in Rework", [
      ["s7.2", '<span class="k">## Rework</span>'],
      ["s7.2+0.4", 'The merger blocked the pull request. Its report'],
      ["s7.2+0.4", 'is the latest comment that starts with symphony-merger.'],
      ["s7.2+1.2", '<span class="g">- Fix what that report marks as blocking — only</span>'],
      ["s7.2+1.2", '<span class="g">  that, on the same branch and pull request.</span>'],
      ["s7.2+2", '- A finding you believe is wrong gets a reply'],
      ["s7.2+2", '  with evidence, not a code change.'],
      ["s7.3", '- When done: push, reply under the report, mark'],
      ["s7.3", '  it ready again, and move the ticket to In Review.'],
    ]);
    card(sec, { x: 1240, y: 410, w: 580, h: 200, in: "s7.4" },
      "<h3>Re-review</h3><p>One reviewer reads only what changed since the reviewed head.</p>");
    card(sec, { x: 1240, y: 640, w: 580, h: 240, in: "s7.5" },
      '<h3>Review watch</h3><p>A person comments on the PR → an <b>answer</b> run, at most 3 turns. No state change, no attempt used.</p>');
  },

  // 8. When an attempt stops
  s8(sec) {
    const A = D.layer(sec, { in: "s8", out: "s8.5" });
    const a = A.el;
    const outcomes = [
      ["done", "green", "the ticket left the active states", "park, or clean up", "s8.1"],
      ["continuation", "blue", "turn cap, time guard", "same attempt, in about 1 s", "s8.2"],
      ["blocked", "amber", "symphony_blocked, or idle continuations", "wait for a person, with no slot", "s8.3"],
      ["failed", "red", "hook, prompt, silence, engine error", "next attempt, after a backoff", "s8.4"],
      ["fenced", "", "a newer dispatch owns the ticket", "nothing: the runner exits", "s8.1+1.2"],
    ];
    const focus = ["s8.2", "s8.3", "s8.4"];
    outcomes.forEach(([k, c, cause, next, ref], i) => {
      D.abs(a, "div", { x: 100, y: 215 + i * 128, w: 1000, h: 112, in: i === 0 ? "s8.1" : t("s8.1", 0.3 * i),
        o: [["s8", 1], ...focus.map((f) => [f, ref === f ? 1 : 0.4])] }, {
        class: "node", html: `<div style="display:flex;gap:24px;align-items:center;font-size:28px"><span class="chip ${c}" style="min-width:230px;text-align:center">${k}</span><div><div>${cause}</div><div style="color:var(--muted)">→ ${next}</div></div></div>`,
        style: { padding: "0 22px" },
      });
    });
    D.abs(a, "div", { x: 1160, y: 215, w: 660, h: 180, in: "s8.2+2", out: "s8.3" }, { class: "card", html: '<h3>Cooldown</h3><p>No change on the ticket? Wait <span class="mono">redispatch_cooldown_s</span> (180 in WORKFLOW.md).</p>' });
    card(a, { x: 1160, y: 215, w: 660, h: 300, in: "s8.3+0.4", out: "s8.4" },
      '<h3>Waiting for a person</h3><p>No slot, no dispatch, no continuation. One comment on the ticket says what the agent needs.</p><p style="margin-top:14px">An answer, a comment, a label or a state change starts one new run.</p>');
    muted(a, { x: 1160, y: 540, w: 660, in: "s8.3+3", out: "s8.4" }, "agent.max_idle_continuations, default 3");
    D.abs(a, "div", { x: 1160, y: 420, in: "s8.4+0.4" }, { html: '<span class="chip red">min(10 s × 2<sup>n−1</sup>, 300 s)</span>' });
    const waits = [10, 20, 40, 80, 160, 300];
    waits.forEach((w, i) => {
      const h = Math.round(300 * (w / 300)) + 20;
      D.abs(a, "div", { x: 1180 + i * 106, y: 790 - h, w: 76, h, in: t("s8.4+2", 0.3 + i * 0.45), kind: "pop" }, { style: { background: i === 5 ? "var(--amber)" : "var(--red)", borderRadius: "8px 8px 0 0", opacity: "0.85" } });
      D.abs(a, "div", { x: 1160 + i * 106, y: 800, w: 116, in: t("s8.4+2", 0.3 + i * 0.45) }, { class: "mono", text: w === 300 ? "5 min" : `${w} s`, style: { fontSize: "28px", textAlign: "center" } });
    });
    muted(a, { x: 1160, y: 850, w: 660, in: "s8.4+5" }, "agent.max_retry_backoff_ms caps it", 28);

    // The retry budget, and failure triage.
    const B = D.layer(sec, { in: "s8.5", out: "s8.7" });
    const b = B.el;
    comment(b, { x: 100, y: 215, w: 1720, h: 340, in: "s8.5" }, "<b>symphony</b> commented on ENG-1", [
      ["s8.5", "<p><b>Symphony stopped retrying this ticket</b> — 8 attempts in a row ended without finishing.</p>"],
      ["s8.5+1.6", "<p>• <b>What failed:</b> the before_run hook exited with status 1</p>"],
      ["s8.5+3.4", "<p>• <b>Who acts:</b> an agent, in ENG-31, finds the cause; a Symphony operator if it cannot fix it.</p>"],
      ["s8.5+4.6", "<p>• <b>Next step:</b> once the cause is fixed, run <code>symphony reevaluate ENG-1</code>, or edit the ticket (a comment is enough) to queue a new attempt.</p>"],
    ]);
    D.abs(b, "div", { x: 100, y: 580, in: "s8.5+1" }, { html: '<span class="chip">agent.max_failed_attempts: 8</span> <span class="chip amber">status: blocked</span> <span class="chip red">🛑 retries_exhausted → the channel</span>' });
    D.box(b, { x: 100, y: 670, w: 900, h: 210, cls: "trusted", in: "s8.6", kick: "Investigation · triage.repo",
      title: '<span class="mono" style="font-size:30px">ENG-31</span> Investigate run failure: …', sub: "the evidence: Job condition, exit, last error, log lines" });
    card(b, { x: 1060, y: 670, w: 760, h: 210, in: "s8.6+1.4" },
      '<h3>Failure triage</h3><p>Off by default (<span class="mono">triage:</span>). One ticket per failure signature; a repeat adds an occurrence.</p>');

    // Stall detection, and a Job that ended with no outcome.
    const C = D.layer(sec, { in: "s8.7+0.3" });
    const c = C.el;
    D.abs(c, "div", { x: 100, y: 300, w: 1720, h: 8, in: "s8.7", kind: "wipe", dur: 1.0 }, { style: { background: "#3a4a62", borderRadius: "4px" } });
    [100, 220, 340].forEach((x, i) => D.abs(c, "div", { x, y: 288, w: 32, h: 32, in: t("s8.7", 0.1 * i), kind: "pop" }, { style: { borderRadius: "50%", background: "var(--amber)" } }));
    muted(c, { x: 100, y: 230, in: "s8.7" }, "engine events");
    D.abs(c, "div", { x: 420, y: 270, w: 600, h: 70, in: "s8.7+0.6" }, { class: "node center", html: '<div class="rowline" style="justify-content:center">window 1: silence · strike 1</div>', style: { borderColor: "var(--amber)", padding: 0 } });
    D.abs(c, "div", { x: 1040, y: 270, w: 600, h: 70, in: "s8.7+1.4" }, { class: "node center", html: '<div class="rowline" style="justify-content:center">window 2: silence · stop</div>', style: { borderColor: "var(--red)", padding: 0 } });
    muted(c, { x: 420, y: 360, w: 1220, in: "s8.7+1" }, "agent.stall_timeout_ms, 30 minutes in WORKFLOW.md");
    comment(c, { x: 100, y: 420, w: 1720, h: 280, in: "s8.7+2" }, "<b>symphony</b> commented on ENG-1", [
      ["s8.7+2", "<p><b>Attempt 2 ended without finishing</b> — it went silent for more than 30 minutes and was stopped. A new attempt is queued; the branch and this Workpad are kept, so the next one continues rather than restarts. This is failure 2 of 8 in a row; at 8 Symphony stops retrying and asks for a human.</p>"],
      ["s8.7+2.6", "<p><i>Reported by the orchestrator, not the agent: it was no longer running to report for itself.</i></p>"],
    ]);
    D.abs(c, "div", { x: 100, y: 730, in: "s8.8" }, { html: '<span class="chip amber">runner Job ended, no outcome</span> <span style="font-size:30px">&nbsp;→ requeued at the next tick, not after two windows</span>' });
    D.abs(c, "div", { x: 100, y: 810, in: "s8.9" }, { html: '<span class="chip green">branch kept</span> <span class="chip green">Workpad kept</span> <span class="chip green">the next attempt continues</span>' });
  },

  // 9. Questions and holds
  s9(sec) {
    const A = D.layer(sec, { in: "s9", out: "s9.5" });
    const a = A.el;
    source(a, { x: 100, y: 215, w: 1000, h: 225, in: "s9.1" }, "Host tool call", [
      ["s9.1+0.4", '<span class="k">symphony_ask</span> {question: "Which word should farewell()'],
      ["s9.1+0.4", '  start with?", options: ["Goodbye", "See you"],'],
      ["s9.1+3.6", '  to: "alice"}'],
    ]);
    chip(a, 1160, 230, "s9.1+1.6", "session: waiting_input", "amber");
    chip(a, 1160, 300, "s9.3+0.3", "DM · alice, who decides", "violet");
    chip(a, 1160, 366, "s9.3+1.4", "DM · bob, the assignee", "violet");
    comment(a, { x: 100, y: 465, w: 840, h: 370, in: "s9.2" }, "<b>symphony</b> commented on ENG-1", [
      ["s9.2+0.3", '<p><b>Question from the agent</b> for <b style="color:var(--blue)">@alice</b></p>'],
      ["s9.2+0.6", "<p>Which word should farewell() start with?</p>"],
      ["s9.2+0.9", "<p>1. Goodbye<br>2. See you</p>"],
      ["s9.2+1.4", '<p style="color:#9da7b3">Answer by replying here, in the Slack thread, with a button, or in a comment on the PR …</p>'],
    ]);
    message(a, { x: 980, y: 465, w: 840, h: 370, in: "s9.2+2.6" }, "Symphony",
      '<p>🙋 <b>Agent is waiting for an answer</b> · <u style="color:var(--blue)">ENG-1 · Add farewell() to app/greet.py</u> · <b style="color:var(--blue)">@alice</b></p>'
      + "<p>Which word should farewell() start with?</p>"
      + '<p><span class="chip">1. Goodbye</span> <span class="chip">2. See you</span></p>'
      + '<p style="color:#8a8d91;font-size:26px">Answer with a button, in this thread, on the ticket or in a comment on the PR.</p>');
    D.abs(a, "div", { x: 100, y: 850, in: "s9.4" }, { html: '<span class="chip">ticket</span> <span class="chip">pull request</span> <span class="chip">Slack</span> <span class="chip">Tern</span> <span class="chip">CLI</span>' });
    chip(a, 1160, 850, "s9.4+3.4", "the first answer wins", "green");

    // The question outlives the run.
    const Q = D.layer(sec, { in: "s9.5", out: "s9.7" });
    const q = Q.el;
    card(q, { x: 100, y: 215, w: 1720, h: 200, in: "s9.5" },
      '<h3>After agent.input_timeout_ms (15 min), the agent reads</h3><p class="mono" style="color:var(--amber)">Nobody answered in time. The question stays open on the ticket and in Slack, and the person who decides is reminded later. Proceed with your best judgment and record the assumption in the Workpad.</p>');
    chip(q, 100, 450, "s9.6+0.2", "question: still open", "green");
    chip(q, 520, 450, "s9.6+1.2", "agent.ask_reminder_ms · 2 h", "amber");
    comment(q, { x: 100, y: 530, w: 1100, h: 260, in: "s9.6+1.6" }, "<b>symphony</b> replied on ENG-1", [
      ["s9.6+1.6", '<p><b>Still waiting for an answer</b>, <b style="color:var(--blue)">@alice</b>: this question has been open for 2 h.</p>'],
      ["s9.6+2", '<p style="color:#9da7b3">&gt; Which word should farewell() start with?</p>'],
      ["s9.6+2.4", "<p>Reply in this thread with an option's number or in your own words.</p>"],
    ]);
    card(q, { x: 1260, y: 450, w: 560, h: 340, in: "s9.6+3.8" },
      '<h3>A late answer</h3><p>→ a message to the run that went on without it</p><p style="margin-top:12px">→ with no run, the ticket goes back to Rework</p>');

    const B = D.layer(sec, { in: "s9.7" });
    const b = B.el;
    D.term(b, { x: 100, y: 215, w: 760, in: "s9.7" }, "A person's machine", [
      { cmd: "symphony takeover ENG-147", in: "s9.7+0.3" },
      { c: "held · transcript · branch · omp --resume", in: "s9.7+1.4" },
      { cmd: "symphony release ENG-147", in: "s9.8+2.6" },
    ]);
    [["the attempt stops at once", "s9.8"], ["dispatch and gateway tokens removed", "s9.8+0.6"], ["workspace, branch and session stay", "s9.8+1.2"], ["no dispatch, retry or continuation", "s9.8+1.8"]].forEach(([text, ref], i) =>
      D.abs(b, "div", { x: 920, y: 225 + i * 52, in: ref }, { html: `<span class="tick">✓</span> ${text}`, style: { fontSize: "28px" } }));
    D.shot(b, {
      x: 100, y: 470, w: 1720, h: 410, in: "s9.7+0.8",
      shots: [{ src: SHOT("tickets"), in: "s9.7+0.8" }],
      cam: [["s9.7", 860, 680, 1.0]],
      spots: [{ x: 20, y: 562, w: 1680, h: 52, in: "s9.8" }, { x: 20, y: 728, w: 1680, h: 88, in: "s9.8+2.6" }],
    });
  },

  // 10. Other kinds of ticket
  s10(sec) {
    const col = (x, ref, title, cls) => card(sec, { x, y: 200, w: 560, h: 690, in: ref }, `<h3>${title}</h3>`, { borderColor: `var(--${cls})` });
    const add = (parent, ref, html, style = {}) => {
      const el = Engine.h("div", { html, style: { fontSize: "28px", lineHeight: "1.4", marginTop: "16px", ...style } });
      parent.append(el);
      Engine.anim(el, { in: ref, lift: 6 });
    };
    const inv = col(100, "s10.1", "Investigation", "blue");
    add(inv, "s10.1+0.4", '<span class="chip blue">investigation</span>');
    add(inv, "s10.1+2.4", '<span style="color:var(--muted)">GitHub token</span><br><span class="chip">contents: read</span> <span class="chip">pull_requests: read</span> <span class="chip">metadata: read</span>');
    add(inv, "s10.1+3.8", '<span class="chip red">push ✕ 403</span> <span class="chip red">pull request ✕ 403</span>');
    add(inv, "s10.2", "→ a report comment on the ticket");
    add(inv, "s10.2+1.2", "→ new tickets for the follow-up work");
    add(inv, "s10.2+2.4", '→ <span class="chip green">Done</span>');

    const sub = col(680, "s10.3", "Sub-issues and files", "violet");
    add(sub, "s10.3+0.4", '<span class="mono">ENG-4</span> plan the shout() feature', { fontWeight: "700" });
    add(sub, "s10.3+1.6", '&nbsp;└ <span class="mono">ENG-5</span> Add shout()<br>&nbsp;└ <span class="mono">ENG-6</span> Document shout()');
    add(sub, "s10.3+2.4", '<span class="chip violet">ENG-5 blocks ENG-6</span><br><span class="chip blue" style="margin-top:8px">symphony</span> <span class="chip blue" style="margin-top:8px">repo:acme/app</span>');
    add(sub, "s10.4", '<span class="chip amber">pytest evidence · log</span>');
    add(sub, "s10.4+0.8", '<span style="color:var(--muted)">Linear: up to 25 MB · GitHub: text</span>');
    add(sub, "s10.4+3", '<span class="chip amber">session-trace-run-1.html</span><br><span style="color:var(--muted)">Linear only · secrets [REDACTED]</span>');
    add(sub, "s10.5+0.4", '<span class="mono" style="font-size:26px">symphony_ticket_attachments<br>symphony_fetch_attachment</span>');

    const mr = col(1260, "s10.6", "Several repositories", "green");
    add(mr, "s10.6+0.4", '<span class="chip blue">repo:acme/app</span> <span class="chip blue">repo:acme/lib</span>');
    add(mr, "s10.6+1.2", '<span class="mono">/workspace</span> <span style="color:var(--muted)">primary</span><br><span class="mono">/workspace/repos/lib</span>');
    add(mr, "s10.6+2.6", '<span style="color:var(--muted)">one token</span><br><span class="chip green">acme/app, acme/lib</span>');
    add(mr, "s10.6+3.8", '<span class="mono">acme/app#2</span> · <span class="mono">acme/lib#1</span><br><span style="color:var(--muted)">one pull request per repository; the merger merges each</span>');
    add(mr, "s10.7", '<span class="chip amber">repository set changed</span><br><span style="color:var(--muted)">→ a new pod, volume, token and session</span>');
  },

  // 11. Reconcile, sweep and learnings
  s11(sec, sv) {
    const A = D.layer(sec, { in: "s11", out: "s11.5" });
    const a = A.el;
    D.abs(a, "div", { x: 100, y: 215, in: "s11.1" }, { html: '<b style="font-size:32px">Reconcile: what the tracker says now</b>' });
    [["ENG-1", "In Review", "parked: workspace kept, no slot", "violet", "s11.2"],
      ["ENG-9", "gone, or label removed", "stopped, workspace kept", "red", "s11.2+2.4"],
      ["ENG-1", "Done", "terminal: clean up", "green", "s11.3"],
      ["ENG-10", "Canceled", "abandon: questions and PRs close", "red", "s11.4"]].forEach(([id, s, act, c, ref], i) =>
      row(a, { x: 100, y: 285 + i * 100, w: 1000, h: 84, in: ref },
        `<b class="mono" style="min-width:110px">${id}</b><span class="chip ${c}">${s}</span><span>${act}</span>`));
    muted(a, { x: 100, y: 700, w: 1000, in: "s11.4+2.6" }, "Its Slack question reads ⛔ Ticket canceled, with no buttons. The merger comments once, then closes each open PR.");
    const steps = card(a, { x: 1180, y: 215, w: 640, h: 440, in: "s11.3+0.4" }, "<h3>Terminal, in this order</h3>");
    ["retrospective (reflector Job)", "monthly metrics", "stop the runner and the engine", "delete the workspace", "delete tokens and the record"].forEach((s, i) => {
      const r = Engine.h("p", { html: `<span class="mono" style="color:var(--muted)">${i + 1}</span> &nbsp;${s}`, style: { fontSize: "28px", lineHeight: "1.75" } });
      steps.append(r);
      Engine.anim(r, { in: t("s11.3", 0.6 + i * 0.35), lift: 6 });
    });

    const B = D.layer(sec, { in: "s11.5" });
    const b = B.el;
    const ins = ["PR diff", "review threads", "CI results", "Workpad", "attempts"];
    ins.forEach((s, i) => chip(b, 100, 230 + i * 66, t("s11.5", 0.2 + i * 0.3), s, "blue"));
    const rf = D.box(b, { x: 420, y: 250, w: 380, h: 250, cls: "store", in: "s11.5+2", kick: "Reflector", title: "≤ 3 lessons", sub: "general ones, or nothing at all" });
    D.wire(b, B.sv, [380, 375], rf, { in: "s11.5+1.5" });
    const cu = D.box(b, { x: 900, y: 250, w: 420, h: 250, cls: "trusted", in: "s11.6", kick: "Code, not a model", title: "curate AGENTS.md", sub: "add, replace, remove · 100-line budget · unused lessons decay" });
    D.wire(b, B.sv, rf, cu, { in: "s11.6", color: "#b197fc" });
    const lp = D.box(b, { x: 1420, y: 250, w: 400, h: 250, cls: "people", in: "s11.7", kick: "Pull request", title: '<span style="font-size:30px">learnings: ENG-1</span>', sub: '<span class="mono" style="font-size:28px">symphony-learnings</span><br>a person reviews and merges' });
    D.wire(b, B.sv, cu, lp, { in: "s11.7", color: "#f5b452" });
    muted(b, { x: 900, y: 520, w: 420, in: "s11.6+1" }, "A recurring procedure becomes a skill.");
    comment(b, { x: 100, y: 600, w: 1100, h: 280, in: "s11.7+0.6", out: "s11.8" }, "<b>symphony[bot]</b> opened <b>learnings: ENG-1</b>", [
      ["s11.7+0.6", "<p>Lessons distilled from the retrospective of ENG-1 (+1 new, …). Human gate required — never auto-merge.</p>"],
      ["s11.7+1.4", "<p><code>AGENTS.md</code> + Paste the exact test command and its output in the PR description</p>"],
    ]);
    card(b, { x: 100, y: 600, w: 1100, h: 280, in: "s11.8" },
      '<h3>Public repository</h3><p>The branch is <span class="mono">symphony/&lt;ticket id&gt;</span>, the tracker\'s opaque id. No ticket key, tracker or company name in the code, commits or PR.</p><p style="margin-top:12px">No learnings PR: the lessons come from the private ticket.</p>');
    chip(b, 1420, 520, "s11.8+2.6", "public repo: no PR", "red");
    card(b, { x: 1260, y: 600, w: 560, h: 280, in: "s11.9" },
      '<h3>Nightly sweep</h3><p>CronJob <span class="mono">symphony-nightly-sweep</span>, 03:30. It starts the retrospectives missed in the last 7 days.</p>');
  },

  // 12. Proof
  s12(sec) {
    const board = card(sec, { x: 100, y: 205, w: 1150, h: 680, in: "s12.1" }, "<h3>Kubernetes on kind, real agent</h3>");
    [["ticket → runner Job", "7.7 s", "s12.1+0.4"], ["→ workspace pod and session PVC", "9.0 s", "s12.1+0.8"], ["→ agent_start on the API stream", "15.3 s", "s12.1+1.2"],
      ["steer → intent applied", "13 ms", "s12.2"], ["refresh → second attempt: same pod, same PVC, resumed session", "2.8 s", "s12.2+2.4"],
      ["Done → pod and PVC deleted", "18 s", "s12.2+3.4"]].forEach(([text, v, ref]) => {
      const r = Engine.h("div", { html: `${text} &nbsp;<b style="color:var(--green)">${v}</b>`, style: { fontSize: "28px", lineHeight: "1.4", marginTop: "12px" } });
      board.append(r);
      Engine.anim(r, { in: ref, lift: 6 });
    });
    const sub = Engine.h("h3", { text: "Mocked run (Linear, Slack)", style: { marginTop: "26px" } });
    board.append(sub);
    Engine.anim(sub, { in: "s12.3" });
    [["1 implement → merge", "s12.3+0.3"], ["2 two repositories", "s12.4+2"], ["3 investigation, read-only", "s12.4"], ["4 sub-issues and evidence", "s12.4+2.4"], ["5 review comment answer", "s12.4+2.8"], ["6 learnings PR", "s12.4+3.2"]].forEach(([text, ref], i) => {
      const r = Engine.h("span", { html: `<span class="tick">✓</span> ${text}`, style: { display: "inline-block", width: "540px", fontSize: "28px", lineHeight: "1.5" } });
      board.append(r);
      Engine.anim(r, { in: ref, lift: 6 });
    });
    const tile = (y, ref, big, text, color) => D.abs(sec, "div", { x: 1290, y, w: 530, h: 210, in: ref, kind: "pop" }, {
      class: "card", html: `<div class="big" style="color:${color}">${big}</div><p style="margin-top:8px">${text}</p>`,
    });
    tile(205, "s12.1", "7.7 s", "kind: a new ticket to its runner Job", "var(--blue)");
    tile(440, "s12.3", "21.8 s", "mocked: ready PR #1, ticket in In Review", "var(--green)");
    tile(675, "s12.4", "403", "mocked: the investigation's push and pull request were refused", "var(--red)");
  },
};
