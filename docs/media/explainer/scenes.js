// The nine scenes of the explainer. Every time reference points at a narration cue (see engine.js `at`).
// Screenshots are the real Tern captures in docs/tern (2880 x 1920 px; the Dashboard is 2880 x 3800).

const SHOT = (name) => `../../tern/${name}-dark.png`;
const VIEW = { x: 100, y: 195, w: 1720, h: 690 }; // screenshot viewport
const FIT = 0.359; // whole screenshot inside VIEW
// The stylesheet draws every screenshot 2880x1920; a taller one (the Dashboard) keeps its own height.
const natural = (vp) => {
  for (const img of vp.querySelectorAll("img")) img.style.height = "auto";
  return vp;
};

const SCENES = {
  // 1. The problem and the idea
  s1(sec, sv, head) {
    const { anim } = Engine;
    anim(head.k, { in: "s1.2-0.1" });
    anim(head.t, { in: "s1.2" });
    D.abs(sec, "div", { x: 0, y: 330, w: 1920, in: -1, out: "s1.2-0.35" }, { class: "big-word", text: "Symphony", style: { textAlign: "center", fontSize: "160px" } });
    D.abs(sec, "div", { x: 0, y: 540, w: 1920, in: -1, out: "s1.2-0.35" }, { text: "Coding agents on Linear or GitHub tickets. People stay in control.", style: { textAlign: "center", fontSize: "46px", color: "var(--muted)" } });

    const ticket = D.box(sec, {
      x: 100, y: 290, w: 380, h: 300, cls: "ext", in: "s1.2", kick: "Tracker", title: "ENG-1",
      sub: "Linear or GitHub Issues", body: D.chips([["symphony", "blue"], ["repo:acme/app", "blue"]]),
    });
    D.swap(sec, 100, 615, [
      ["s1.2+0.2", "Todo", ""], ["s1.2+1.2", "In Progress", "blue"], ["s1.3+0.9", "In Review", "violet"],
      ["s1.3+2.6", "Merging", "violet"], ["s1.3+3.4", "Done", "green"],
    ]);
    D.abs(sec, "div", { x: 100, y: 668, w: 460, in: "s1.2+1.4" }, { text: "Linear state, or the Status column of a GitHub board", style: { fontSize: "28px", color: "var(--muted)", lineHeight: "1.25" } });
    const ws = D.box(sec, {
      x: 600, y: 290, w: 360, h: 300, cls: "ws", in: "s1.2+0.6", kick: "Workspace", title: '<span class="mono">omp</span>',
      sub: "one agent for each ticket, in its own pod",
    });
    const pr = D.box(sec, {
      x: 1080, y: 290, w: 340, h: 300, cls: "ext", in: "s1.3", kick: "GitHub", title: "PR #1",
      sub: "ENG-1: Add farewell()", body: D.chips([["ready", "green"]]),
    });
    const merger = D.box(sec, { x: 1540, y: 290, w: 280, h: 300, cls: "merger", in: "s1.3+1.4", kick: "Merger", title: "review<br>+ train" });
    D.abs(sec, "span", { x: 1540, y: 615, in: "s1.3+3.0", kind: "pop" }, { class: "chip green", text: "merged" });

    const w1 = D.wire(sec, sv, ticket, ws, { in: "s1.2+0.4" });
    const w2 = D.wire(sec, sv, ws, pr, { in: "s1.3+0.1" });
    const w3 = D.wire(sec, sv, pr, merger, { in: "s1.3+1.2" });
    D.packet(sv, w1, "s1.2+0.9", "s1.2+1.6");
    D.packet(sv, w2, "s1.3+0.2", "s1.3+0.9");
    D.packet(sv, w3, "s1.3+1.4", "s1.3+2.1", "#b197fc");

    const people = D.box(sec, { x: 600, y: 700, w: 360, h: 170, cls: "people", in: "s1.4", kick: "People", title: "Tern", sub: "follow, steer, take over" });
    D.wire(sec, sv, people, ws, { from: "t", to: "b", in: "s1.4+0.3", color: "#f5b452", dash: true, bi: true });
    D.abs(sec, "div", { x: 1000, y: 760, in: "s1.4+0.7" }, { html: '<span class="chip amber">symphony CLI</span> &nbsp; <span class="chip amber">MCP</span>' });
  },

  // 2. Architecture
  s2(sec) {
    const L = D.layer(sec, { o: [["s2", 1], ["s2.8", 0.1]] });
    const p = L.el;
    const sv = L.sv;
    const linear = D.box(p, { x: 100, y: 240, w: 260, h: 130, cls: "ext", in: "s2.1", title: "Tracker", sub: "Linear or GitHub Issues" });
    const github = D.box(p, { x: 100, y: 470, w: 260, h: 110, cls: "ext", in: "s2.1+0.4", title: "GitHub", sub: "code, CI" });
    const people = D.box(p, { x: 100, y: 700, w: 260, h: 120, cls: "people", in: "s2.1+0.8", title: "Tern", sub: "CLI, MCP" });
    const api = D.box(p, { x: 470, y: 320, w: 300, h: 150, cls: "trusted", in: "s2.1+0.2", title: "Control API", sub: "/v1, SSE, MCP,<br>webhooks" });
    const daemon = D.box(p, { x: 880, y: 250, w: 300, h: 120, cls: "trusted", in: "s2.2", title: "Daemon", sub: "reconcile, dispatch" });
    const runner = D.box(p, { x: 1290, y: 250, w: 340, h: 120, cls: "trusted", in: "s2.2+1.3", title: "Runner Job", sub: "one per dispatch" });
    const ws = D.box(p, { x: 1290, y: 470, w: 340, h: 180, cls: "ws", in: "s2.3", kick: "untrusted", title: "Workspace pod", sub: '<span class="mono">omp --mode rpc</span>' });
    const gw = D.box(p, { x: 880, y: 730, w: 300, h: 120, cls: "trusted", in: "s2.4", title: "Gateway", sub: "telemetry only" });
    const redis = D.box(p, { x: 880, y: 495, w: 300, h: 130, cls: "store", in: "s2.5", title: "Redis", sub: "all shared state" });
    const merger = D.box(p, { x: 470, y: 650, w: 300, h: 150, cls: "merger", in: "s2.6", title: "Merger", sub: "reviews and<br>merges PRs" });
    D.box(p, { x: 1470, y: 700, w: 330, h: 140, cls: "trusted", in: "s2.6+1.6", title: "Assistant", sub: "chat bot on<br>Slack and Discord" });

    const blue = "#5cb8ff";
    const wl = D.wire(p, sv, linear, api, { tf: 0.3, in: "s2.1+0.3", label: "webhook", lo: [24, -26] });
    const wg = D.wire(p, sv, github, api, { tf: 0.7, in: "s2.1+0.6", label: "webhook", lo: [24, 26] });
    D.wire(p, sv, people, api, { tf: 0.92, in: "s2.1+1.0", color: "#f5b452", bi: true });
    D.packet(sv, wl, "s2.1+0.8", "s2.1+1.5");
    D.packet(sv, wg, "s2.1+1.1", "s2.1+1.8");
    const wd = D.wire(p, sv, api, daemon, { ff: 0.25, in: "s2.2+0.1", label: "wake", color: blue });
    D.packet(sv, wd, "s2.2+0.3", "s2.2+0.9");
    const wj = D.wire(p, sv, daemon, runner, { in: "s2.2+1.1", label: "Job", color: blue });
    D.packet(sv, wj, "s2.2+1.3", "s2.2+1.9");
    const we = D.wire(p, sv, runner, ws, { from: "b", to: "t", in: "s2.3+0.3", label: "pods/exec", color: blue });
    D.packet(sv, we, "s2.3+0.6", "s2.3+1.2");
    const wt = D.wire(p, sv, ws, gw, { from: "b", ff: 0.3, to: "r", in: "s2.4+0.2", label: "telemetry", color: "#f5b452", lo: [40, 18] });
    D.packet(sv, wt, "s2.4+0.6", "s2.4+1.4", "#f5b452");
    D.abs(p, "span", { x: 1660, y: 495, in: "s2.4+1.6", kind: "pop" }, { class: "chip red", text: "✕ API" });
    D.abs(p, "span", { x: 1660, y: 560, in: "s2.4+1.9", kind: "pop" }, { class: "chip red", text: "✕ Redis" });

    const muted = "#6d5bc4";
    D.wire(p, sv, api, redis, { ff: 0.85, tf: 0.3, in: "s2.5", color: muted, noArrow: true });
    D.wire(p, sv, daemon, redis, { from: "b", to: "t", in: "s2.5+0.2", color: muted, noArrow: true });
    D.wire(p, sv, runner, redis, { from: "l", ff: 0.85, to: "r", tf: 0.3, in: "s2.5+0.4", color: muted, noArrow: true });
    D.wire(p, sv, gw, redis, { from: "t", to: "b", in: "s2.5+0.6", color: muted, noArrow: true });
    D.wire(p, sv, merger, redis, { from: "r", ff: 0.3, to: "l", tf: 0.8, in: "s2.6+0.3", color: muted, noArrow: true });
    D.wire(p, sv, api, merger, { from: "b", to: "t", in: "s2.6+0.2", label: "wake", color: "#b197fc" });

    D.abs(p, "div", { x: 440, y: 222, w: 1380, h: 650, in: "s2.7", kind: "fade", lift: 0 }, { style: { border: "3px dashed #3b82c4", borderRadius: "26px" } });
    D.abs(p, "div", { x: 1240, y: 200, in: "s2.7+0.3" }, { class: "wl", text: "namespace symphony · Kubernetes or k3s", style: { transform: "none", color: "#8fc8ff", padding: "0 14px" } });

    const local = D.box(sec, {
      x: 330, y: 250, w: 1260, h: 600, cls: "trusted", in: "s2.8", kind: "pop", kick: "Local mode", title: "One process on one machine",
      body: Engine.h("div", {}, [
        D.chips([["Control API :8080", "blue"], ["Gateway :8090", "blue"], ["Daemon tick", "blue"], ["Runner threads", "blue"]]),
        Engine.h("div", { class: "s", style: { marginTop: "26px" }, html: 'Each attempt drives a local <span class="mono">omp --mode rpc</span> in a directory.<br>Redis runs on the host. Tickets can live in memory.' }),
        Engine.h("div", { class: "s", style: { marginTop: "26px", color: "var(--amber)" }, html: "No container: the agent runs as your user, with your files." }),
      ]),
    });
    local.el.style.padding = "34px 44px";
  },

  // 3. Ticket lifecycle
  s3(sec, sv) {
    const { h } = Engine;
    D.abs(sec, "div", { x: 100, y: 205, in: "s3.1" }, {
      html: '<span style="font-size:34px;font-weight:700">ENG-1</span> <span style="font-size:32px;color:var(--muted)">&nbsp;Add farewell()&nbsp;&nbsp;</span>',
      style: { display: "flex", alignItems: "center", gap: "12px" },
    }).append(D.chip("symphony", "blue"), D.chip("repo:acme/app", "blue"));
    D.abs(sec, "div", { x: 100, y: 262, in: "s3.1+1.6" }, { text: "On a GitHub Projects board, the Status column holds the state.", style: { fontSize: "28px", color: "var(--muted)" } });

    const names = [["Todo", "implementer"], ["In Progress", "implementer"], ["In Review", "merger"], ["Merging", "merge train"], ["Done", "after the<br>merge"]];
    const S = names.map(([n, who], i) => {
      const b = D.box(sec, { x: 100 + i * 350, y: 330, w: 300, h: 150, cls: "center", in: `s3.1+${(0.2 + i * 0.15).toFixed(2)}`, title: n, sub: who });
      return b;
    });
    S[4].el.classList.add("good");
    const rework = D.box(sec, { x: 975, y: 590, w: 300, h: 130, cls: "center bad", in: "s3.3", title: "Rework", sub: "implementer" });

    D.wire(sec, sv, S[0], S[1], { in: "s3.2", color: "#5cb8ff" });
    D.wire(sec, sv, S[1], S[2], { in: "s3.2+1.6", color: "#5cb8ff" });
    D.wire(sec, sv, S[2], rework, { from: "b", ff: 0.3, to: "l", in: "s3.3", color: "#ff6b6b", label: "blocked", lo: [-60, 10] });
    D.wire(sec, sv, rework, S[2], { from: "t", ff: 0.25, to: "b", tf: 0.85, in: "s3.3+1.2", color: "#3ddc97", bend: 30 });
    D.wire(sec, sv, S[2], S[3], { in: "s3.3+2.0", color: "#5cb8ff" });
    D.wire(sec, sv, S[3], rework, { from: "b", ff: 0.4, to: "t", tf: 0.85, in: "s3.3+2.8", color: "#ff6b6b", dash: true, label: "red CI", lo: [70, 0] });
    D.wire(sec, sv, S[3], S[4], { in: "s3.4", color: "#3ddc97" });

    // The current state ring.
    D.abs(sec, "div", {
      x: 92, y: 322, w: 316, h: 166, in: "s3.1+0.8",
      dx: [["s3.1", 0], ["s3.2", 350], ["s3.2+1.8", 700], ["s3.3", 875], ["s3.3+1.4", 700], ["s3.3+2.2", 1050], ["s3.4", 1400]],
      dy: [["s3.1", 0], ["s3.3", 260], ["s3.3+1.4", 0]],
    }, { style: { border: "4px solid #5cb8ff", borderRadius: "22px", boxShadow: "0 0 30px rgba(92,184,255,0.55)" } });

    const card = (x, ref, title, text) => D.abs(sec, "div", { x, y: 755, w: 545, h: 145, in: ref }, { class: "card", html: `<h3>${title}</h3><p>${text}</p>` });
    card(100, "s3.5", "Continuation", "Turn limit reached. The same attempt runs again after about 1 s.");
    card(687, "s3.6", "Retry with backoff", "Next attempt after 10 s, 20 s, 40 s … at most 300 s.");
    card(1275, "s3.7", "Hold", "A person holds the ticket. No dispatch until it is released.");
  },

  // 4. One attempt in motion
  s4(sec, sv) {
    const top = [["Tracker", "ext"], ["Control API", "trusted"], ["Daemon", "trusted"], ["Runner Job", "trusted"], ["Workspace", "ws"]];
    const ins = ["s4.1", "s4.1+0.5", "s4.1+1.2", "s4.2+0.6", "s4.3"];
    const N = top.map(([t, c], i) => D.box(sec, { x: 100 + i * 355, y: 215, w: 300, h: 110, cls: `center ${c}`, in: ins[i], title: t }));
    const w1 = D.wire(sec, sv, N[0], N[1], { in: "s4.1+0.2" });
    const w2 = D.wire(sec, sv, N[1], N[2], { in: "s4.1+0.9", color: "#5cb8ff" });
    const w3 = D.wire(sec, sv, N[2], N[3], { in: "s4.2+0.4", color: "#5cb8ff" });
    const w4 = D.wire(sec, sv, N[3], N[4], { in: "s4.3", color: "#5cb8ff" });
    D.packet(sv, w1, "s4.1+0.4", "s4.1+1.0");
    D.packet(sv, w2, "s4.1+1.0", "s4.1+1.6");
    D.packet(sv, w3, "s4.2+0.5", "s4.2+1.1");
    D.packet(sv, w4, "s4.3+0.1", "s4.3+0.7");

    const card = (o, html, cls = "") => D.abs(sec, "div", { ...o }, { class: `card ${cls}`, html });
    card({ x: 100, y: 375, w: 640, h: 215, in: "s4.1+1.4" },
      '<h3>Webhook wake</h3><div class="big" style="color:var(--blue)">&lt; 3 s</div><p>A safety poll also runs every 15 s.</p>');
    card({ x: 780, y: 375, w: 500, h: 215, in: "s4.2" },
      '<h3>Dispatch token</h3><p class="mono" style="color:var(--amber)">dispatch:&lt;issue&gt;:1</p><p>Put-if-absent. An old dispatch exits with no effect.</p>');
    card({ x: 1320, y: 375, w: 500, h: 215, in: "s4.3+0.4" },
      '<h3>Workspace pod</h3><p class="mono" style="color:var(--amber)">ws-eng-1-… + session volume</p><p>The next attempt reuses it.</p>');
    card({ x: 100, y: 630, w: 640, h: 255, in: "s4.4" },
      '<h3>GitHub App token</h3><div class="chips"><span class="chip blue">repos: acme/app</span><span class="chip blue">≤ 1 hour</span><span class="chip blue">file 0600</span></div><p style="margin-top:14px">The runner renews it between turns.</p>');

    const omp = D.abs(sec, "div", { x: 780, y: 630, w: 500, h: 255, in: "s4.5" }, { class: "card" });
    omp.append(Engine.h("h3", { html: '<span class="mono">omp</span> events' }));
    ["agent_start", "tool_execution_start", "message_update", "turn_end"].forEach((ev, i) => {
      const ln = Engine.h("p", { class: "mono", text: `▸ ${ev}`, style: { color: i === 1 ? "var(--amber)" : "#c8d3e1" } });
      omp.append(ln);
      Engine.anim(ln, { in: `s4.5+${(0.3 + i * 0.45).toFixed(2)}`, lift: 8 });
    });

    card({ x: 1320, y: 630, w: 500, h: 255, in: "s4.6" },
      '<h3>Pull request #1</h3><p>ENG-1: Add farewell() to app/greet.py</p><div class="chips"><span class="chip green">ready</span></div>');
    D.abs(sec, "span", { x: 1560, y: 795, in: "s4.6+1.8", kind: "pop" }, { class: "chip violet", text: "In Review" });
  },

  // 5. People in the loop
  s5(sec) {
    natural(D.shot(sec, {
      ...VIEW, in: "s5", out: "s5.7",
      shots: [
        { src: SHOT("dashboard"), in: "s5", out: "s5.2" },
        { src: SHOT("real-chat"), in: "s5.2", out: "s5.4" },
        { src: SHOT("dashboard"), in: "s5.4", out: "s5.5" },
        { src: SHOT("tickets"), in: "s5.5" },
      ],
      cam: [
        ["s5", 1440, 960, FIT], ["s5.1+1.2", 1450, 900, 0.62], ["s5.2", 1440, 600, 0.95], ["s5.3", 1440, 1550, 0.95],
        ["s5.4", 930, 800, 0.95], ["s5.5", 930, 600, 0.95], ["s5.6", 770, 650, 1.15],
      ],
      spots: [
        { x: 545, y: 645, w: 670, h: 66, in: "s5.2+1.3", out: "s5.3" },
        { x: 45, y: 712, w: 1770, h: 228, in: "s5.4+1.0", out: "s5.5" },
        { x: 430, y: 770, w: 255, h: 50, in: "s5.6+0.8", out: "s5.7" },
      ],
    }));
    const at = { x: 130, y: 805 };
    D.callout(sec, { ...at, in: "s5.1+1.6", out: "s5.2" }, "Dashboard: needs input, live sessions, and actions");
    D.callout(sec, { ...at, in: "s5.2+0.5", out: "s5.3" }, 'Live chat over ACP &nbsp;<span class="mono">tern acp symphony --resume</span>');
    D.callout(sec, { x: 130, y: 215, in: "s5.3+0.6", out: "s5.4" }, 'Steer, follow-up, interrupt: target under 300 ms &nbsp;<span class="mono">steer: 13 ms on kind</span>');
    D.abs(sec, "div", { x: 1150, y: 520, in: "s5.3+0.2", out: "s5.4", kind: "pop" }, {
      text: "Cover time zones too.",
      style: { fontSize: "32px", padding: "14px 24px", borderRadius: "18px", background: "#2a3140", border: "2px solid var(--amber)" },
    });
    D.abs(sec, "span", { x: 1150, y: 450, in: "s5.3+1.4", out: "s5.4", kind: "pop" }, { class: "chip amber", text: "steer → joins the current turn" });
    D.callout(sec, { ...at, in: "s5.4+0.5", out: "s5.5" }, '<span class="mono">symphony_ask</span> &nbsp;Slack or Discord notice · a timeout lets the agent continue');
    D.callout(sec, { ...at, in: "s5.5+0.6", out: "s5.6" }, '<span class="mono">symphony takeover ENG-147</span> &nbsp;holds it and opens <span class="mono">omp --resume</span>');
    D.callout(sec, { ...at, in: "s5.6+0.6", out: "s5.7" }, 'Return to Symphony = <span class="mono">symphony release ENG-147</span>');

    // The assistant: a chat thread on Slack or Discord, then a plan with buttons.
    const A = D.layer(sec, { in: "s5.7", out: "s5.9" });
    const thread = D.abs(A.el, "div", { x: 100, y: 205, w: 1120, h: 680, in: "s5.7" }, { class: "card", style: { padding: "18px 26px" } });
    thread.append(Engine.h("p", { html: '<span class="mono">#eng-help</span> · thread', style: { color: "var(--muted)", marginBottom: "10px" } }));
    const msg = (ref, who, bot, html) => {
      const el = Engine.h("div", { style: { display: "flex", gap: "16px", marginTop: "14px", fontSize: "28px", lineHeight: "1.35" } }, [
        Engine.h("div", { text: who[0].toUpperCase(), style: { flex: "0 0 48px", height: "48px", borderRadius: "10px", background: bot ? "var(--violet)" : "#3a4a62", color: "#0a0e16", fontWeight: "800", display: "flex", alignItems: "center", justifyContent: "center" } }),
        Engine.h("div", { html: `<b>${who}</b>${bot ? ' <span style="color:var(--muted)">app</span>' : ""}<br>${html}` }),
      ]);
      thread.append(el);
      Engine.anim(el, { in: ref, lift: 8 });
    };
    msg("s5.7+0.2", "bob", false, "@symphony where do we check the webhook signature?");
    msg("s5.7+1.4", "symphony", true, 'In <span class="mono" style="color:var(--blue)">acme/app:app/webhooks.py:42</span>, <span class="mono">verify()</span> compares the HMAC in constant time.');
    msg("s5.8", "bob", false, "@symphony plan add rate limits to the public API of acme/app");
    msg("s5.8+1.0", "symphony", true, 'Proposal: 3 tickets<br>1. Add a rate limiter to the public API<br>2. Return 429 with Retry-After<br>3. Document the limits<div class="chips"><span class="chip green">Create</span><span class="chip blue">Create and start</span><span class="chip">Dismiss</span></div>');
    const side = (y, h, ref, html) => D.abs(A.el, "div", { x: 1260, y, w: 560, h, in: ref }, { class: "card", html });
    side(205, 200, "s5.7+0.6", '<h3>Three modes</h3><div class="chips"><span class="chip violet">ask</span><span class="chip violet">research …</span><span class="chip violet">plan …</span></div>');
    side(425, 200, "s5.7+2.0", "<h3>Read-only tools</h3><p>read, grep, glob, web search, Symphony state, tracker search. No bash, no edit.</p>");
    side(645, 240, "s5.8+1.8", '<h3>Who can confirm</h3><p>Only a person mapped in <span class="mono">notify.users</span>. Each conversation is also a session in Tern.</p>');

    D.term(sec, { x: 100, y: 215, w: 1000, in: "s5.9" }, "symphony CLI", [
      { cmd: "symphony watch ENG-1", in: "s5.9+0.2" },
      { cmd: 'symphony say ENG-1 "Cover time zones too."', in: "s5.9+0.5" },
      { cmd: "symphony interrupt ENG-1", in: "s5.9+0.8" },
      { cmd: "symphony takeover ENG-1", in: "s5.9+1.1" },
      { cmd: "symphony release ENG-1", in: "s5.9+1.4" },
    ]);
    const mcp = D.abs(sec, "div", { x: 1150, y: 215, w: 670, h: 330, in: "s5.9+0.6" }, { class: "card", html: '<h3>MCP at <span class="mono">/mcp</span></h3><p>The same controls for your own omp.</p>' });
    mcp.append(D.chips([["list_sessions", ""], ["send_message", "amber"], ["interrupt", "amber"], ["hold", "amber"], ["release", "amber"], ["merger_status", ""]]));
    D.abs(sec, "div", { x: 100, y: 590, w: 1720, h: 150, in: "s5.9+1.4" }, {
      class: "card", html: '<h3>Token roles</h3><p><span class="mono">viewer</span> reads. <span class="mono">operator</span> also sends messages, interrupts, holds and releases. <span class="mono">admin</span> also manages tokens, keys and models.</p>',
    });
  },

  // 6. The merger
  s6(sec) {
    D.shot(sec, {
      ...VIEW, in: "s6", out: "s6.2",
      shots: [{ src: SHOT("merger"), in: "s6" }],
      cam: [["s6", 1440, 960, FIT], ["s6.1+0.5", 1440, 560, 0.6]],
      spots: [{ x: 14, y: 398, w: 2846, h: 240, in: "s6.1+1.3" }],
    });
    D.callout(sec, { x: 130, y: 805, in: "s6.1+1.3", out: "s6.2" }, "One merger for every repository, each with its own train lease");

    // Review
    const R = D.layer(sec, { in: "s6.2", out: "s6.8" });
    const p = R.el;
    const sv = R.sv;
    const pr = D.box(p, { x: 100, y: 420, w: 250, h: 170, cls: "ext", in: "s6.2", kick: "PR #4", title: "is_even", sub: "ENG-8" });
    const lanes = ["correctness", "security-spec", "design"].map((n, i) =>
      D.box(p, { x: 420, y: 250 + i * 150, w: 320, h: 100, cls: "merger center", in: `s6.2+${(0.3 + i * 0.25).toFixed(2)}`, title: `<span class="mono" style="font-size:30px">${n}</span>` }));
    lanes.forEach((l, i) => D.wire(p, sv, pr, l, { in: `s6.2+${(0.2 + i * 0.25).toFixed(2)}`, color: "#b197fc" }));
    const oracle = D.box(p, { x: 810, y: 380, w: 300, h: 170, cls: "merger", in: "s6.3", kick: "Oracle", title: "other model", sub: "confirms or refutes" });
    lanes.forEach((l, i) => D.wire(p, sv, l, oracle, { in: `s6.3+${(0.1 + i * 0.15).toFixed(2)}`, color: "#b197fc" }));
    const decide = D.box(p, { x: 1180, y: 370, w: 290, h: 190, cls: "people", in: "s6.4", kick: "Rule", title: "Both agree?", sub: "reviewer and oracle say blocking" });
    D.wire(p, sv, oracle, decide, { in: "s6.4", color: "#f5b452" });

    [
      { y: 230, h: 110, cls: "bad", title: "Rework", sub: "first rejection", in: "s6.5" },
      { y: 370, h: 120, cls: "merger", title: "Arbiter", sub: "uphold or dismiss", in: "s6.5+2.0" },
      { y: 520, h: 110, cls: "", title: "Parked", sub: "waits for CI", in: "s6.7" },
      { y: 660, h: 110, cls: "good", title: "Merging", sub: "CI green", in: "s6.7+1.4" },
    ].forEach((o) => {
      const b = D.box(p, { x: 1540, w: 280, ...o, cls: `center ${o.cls}` });
      D.wire(p, sv, decide, b, { in: o.in, color: "#5b6b82" });
    });
    D.abs(p, "div", { x: 100, y: 720, w: 650, h: 160, in: "s6.6" }, {
      class: "card", html: '<h3>Re-review</h3><p class="mono" style="color:var(--violet)">git diff &lt;reviewed head&gt; &lt;head&gt;</p><p>One reviewer reads only this delta.</p>',
    });
    D.abs(p, "div", { x: 790, y: 720, w: 690, h: 160, in: "s6.7+0.4" }, {
      class: "card", html: "<h3>Parked CI</h3><p>The report is posted now. The review settles when CI ends: green goes on, red goes back.</p>",
    });

    // Merge train
    const T = D.layer(sec, { in: "s6.8" });
    const q = T.el;
    D.abs(q, "div", { x: 100, y: 838, w: 1720, h: 6, in: "s6.8", kind: "wipe", dur: 0.8 }, { style: { background: "#3a4a62", borderRadius: "3px" } });
    D.abs(q, "div", { x: 100, y: 856, in: "s6.8" }, { class: "mono", text: "main", style: { fontSize: "30px", color: "var(--muted)" } });
    D.abs(q, "div", { x: 1300, y: 836, w: 520, h: 10, in: "s6.11", kind: "wipe" }, { style: { background: "var(--red)", borderRadius: "5px" } });
    const commit = (x, ref) => D.abs(q, "div", { x, y: 829, w: 24, h: 24, in: ref, kind: "pop" }, { style: { borderRadius: "50%", background: "var(--green)" } });

    // Zone A: a clean batch of three
    D.abs(q, "div", { x: 120, y: 240, in: "s6.8" }, { html: "<b style='font-size:32px'>Batch</b>", style: {} });
    const prCard = (o, label) => D.abs(q, "div", { w: 260, h: 70, kind: "pop", ...o }, {
      class: "node center", html: `<div class="t mono" style="font-size:30px">${label}</div>`,
      style: { padding: "0" },
    });
    const A = [["#5", 560], ["#6", 470], ["#7", 380]];
    A.forEach(([n, y], i) => {
      prCard({ x: 120, y, in: `s6.8+${(0.1 + i * 0.25).toFixed(2)}`, dy: [["s6.8", 0], [`s6.8+${(2.6 + i * 0.45).toFixed(2)}`, 830 - y - 35]], o: [["s6.8", 1], [`s6.8+${(2.9 + i * 0.45).toFixed(2)}`, 0]], tdur: 0.45 }, n);
      commit(160 + i * 60, `s6.8+${(3.0 + i * 0.45).toFixed(2)}`);
    });
    D.swap(q, 400, 392, [["s6.8+0.8", "CI running", "amber"], ["s6.8+1.9", "CI green", "green"], ["s6.8+2.6", "merging in order", "green"], ["s6.8+4.1", "3 merged", "green"]]);
    D.abs(q, "span", { x: 400, y: 482, in: "s6.8+1.0", out: "s6.8+2.6", kind: "pop" }, { class: "chip", text: "CI cancelled" });
    D.abs(q, "span", { x: 400, y: 572, in: "s6.8+1.1", out: "s6.8+2.6", kind: "pop" }, { class: "chip", text: "CI cancelled" });

    // Zone B: a red batch is bisected
    D.abs(q, "div", { x: 760, y: 240, in: "s6.9" }, { html: "<b style='font-size:32px'>Red batch</b>" });
    const B = { "#8": 590, "#9": 510, "#10": 430, "#11": 350 };
    const b = "s6.9";
    const t = (d) => `${b}+${d.toFixed(2)}`;
    prCard({ x: 760, y: B["#8"], in: t(0.1), o: [[b, 1], [t(2.9), 0]] }, "#8");
    prCard({ x: 760, y: B["#9"], in: t(0.25), o: [[b, 1], [t(2.9), 0]] }, "#9");
    prCard({ x: 760, y: B["#10"], in: t(0.4), dx: [[b, 0], [t(1.6), 300]], dy: [[b, 0], [t(1.6), 80]] }, "#10");
    prCard({ x: 760, y: B["#11"], in: t(0.55), dx: [[b, 0], [t(1.6), 300]], dy: [[b, 0], [t(1.6), 80], [t(3.2), 20]], o: [[b, 1], [t(4.0), 0]] }, "#11");
    D.swap(q, 760, 290, [[t(0.7), "CI red", "red"], [t(1.1), "rerun: red", "red"], [t(1.6), "split in halves", "amber"], [t(2.3), "bottom half green", "green"], [t(3.0), "top half red: split", "red"], [t(3.7), "#10 alone: Rework", "red"]]);
    commit(1000, t(3.0));
    commit(1060, t(3.1));
    commit(1120, t(4.1));
    D.abs(q, "div", { x: 1054, y: 504, w: 272, h: 82, in: t(3.8), kind: "pop" }, { style: { border: "4px solid var(--red)", borderRadius: "20px" } });

    // Zone C: conflict resolution and the exact head
    const C = D.layer(q, { in: "s6.10", out: "s6.11" });
    D.abs(C.el, "div", { x: 1400, y: 240, in: "s6.10" }, { html: "<b style='font-size:32px'>Conflict</b>" });
    D.abs(C.el, "span", { x: 1400, y: 300, in: "s6.10+0.1", kind: "pop" }, { class: "chip red", text: "#12 conflicts" });
    D.abs(C.el, "span", { x: 1400, y: 370, in: "s6.10+0.7", kind: "pop" }, { class: "chip violet", text: "developer agent: resolves" });
    D.abs(C.el, "span", { x: 1400, y: 440, in: "s6.10+1.3", kind: "pop" }, { class: "chip violet", text: "reviewer agent: checks" });
    D.abs(C.el, "span", { x: 1400, y: 510, in: "s6.10+1.9", kind: "pop" }, { class: "chip green", text: "rebased onto main" });
    D.abs(C.el, "span", { x: 1400, y: 610, in: "s6.10+2.8", kind: "pop" }, { class: "chip blue", text: "merge sha = CI-tested head" });
    commit(1460, "s6.10+3.3");

    // Zone D: main watch
    D.abs(q, "div", { x: 1380, y: 300, w: 440, h: 300, in: "s6.11+0.4" }, {
      class: "card", html: '<h3 style="color:var(--red)">Main CI red</h3><p>The main watch opens one ticket with the job logs.</p><div class="chips"><span class="chip">ENG-7</span><span class="chip blue">repo:acme/app</span><span class="chip blue">symphony</span><span class="chip blue">ci</span></div>',
      style: { borderColor: "var(--red)" },
    });
  },

  // 7. Models and credentials
  s7(sec, sv) {
    D.shot(sec, {
      ...VIEW, in: "s7", out: "s7.4",
      shots: [{ src: SHOT("admin-models"), in: "s7", out: "s7.2" }, { src: SHOT("admin-providers"), in: "s7.2", out: "s7.3" }, { src: SHOT("admin-mcp"), in: "s7.3" }],
      cam: [["s7", 930, 640, 0.95], ["s7.1+1.2", 1950, 640, 0.95], ["s7.2", 930, 560, 0.95], ["s7.2+1.8", 1950, 560, 0.95],
        ["s7.3", 930, 560, 0.95], ["s7.3+1.8", 1950, 600, 0.95]],
      spots: [{ x: 2030, y: 588, w: 790, h: 84, in: "s7.3+2.4", out: "s7.4" }],
    });
    D.callout(sec, { x: 130, y: 805, in: "s7.1+0.8", out: "s7.2" }, "Admin › Models: seven roles, published as a new generation");
    D.callout(sec, { x: 130, y: 805, in: "s7.2+0.6", out: "s7.3" }, "Admin › Providers: a key is write-only. Tern shows only key set or no key.");
    D.callout(sec, { x: 130, y: 805, in: "s7.3+0.6", out: "s7.4" }, "Admin › MCP: agents call the gateway, and the gateway adds the stored headers");

    D.box(sec, { x: 100, y: 205, w: 760, h: 680, cls: "trusted", in: "s7.4", kick: "Trusted side", title: "API, daemon, runners, merger", style: { justifyContent: "flex-start" } });
    const keys = ["Linear API key", "GitHub App private key", "Slack and Discord tokens", "Symphony API tokens", "MCP server headers (in the gateway)"];
    keys.forEach((k, i) => D.abs(sec, "span", { x: 140, y: 330 + i * 72, in: `s7.4+${(0.3 + i * 0.2).toFixed(2)}`, kind: "pop" }, { class: "chip amber", text: k }));
    D.abs(sec, "div", { x: 140, y: 720, w: 680, in: "s7.5+0.4", kind: "pop" }, {
      class: "chip red", text: "✕ never enters a workspace", style: { fontSize: "30px", padding: "10px 20px" },
    });

    D.abs(sec, "div", { x: 958, y: 205, w: 4, h: 680, in: "s7.4+0.2" }, { style: { background: "repeating-linear-gradient(#f5b452 0 14px, transparent 14px 28px)" } });
    D.box(sec, { x: 1060, y: 205, w: 760, h: 680, cls: "ws", in: "s7.4+0.3", kick: "Untrusted", title: "Workspace pod", style: { justifyContent: "flex-start" } });
    const creds = [
      ["Model credential of the role", "/run/symphony/engine.env · 0600"],
      ["GitHub installation token", "ticket's repositories · ≤ 1 h"],
      ["Telemetry token", "one dispatch · gateway only"],
      ["MCP session token", "its role's servers · via the gateway"],
    ];
    creds.forEach(([t, s], i) => {
      const ref = Engine.at("s7.4") + 0.8 + i * 1.2;
      D.abs(sec, "div", { x: 1090, y: 300 + i * 142, w: 700, h: 128, in: ref, kind: "pop" }, { class: "card", html: `<h3>${t}</h3><p class="mono" style="color:var(--amber)">${s}</p>`, style: { padding: "16px 24px" } });
      const path = D.wire(sec, sv, [860, 470], [1090, 364 + i * 142], { in: ref, color: "#f5b452", noArrow: true, o: [["s7", 1], ["s7.5", 0.25]] });
      D.packet(sv, path, ref, ref + 0.7, "#f5b452");
    });
  },

  // 8. Proof
  s8(sec) {
    const mocks = [["Linear", "GraphQL, webhooks"], ["GitHub", "REST, git, CI"], ["Slack", "Web API"], ["Discord", "webhooks, messages"], ["Model APIs", "streams"]];
    mocks.forEach(([t, s], i) => D.box(sec, { x: 100 + i * 349, y: 205, w: 325, h: 130, cls: "ext", in: `s8.1+${(0.1 + i * 0.25).toFixed(2)}`, kick: "mock", title: t, sub: s }));
    D.abs(sec, "div", { x: 100, y: 350, in: "s8.1+1.4" }, { html: "Each request is checked against the vendor's published specification.", style: { fontSize: "30px", color: "var(--muted)" } });

    // Two mocked runs: the default (Linear, Slack) and the GitHub tracker with Discord.
    const board = D.abs(sec, "div", { x: 100, y: 412, w: 1010, h: 478, in: "s8.2" }, { class: "card", style: { padding: "18px 26px" } });
    board.append(Engine.h("h3", { text: "Mocked end-to-end certification" }));
    const grid = Engine.h("div", { style: { display: "grid", gridTemplateColumns: "26px 1fr 190px 220px", columnGap: "14px", fontSize: "28px", lineHeight: "38px" } });
    board.append(grid);
    const cell = (text, style = {}, cls = "") => {
      const el = Engine.h("span", { text, class: cls, style });
      grid.append(el);
      return el;
    };
    const muted = { color: "var(--muted)" };
    cell("");
    cell("Scenario", muted);
    cell("Linear, Slack", muted);
    Engine.anim(cell("GitHub, Discord", muted), { in: "s8.3", kind: "pop" });
    const rows = [
      ["Implement → PR → merge → Done", true], ["One ticket, two repositories", false], ["Read-only investigation", true],
      ["Sub-issues and evidence", true], ["Review comment → agent answer", false], ["Learnings pull request", false],
      ["Red main → ticket → closed", true], ["A real bug goes back", false],
    ];
    rows.forEach(([name, github], i) => {
      cell(String(i + 1), muted, "mono");
      cell(name);
      Engine.anim(cell("✓", {}, "tick"), { in: `s8.2+${(0.4 + i * 0.25).toFixed(2)}`, kind: "pop" });
      Engine.anim(cell(github ? "✓" : "—", github ? {} : muted, github ? "tick" : ""), { in: `s8.3+${(0.3 + i * 0.2).toFixed(2)}`, kind: "pop" });
    });
    cell("");
    cell("Time in total (real agents)", muted);
    Engine.anim(cell("278.4 s", { fontWeight: "700" }), { in: "s8.2+2.6", kind: "pop" });
    Engine.anim(cell("182.9 s", { fontWeight: "700" }), { in: "s8.3+2.0", kind: "pop" });

    const tile = (y, ref, big, text, color) => D.abs(sec, "div", { x: 1150, y, w: 670, h: 146, in: ref, kind: "pop" }, {
      class: "card", html: `<div style="display:flex;align-items:center;gap:26px;height:100%"><div class="big" style="color:${color};min-width:210px">${big}</div><p>${text}</p></div>`,
      style: { padding: "0 26px" },
    });
    tile(412, "s8.4", "69.5 s", "Assistant on Slack: ask, plan, MCP through the gateway (9, 10, 12)", "var(--violet)");
    tile(578, "s8.4+0.7", "43.2 s", "Assistant on Discord with the GitHub tracker (11). Slack ack: 0.003 s", "var(--violet)");
    tile(744, "s8.5", "44 ms", "Ticket → session start. The target is under 3 s.", "var(--blue)");
  },

  // 9. Get started: the minimal stack, then the setup wizard (CLI or Tern).
  s9(sec) {
    D.term(sec, { x: 100, y: 205, w: 1720, in: "s9.1", out: "s9.4" }, "Minimal stack: the API and Redis. Log in with the bootstrap token.", [
      { cmd: "kubectl apply -k deploy/", in: "s9.1+0.1" },
      { cmd: "symphony login --url http://127.0.0.1:8080 --token -", in: "s9.1+0.6" },
      { cmd: "symphony setup", in: "s9.2+0.2" },
    ]);
    D.shot(sec, {
      x: 100, y: 460, w: 1720, h: 425, in: "s9.2", out: "s9.4",
      shots: [{ src: SHOT("setup-verifying"), in: "s9.2", out: "s9.3" }, { src: SHOT("setup-ready"), in: "s9.3" }],
      cam: [["s9.2", 1440, 560, 0.59], ["s9.2+1.0", 1150, 520, 0.75], ["s9.3", 1440, 560, 0.59]],
      spots: [
        { x: 25, y: 290, w: 1930, h: 400, in: "s9.2+0.6", out: "s9.3" },
        { x: 25, y: 245, w: 2825, h: 330, in: "s9.3+0.6" },
      ],
    });
    D.callout(sec, { x: 130, y: 810, in: "s9.2+1.2", out: "s9.3" }, "Setup in Tern: the same steps as <span class=\"mono\">symphony setup</span>");
    D.callout(sec, { x: 130, y: 810, in: "s9.3+0.4", out: "s9.4" }, "Tracker, GitHub App, repositories, models, notifications, assistant, webhooks, skills: each one verified");
    D.abs(sec, "div", { x: 0, y: 300, w: 1920, in: "s9.4+0.1" }, { class: "big-word", text: "Symphony", style: { textAlign: "center", fontSize: "150px" } });
    D.abs(sec, "div", { x: 0, y: 520, w: 1920, in: "s9.4+0.4" }, { class: "mono", text: "docs/install.md · docs/architecture.md · docs/concepts.md", style: { textAlign: "center", fontSize: "36px", color: "var(--blue)" } });
    D.abs(sec, "div", { x: 0, y: 600, w: 1920, in: "s9.4+0.7" }, { text: "MIT License", style: { textAlign: "center", fontSize: "32px", color: "var(--muted)" } });
  },
};
