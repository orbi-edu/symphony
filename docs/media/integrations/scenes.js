// Trackers and notifications: ten scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/configuration.md, docs/concepts.md, docs/internals.md §5 and §8, docs/api.md (Webhooks),
// docs/merger.md (T8), CHANGELOG.md, src/symphony/trackers/{github,linear}.py, src/symphony/notify.py and
// src/symphony/questions.py.

const SHOT = (name) => `../../tern/${name}-dark.png`;

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const note = (parent, x, y, ref, text, o = {}) =>
  D.abs(parent, "div", { x, y, in: ref, w: o.w, out: o.out }, { text, style: { fontSize: "28px", color: "var(--muted)", ...(o.style || {}) } });
const t = (ref, d) => Engine.at(ref) + d;

// A comment in GitHub's dark style: header html, then [ref, html] blocks in order.
function comment(parent, o, header, parts) {
  const el = D.abs(parent, "div", o, { class: "ghc" });
  el.append(Engine.h("div", { class: "hd", html: header }));
  const bd = Engine.h("div", { class: "bd" });
  el.append(bd);
  for (const [ref, html] of parts) {
    const p = Engine.h("div", { html });
    bd.append(p);
    Engine.anim(p, { in: ref, lift: 8 });
  }
  return el;
}

// Rows in a card, each fading in after the previous one.
function rows(el, ref, list, gap = 0.3, style = {}) {
  list.forEach((html, i) => {
    const r = Engine.h("p", { html, style: { fontSize: "28px", lineHeight: "1.6", ...style } });
    el.append(r);
    Engine.anim(r, { in: t(ref, 0.3 + i * gap), lift: 6 });
  });
}

const SCENES = {
  // 1. Overview
  s1(sec, sv) {
    const kinds = Engine.h("div", { class: "chips" }, [D.chip("Linear", "violet"), D.chip("GitHub Projects", "blue")]);
    Engine.anim(kinds, { in: "s1.2", lift: 6 });
    const trk = D.box(sec, { x: 100, y: 240, w: 480, h: 300, cls: "ext", in: "s1.1", kick: "Tracker", title: "Tickets",
      sub: "states, labels, comments", body: kinds });
    const sym = D.box(sec, { x: 740, y: 290, w: 440, h: 210, cls: "trusted", in: "s1.1+0.4", kick: "Symphony", title: "daemon · merger",
      sub: "dispatch, review, merge" });
    const chat = D.box(sec, { x: 1380, y: 290, w: 440, h: 210, cls: "people", in: "s1.1+0.8", out: "s1.4", kick: "Chat", title: "Notices" });
    D.wire(sec, sv, trk, sym, { in: "s1.1+0.3", bi: true, color: "#5cb8ff" });
    D.wire(sec, sv, sym, chat, { in: "s1.1+0.7", out: "s1.4", color: "#f5b452" });
    D.term(sec, { x: 100, y: 600, w: 1240, in: "s1.3" }, "WORKFLOW.md", [
      { c: "tracker:", in: "s1.3+0.2" },
      { c: "  kind: github        # linear | github | memory", in: "s1.3+0.5" },
      { c: "  provider: {owner: acme, project_number: 1, status_field: Status}", in: "s1.3+0.9" },
    ]);
    const slack = D.box(sec, { x: 1380, y: 230, w: 440, h: 140, cls: "people", in: "s1.4", kick: "Chat", title: "Slack" });
    const disc = D.box(sec, { x: 1380, y: 420, w: 440, h: 140, cls: "people", in: "s1.4+0.3", kick: "Chat", title: "Discord" });
    const w1 = D.wire(sec, sv, sym, slack, { in: "s1.4", color: "#f5b452" });
    const w2 = D.wire(sec, sv, sym, disc, { in: "s1.4+0.3", color: "#f5b452" });
    D.packet(sv, w1, "s1.4+0.6", "s1.4+1.2", "#f5b452");
    D.packet(sv, w2, "s1.4+0.8", "s1.4+1.4", "#f5b452");
    note(sec, 1380, 610, "s1.4+0.8", "one channel, the other, or both", { w: 440 });
  },

  // 2. States
  s2(sec) {
    D.swap(sec, 100, 205, [["s2.1", "Linear · the team's workflow states", "violet"], ["s2.2", "GitHub · the options of the board's Status field", "blue"]]);
    const cols = ["Todo", "In Progress", "Rework", "In Review", "Merging", "Done", "Canceled", "Duplicate"];
    const X = (i) => 100 + i * 215;
    cols.forEach((name, i) => {
      D.abs(sec, "div", { x: X(i), y: 290, w: 205, h: 400, in: t("s2.1", 0.2 + i * 0.12) }, { class: "col", html: `<div class="h">${name}</div>` });
    });
    const group = (i0, n, ref, color, label) => {
      D.abs(sec, "div", { x: X(i0) - 4, y: 286, w: n * 215 - 2, h: 408, in: ref, kind: "fade", lift: 0 },
        { style: { border: `4px solid ${color}`, borderRadius: "18px", boxShadow: `0 0 22px ${color}55` } });
      D.abs(sec, "div", { x: X(i0), y: 720, w: n * 215 - 10, in: ref }, { html: label, style: { fontSize: "28px", color } });
    };
    group(0, 3, "s2.3", "#5cb8ff", "<b>active</b> · the implementer works");
    group(3, 2, "s2.4", "#b197fc", "<b>parked</b> · the merger owns it");
    group(5, 3, "s2.5", "#3ddc97", "<b>terminal</b> · cleanup, retrospective");
    const tk = (i, y, ref, html, out = null) => D.abs(sec, "div", { x: X(i) + 12, y, w: 181, in: ref, out, kind: "pop" }, { class: "tk", html });
    tk(0, 370, "s2.3+0.4", 'app-12<br><span class="mono">agent</span>');
    tk(2, 370, "s2.3+0.8", 'app-8<br><span class="mono">agent</span>');
    tk(3, 370, "s2.4+0.4", 'app-9<br><span class="mono">merger</span>');
    tk(4, 370, "s2.4+0.8", 'app-6<br><span class="mono">train</span>');
    tk(5, 370, "s2.5+0.3", 'app-3');
    tk(1, 530, "s2.5+2.0", 'app-7<br><span style="color:var(--red)">closed</span>');
    D.abs(sec, "div", { x: 100, y: 790, in: "s2.5+2.4", out: "s2.6" }, { html: '<span class="chip red">app-7 is closed</span> &nbsp;<span style="font-size:30px">→ terminal on GitHub, whatever its Status says</span>' });
    // The abandon states, inside the terminal group.
    D.abs(sec, "div", { x: X(6) + 4, y: 294, w: 2 * 215 - 18, h: 392, in: "s2.6", kind: "fade", lift: 0 },
      { style: { border: "4px dashed var(--red)", borderRadius: "14px" } });
    tk(6, 370, "s2.6+0.6", 'app-5<br><span class="mono">PR closed</span>');
    D.abs(sec, "div", { x: 100, y: 790, in: "s2.6+0.3" }, { html: '<span class="chip red">tracker.abandon_states</span> &nbsp;<span style="font-size:30px">→ its open questions close, and the merger closes its pull requests</span>' });
  },

  // 3. Identifiers, labels, repositories
  s3(sec) {
    const lin = card(sec, { x: 100, y: 210, w: 820, h: 340, in: "s3.1" },
      '<p style="color:var(--violet);font-weight:700">LINEAR</p><div class="big mono">ENG-42</div><p style="margin-top:10px">Add the CSV export</p><p class="mono" style="margin-top:6px;color:var(--muted)">the Linear key: team ENG, issue 42</p>');
    const gh = card(sec, { x: 1000, y: 210, w: 820, h: 340, in: "s3.2" },
      '<p style="color:var(--blue);font-weight:700">GITHUB · acme/app#42</p><div class="big mono">app-42</div><p style="margin-top:10px">Add the CSV export</p><p class="mono" style="margin-top:6px;color:var(--muted)">branch symphony/app-42</p>');
    const labels = (el, ref, list) => {
      const row = Engine.h("div", { class: "chips" }, list.map(([txt, cls]) => D.chip(txt, cls)));
      el.append(row);
      Engine.anim(row, { in: ref, lift: 6 });
    };
    labels(lin, "s3.3+0.3", [["symphony", "green"]]);
    labels(gh, "s3.3+0.5", [["symphony", "green"]]);
    card(sec, { x: 100, y: 590, w: 820, h: 130, in: "s3.3+0.8" },
      '<p class="mono">tracker.required_labels: [symphony]</p><p style="color:var(--muted)">all of them, case ignored</p>');
    card(sec, { x: 100, y: 750, w: 820, h: 120, in: "s3.4" },
      '<p><span class="chip blue">repo:acme/app</span> names the repository</p><p style="color:var(--muted);margin-top:6px">two repo labels: one ticket, two repositories</p>', { padding: "14px 26px" });
    card(sec, { x: 1000, y: 590, w: 820, h: 280, in: "s3.4+1.6" },
      '<h3>No repo label on GitHub</h3><p>The issue\'s own repository, <span class="mono">acme/app</span>.</p><p style="color:var(--muted);margin-top:8px">With Linear, the default is <span class="mono">kubernetes.default_repo</span>.</p>');
  },

  // 4. Sub-issues and dependencies
  s4(sec, sv) {
    const tree = (x0, kick, parent, a, b, cls) => {
      D.abs(sec, "div", { x: x0, y: 205, in: "s4.1" }, { text: kick, style: { fontSize: "28px", fontWeight: "700", color: cls === "merger" ? "var(--violet)" : "var(--blue)" } });
      const P = D.box(sec, { x: x0, y: 250, w: 800, h: 100, cls, in: "s4.1+0.2", title: `<span class="mono" style="font-size:30px">${parent}</span>` });
      const A = D.box(sec, { x: x0, y: 430, w: 370, h: 110, cls: "center", in: "s4.1+0.8", title: `<span class="mono" style="font-size:30px">${a}</span>`, sub: "Add shout()" });
      const B = D.box(sec, { x: x0 + 430, y: 430, w: 370, h: 110, cls: "center", in: "s4.1+1.0", title: `<span class="mono" style="font-size:30px">${b}</span>`, sub: "Document shout()" });
      D.wire(sec, sv, P, A, { from: "b", to: "t", ff: 0.23, in: "s4.1+0.7", dash: true, noArrow: true });
      D.wire(sec, sv, P, B, { from: "b", to: "t", ff: 0.77, in: "s4.1+0.9", dash: true, noArrow: true });
      return { A, B };
    };
    const L = tree(100, "LINEAR", "ENG-40 · Plan: shout support", "ENG-41", "ENG-42", "merger");
    const G = tree(1020, "GITHUB", "app-40 · Plan: shout support", "app-41", "app-42", "trusted");
    D.wire(sec, sv, L.A, L.B, { in: "s4.2", color: "#f5b452", label: "blocks", lo: [0, 74] });
    D.wire(sec, sv, G.B, G.A, { from: "l", to: "r", in: "s4.3", color: "#f5b452", label: "blocked by", lo: [0, 74] });
    chip(sec, 1020, 610, "s4.3+0.6", "Status: Todo", "blue");
    chip(sec, 1450, 610, "s4.3+0.8", "Status: Todo", "blue");
    note(sec, 1020, 670, "s4.3+1.2", "the first active state, on the board");
    chip(sec, 100, 610, "s4.4", "ENG-42 waits for ENG-41", "amber");
    chip(sec, 100, 670, "s4.4+1.8", "ENG-41 Done → ENG-42 dispatches", "green");
    D.abs(sec, "div", { x: 100, y: 730, in: "s4.4+3.0" }, { html: '<span class="chip">parent link</span> &nbsp;<span style="font-size:28px;color:var(--muted)">never holds a child back</span>' });
    card(sec, { x: 100, y: 790, w: 1720, h: 90, in: "s4.5" },
      '<p><span class="mono" style="color:var(--amber)">symphony_create_issues</span> &nbsp;idempotent for each key: a second call returns the same issues</p>', { padding: "26px 26px" });
  },

  // 5. Workpad and evidence
  s5(sec) {
    const A = D.layer(sec, { in: "s5", out: "s5.3" });
    comment(A.el, { x: 100, y: 210, w: 900, h: 470, in: "s5.1" }, "<b>symphony[bot]</b> commented · edited", [
      ["s5.1+0.3", "<h4>## Workpad</h4>"],
      ["s5.1+0.7", "<p><b>Plan</b><br>1. Add <code>export_csv()</code> &nbsp;2. Test it &nbsp;3. Open the PR</p>"],
      ["s5.1+1.2", "<p><b>Notes</b><br>the export follows the list's filters</p>"],
      ["s5.1+1.7", "<p><b>Confusions</b><br>none</p>"],
    ]);
    chip(A.el, 120, 710, "s5.1+2.2", "one comment, edited in place", "green");
    D.abs(A.el, "div", { x: 1080, y: 230, in: "s5.2" }, { html: '<span class="chip violet">linear_graphql</span><br><br><span class="chip blue">github_graphql</span>' });
    card(A.el, { x: 1080, y: 420, w: 740, h: 230, in: "s5.2+1.2" },
      "<h3>Runs on the host</h3><p>The runner executes the call with the team's credential. The tracker credential never enters the workspace.</p>");

    const B = D.layer(sec, { in: "s5.3" });
    card(B.el, { x: 100, y: 210, w: 820, h: 270, in: "s5.3" },
      '<p style="color:var(--violet);font-weight:700">LINEAR</p><h3>A file on the issue</h3><p class="mono" style="color:var(--muted)">fileUpload → signed PUT → attachmentCreate</p><p style="margin-top:12px">Any file, at most 25 MB: a log, a screenshot, a video.</p><div class="chips"><span class="chip">pytest evidence · log</span></div>');
    D.abs(B.el, "div", { x: 1000, y: 210, in: "s5.4" }, { html: '<span style="font-size:28px;color:var(--blue);font-weight:700">GITHUB</span>' });
    comment(B.el, { x: 1000, y: 260, w: 820, h: 310, in: "s5.4" }, "<b>symphony[bot]</b> commented", [
      ["s5.4+0.4", '<p class="mono" style="font-size:26px;color:#6f7f94">&lt;!-- symphony:evidence {"title": "pytest evidence", "kind": "log"} --&gt;</p>'],
      ["s5.4+1.0", "<p>▸ <b>Evidence (log): pytest evidence — pytest.log</b></p>"],
      ["s5.4+1.6", '<p style="color:var(--muted)">a collapsible &lt;details&gt; with the text</p>'],
    ]);
    chip(B.el, 1000, 610, "s5.5", "at most 65,536 characters", "amber");
    card(B.el, { x: 1000, y: 680, w: 820, h: 130, in: "s5.5+1.6" },
      '<p class="mono" style="color:var(--red);font-size:26px">cannot attach shot.png (image/png): GitHub has no API to upload files to an issue …</p>', { borderColor: "var(--red)" });
  },

  // 6. Close, reopen, closing keywords
  s6(sec, sv) {
    const A = D.layer(sec, { in: "s6", out: "s6.3" });
    D.abs(A.el, "div", { x: 100, y: 215, in: "s6.1" }, { html: '<b class="mono" style="font-size:32px">acme/app#42 · app-42</b>' });
    const row = (y, ref, from, to, cls) => D.abs(A.el, "div", { x: 100, y, w: 1720, h: 96, in: ref }, {
      class: "node", html: `<div style="display:flex;gap:24px;align-items:center;font-size:30px"><span style="min-width:420px">Status → <b>${from}</b></span><span style="color:var(--muted)">→</span><span class="chip ${cls}">${to}</span></div>`,
      style: { padding: "0 26px" },
    });
    row(290, "s6.1+0.4", "Done", "closed · completed", "green");
    row(410, "s6.2+0.2", "Canceled, Duplicate", "closed · not planned", "amber");
    row(530, "s6.2+2.4", "Todo, In Review, …", "reopened", "blue");
    D.abs(A.el, "div", { x: 100, y: 670, in: "s6.1+1.6" }, { class: "mono", text: "PATCH /repos/acme/app/issues/42  {state, state_reason}", style: { fontSize: "28px", color: "var(--muted)" } });

    const B = D.layer(sec, { in: "s6.3", out: "s6.6" });
    comment(B.el, { x: 100, y: 210, w: 860, h: 200, in: "s6.3" }, "<b>symphony[bot]</b> opened PR #57", [
      ["s6.3+0.4", "<p><code>Closes acme/app#42</code></p>"],
      ["s6.3+1.0", "<p>Adds the CSV export, with a test.</p>"],
    ]);
    const steps = [["s6.4", "The train merges PR #57", "trusted"], ["s6.4+1.2", "GitHub closes acme/app#42", "trusted"], ["s6.4+2.6", "Item closed workflow → Status Done", "good"]];
    const B2 = steps.map(([ref, text, cls], i) => D.box(B.el, { x: 1080, y: 210 + i * 170, w: 740, h: 120, cls: `center ${cls}`, in: ref, title: `<span style="font-size:30px">${text}</span>` }));
    B2.slice(0, -1).forEach((b, i) => D.wire(B.el, B.sv, b, B2[i + 1], { from: "b", to: "t", in: steps[i + 1][0], color: "#5cb8ff" }));
    card(B.el, { x: 100, y: 560, w: 860, h: 240, in: "s6.5" },
      '<p style="color:var(--violet);font-weight:700">LINEAR</p><p>The Linear–GitHub integration moves <span class="mono">ENG-42</span> to Done when its pull request merges.</p>');

    const C = D.layer(sec, { in: "s6.6" });
    const links = [["branch", "symphony/app-42"], ["reference", "acme/app#42"], ["issue URL", "https://github.com/acme/app/issues/42"],
      ["closing keyword", "Closes #42  (also fixes, resolves)"], ["leading identifier", "app-42: Add the CSV export"]];
    links.forEach(([k, v], i) => D.abs(C.el, "div", { x: 100, y: 210 + i * 100, w: 1720, h: 84, in: t("s6.6", 0.3 + i * 0.6) }, {
      class: "node", html: `<div style="display:flex;gap:24px;align-items:center;font-size:30px"><b style="min-width:330px">${k}</b><span class="mono" style="color:var(--blue)">${v}</span></div>`,
      style: { padding: "0 26px" },
    }));
    note(C.el, 100, 730, t("s6.6", 3.6), "A structured reference wins over a leading identifier. With Linear, a key like ENG-42 in the text links it.", { w: 1720 });
  },

  // 7. Notices
  s7(sec) {
    const ev = card(sec, { x: 100, y: 210, w: 820, h: 620, in: "s7.1" }, "<h3>Events</h3>");
    rows(ev, "s7.1", [
      "🙋 Agent is waiting for an answer", "↩️ PR returned to its author", "🚨 A maintainer is needed",
      "👀 Waiting for human approval", "📱 Simulator preview ready", "✂️ Merge train batch was split",
      "🔴 main is broken", "🟢 main is green again", "❌ Run failed with an error",
      "🛑 Symphony stopped retrying a ticket", "📥 Ticket not queued", "🩺 Failure under investigation",
    ], 0.3, { lineHeight: "1.5" });
    const box = (y, ref, name, a, b) => D.abs(sec, "div", { x: 1000, y, w: 820, h: 200, in: ref, out: "s7.6" }, {
      class: "node", html: `<div class="t">${name}</div><div class="s mono" style="font-size:26px">bot: ${a}</div><div class="s mono" style="font-size:26px">or ${b}</div>`,
    });
    box(210, "s7.2", "Slack", "SLACK_BOT_TOKEN + SLACK_CHANNEL", "SLACK_WEBHOOK_URL");
    box(440, "s7.2+0.4", "Discord", "DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID", "DISCORD_WEBHOOK_URL");
    D.abs(sec, "div", { x: 1000, y: 215, w: 820, h: 425, in: "s7.3", kind: "fade", lift: 0, out: "s7.4+2" },
      { style: { border: "4px solid var(--amber)", borderRadius: "20px" } });
    chip(sec, 1000, 670, "s7.4", "both set → the bot wins", "amber", "s7.6");
    chip(sec, 1000, 740, "s7.5", "no channel → nothing sent", "", "s7.6");
    chip(sec, 1000, 810, "s7.5+1.8", "post() never raises · 2 s per send", "green", "s7.6");

    // Direct messages, and a question in Slack.
    const msg = (y, ref, where, html) => D.abs(sec, "div", { x: 1000, y, w: 820, in: ref }, { class: "msg", html: `<div class="who">${where}</div>${html}` });
    msg(210, "s7.6+0.3", "Slack · direct message to bob, the assignee",
      "🛑 <b>Symphony stopped retrying a ticket</b><br>ENG-42 stopped after 8 failed attempts in a row.");
    msg(410, "s7.7", "Slack · #symphony",
      '<div style="border-top:2px solid #2b3648;margin:4px 0 10px"></div>🙋 <b>Agent is waiting for an answer</b> · <span style="color:var(--blue)">ENG-42 · Add the CSV export</span> · <span class="at">@alice</span>'
      + '<div style="margin-top:8px">Which delimiter should the export use?</div>'
      + '<div style="margin-top:10px"><span class="chip">1. Comma</span> <span class="chip">2. Semicolon</span></div>'
      + '<div style="margin-top:8px;font-size:26px;color:var(--muted)">Answer with a button, in this thread, on the ticket or in a comment on the PR.</div>');
    chip(sec, 1000, 784, "s7.7+4.4", "its DM: the same buttons", "violet");
    chip(sec, 100, 850, "s7.8", "notify.direct_messages: true", "");
    chip(sec, 1000, 850, "s7.8+2.2", "notify.direct_events: [retries_exhausted]", "");
  },

  // 8. Mentions
  s8(sec) {
    const A = D.layer(sec, { in: "s8", out: "s8.4" });
    D.term(A.el, { x: 100, y: 210, w: 820, in: "s8.1" }, "WORKFLOW.md", [
      { c: "notify:", in: "s8.1+0.2" },
      { c: "  users:", in: "s8.1+0.4" },
      { c: "    slack:", in: "s8.1+0.8" },
      { c: "      octocat: U0OCTOCAT", in: "s8.1+1.0" },
      { c: "    discord:", in: "s8.1+1.6" },
      { c: "      octocat: \"123456789012345678\"", in: "s8.1+1.8" },
    ]);
    note(A.el, 100, 600, "s8.1+2.4", "GitHub logins ignore case", { w: 820 });
    const msg = (y, ref, where, html) => D.abs(A.el, "div", { x: 1000, y, w: 820, in: ref }, { class: "msg", html: `<div class="who">${where}</div>${html}` });
    msg(210, "s8.2", "Slack", '↩️ <b>PR returned to its author</b> <span class="at">@octocat</span><br>PR #4 ENG-8: add is_even went back to the author');
    msg(420, "s8.2+0.8", "Discord", '↩️ <b>PR returned to its author</b> <span class="at">@octocat</span><br>PR #4 ENG-8: add is_even went back to the author');
    msg(650, "s8.3", "Slack", '↩️ <b>PR returned to its author</b><br>PR #9 went back to the author <span class="chip" style="margin-left:8px">bob: no mapping</span>');

    const B = D.layer(sec, { in: "s8.4" });
    D.term(B.el, { x: 100, y: 210, w: 1720, in: "s8.4" }, "Discord: POST /channels/{id}/messages", [
      { c: '{"content": "↩️ **PR returned to its author** <@123456789012345678> …",', in: "s8.4+0.3" },
      { c: ' "allowed_mentions": {"parse": [], "users": ["123456789012345678"]}}', in: "s8.4+0.9" },
    ]);
    D.abs(B.el, "div", { x: 100, y: 440, in: "s8.4+2.2" }, { html: '<span class="chip red">a role</span> <span class="chip red">@everyone</span> <span class="chip red">@here</span> &nbsp;<span style="font-size:30px">never pinged, whatever the text says</span>' });
    card(B.el, { x: 100, y: 540, w: 1720, h: 250, in: "s8.5" },
      '<h3>At most 2,000 characters</h3><p class="mono" style="font-size:26px;line-height:1.6">↩️ **title** &lt;mention&gt;<br><span style="color:var(--amber)">the text, cut with …</span><br>[Open](&lt;link&gt;)</p><p style="margin-top:8px;color:var(--muted)">The title, the mention and the link stay.</p>');
  },

  // 9. Webhooks
  s9(sec) {
    const A = D.layer(sec, { in: "s9", out: "s9.6" });
    const lin = D.box(A.el, { x: 100, y: 240, w: 300, h: 110, cls: "ext center", in: "s9.1", title: "Linear" });
    const gh = D.box(A.el, { x: 100, y: 420, w: 300, h: 110, cls: "ext center", in: "s9.1+0.3", title: "GitHub" });
    const api = D.box(A.el, { x: 500, y: 240, w: 660, h: 290, cls: "trusted", in: "s9.1+0.5", kick: "Control API",
      title: '<span class="mono" style="font-size:30px">POST /v1/webhooks/linear</span>', sub: '<span class="mono" style="font-size:30px;color:var(--text);font-weight:680">POST /v1/webhooks/github</span>' });
    const w1 = D.wire(A.el, A.sv, lin, api, { in: "s9.1+0.6", tf: 0.35 });
    const w2 = D.wire(A.el, A.sv, gh, api, { in: "s9.1+0.8", tf: 0.75 });
    D.packet(A.sv, w1, "s9.1+1.2", "s9.1+1.7");
    D.packet(A.sv, w2, "s9.1+1.4", "s9.1+1.9");
    const chk = card(A.el, { x: 500, y: 560, w: 660, h: 230, in: "s9.2" }, "<h3>HMAC-SHA256 over the raw body</h3>");
    rows(chk, "s9.2", ['<span class="mono">Linear-Signature</span>', '<span class="mono">X-Hub-Signature-256</span>', 'Linear: <span class="mono">webhookTimestamp</span> within 5 min'], 0.6, { lineHeight: "1.45" });
    const dmn = D.box(A.el, { x: 1300, y: 240, w: 520, h: 130, cls: "trusted", in: "s9.3", kick: "wake", title: "Daemon", sub: "tickets" });
    const mrg = D.box(A.el, { x: 1300, y: 400, w: 520, h: 130, cls: "merger", in: "s9.3+1.6", kick: "wake", title: "Merger acme/app", sub: "PRs, CI, push to main" });
    const w3 = D.wire(A.el, A.sv, api, dmn, { in: "s9.3+0.2", ff: 0.3, color: "#5cb8ff" });
    const w4 = D.wire(A.el, A.sv, api, mrg, { in: "s9.3+1.6", ff: 0.7, color: "#b197fc" });
    D.packet(A.sv, w3, "s9.3+0.6", "s9.3+1.1");
    D.packet(A.sv, w4, "s9.3+2.0", "s9.3+2.5", "#b197fc");
    D.abs(A.el, "div", { x: 1300, y: 565, w: 520, in: "s9.4" }, { text: "GitHub tracker, to the daemon:", style: { fontSize: "28px", color: "var(--muted)" } });
    D.abs(A.el, "div", { x: 1300, y: 610, w: 520, in: "s9.4+0.4" }, { html: '<div class="chips"><span class="chip blue">issues</span><span class="chip blue">sub_issues</span><span class="chip blue">issue_dependencies</span><span class="chip blue">projects_v2_item</span></div>' });
    D.abs(A.el, "div", { x: 100, y: 820, in: "s9.5" }, { html: '<span class="chip amber">a wake is a hint</span> &nbsp;<span style="font-size:28px">the loop reads the source again · safety poll 15 s (daemon), 30 s (merger)</span>' });

    const B = D.layer(sec, { in: "s9.6" });
    D.shot(B.el, {
      x: 100, y: 200, w: 1720, h: 670, in: "s9.6",
      shots: [{ src: SHOT("setup-webhooks"), in: "s9.6" }],
      cam: [["s9.6", 1440, 960, 0.6], ["s9.6+1.0", 1763, 1037, 0.77]],
      spots: [{ x: 1982, y: 611, w: 870, h: 852, in: "s9.6+1.8" }],
    });
  },

  // 10. Main watch
  s10(sec) {
    D.abs(sec, "div", { x: 100, y: 290, w: 1720, h: 8, in: "s10.1", kind: "wipe", dur: 0.8 }, { style: { background: "#3a4a62", borderRadius: "4px" } });
    D.abs(sec, "div", { x: 100, y: 215, in: "s10.1" }, { html: '<span class="mono" style="font-size:28px;color:var(--muted)">main · finished push runs of CI</span>' });
    [[300, "var(--green)", "s10.1+0.4"], [520, "var(--green)", "s10.1+0.7"], [740, "var(--red)", "s10.2"], [1500, "var(--green)", "s10.5"]].forEach(([x, c, ref]) =>
      D.abs(sec, "div", { x, y: 278, w: 32, h: 32, in: ref, kind: "pop" }, { style: { borderRadius: "50%", background: c } }));
    D.abs(sec, "div", { x: 756, y: 290, w: 760, h: 8, in: "s10.2", out: "s10.5", kind: "wipe" }, { style: { background: "var(--red)" } });
    card(sec, { x: 100, y: 350, w: 1000, h: 220, in: "s10.2+0.4" },
      '<h3 style="color:var(--red)">Main CI red: CI on d82d75555</h3><p>CI on <span class="mono">main</span> failed on d82d75555 (…).</p><p class="mono" style="color:var(--amber);margin-top:10px;font-size:26px">FAILED tests/test_greet.py::test_greet - AssertionError</p>',
      { borderColor: "var(--red)" });
    card(sec, { x: 1160, y: 350, w: 660, h: 220, in: "s10.3" },
      '<p><b style="color:var(--violet)">Linear</b> &nbsp;<span class="mono">ENG-7</span><br><span style="color:var(--muted)">the board\'s project, or team ENG</span></p><p style="margin-top:14px"><b style="color:var(--blue)">GitHub</b> &nbsp;<span class="mono">acme/app#7 · app-7</span><br><span style="color:var(--muted)">an issue, put on the board in Todo</span></p>');
    D.abs(sec, "div", { x: 100, y: 660, in: "s10.4", out: "s10.6" }, { html: '<span class="chip blue">repo:acme/app</span> <span class="chip blue">symphony</span> <span class="chip blue">ci</span> &nbsp;<span style="font-size:28px;color:var(--muted)">missing labels are created</span>' });
    chip(sec, 100, 740, "s10.4+1.5", "daemon → an agent fixes main", "violet", "s10.6");
    D.abs(sec, "div", { x: 1160, y: 660, w: 660, h: 210, in: "s10.5+0.6" }, { class: "card", html: "<p>Main CI is green again on 5c3a9e7f1 (…); closing this alert.</p><p style='color:var(--green);margin-top:8px'>ticket → Done · 🟢 main_green</p>" });
    const tile = (x, ref, big, text) => D.abs(sec, "div", { x, y: 660, w: 490, h: 210, in: ref, kind: "pop" }, {
      class: "card", html: `<div class="big" style="color:var(--green)">${big}</div><p style="margin-top:8px">${text}</p>`,
    });
    tile(100, "s10.6", "2.3 s", "mocked, Linear: red run → ticket ENG-7");
    tile(610, "s10.6+0.6", "2.7 s", "mocked, GitHub: red run → issue #7 on the board");
  },
};
