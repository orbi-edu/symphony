// The merger deep dive: nine scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/merger.md, src/symphony/entry/merger.py and src/symphony/merger/; the comment texts
// are the formats of merger/comments.py and merger/review.py (compose, ci_line, report_body, hand_off_text,
// DRAFTED, preview_comment), merger/approval.py (the needs_approval notice) and merger/abandon.py.

const SHOT = (name) => `../../tern/${name}-dark.png`;
const SHA = "d45226c47"; // example head commits (9 characters, as the merger prints them)
const SHA2 = "7c1e0b2aa";

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const t = (ref, d) => Engine.at(ref) + d;

// A pull request comment in GitHub's dark style. `parts`: [ref, html, out] blocks in order; a block
// can also be a list of such entries that share one place (an edit in place).
function comment(parent, o, header, parts) {
  const el = D.abs(parent, "div", o, { class: "ghc" });
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

const SCENES = {
  // 1. What the merger is
  s1(sec, sv) {
    const pr = D.box(sec, { x: 100, y: 300, w: 330, h: 230, cls: "ext", in: "s1.1", kick: "acme/app", title: "PR #42",
      sub: "ready for review", body: D.chips([["ready", "green"]]) });
    // One merger process; each repository is a lane inside it.
    D.abs(sec, "div", { x: 500, y: 215, w: 920, h: 590, in: "s1.1+0.4", kind: "fade", lift: 0 },
      { style: { border: "3px solid var(--violet)", borderRadius: "24px", background: "rgba(177,151,252,0.05)" } });
    D.abs(sec, "div", { x: 528, y: 226, in: "s1.1+0.4" }, { html: '<span style="font-size:34px;font-weight:700">Merger</span>' });
    D.abs(sec, "div", { x: 525, y: 280, w: 870, h: 340, in: "s1.1+0.4", kind: "fade", lift: 0 },
      { style: { border: "2px solid #3a475b", borderRadius: "16px" } });
    D.abs(sec, "div", { x: 545, y: 288, in: "s1.1+0.4" }, { class: "mono", text: "acme/app", style: { fontSize: "28px", color: "var(--muted)" } });
    const review = D.box(sec, { x: 545, y: 335, w: 400, h: 215, cls: "merger", in: "s1.5", kick: "Stage 1", title: "Review",
      sub: "agents review, one report", body: D.chips([["lanes", "violet"], ["oracle", "violet"]]) });
    const train = D.box(sec, { x: 975, y: 335, w: 400, h: 215, cls: "trusted", in: "s1.6", kick: "Stage 2", title: "Merge train",
      sub: "rebase, test, merge", body: D.chips([["deterministic", "blue"]]) });
    const main = D.box(sec, { x: 1490, y: 300, w: 330, h: 230, cls: "good", in: "s1.6+1.0", kick: "acme/app", title: "main",
      body: D.chips([["merged", "green"]]) });
    const w1 = D.wire(sec, sv, pr, review, { in: "s1.5" });
    const w2 = D.wire(sec, sv, review, train, { in: "s1.6", color: "#b197fc" });
    const w3 = D.wire(sec, sv, train, main, { in: "s1.6+0.8", color: "#3ddc97" });
    D.packet(sv, w1, "s1.5+0.3", "s1.5+0.9", "#5cb8ff");
    D.packet(sv, w2, "s1.6+0.2", "s1.6+0.7", "#b197fc");
    D.packet(sv, w3, "s1.6+1.0", "s1.6+1.5", "#3ddc97");
    chip(sec, 975, 562, "s1.7", "agents only on a conflict", "amber");
    ["acme/lib", "acme/web"].forEach((r, i) =>
      D.box(sec, { x: 525 + i * 445, y: 640, w: 425, h: 90, cls: "merger", in: `s1.2+${(0.3 + i * 0.3).toFixed(1)}`,
        title: `<span class="mono" style="font-size:30px">${r}</span>` }));
    D.abs(sec, "div", { x: 100, y: 640, w: 370, in: "s1.2+0.2" }, { text: "one merger, every repository with a policy", style: { fontSize: "30px", color: "var(--muted)" } });
    chip(sec, 525, 745, "s1.3", "each: own train, lease and review workers", "violet");
    D.abs(sec, "div", { x: 100, y: 835, in: "s1.4" }, { html: '<span class="chip violet">AI agents review</span> &nbsp; <span class="chip">no outside review service</span> &nbsp; <span class="chip amber">human approval: optional</span>' });
  },

  // 2. How it watches
  s2(sec) {
    const A = D.layer(sec, { in: "s2", out: "s2.9" });
    const p = A.el;
    const sv = A.sv;
    const events = ["pull_request", "check_run", "check_suite", "workflow_run", "push to main"];
    events.forEach((e, i) => chip(p, 100, 240 + i * 70, `s2.1+${(0.2 + i * 0.3).toFixed(1)}`, e, "blue"));
    const api = D.box(p, { x: 420, y: 270, w: 330, h: 130, cls: "trusted", in: "s2.1+0.3", title: "Control API", sub: '<span class="mono">/v1/webhooks/github</span>' });
    const wake = D.box(p, { x: 420, y: 470, w: 330, h: 130, cls: "store", in: "s2.2", title: "Wake list", sub: '<span class="mono">merger:acme/app</span>' });
    D.box(p, { x: 420, y: 640, w: 330, h: 90, cls: "store", in: "s2.2+0.8", o: [["s2", 1], ["s2.2+0.8", 0.35]], title: '<span class="mono" style="font-size:28px">merger:acme/lib</span>' });
    D.wire(p, sv, [360, 400], api, { in: "s2.1+0.8" });
    const ww = D.wire(p, sv, api, wake, { from: "b", to: "t", in: "s2.2", label: "wake", color: "#5cb8ff" });
    D.packet(sv, ww, "s2.2+0.3", "s2.2+0.9");
    const coord = D.box(p, { x: 850, y: 360, w: 330, h: 190, cls: "trusted", in: "s2.3", kick: "Coordinator", title: '<span class="mono" style="font-size:30px">wake.wait</span>', sub: "returns on a wake, or after 30 s" });
    D.wire(p, sv, wake, coord, { in: "s2.3", color: "#5cb8ff" });
    const loops = ["review worker 1", "review worker 2", "train loop", "main watch"].map((n, i) =>
      D.box(p, { x: 1290, y: 220 + i * 100, w: 330, h: 90, cls: i < 2 ? "merger center" : "trusted center", in: `s2.4+${(0.2 + i * 0.25).toFixed(2)}`, title: `<span style="font-size:30px">${n}</span>` }));
    loops.forEach((l, i) => {
      const w = D.wire(p, sv, coord, l, { in: `s2.4+${(0.1 + i * 0.25).toFixed(2)}`, color: "#b197fc" });
      D.packet(sv, w, t("s2.4", 1.4), t("s2.4", 2.0), "#b197fc", 8);
    });
    D.abs(p, "div", { x: 1290, y: 622, in: "s2.4+1.5" }, { class: "mono", text: "SYMPHONY_MERGER_REVIEWERS=2", style: { fontSize: "28px", color: "var(--muted)" } });
    D.abs(p, "div", { x: 1290, y: 664, in: "s2.5" }, { class: "mono", text: "SYMPHONY_MERGER_CONCURRENCY=8", style: { fontSize: "28px", color: "var(--violet)" } });
    chip(p, 850, 580, "s2.6", "heartbeat · TTL 90 s", "green");
    card(p, { x: 820, y: 720, w: 1000, h: 160, in: "s2.7" },
      '<h3>Claim <span class="mono" style="color:var(--amber)">pr:42</span></h3><p>Held for 2 minutes and renewed by a heartbeat while the worker runs. A fix slot lasts 1 minute.</p>');
    chip(p, 100, 640, "s2.8", "pod A stops ✕", "red");
    chip(p, 100, 720, "s2.8+1.2", "claim expires", "amber");
    chip(p, 100, 800, "s2.8+2.2", "pod B takes pr:42", "green");

    // The queue
    const B = D.layer(sec, { in: "s2.9" });
    const q = B.el;
    const groups = [
      ["1 · label review-bypass", [["#51", "Bump the lockfile", "ready 2 min ago", 1]]],
      ["2 · re-reviews (a baseline exists)", [["#47", "Retry the webhook", "ready 9 min ago", 2], ["#49", "Cache the session list", "ready 3 min ago", 3]]],
      ["3 · first reviews", [["#44", "Add the CSV export", "ready 12 min ago", 4], ["#52", "Fix the date filter", "ready 1 min ago", 5]]],
    ];
    let y = 220;
    groups.forEach(([title, prs], gi) => {
      D.abs(q, "div", { x: 100, y, in: `s2.9+${(0.2 + gi * 0.7).toFixed(1)}` }, { text: title, style: { fontSize: "30px", fontWeight: "700", color: gi === 0 ? "var(--amber)" : gi === 1 ? "var(--violet)" : "var(--blue)" } });
      y += 50;
      prs.forEach(([n, name, ready, order]) => {
        D.abs(q, "div", { x: 100, y, w: 960, h: 62, in: `s2.9+${(0.4 + gi * 0.7).toFixed(1)}` }, {
          class: "node", html: `<div style="display:flex;gap:22px;font-size:28px;align-items:center"><b class="mono">${n}</b><span>${name}</span><span style="margin-left:auto;color:var(--muted)">${ready}</span></div>`,
          style: { padding: "0 22px" },
        });
        D.abs(q, "div", { x: 1080, y: y + 8, in: `s2.10+${(0.2 + order * 0.35).toFixed(2)}`, kind: "pop" }, {
          text: String(order), style: { width: "46px", height: "46px", borderRadius: "50%", background: "var(--green)", color: "#06110c", fontSize: "28px", fontWeight: "800", display: "flex", alignItems: "center", justifyContent: "center" },
        });
        y += 76;
      });
      y += 16;
    });
    const checks = ["open", "not a draft", "base is the default branch", "the head has a check run", "head not reviewed or parked yet", "not in the train", "no active skip"];
    const list = card(q, { x: 1200, y: 220, w: 620, h: 560, in: "s2.11" }, "<h3>Eligible when</h3>");
    checks.forEach((c, i) => {
      const row = Engine.h("p", { html: `<span class="tick">✓</span> ${c}`, style: { fontSize: "28px", lineHeight: "1.7" } });
      list.append(row);
      Engine.anim(row, { in: t("s2.11", 0.3 + i * 0.3), lift: 6 });
    });
    // Label filter (merger.label_filter, on by default).
    const labelRow = Engine.h("p", { html: '<span class="tick">✓</span> <span style="color:var(--amber)">a symphony label, or review-bypass</span>', style: { fontSize: "28px", lineHeight: "1.7" } });
    list.append(labelRow);
    Engine.anim(labelRow, { in: t("s2.12", 0.3), lift: 6 });
    chip(q, 1200, 800, "s2.12+1.2", "merger.label_filter", "amber");
  },

  // 3. One review
  s3(sec, sv) {
    const names = ["Probe", "Precheck", "Reviewer lanes", "Oracle", "Both agree?"];
    const ins = ["s3.1", "s3.4", "s3.7", "s3.10", "s3.11"];
    const cls = ["", "", "merger", "merger", "people"];
    const N = names.map((n, i) => D.box(sec, { x: 100 + i * 355, y: 210, w: 300, h: 100, cls: `center ${cls[i]}`, in: ins[i], title: `<span style="font-size:32px">${n}</span>` }));
    N.slice(0, -1).forEach((b, i) => D.wire(sec, sv, b, N[i + 1], { in: ins[i + 1], color: "#5b6b82" }));
    D.abs(sec, "div", { x: 92, y: 202, w: 316, h: 116, in: "s3.1+0.3", dx: [["s3.1", 0], ["s3.4", 355], ["s3.7", 710], ["s3.10", 1065], ["s3.11", 1420]] },
      { style: { border: "4px solid #5cb8ff", borderRadius: "20px", boxShadow: "0 0 26px rgba(92,184,255,0.5)" } });

    const P = D.layer(sec, { in: "s3.1", out: "s3.4" });
    card(P.el, { x: 100, y: 380, w: 820, h: 300, in: "s3.2" },
      `<h3>A nonce only the read tool can see</h3><p class="mono" style="color:var(--amber)">probe/reviewer-9f3c…/PROBE.txt → 9f3c41d07a2be815</p><p style="margin-top:12px">The agent returns it with <span class="mono">submit_review {summary}</span>. Each probe has its own directory and nonce. The result is cached 10 minutes per role and model.</p>`);
    card(P.el, { x: 980, y: 380, w: 840, h: 300, in: "s3.1+0.5" },
      `<h3>Roles probed</h3><div class="chips"><span class="chip violet">reviewer</span><span class="chip violet">oracle</span><span class="chip violet">arbiter</span></div><p style="margin-top:14px">Before a conflict resolution: developer and reviewer.</p>`);
    D.abs(P.el, "div", { x: 100, y: 720, in: "s3.3" }, { html: '<b style="font-size:30px">Probe fails:</b> &nbsp;<span class="chip red">wait 60 s</span> <span class="chip red">2 min</span> <span class="chip red">5 min</span> <span class="chip red">10 min</span> &nbsp;<span style="font-size:28px;color:var(--muted)">then try again</span>' });

    const R = D.layer(sec, { in: "s3.4", out: "s3.7" });
    const rules = [
      ["Commit subjects", "empty, fixup!, squash!, amend!, WIP, over 100 characters"],
      ["Large files", "over max_file_kib"],
      ["Local paths", "/Users/…, /home/…, C:\\Users\\…"],
      ["Docs", "broken relative links"],
      ["Secrets", "AWS, GitHub, private keys, Slack, sk-…, Linear"],
    ];
    rules.forEach(([k, v], i) => D.abs(R.el, "div", { x: 100, y: 375 + i * 94, w: 1060, h: 84, in: t("s3.5", i * 0.35) }, {
      class: "node", html: `<div style="display:flex;gap:20px;font-size:28px;align-items:center;line-height:1.25"><b style="min-width:240px">${k}</b><span class="mono" style="color:var(--muted)">${v}</span></div>`, style: { padding: "0 22px" },
    }));
    D.term(R.el, { x: 1210, y: 380, w: 610, in: "s3.6" }, ".symphony/merger.md (on main)", [
      { c: "rule_checks:", in: "s3.6+0.2" },
      { c: "  - {name: lint, run: make lint}", in: "s3.6+0.5" },
    ]);
    D.abs(R.el, "div", { x: 1210, y: 620, w: 610, in: "s3.6+1.0" }, { text: "A break blocks the pull request. All results go into the same report.", style: { fontSize: "28px", color: "var(--muted)" } });

    const L = D.layer(sec, { in: "s3.7", out: "s3.10" });
    ["correctness", "security-spec", "design"].forEach((n, i) =>
      D.box(L.el, { x: 100, y: 380 + i * 120, w: 420, h: 96, cls: "merger center", in: t("s3.8", i * 0.3), title: `<span class="mono" style="font-size:30px">${n}</span>` }));
    D.term(L.el, { x: 600, y: 380, w: 1220, in: "s3.9" }, "Typed output: host tools with a schema", [
      { c: "submit_finding  {severity, class, audit, location, claim, fix}", in: "s3.9+0.3" },
      { c: "                severity: blocking | nit · one call per finding", in: "s3.9+0.9" },
      { c: "submit_review   {strengths, verified}   ← terminal, counts once", in: "s3.9+1.5" },
    ]);
    D.abs(L.el, "div", { x: 600, y: 640, w: 1220, in: "s3.9+2.2" }, { text: "An invalid call returns the error and does not count. A turn with no terminal call gets one reminder.", style: { fontSize: "28px", color: "var(--muted)" } });

    const O = D.layer(sec, { in: "s3.10" });
    D.abs(O.el, "div", { x: 100, y: 390, in: "s3.10+0.2" }, { html: '<span class="chip">A</span> <span class="chip">B</span> <span class="chip">C</span> <span class="chip">D</span> &nbsp;→ <b style="font-size:30px">oracle call 1</b>' });
    D.abs(O.el, "div", { x: 100, y: 460, in: "s3.10+0.6" }, { html: '<span class="chip">E</span> <span class="chip">F</span> &nbsp;→ <b style="font-size:30px">oracle call 2</b> <span style="font-size:28px;color:var(--muted)">&nbsp;in parallel</span>' });
    D.abs(O.el, "div", { x: 100, y: 540, in: "s3.10+1.4" }, { html: '<span class="chip green">confirmed</span> <span class="chip green">partial</span> <span class="chip red">refuted</span> <span class="chip">defer</span>' });
    card(O.el, { x: 900, y: 380, w: 920, h: 300, in: "s3.11" },
      `<h3>A finding blocks only when</h3><p>the reviewer grades it <b>blocking</b>, the oracle grades it <b>blocking</b>, its class is a blocking class (bug, regression, security, spec, duplicate-owner), and the verdict is confirmed or partial.</p>`);
    D.abs(O.el, "div", { x: 100, y: 740, in: "s3.12" }, { html: '<span class="chip blue">--tools read,grep</span> &nbsp;<span style="font-size:30px">reviewer, oracle and arbiter cannot edit</span>' });
  },

  // 4. What it writes on the PR
  s4(sec) {
    D.abs(sec, "div", { x: 100, y: 205, w: 1720, h: 66, in: "s4.1" }, {
      class: "node", html: '<div style="display:flex;gap:20px;align-items:center;font-size:30px"><b>#4</b><span>ENG-8: add is_even</span><span class="mono" style="font-size:28px;color:var(--muted)">acme/app · eng-8-is-even</span></div>', style: { padding: "0 22px" },
    });
    // Right panel: the PR's labels, the ticket and the channel.
    const rp = { x: 1290, w: 530 };
    D.abs(sec, "div", { x: rp.x, y: 300, in: "s4.1" }, { text: "Labels and state", style: { fontSize: "28px", color: "var(--muted)" } });
    D.swap(sec, rp.x, 345, [
      ["s4.1+0.3", "in-audit", "violet"], ["s4.5", "(in-audit removed)", ""], ["s4.7", "reviewed", "green"],
      ["s4.9", "wait-approval", "amber"], ["s4.10", "in-audit", "violet"], ["s4.11", "draft · review-requested", "red"],
      ["s4.14", "(in-audit removed)", ""],
    ]);
    D.swap(sec, rp.x, 425, [["s4.7+0.8", "ticket ENG-8 → Merging", "violet"], ["s4.8+0.6", "ticket waits for the approval", "amber"],
      ["s4.11+0.8", "ticket ENG-8 → Rework", "red"], ["s4.14", "ticket unchanged", ""]]);
    D.abs(sec, "div", { x: rp.x, y: 505, w: rp.w, h: 285, in: "s4.9+0.8", out: "s4.10" }, { class: "card", html: `<p style="font-size:28px"><b>needs_approval</b> to the reviewers<br>PR #4 ENG-8: add is_even<br>CI: passed on ${SHA}<br>Waiting on: an approval on GitHub from alice</p>` });
    D.abs(sec, "div", { x: rp.x, y: 505, w: rp.w, h: 150, in: "s4.11+1.6", out: "s4.14" }, { class: "card", html: '<p style="font-size:28px"><b>Slack / Discord</b><br>PR #4 ENG-8: add is_even went back to the author</p>' });
    D.abs(sec, "div", { x: rp.x, y: 670, w: rp.w, h: 130, in: "s4.12", out: "s4.13" }, { class: "card", html: '<p style="font-size:28px">Draft at most once. The baseline counts the rounds.</p>' });
    D.abs(sec, "div", { x: rp.x, y: 670, w: rp.w, h: 215, in: "s4.13", out: "s4.14" }, { class: "card", html: '<p style="font-size:28px"><b class="mono">polish_rounds: 1</b><br>An agent\'s PR with fixable nits goes back to draft. Not counted as the rejection. Never a person\'s PR.</p>' });
    D.abs(sec, "div", { x: rp.x, y: 505, w: rp.w, h: 220, in: "s4.14+0.4" }, { class: "card", html: '<p style="font-size:28px"><b>run_error</b> to the channel<br>The review of PR #4 ENG-8: add is_even failed (no PR comment posted): …</p>' });

    // The parked report, edited in place when CI ends.
    const head = '<b>symphony[bot]</b> commented';
    comment(sec, { x: 100, y: 300, w: 1150, h: 585, in: "s4.2", out: "s4.8" }, head, [
      [["s4.3", "<b>Waiting for CI</b>", "s4.6+0.4"],
        ["s4.6+0.6", '<b>Reviewed</b> <span style="color:var(--muted);font-size:26px">(edited)</span>']],
      [["s4.4", `<p><b>symphony-merger</b> on <code>${SHA}</code>: reviewed, nothing blocking: once CI passes on <code>${SHA}</code> the merge train takes it; a red CI sends it back.</p>`, "s4.6+0.4"],
        ["s4.6+0.6", `<p><b>symphony-merger</b> on <code>${SHA}</code>: reviewed, waiting to merge: the merge train rebases <code>${SHA}</code> onto main and merges it once CI passes, else it goes back to draft.</p>`]],
      [["s4.4+1.0", `<p>CI is still running on <code>${SHA}</code>: its result is added to this comment when it ends.</p>`, "s4.6+0.4"],
        ["s4.6+0.6", `<p>CI passed on <code>${SHA}</code>.</p>`]],
      ["s4.4+1.8", "<p>No reviewer findings.</p>"],
    ]);
    D.abs(sec, "div", { x: 1290, y: 520, w: 530, in: "s4.3", out: "s4.7" }, { html: '<p style="font-size:28px;color:var(--muted);margin:0 0 10px">Report titles</p><span class="chip green">Reviewed</span> <span class="chip red">Not merged</span><br><span class="chip amber" style="margin-top:10px">Waiting for CI</span>' });
    D.abs(sec, "div", { x: 120, y: 820, in: "s4.5", out: "s4.8" }, { html: '<span class="chip amber">parked: kind clean</span> &nbsp;<span class="chip">settle_parked edits the same comment</span>' });

    // The approval gate (require_approval): the PR's merge box on GitHub.
    comment(sec, { x: 100, y: 300, w: 1150, h: 585, in: "s4.8", out: "s4.10" }, "<b>Review required</b> · merging waits", [
      ["s4.8", "<p>At least one approving review, and no changes requested: <code>require_approval: true</code> in <code>.symphony/merger.md</code>.</p>"],
      ["s4.8+1.2", `<p>Merger review: nothing blocking. CI passed on <code>${SHA}</code>.</p>`],
      ["s4.9", '<p>Label <code>wait-approval</code>. Tern and <code>symphony merger status</code> show it as <b style="color:var(--amber)">waiting for approval</b>.</p>'],
      ["s4.9+2.0", "<p>Once approved, the merge train takes it, and checks the approval again before it merges.</p>"],
    ]);

    // The rejection report.
    comment(sec, { x: 100, y: 300, w: 1150, h: 585, in: "s4.10", out: "s4.14" }, head, [
      ["s4.10", "<b>Not merged</b>"],
      ["s4.10+0.3", `<p><b>symphony-merger</b> on <code>${SHA2}</code>: not merged: #4 A: confirmed, for its author to fix.</p>`],
      ["s4.10+0.8", "<h4>A: blocking (bug), confirmed</h4><p><b>Location:</b> app/parity.py:6</p><p><b>Claim:</b> <code>return n % 2 == 1</code> is an odd check, not an even check.</p>"],
      ["s4.10+1.6", '<p><b>Suggested fix:</b><br><span class="box"></span>Change app/parity.py:6 to <code>return n % 2 == 0</code>.</p>'],
      ["s4.11", "<p>This PR is back to draft with the <code>review-requested</code> label: address this comment, then mark it ready for review.</p>"],
    ]);
    // A failed run: no comment at all.
    D.abs(sec, "div", { x: 100, y: 300, w: 1150, h: 585, in: "s4.14" }, {
      html: '<div style="display:flex;height:100%;align-items:center;justify-content:center;font-size:34px;color:var(--muted)">no comment on the PR, no draft</div>',
      style: { border: "3px dashed #3a475b", borderRadius: "14px" },
    });
  },

  // 5. It keeps watching
  s5(sec) {
    const G = D.layer(sec, { in: "s5", out: "s5.8" });
    const g = G.el;
    const gs = G.sv;
    const dot = (x, y, ref, color, label, out = null, below = false) => {
      D.abs(g, "div", { x: x - 16, y: y - 16, w: 32, h: 32, in: ref, out, kind: "pop" }, { style: { borderRadius: "50%", background: color } });
      if (label) D.abs(g, "div", { x: x - 60, y: below ? y + 26 : y - 64, w: 120, in: ref, out }, { class: "mono", text: label, style: { fontSize: "28px", textAlign: "center", color: "var(--muted)" } });
    };
    const line = (x1, y1, x2, y2, ref, color = "#3a4a62", out = null) => {
      const path = D.wire(g, gs, [x1, y1], [x2, y2], { in: ref, out, color, width: 5, noArrow: true, straight: true });
      return path;
    };
    line(150, 740, 1150, 740, "s5");
    D.abs(g, "div", { x: 150, y: 770, in: "s5" }, { class: "mono", text: "main", style: { fontSize: "28px", color: "var(--muted)" } });
    dot(300, 740, "s5", "#5b6b82", "F", null, true);
    line(300, 740, 420, 560, "s5");
    line(420, 560, 640, 560, "s5");
    dot(420, 560, "s5", "#b197fc", "");
    dot(540, 560, "s5", "#b197fc", "");
    dot(640, 560, "s5", "#b197fc", "R");
    chip(g, 420, 440, "s5.1", "sent back · baseline R", "red", "s5.5");
    line(640, 560, 800, 560, "s5.2", "#3ddc97", "s5.5");
    dot(800, 560, "s5.2", "#3ddc97", "fix", "s5.5");
    chip(g, 700, 620, "s5.2+0.8", "ready", "green", "s5.5");
    chip(g, 860, 300, "s5.3", "queue: re-review, ahead of first reviews", "violet", "s5.5");
    D.abs(g, "div", { x: 640, y: 640, w: 170, h: 0, in: "s5.4", out: "s5.5" }, { style: { borderTop: "4px solid var(--amber)" } });
    D.abs(g, "div", { x: 560, y: 660, in: "s5.4", out: "s5.5" }, { class: "mono", text: "git diff R head", style: { fontSize: "28px", color: "var(--amber)" } });
    // After a rebase: main moved, the reviewed commits are replayed onto the new fork point.
    dot(520, 740, "s5.5", "#5b6b82", "", null);
    dot(680, 740, "s5.5", "#5b6b82", "F′", null, true);
    line(680, 740, 760, 380, "s5.5+0.4", "#3a4a62");
    line(760, 380, 980, 380, "s5.5+0.6", "#b197fc");
    dot(760, 380, "s5.5+0.6", "#b197fc80", "");
    dot(880, 380, "s5.5+0.8", "#b197fc80", "");
    dot(980, 380, "s5.5+1.0", "#b197fc80", "R′");
    line(980, 380, 1120, 380, "s5.5+1.6", "#3ddc97");
    dot(1120, 380, "s5.5+1.6", "#3ddc97", "head");
    D.abs(g, "div", { x: 150, y: 250, in: "s5.5+1.0" }, { html: '<span class="chip violet">R replayed onto F′ (merge-tree)</span>' });
    D.abs(g, "div", { x: 980, y: 460, w: 140, h: 0, in: "s5.6" }, { style: { borderTop: "4px solid var(--amber)" } });
    D.abs(g, "div", { x: 860, y: 480, in: "s5.6" }, { class: "mono", text: "git diff R′ head", style: { fontSize: "28px", color: "var(--amber)" } });
    card(g, { x: 1250, y: 230, w: 570, h: 300, in: "s5.4" },
      "<h3>Delta re-review</h3><p>One reviewer with all the audits. The prompt has the earlier findings and the delta. A merge commit, or a replay that conflicts, gives a range-diff instead.</p>");
    card(g, { x: 1250, y: 560, w: 570, h: 250, in: "s5.7" },
      "<h3>After one rejection</h3><p>A finding blocks only if it reopens an earlier blocking finding, or is a bug or regression within 10 lines of a change since the last review.</p>",
      { padding: "18px 24px" });

    // The arbiter.
    const A = D.layer(sec, { in: "s5.8" });
    const confirmed = D.box(A.el, { x: 100, y: 330, w: 380, h: 170, cls: "bad", in: "s5.8", kick: "Re-review", title: "confirmed again", sub: "reviewer + oracle" });
    const arb = D.box(A.el, { x: 620, y: 330, w: 380, h: 170, cls: "merger", in: "s5.8+0.6", kick: "Arbiter", title: "uphold or dismiss", sub: "one ruling per finding" });
    D.wire(A.el, A.sv, confirmed, arb, { in: "s5.8+0.5", color: "#b197fc" });
    const up = D.box(A.el, { x: 1160, y: 230, w: 660, h: 170, cls: "people", in: "s5.9", kick: "Uphold, or the arbiter fails", title: '<span class="chip amber">needs-maintainer</span>', sub: "stays ready; a person answers and removes the label, or adds review-bypass" });
    const dis = D.box(A.el, { x: 1160, y: 440, w: 660, h: 150, cls: "good", in: "s5.10", kick: "Dismiss every finding", title: "the findings become nits", sub: "then hand-off, unless a rule or CI still fails" });
    D.wire(A.el, A.sv, arb, up, { tf: 0.5, in: "s5.9", color: "#f5b452" });
    D.wire(A.el, A.sv, arb, dis, { tf: 0.5, in: "s5.10", color: "#3ddc97" });
    chip(A.el, 100, 540, "s5.8+1.2", "require_human: true → no arbiter", "");
    card(A.el, { x: 100, y: 650, w: 1720, h: 210, in: "s5.11", out: "s5.12" },
      `<h3>A push during the review</h3><p class="mono" style="color:var(--amber)">stopped: its author pushed 3b9e2f1c0 during the review of ${SHA2}; the next review covers what the push changed since this one.</p><p style="margin-top:10px">No draft. This review becomes the baseline (interrupted).</p>`);
    // A canceled ticket (tracker.abandon_states): its PRs are closed.
    card(A.el, { x: 100, y: 650, w: 1720, h: 210, in: "s5.12" },
      '<h3>A canceled ticket</h3><p class="mono" style="color:var(--amber)">symphony-merger: Closed by symphony-merger: ENG-8 was moved to Canceled. Reopen the PR to work on it again.</p><p style="margin-top:10px">It leaves the queue, the parked reviews and the train. Its preview devices are released.</p>');
  },

  // 6. The merge train
  s6(sec) {
    const L1 = D.layer(sec, { in: "s6", out: "s6.6" });
    const a = L1.el;
    D.abs(a, "div", { x: 100, y: 210, in: "s6.1" }, { html: '<span class="chip blue">train lease · SET NX PX + heartbeat</span> &nbsp;<span style="font-size:30px">one step at a time; a second loop gets TrainBusy</span>' });
    const order = card(a, { x: 100, y: 290, w: 760, h: 330, in: "s6.2" }, "<h3>Order of the reviewed PRs</h3>");
    [["#61", "0 packages shared with main", "1"], ["#58", "1 package shared", "2"], ["#63", "2 packages shared", "3"]].forEach(([n, why, k], i) => {
      const r = Engine.h("p", { html: `<b class="mono">${k}. ${n}</b> &nbsp;${why}`, style: { fontSize: "28px", lineHeight: "1.6" } });
      order.append(r);
      Engine.anim(r, { in: t("s6.2", 0.4 + i * 0.4), lift: 6 });
    });
    const tie = Engine.h("p", { text: "Then: fewer commits behind main, a smaller diff, the oldest hand-off.", style: { fontSize: "28px", color: "var(--muted)", marginTop: "10px" } });
    order.append(tie);
    Engine.anim(tie, { in: t("s6.2", 1.8) });
    const steps = [
      ["s6.3", "ls-remote: did the author push?"], ["s6.3+0.6", "rebase onto origin/main"], ["s6.3+1.2", "push --force-with-lease"],
      ["s6.4", "label wait-ci; wait for CI on this head"], ["s6.4+1.2", "webhook wake, or a check every 30 s"],
      ["s6.5", "merge with sha = this head"], ["s6.5+0.8", "post the Merged comment"], ["s6.5+1.6", "delete the branch, if still at that head"],
    ];
    steps.forEach(([ref, text], i) => D.abs(a, "div", { x: 920, y: 290 + i * 72, w: 900, h: 60, in: ref }, {
      class: "node", html: `<div style="display:flex;gap:18px;align-items:center;font-size:28px"><span class="mono" style="color:var(--muted)">${i + 1}</span><span>${text}</span></div>`,
      style: { padding: "0 20px", borderColor: i >= 5 ? "var(--green)" : undefined },
    }));
    comment(a, { x: 100, y: 660, w: 760, h: 220, in: "s6.5+0.8" }, "<b>symphony[bot]</b> commented", [
      ["s6.5+0.8", "<b>Merged</b>"],
      ["s6.5+1.0", `<p><b>symphony-merger</b> merge train on <code>${SHA}</code>: CI passed; merged, main is at <code>e81f0c2a4</code>.</p>`],
    ]);

    // Batch and bisect.
    const L2 = D.layer(sec, { in: "s6.6", out: "s6.9" });
    const b = L2.el;
    const members = ["#70", "#71", "#72", "#73"];
    members.forEach((n, i) => {
      const y = 650 - i * 100;
      const half = i < 2 ? "bottom" : "top";
      D.abs(b, "div", {
        x: 140, y, w: 260, h: 74, in: t("s6.6", 0.2 + i * 0.25), kind: "pop",
        dx: [["s6.6", 0], ["s6.8+1.8", half === "top" ? 520 : 0], ["s6.8+3.2", n === "#73" ? 980 : half === "top" ? 520 : 0]],
        dy: [["s6.6", 0], ["s6.8+3.2", n === "#73" ? 100 : 0]],
        o: [["s6.6", 1], ["s6.8+2.6", half === "bottom" ? 0.25 : 1]],
      }, { class: "node center", html: `<div class="t mono" style="font-size:30px">${n}</div>`, style: { padding: "0" } });
    });
    D.abs(b, "div", { x: 140, y: 230, in: "s6.6" }, { html: '<b style="font-size:32px">Batch: up to batch_max (default 4), each on the one below</b>' });
    D.swap(b, 440, 365, [["s6.7", "CI on the top only", "amber"], ["s6.7+1.5", "members merge in order", "green"], ["s6.8", "top CI red", "red"], ["s6.8+0.8", "rerun failed jobs once: red", "red"], ["s6.8+1.6", "split by caps: halves", "amber"]]);
    D.abs(b, "div", { x: 440, y: 455, in: "s6.7+0.4", out: "s6.8" }, { html: '<span class="chip">other runs cancelled</span>' });
    D.abs(b, "div", { x: 440, y: 545, in: "s6.8+2.4" }, { html: '<span class="chip green">bottom half green: merged</span>' });
    D.abs(b, "div", { x: 1100, y: 290, in: "s6.8+3.0" }, { html: '<span class="chip red">#73 alone: red → draft, Rework</span>' });
    D.abs(b, "div", { x: 1100, y: 365, in: "s6.8+3.4" }, { html: '<span class="chip">batch_split notice</span>' });

    // Conflict resolution.
    const L3 = D.layer(sec, { in: "s6.9", out: "s6.11" });
    const c = L3.el;
    chip(c, 100, 230, "s6.9", "#64 conflicts with main in app/ops.py", "red");
    const dev = D.box(c, { x: 100, y: 320, w: 520, h: 160, cls: "merger", in: "s6.9+0.6", kick: "Developer agent", title: "resolves inside the rebase", sub: "read, grep, bash, edit, write" });
    const rev = D.box(c, { x: 720, y: 320, w: 520, h: 160, cls: "merger", in: "s6.9+1.4", kick: "Reviewer agent", title: "checks the resolution", sub: "range-diff and each side" });
    D.wire(c, L3.sv, dev, rev, { in: "s6.9+1.3", color: "#b197fc" });
    D.box(c, { x: 1340, y: 320, w: 480, h: 160, cls: "", in: "s6.9+2.0", kick: "fix_slots", title: "3 at once", sub: "2 errors or a rejection → draft, Rework" });
    comment(c, { x: 100, y: 530, w: 1720, h: 340, in: "s6.10" }, "<b>symphony[bot]</b> commented", [
      ["s6.10", "<b>Rebased onto main</b>"],
      ["s6.10+0.4", "<p><b>symphony-merger</b> rebased <code>1f2e3d4c5</code> onto main as <code>9a8b7c6d5</code>: it conflicted with main in <code>app/ops.py</code>, which an agent resolved keeping both sides, and a reviewer checked. It is back in the merge train, in its place.</p>"],
    ]);

    // Crash-safe intents and the ticket.
    const L4 = D.layer(sec, { in: "s6.11" });
    const d = L4.el;
    const tbl = card(d, { x: 100, y: 230, w: 1120, h: 420, in: "s6.11" }, "<h3>Intent saved before the effect</h3>");
    [["publishing", "publication.done", "hand-off: comment, labels, ticket"], ["pushing", "the saved head", "adopted if the remote has it"],
      ["finalizing", "outcome.done", "draft, labels, comment, notice"], ["batch record", "—", "the batch in progress"]].forEach(([k, done, eff], i) => {
      const r = Engine.h("div", { html: `<span class="mono" style="color:var(--amber);min-width:230px;display:inline-block">${k}</span><span class="mono" style="color:var(--muted);min-width:290px;display:inline-block">${done}</span>${eff}`, style: { fontSize: "28px", lineHeight: "1.9" } });
      tbl.append(r);
      Engine.anim(r, { in: t("s6.11", 0.3 + i * 0.35), lift: 6 });
    });
    D.swap(d, 1300, 300, [["s6.12", "ticket → Merging (hand-off)", "violet"], ["s6.12+2.4", "ticket → Done (the tracker)", "green"]]);
    D.abs(d, "div", { x: 1300, y: 380, w: 520, in: "s6.12+2.6" }, { text: "Linear: the Linear–GitHub integration. GitHub: the merge closes the issue.", style: { fontSize: "28px", color: "var(--muted)" } });
  },

  // 7. Main watch
  s7(sec) {
    D.abs(sec, "div", { x: 100, y: 330, w: 1720, h: 8, in: "s7.1", kind: "wipe", dur: 0.8 }, { style: { background: "#3a4a62", borderRadius: "4px" } });
    D.abs(sec, "div", { x: 100, y: 260, in: "s7.1" }, { html: '<span class="mono" style="font-size:28px;color:var(--muted)">main · finished push runs of the CI workflow</span>' });
    [[300, "var(--green)", "s7.1+0.4"], [520, "var(--green)", "s7.1+0.7"], [740, "var(--red)", "s7.2"], [1500, "var(--green)", "s7.4"]].forEach(([x, c, ref]) =>
      D.abs(sec, "div", { x, y: 318, w: 32, h: 32, in: ref, kind: "pop" }, { style: { borderRadius: "50%", background: c } }));
    D.abs(sec, "div", { x: 756, y: 336, w: 760, h: 8, in: "s7.2", out: "s7.4", kind: "wipe" }, { style: { background: "var(--red)" } });
    card(sec, { x: 100, y: 410, w: 1000, h: 470, in: "s7.2+0.4" },
      `<h3 style="color:var(--red)">Main CI red: CI on d82d75555</h3><p>CI on <span class="mono">main</span> failed on d82d75555 (…).</p><p>Run: https://github.com/acme/app/actions/runs/…</p><p>Fix main so this workflow passes again. Keep the change to what the failure needs.</p><p class="mono" style="color:var(--amber);margin-top:8px">### test (failed step: Run pytest) …the last 120 log lines</p>`,
      { borderColor: "var(--red)" });
    D.abs(sec, "div", { x: 1160, y: 410, in: "s7.3" }, { html: '<span class="chip blue">repo:acme/app</span> <span class="chip blue">symphony</span> <span class="chip blue">ci</span>' });
    D.abs(sec, "div", { x: 1160, y: 480, w: 660, in: "s7.3+0.6" }, { text: "State main_watch.state (Todo). The daemon dispatches the fix like any ticket.", style: { fontSize: "28px", color: "var(--muted)" } });
    D.abs(sec, "div", { x: 1160, y: 600, w: 660, h: 140, in: "s7.4+0.6" }, { class: "card", html: "<p>Main CI is green again on 5c3a9e7f1 (…); closing this alert.</p><p style='color:var(--green)'>ticket → Done</p>" });
    D.abs(sec, "div", { x: 1160, y: 770, in: "s7.5" }, { html: '<span class="chip red">main_red</span> <span class="chip green">main_green</span> &nbsp;<span style="font-size:28px">Slack, Discord</span>' });
    D.abs(sec, "div", { x: 1160, y: 230, in: "s7.2+1.0" }, { html: '<span class="chip">with main_watch.ticket: true</span>' });
  },

  // 8. Operators
  s8(sec) {
    D.term(sec, { x: 100, y: 205, w: 1720, in: "s8.1", out: "s8.4" }, ".symphony/merger.md, read from the default branch", [
      { c: "lanes:                       # default: correctness, security-spec, design", in: "s8.3" },
      { c: "blocking_classes: [stand-in] # added to the generic blocking classes", in: "s8.3+0.3" },
      { c: "rule_checks: [{name: lint, run: \"make lint\"}]   # run at the head", in: "s8.3+0.6" },
      { c: "merge_method: rebase         # rebase | squash | merge", in: "s8.3+0.9" },
      { c: "batch_max: 4", in: "s8.3+1.2" },
      { c: "require_human: false         # true: a maintainer instead of the arbiter", in: "s8.3+1.5" },
      { c: "require_approval: false      # true: a GitHub approval before the train", in: "s8.3+1.8" },
      { c: "polish_rounds: 0             # nit rounds for agent pull requests", in: "s8.3+2.1" },
      { c: "ci: {checks: [test, lint], workflow: CI}", in: "s8.3+2.4" },
      { c: "main_watch: {ticket: true, state: Todo, labels: [ci]}", in: "s8.3+2.7" },
      { c: "preview: {artifact: ios-simulator-preview}", in: "s8.3+3.0" },
    ]);
    D.abs(sec, "div", { x: 100, y: 795, in: "s8.2", out: "s8.4" }, { html: '<span class="chip amber">a pull request cannot change the rules that review it</span>' });

    // Previews: one Slack announcement per head, one Preview comment edited in place.
    comment(sec, { x: 100, y: 205, w: 1100, h: 540, in: "s8.4", out: "s8.5" }, "<b>symphony[bot]</b> commented", [
      ["s8.4", "<b>Preview</b>"],
      ["s8.4+0.3", "<p><b>symphony-merger</b>: builds of this PR to try on a simulator in Seiji. A link opens that build, now or later, while GitHub keeps its artifact; open it again for a new viewer.</p>"],
      [[`s8.4+1.0`, `<p>• <code>${SHA}b80</code> (newest): <u style="color:var(--blue)">Open in Seiji</u></p>`, "s8.4+2.6"],
        ["s8.4+2.8", `<p>• <code>${SHA2}3f1</code> (newest): <u style="color:var(--blue)">Open in Seiji</u><br>• <code>${SHA}b80</code>: <u style="color:var(--blue)">Open in Seiji</u></p>`]],
    ]);
    D.abs(sec, "div", { x: 1260, y: 205, w: 560, h: 300, in: "s8.4+0.6", out: "s8.5" }, {
      class: "card", html: '<p style="font-size:28px"><b>Slack</b> · preview_ready<br>#4 ENG-8: add is_even: an iOS build of this head is ready to try in a simulator</p><p style="margin-top:14px"><span class="chip green">Open in simulator</span></p>',
    });
    chip(sec, 1260, 530, "s8.4+2.8", "a new head: one more link", "blue", "s8.5");
    D.shot(sec, {
      x: 100, y: 200, w: 1720, h: 560, in: "s8.5",
      shots: [{ src: SHOT("merger"), in: "s8.5" }],
      cam: [["s8.5", 1440, 1200, 0.59], ["s8.7", 1440, 830, 0.59]],
      spots: [{ x: 225, y: 1448, w: 500, h: 52, in: "s8.5+0.8", out: "s8.7" }, { x: 20, y: 712, w: 2840, h: 150, in: "s8.7+0.4" }],
    });
    D.abs(sec, "div", { x: 100, y: 790, in: "s8.6", out: "s8.7" }, { html: '<span class="chip">symphony merger requeue 42 --reason "…" --repo acme/app</span> &nbsp;<span class="chip">POST /v1/merger/prs/42/requeue</span>' });
    D.callout(sec, { x: 100, y: 785, in: "s8.7+0.2" }, 'Live session <span class="mono">merger:acme:app:42:reviewer:1</span> · follow and interrupt');
  },

  // 9. Proof
  s9(sec) {
    const board = card(sec, { x: 100, y: 205, w: 1150, h: 680, in: "s9.1" }, "<h3>Real GitHub repository, GitHub Actions CI, real agents</h3>");
    [["a", "Clean PR → review → hand-off → merged at the reviewed head", "s9.2", "pass"],
      ["b", "Real bug → reviewer and oracle agree → draft once", "s9.3", "pass"],
      ["c", "Rebase conflict → developer and reviewer → merged", "s9.4", "pass"],
      ["d", "Batch of 3, CI on the top only", "s9.5", "pass"],
      ["e", "Batch on a protected main", "s9.5+0.4", "bug 7 found, fixed"],
      ["f", "Batch of 3 on a protected main, after the fix", "s9.5+0.8", "pass"]].forEach(([k, text, ref, res]) => {
      const r = Engine.h("div", { html: `<span class="mono" style="color:var(--muted);display:inline-block;width:36px">${k}</span>${text} &nbsp;<span class="${res === "pass" ? "tick" : ""}" style="${res === "pass" ? "" : "color:var(--amber);font-weight:700"}">${res === "pass" ? "✓ pass" : res}</span>`, style: { fontSize: "28px", lineHeight: "1.45", marginTop: "12px" } });
      board.append(r);
      Engine.anim(r, { in: ref, lift: 6 });
    });
    const tile = (y, ref, big, text, color) => D.abs(sec, "div", { x: 1290, y, w: 530, h: 210, in: ref, kind: "pop" }, {
      class: "card", html: `<div class="big" style="color:${color}">${big}</div><p style="margin-top:8px">${text}</p>`,
    });
    tile(205, "s9.6", "10–40 s", "a review with no findings (2 lanes); the oracle added about 20 s with findings", "var(--blue)");
    tile(440, "s9.7", "64.8 s", "mocked, Linear: review and merge, from the merger start", "var(--green)");
    tile(675, "s9.7+0.6", "55.8 s", "mocked, GitHub tracker: review and merge, from the merger start", "var(--green)");
  },
};
