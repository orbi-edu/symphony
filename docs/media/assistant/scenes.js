// The assistant video: eight scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/assistant.md, docs/install.md and src/symphony/assistant/ (runtime.py, tools.py,
// sandbox.py, share.py); the bot texts are the strings of engine.py (WORKING, PLAN_THIS, the handed-over note),
// confirm.py (render with its attachments, ONLY_MAPPED, the created and busy answers) and the citation format
// of engine.link_citations. The figures are docs/mocked-certification.md, scenarios 9 to 11.

const SHOT = (name) => `../../tern/${name}-dark.png`;

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const t = (ref, d) => Engine.at(ref) + d;

// The people of the example threads. alice is mapped in notify.users; bob is not.
const WHO = {
  alice: { name: "alice", color: "#e8912d" },
  bob: { name: "bob", color: "#2bac76" },
  octocat: { name: "octocat", color: "#b197fc" },
  bot: { name: "symphony", color: "#3fd3e8", bot: true },
};
const WORKING = "Working on it…";
const MENTION = '<span class="mention">@symphony</span>';
const BUTTONS = '<div class="btns"><span class="p">Create</span><span>Create and start</span><span class="d">Dismiss</span></div>';
const ASK_PLAN = `${MENTION} plan add rate limits to the public API of acme/app`;
// confirm.render for a proposal of three tickets: a parent and two children, the second after the first.
const PROPOSAL =
  "<p>Proposed tickets: Rate limits for the public API of acme/app</p>" +
  "<p style=\"margin-top:8px\">1. API rate limits (acme/app)<br>2. Token bucket per key (part of 'API rate limits')<br>" +
  "3. 429 with Retry-After (part of 'API rate limits'; after 'Token bucket per key')</p>" +
  '<p style="margin-top:8px">Nothing is created until someone mapped in notify.users clicks Create.</p>';
// The same proposal after the agent drafted a patch in the thread's sandbox (`attachments` of ticket 2).
const PROPOSAL_FILES = PROPOSAL.replace("(part of 'API rate limits')", "(part of 'API rate limits'; with bucket.patch)");
// engine.PLAN_THIS: the button of an `offer_plan`.
const PLAN_BTN = '<div class="btns"><span class="p">Plan this</span></div>';
const PLAN_ANSWER = "This needs a plan: settle how the limits work, then three changes.";
const LINEAR = (n) => `<span class="ln">https://linear.app/…/ENG-${n}</span>`;
const CREATED =
  "<p>Created by alice:</p>" +
  `<p>- ENG-1: API rate limits ${LINEAR(1)}<br>- ENG-2: Token bucket per key ${LINEAR(2)}<br>- ENG-3: 429 with Retry-After ${LINEAR(3)}</p>`;

// One chat message: avatar, name (with the APP or BOT badge for the bot) and the text.
function msg(who, html, kind = "slack", cls = "") {
  const w = WHO[who];
  const badge = w.bot ? `<span class="badge">${kind === "slack" ? "APP" : "BOT"}</span>` : "";
  return `<div class="msg ${cls}"><div class="av" style="background:${w.color}">${w.name[0].toUpperCase()}</div>` +
    `<div class="mb"><div class="who"><b>${w.name}</b>${badge}</div><div class="tx">${html}</div></div></div>`;
}
// A private answer: Slack shows it to the clicker only.
const ephemeral = (html) => msg("bot", `<div class="note muted">Only visible to you</div>${html}`, "slack", "eph");

// A thread in Slack's or Discord's dark style. `parts`: [ref, html, out] messages in order; a part can
// also be a list of such entries that share one place (the bot edits its message in place).
function chat(parent, o, kind, header, parts) {
  const el = D.abs(parent, "div", o, { class: `chat ${kind}` });
  el.append(Engine.h("div", { class: "hd", html: header }));
  const bd = Engine.h("div", { class: "bd" });
  el.append(bd);
  for (const part of parts) {
    const alts = Array.isArray(part[0]) ? part : [part];
    const slot = Engine.h("div", { style: { display: "grid" } });
    bd.append(slot);
    for (const [ref, html, out] of alts) {
      const p = Engine.h("div", { html, style: { gridArea: "1 / 1" } });
      slot.append(p);
      Engine.anim(p, { in: ref, out, lift: 8 });
    }
  }
  return el;
}
const SLACK = (channel) => `<b># ${channel}</b><span>Slack</span>`;
const DISCORD = (channel) => `<b># ${channel}</b><span>Discord</span>`;

const SCENES = {
  // 1. What the assistant is
  s1(sec, sv) {
    const slack = D.box(sec, { x: 100, y: 250, w: 300, h: 150, cls: "ext", in: "s1.1", kick: "Chat", title: "Slack", sub: "channels, DMs" });
    const discord = D.box(sec, { x: 100, y: 470, w: 300, h: 150, cls: "ext", in: "s1.1+0.4", kick: "Chat", title: "Discord", sub: "channels, DMs" });
    const cont = { x: 600, y: 230, w: 680, h: 450 };
    D.abs(sec, "div", { ...cont, in: "s1.1+0.6", kind: "fade", lift: 0 },
      { style: { border: "3px solid var(--cyan)", borderRadius: "24px", background: "rgba(63,211,232,0.05)" } });
    D.abs(sec, "div", { x: 628, y: 246, in: "s1.1+0.6" }, { html: '<span style="font-size:34px;font-weight:700">Assistant</span> <span class="mono" style="font-size:28px;color:var(--muted)">&nbsp;one bot, three roles</span>' });
    [["Ask", "assistant", "s1.2"], ["Research", "researcher", "s1.3"], ["Plan", "planner", "s1.3+0.8"]].forEach(([mode, role, ref], i) =>
      D.box(sec, { x: 630, y: 315 + i * 115, w: 620, h: 90, cls: "merger", in: ref,
        title: `<div style="display:flex;justify-content:space-between;align-items:center"><span style="font-size:32px">${mode}</span><span class="mono" style="font-size:28px;color:var(--violet);font-weight:500">role ${role}</span></div>` }));
    const code = D.box(sec, { x: 1400, y: 230, w: 420, h: 140, cls: "trusted", in: "s1.2+0.3", kick: "Reads", title: "your code", sub: '<span class="mono">acme/app, acme/lib</span>' });
    const state = D.box(sec, { x: 1400, y: 400, w: 420, h: 140, cls: "trusted", in: "s1.2+0.8", kick: "Reads", title: "your Symphony", sub: "tickets, sessions, merger" });
    const tracker = D.box(sec, { x: 1400, y: 580, w: 420, h: 150, cls: "people", in: "s1.4", kick: "Tracker", title: "tickets", sub: "only after a click" });
    D.wire(sec, sv, cont, code, { ff: 0.15, in: "s1.2+0.3", color: "#5cb8ff" });
    D.wire(sec, sv, cont, state, { ff: 0.4, in: "s1.2+0.8", color: "#5cb8ff" });
    const wt = D.wire(sec, sv, cont, tracker, { ff: 0.8, in: "s1.4+0.3", color: "#f5b452" });
    D.packet(sv, wt, "s1.4+1.2", "s1.4+1.8", "#f5b452");
    chip(sec, 1400, 760, "s1.4+0.6", "alice confirms ✓", "amber");
    const ws = D.wire(sec, sv, cont, slack, { from: "l", to: "r", ff: 0.25, in: "s1.5+0.4", color: "#3fd3e8", label: "Socket Mode", lo: [0, -34] });
    const wd = D.wire(sec, sv, cont, discord, { from: "l", to: "r", ff: 0.78, in: "s1.5+0.8", color: "#3fd3e8", label: "Gateway", lo: [0, -34] });
    D.packet(sv, ws, "s1.5+1.2", "s1.5+1.8", "#3fd3e8");
    D.packet(sv, wd, "s1.5+1.6", "s1.5+2.2", "#3fd3e8");
    chip(sec, 600, 710, "s1.5", "Deployment symphony-assistant · 1 replica", "blue");
    chip(sec, 600, 790, "s1.5+2.6", "outbound only · no public URL", "green");
  },

  // 2. Three modes
  s2(sec) {
    const I = D.layer(sec, { in: "s2", out: "s2.2" });
    [["@symphony …", "a mention in an allowed channel"], ["DM", "a direct message"], ["reply", "a reply in its thread"]].forEach(([k, v], i) =>
      D.box(I.el, { x: 100 + i * 585, y: 400, w: 550, h: 170, cls: "center ext", in: t("s2.1", 0.3 + i * 0.7),
        title: `<span class="mono" style="font-size:34px">${k}</span>`, sub: v }));

    const C = D.layer(sec, { in: "s2.2" });
    const c = C.el;
    const X = [100, 685, 1270];
    const label = (i, ref, word, role) => D.abs(c, "div", { x: X[i], y: 205, w: 550, in: ref },
      { html: `<span class="mono" style="font-size:28px;color:var(--muted)">${word}</span> &nbsp;→&nbsp; <span class="chip violet">${role}</span>` });
    label(0, "s2.2", "anything else", "assistant");
    label(1, "s2.4", "research …", "researcher");
    label(2, "s2.5", "plan …", "planner");
    chat(c, { x: X[0], y: 262, w: 550, h: 560, in: "s2.2" }, "slack", SLACK("eng-help"), [
      ["s2.2", msg("alice", `${MENTION} where do we validate webhooks?`)],
      [["s2.3", msg("bot", WORKING), "s2.3+1.4"],
        ["s2.3+1.5", msg("bot", "The signature is checked in"), "s2.3+2.6"],
        ["s2.3+2.7", msg("bot", 'The signature is checked in <span class="ln">acme/app:src/webhooks.py:42</span>, before the body is parsed.')]],
      ["s2.7", msg("alice", "And where is that tested?")],
      [["s2.7+0.8", msg("bot", WORKING), "s2.7+1.7"],
        ["s2.7+1.8", msg("bot", 'In <span class="ln">acme/app:tests/test_webhooks.py:15</span>.')]],
    ]);
    chat(c, { x: X[1], y: 262, w: 550, h: 560, in: "s2.4" }, "discord", DISCORD("eng"), [
      ["s2.4", msg("octocat", `${MENTION} research how other projects retry Slack 429 answers`, "discord")],
      [["s2.4+1.6", msg("bot", WORKING, "discord"), "s2.4+2.6"],
        ["s2.4+2.7", msg("bot", '<p>Most clients wait for the <b>Retry-After</b> header, then send again.</p><p><b>Sources:</b> <span class="ln">api.slack.com/apis/rate-limits</span>, <span class="ln">acme/app:src/notify.py:88</span></p><p class="muted">Not confirmed: how other clients pick the backoff.</p>', "discord")]],
    ]);
    chat(c, { x: X[2], y: 262, w: 550, h: 560, in: "s2.5" }, "slack", SLACK("eng-plan"), [
      ["s2.5", msg("alice", ASK_PLAN)],
      [["s2.5+1.1", msg("bot", WORKING), "s2.5+1.9"],
        ["s2.5+2.0", msg("bot", `<p>Proposed tickets: Rate limits for the public API of acme/app</p><p style="margin-top:6px">1. API rate limits<br>2. Token bucket per key<br>3. 429 with Retry-After</p>${BUTTONS}`)]],
    ]);
    D.swap(c, X[0], 838, [["s2.6", "team role · own model", "violet"], ["s2.7", "same thread: role kept", "green"]]);
    chip(c, X[1], 838, "s2.6+0.3", "team role · own model", "violet");
    chip(c, X[2], 838, "s2.6+0.6", "team role · own model", "violet");
  },

  // 3. Citations, tools and the optional sandbox
  s3(sec) {
    const A = D.layer(sec, { in: "s3", out: "s3.5" });
    const a = A.el;
    D.term(a, { x: 100, y: 205, w: 1720, in: "s3.1" }, "The agent's working directory: read-only worktrees at the default branch", [
      { c: "./acme/app    detached at 9f2c41e    (origin/HEAD: main)", in: "s3.1+0.3" },
      { c: "./acme/lib    detached at 4b7d0a3    (origin/HEAD: trunk)", in: "s3.1+0.6" },
      { c: "# or kubernetes.base_branches · fetched every 5 minutes, read-only App token", in: "s3.1+1.4" },
      { c: "open_repository(\"acme/docs\")   # any repository the App reaches, kept a day", in: "s3.2+0.3" },
    ]);
    const before = 'The signature is checked in <code>acme/app:src/webhooks.py:42</code>, before the body is parsed.';
    const after = 'The signature is checked in <span class="ln" style="text-decoration:underline">acme/app:src/webhooks.py:42</span>, before the body is parsed.';
    chat(a, { x: 100, y: 490, w: 1100, in: "s3.3" }, "slack", SLACK("eng-help"), [
      [["s3.3", msg("bot", before), "s3.4"], ["s3.4+0.1", msg("bot", after)]],
    ]);
    D.abs(a, "div", { x: 100, y: 700, in: "s3.3+0.8" }, {
      html: '<span class="chip blue">acme/app</span> <span style="font-size:28px;color:var(--muted)">repository</span> &nbsp; ' +
        '<span class="chip blue">src/webhooks.py</span> <span style="font-size:28px;color:var(--muted)">path</span> &nbsp; ' +
        '<span class="chip blue">42</span> <span style="font-size:28px;color:var(--muted)">line</span>',
    });
    D.abs(a, "div", { x: 100, y: 775, in: "s3.4+0.3" }, { class: "mono", text: "https://github.com/acme/app/blob/9f2c41e…/src/webhooks.py#L42", style: { fontSize: "28px", color: "var(--green)" } });
    D.abs(a, "div", { x: 100, y: 825, in: "s3.4+0.9" }, { text: "at the commit of the checkout, so the line matches what the agent read", style: { fontSize: "28px", color: "var(--muted)" } });
    card(a, { x: 1260, y: 490, w: 560, h: 290, in: "s3.4+0.6" },
      '<h3>The link per transport</h3><p>Slack</p><p class="mono" style="color:var(--amber);margin-bottom:12px">&lt;url|acme/app:…:42&gt;</p><p>Discord</p><p class="mono" style="color:var(--amber)">[acme/app:…:42](&lt;url&gt;)</p>');

    const B = D.layer(sec, { in: "s3.5" });
    const b = B.el;
    const built = card(b, { x: 100, y: 205, w: 820, h: 200, in: "s3.5" },
      '<h3>Built-in tools</h3><div class="chips"><span class="chip blue">read</span><span class="chip blue">grep</span><span class="chip blue">glob</span><span class="chip blue">web_search</span></div>');
    const img = Engine.h("p", { html: "and the chat's images: PNG, JPEG, GIF, WebP · ≤ 5 MiB · 4 a turn", style: { marginTop: "12px", fontSize: "26px" } });
    built.append(img);
    Engine.anim(img, { in: "s3.5+2.6", lift: 6 });
    const host = card(b, { x: 100, y: 430, w: 820, h: 450, in: "s3.6" }, "<h3>Host tools, run by Symphony</h3>");
    [["symphony_status", "read-only: tickets, sessions, merger, failures", "s3.6+0.3"],
      ["tracker_search", "read-only: Linear or GitHub issue search", "s3.7"],
      ["propose_tickets", "assistant and planner: creates nothing", "s3.7+1.4"],
      ["offer_plan", "assistant: a Plan this button", "s3.7+2.0"]].forEach(([name, what, ref]) => {
      const r = Engine.h("p", { html: `<span class="mono" style="color:var(--amber)">${name}</span><br>${what}`, style: { marginTop: "14px" } });
      host.append(r);
      Engine.anim(r, { in: ref, lift: 6 });
    });
    const no = card(b, { x: 980, y: 205, w: 840, h: 300, in: "s3.8", out: "s3.9" }, "<h3>By default, it cannot</h3>");
    ["run commands (no bash)", "change files (no edit, no write)", "push, comment, or move a ticket", "create a ticket without a person's click"].forEach((s, i) => {
      const r = Engine.h("p", { html: `<span class="cross">✕</span> ${s}`, style: { lineHeight: "1.6" } });
      no.append(r);
      Engine.anim(r, { in: t("s3.8", 0.2 + i * 0.35), lift: 6 });
    });
    const sb = card(b, { x: 980, y: 205, w: 840, h: 300, in: "s3.9" }, '<h3>Optional: <span class="mono" style="font-size:30px">assistant.sandbox</span></h3>');
    [['<span class="mono" style="color:var(--amber)">sandbox_run</span> · bash in a pod per thread', "s3.9+0.4"],
      ["the repository cloned at its default branch", "s3.9+1.4"],
      ["only for people mapped in notify.users", "s3.9+3.0"],
      ['<span class="mono" style="color:var(--amber)">share_file</span> · a file of the pod in the thread', "s3.10"]].forEach(([s, ref]) => {
      const r = Engine.h("p", { html: s, style: { lineHeight: "1.6" } });
      sb.append(r);
      Engine.anim(r, { in: ref, lift: 6 });
    });
    card(b, { x: 980, y: 535, w: 840, h: 345, in: "s3.11" },
      "<h3>The agent's environment</h3>" +
      '<div class="chips">' + ["SLACK_*", "DISCORD_*", "GITHUB_APP_*", "LINEAR_*", "SYMPHONY_API_*", "REDIS_URL"].map((v) =>
        `<span class="chip red" style="text-decoration:line-through">${v}</span>`).join("") + "</div>" +
      '<p style="margin-top:16px">Removed before omp starts. The model credential stays. The tools that reach the tracker run on the host.</p>');
  },

  // 4. Proposals, Plan this and who can confirm
  s4(sec) {
    chat(sec, { x: 100, y: 205, w: 1040, h: 600, in: "s4" }, "slack", SLACK("eng-plan"), [
      ["s4", msg("alice", ASK_PLAN)],
      [["s4.1", msg("bot", WORKING), "s4.3"], ["s4.3+0.1", msg("bot", PROPOSAL + BUTTONS), "s4.7"],
        ["s4.7+0.1", msg("bot", PROPOSAL_FILES + BUTTONS)]],
      ["s4.11+0.4", ephemeral("Only people mapped in notify.users can create tickets.")],
    ]);
    const R0 = D.layer(sec, { in: "s4", out: "s4.3" });
    D.term(R0.el, { x: 1180, y: 205, w: 640, in: "s4.1" }, "propose_tickets", [
      { c: "summary", in: "s4.1+0.3" },
      { c: "tickets: [{key, title, body,", in: "s4.1+0.5" },
      { c: "  repo, labels, parent,", in: "s4.1+0.7" },
      { c: "  blocked_by}]", in: "s4.1+0.9" },
      { c: "# at most 15 tickets", in: "s4.1+1.4" },
      { c: "# title ≤ 120, body ≤ 20,000", in: "s4.1+1.8" },
    ]);
    chip(R0.el, 1180, 590, "s4.2", "Ask: one ticket or a few", "violet");
    chip(R0.el, 1180, 660, "s4.2+1.6", "Plan: a plan, with a spec first", "violet");
    const R1 = D.layer(sec, { in: "s4.3", out: "s4.7" });
    card(R1.el, { x: 1180, y: 205, w: 640, h: 200, in: "s4.4" },
      '<span class="chip green">Create</span><p style="margin-top:12px">state <b>Backlog</b> (assistant.ticket_state), the ticket labels and <span class="mono">repo:acme/app</span></p>');
    card(R1.el, { x: 1180, y: 430, w: 640, h: 230, in: "s4.5" },
      '<span class="chip blue">Create and start</span><p style="margin-top:12px">the first active state, plus the required labels such as <span class="mono">symphony</span>: the daemon picks them up</p>');
    card(R1.el, { x: 1180, y: 685, w: 640, h: 130, in: "s4.6" },
      '<span class="chip red">Dismiss</span><p style="margin-top:12px">creates nothing</p>');
    const R2 = D.layer(sec, { in: "s4.7", out: "s4.8" });
    D.term(R2.el, { x: 1180, y: 205, w: 640, in: "s4.7" }, "Files from the sandbox", [
      { c: "attachments: [{path, title}]", in: "s4.7+0.3" },
      { c: "  path: /tmp/bucket.patch", in: "s4.7+0.6" },
      { c: "# read at the call", in: "s4.7+1.2" },
      { c: "# ≤ 10 files, 50 MB a proposal", in: "s4.7+1.5" },
      { c: "# ≤ 25 MB a file", in: "s4.7+1.8" },
      { c: "# no .git, no credential file", in: "s4.7+2.1" },
      { c: "# kept beside it, 7 days", in: "s4.7+2.4" },
    ]);
    chip(R2.el, 1180, 640, "s4.7+3.0", "Create: each file on its ticket", "green");
    const R3 = D.layer(sec, { in: "s4.8", out: "s4.10" });
    chat(R3.el, { x: 1180, y: 205, w: 640, in: "s4.8" }, "slack", SLACK("eng-help"), [
      ["s4.8", msg("alice", `${MENTION} add rate limits to the public API of acme/app`)],
      [["s4.8+1.4", msg("bot", PLAN_ANSWER + PLAN_BTN), "s4.9+0.6"], ["s4.9+0.7", msg("bot", PLAN_ANSWER)]],
      ["s4.9+1.0", msg("bot", "alice handed this thread to the planner.")],
      ["s4.9+1.5", msg("bot", WORKING)],
    ]);
    chip(R3.el, 1180, 790, "s4.9+0.2", "alice clicks Plan this → planner", "green");
    const R4 = D.layer(sec, { in: "s4.10" });
    D.term(R4.el, { x: 1180, y: 205, w: 640, in: "s4.10" }, "WORKFLOW.md: GitHub login → chat ID", [
      { c: "notify:", in: "s4.10+0.2" },
      { c: "  users:", in: "s4.10+0.3" },
      { c: "    slack:", in: "s4.10+0.4" },
      { c: "      alice: U0ALICE", in: "s4.10+0.6" },
      { c: "    discord:", in: "s4.10+0.8" },
      { c: "      octocat: \"123456789…\"", in: "s4.10+1.0" },
    ]);
    chip(R4.el, 1180, 640, "s4.11", "bob clicks Create: not mapped", "red");
    chip(R4.el, 1180, 700, "s4.11+0.8", "no ticket", "");
    chip(R4.el, 1180, 790, "s4.12", "audit actor: alice", "green");
  },

  // 5. Idempotency, audit and blocking relations
  s5(sec) {
    chat(sec, { x: 100, y: 205, w: 860, h: 685, in: "s5" }, "slack", SLACK("eng-plan"), [
      ["s5", msg("alice", ASK_PLAN)],
      [["s5", msg("bot", PROPOSAL + BUTTONS), "s5.1+0.6"],
        ["s5.1+0.7", msg("bot", "Creating the tickets…"), "s5.4"],
        ["s5.4+0.1", msg("bot", CREATED)]],
      ["s5.2+0.3", ephemeral("These tickets are being created.")],
    ]);
    const R1 = D.layer(sec, { in: "s5", out: "s5.5" });
    D.term(R1.el, { x: 1000, y: 205, w: 820, in: "s5" }, "Redis, next to the proposal", [
      { c: "sym:<ns>:assistant:proposal:3f9c…", in: "s5+0.3" },
      { c: "  status open · expires in 7 days", in: "s5+0.6" },
      { c: "…:claim     SET NX · 10 minutes", in: "s5.1+0.3" },
      { c: "…:records   issue:limits → ENG-1", in: "s5.3" },
      { c: "            issue:bucket → ENG-2", in: "s5.3+0.4" },
      { c: "            issue:retry  → ENG-3", in: "s5.3+0.8" },
      { c: "            rel:bucket>retry", in: "s5.3+1.2" },
      { c: "  status created · by alice", in: "s5.4+0.3" },
    ]);
    D.swap(R1.el, 1000, 690, [["s5.1", "click 1: alice takes the claim", "amber"], ["s5.2", "click 2: the claim is taken", "red"], ["s5.4", "double click: still 3 tickets", "green"]]);
    chip(R1.el, 1000, 760, "s5.4+0.8", "retry after a failure: no duplicate", "green");

    const R2 = D.layer(sec, { in: "s5.5" });
    const r = R2.el;
    const p = D.box(r, { x: 1000, y: 205, w: 820, h: 100, cls: "people", in: "s5.5", kick: "1 · parent", title: "ENG-1 &nbsp;API rate limits" });
    const k1 = D.box(r, { x: 1000, y: 365, w: 320, h: 150, cls: "trusted", in: "s5.5+0.6", kick: "2 · sub-issue", title: "ENG-2", sub: "Token bucket per key" });
    const k2 = D.box(r, { x: 1500, y: 365, w: 320, h: 150, cls: "trusted", in: "s5.5+1.2", kick: "3 · sub-issue", title: "ENG-3", sub: "429 with Retry-After" });
    D.wire(r, R2.sv, p, k1, { from: "b", to: "t", ff: 0.2, in: "s5.5+0.5" });
    D.wire(r, R2.sv, p, k2, { from: "b", to: "t", ff: 0.8, in: "s5.5+1.1" });
    D.wire(r, R2.sv, k1, k2, { in: "s5.6", color: "#ff6b6b", label: "blocks" });
    D.abs(r, "div", { x: 1000, y: 540, in: "s5.6+0.8" }, { html: '<span class="chip">Linear: a relation</span> <span class="chip">GitHub: a dependency</span>' });
    card(r, { x: 1000, y: 605, w: 820, h: 115, in: "s5.7" },
      '<p style="font-size:26px">If the tracker refuses the link, a comment on ENG-3:</p><p class="mono" style="color:var(--amber);font-size:26px">Blocked by ENG-2. The tracker did not record the link: …</p>', { padding: "12px 22px" });
    card(r, { x: 1000, y: 740, w: 820, h: 140, in: "s5.8" },
      '<h3 class="mono" style="font-size:28px;color:var(--green)">assistant.tickets_created</h3><p>actor alice · proposal:3f9c… · ENG-1, ENG-2, ENG-3</p>', { padding: "16px 22px" });
  },

  // 6. assist sessions in Tern
  s6(sec) {
    D.shot(sec, {
      x: 100, y: 205, w: 1720, h: 670, in: "s6",
      shots: [{ src: SHOT("sessions"), in: "s6" }],
      cam: [["s6", 1440, 640, 0.597], ["s6.3", 1975, 700, 0.95]],
      spots: [
        { x: 28, y: 428, w: 1925, h: 50, in: "s6.1+0.6", out: "s6.2" },
        { x: 990, y: 428, w: 640, h: 50, in: "s6.2", out: "s6.3" },
        { x: 2040, y: 424, w: 156, h: 52, in: "s6.3+0.9", out: "s6.4" },
        { x: 2188, y: 424, w: 196, h: 52, in: "s6.4", out: "s6.6" },
      ],
    });
    D.callout(sec, { x: 140, y: 790, in: "s6.1+0.8", out: "s6.3" }, 'Session <span class="mono">assist:slack:C0123ABCD:&lt;thread&gt;:…</span>');
    D.callout(sec, { x: 140, y: 790, in: "s6.3+0.6", out: "s6.4" }, "Open chat: the turn, live");
    D.callout(sec, { x: 140, y: 790, in: "s6.4+0.2", out: "s6.5" }, "Send message while it runs: a steer");
    D.callout(sec, { x: 140, y: 790, in: "s6.5+0.2", out: "s6.6" }, 'When idle: the next question · <span class="mono">alice asked from Tern: …</span>');
    D.callout(sec, { x: 140, y: 790, in: "s6.6+0.2" }, '<span class="mono">max_concurrent 3</span> · idle 30 min: the session ends');
  },

  // 7. Setup
  s7(sec) {
    D.abs(sec, "div", { x: 100, y: 205, in: "s7.1" }, {
      html: '<span style="font-size:30px">Reuses the notification bots:</span> &nbsp;<span class="chip">Slack bot token xoxb-…</span> <span class="chip">DISCORD_BOT_TOKEN</span>',
    });
    D.shot(sec, {
      x: 100, y: 280, w: 1720, h: 515, in: "s7.2", out: "s7.4",
      shots: [{ src: SHOT("setup-assistant"), in: "s7.2" }],
      cam: [["s7.2", 2266, 1180, 1.4], ["s7.3", 2266, 1400, 1.4]],
      spots: [{ x: 2042, y: 1146, w: 790, h: 62, in: "s7.2+0.6", out: "s7.3" }, { x: 2042, y: 1416, w: 530, h: 52, in: "s7.3+0.5" }],
    });
    D.callout(sec, { x: 100, y: 815, in: "s7.2+0.8", out: "s7.3" }, 'Socket Mode on · app-level token <span class="mono">xapp-…</span> · <span class="mono">connections:write</span>');
    D.callout(sec, { x: 100, y: 815, in: "s7.3+0.8", out: "s7.4" }, 'Bot › Message Content Intent on · else <span class="mono">message_content_intent_missing</span>');
    D.term(sec, { x: 100, y: 280, w: 1720, in: "s7.4", out: "s7.8" }, "Terminal", [
      { cmd: "symphony setup assistant --slack-channel C0123ABCD --repo acme/app", in: "s7.4+0.3" },
      { c: "# and --discord-channel <id> for Discord", in: "s7.4+1.6" },
      { c: "# the Slack app-level token (xapp-…): stdin or a hidden prompt; empty keeps the stored one", in: "s7.5" },
      { c: "# Slack: apps.connections.open (app token), auth.test (bot token)", in: "s7.6" },
      { c: "# Discord: the bot has the Message Content Intent", in: "s7.6+0.9" },
      { c: "# then: assistant: in WORKFLOW, and symphony-assistant restarts", in: "s7.7" },
      { cmd: "symphony assistant status", in: "s7.7+1.4" },
    ]);
    D.shot(sec, {
      x: 100, y: 280, w: 1720, h: 595, in: "s7.8",
      shots: [{ src: SHOT("setup-assistant"), in: "s7.8" }],
      cam: [["s7.8", 1440, 860, 0.597]],
      spots: [{ x: 28, y: 838, w: 1925, h: 50, in: "s7.8+0.6" }],
    });
  },

  // 8. Proof
  s8(sec) {
    const board = card(sec, { x: 100, y: 205, w: 1150, h: 680, in: "s8.1" }, "<h3>Mocked vendors, real Symphony processes, real agents</h3>");
    [["9", "Ask on Slack: the envelope acked, Working on it…, the answer with a code citation", "s8.2"],
      ["9", "A thread follow-up runs in the same assist session", "s8.3+0.8"],
      ["10", "Plan on Slack: ENG-1 with ENG-2 and ENG-3, and ENG-2 blocks ENG-3", "s8.4"],
      ["10", "An unmapped click refused; a double click: still 3 tickets; one audit entry", "s8.5"],
      ["11", "Discord: Identify asks for MESSAGE_CONTENT (intents 37377)", "s8.6"],
      ["11", "GitHub tracker: #1 with sub-issues #2 and #3; #3 blocked by #2", "s8.6+0.6"]].forEach(([n, text, ref]) => {
      const row = Engine.h("div", { html: `<span class="mono" style="color:var(--muted);flex:none;width:52px">${n}</span><span>${text} &nbsp;<span class="tick">✓</span></span>`, style: { display: "flex", fontSize: "28px", lineHeight: "1.4", marginTop: "14px" } });
      board.append(row);
      Engine.anim(row, { in: ref, lift: 6 });
    });
    const end = Engine.h("p", { html: "<b>Every check passed.</b> 69.5 s for scenarios 9, 10 and 12 (Linear, Slack); 43.2 s for scenario 11 (GitHub, Discord).", style: { marginTop: "22px", color: "var(--green)" } });
    board.append(end);
    Engine.anim(end, { in: "s8.7", lift: 6 });
    const tile = (y, ref, big, text, color) => D.abs(sec, "div", { x: 1290, y, w: 530, h: 210, in: ref, kind: "pop" }, {
      class: "card", html: `<div class="big" style="color:${color}">${big}</div><p style="margin-top:8px">${text}</p>`,
    });
    tile(205, "s8.2", "0.003 s", "the Slack envelope acked (limit 3 s)", "var(--blue)");
    tile(440, "s8.3", "10.1 s", "the answer; Working on it… came after 0.01 s", "var(--blue)");
    tile(675, "s8.4", "0.51 s", "a mapped click to three Linear tickets", "var(--green)");
  },
};
