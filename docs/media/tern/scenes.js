// Operating Symphony from Tern: nine scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/tern.md, docs/cli.md, docs/security.md, CHANGELOG.md (0.2.1 to Unreleased) and tern-plugin/
// (the footers are the LEGENDS of lib/views.luau; the confirm and prompt texts are those of lib/app.luau; the chat's
// first line is remote_note in src/symphony/cli/acp_map.py). The screenshots are docs/tern/*-dark.png (2880x1920;
// the Dashboard and Telemetry are 2880x3800); spots are in image pixels. The status bar is not in every screenshot.

const SHOT = (name) => `../../tern/${name}-dark.png`;
const VP = { x: 100, y: 200, w: 1720, h: 590 }; // the screenshot viewport, above the key row at y 812
const TOP = [1440, 540, 0.6]; // the whole width and the top half of a screenshot

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const kbd = (k) => `<span class="kbd">${k}</span>`;
const keyrow = (items) => items.map(([k, l]) => `${kbd(k)}<span class="lbl">${l}</span>`).join("");
// One key cap and its label at (x, 812).
const key = (parent, x, ref, k, label, out = null, y = 812) =>
  D.abs(parent, "div", { x, y, in: ref, out, kind: "pop" }, { class: "keyrow", html: keyrow([[k, label]]) });
const spot = (x, y, w, h, i, o = null) => ({ x, y, w, h, in: i, out: o });
// The stylesheet draws every screenshot 2880x1920; a taller one (the Dashboard, Telemetry) keeps its own height.
const natural = (vp) => {
  for (const img of vp.querySelectorAll("img")) img.style.height = "auto";
  return vp;
};
const shot = (parent, name, ref, cam, spots = [], out = null) =>
  natural(D.shot(parent, { ...VP, in: ref, out, shots: [{ src: SHOT(name), in: ref }], cam, spots }));
// Chips side by side at (x, y), each popping in at its own time: items [[ref, text, cls, out?]].
function row(parent, x, y, items) {
  const el = D.abs(parent, "div", { x, y, in: items[0][0], kind: "none" }, { style: { display: "flex", gap: "18px" } });
  for (const [ref, text, cls, out] of items) {
    const c = Engine.h("span", { class: `chip ${cls || ""}`, text });
    el.append(c);
    Engine.anim(c, { in: ref, out, kind: "pop" });
  }
  return el;
}
const ZOOM = [1012, 420, 0.85]; // the left two thirds of the top of a screenshot, for short pages
// Readable zooms at 0.85 (2024x694 image px): the left part ends at x 1892, the right part starts at x 856.
const LEFT = (y) => [880, y, 0.85];
const RIGHT = (y) => [1868, y, 0.85];
const SIDE = { x: 900, y: 200, w: 920, h: 590 }; // a narrow viewport on the right, for one card at scale 1
const vshot = (parent, vp, name, ref, cam, spots = [], out = null) =>
  natural(D.shot(parent, { ...vp, in: ref, out, shots: [{ src: SHOT(name), in: ref }], cam, spots }));

const SCENES = {
  // 1. The plugin and sign-in
  s1(sec, sv) {
    const A = D.layer(sec, { in: "s1", out: "s1.3" });
    const tern = D.box(A.el, { x: 100, y: 280, w: 620, h: 300, in: "s1.1", cls: "ext", kick: "Tern", title: "symphony plugin",
      sub: "a panel, a status line and live chats" });
    const sym = D.box(A.el, { x: 1200, y: 280, w: 620, h: 300, in: "s1.1+0.6", cls: "trusted", kick: "Your team", title: "Symphony",
      sub: "agents, tickets, merger, administration" });
    D.wire(A.el, A.sv, tern, sym, { in: "s1.1+0.9", bi: true, label: "API", color: "#5cb8ff" });
    [["Dashboard", "blue"], ["Live chat", "blue"], ["Merger", "violet"], ["Admin", "amber"]].forEach(([t, c], i) =>
      chip(A.el, 100 + i * 250, 640, `s1.2+${(0.3 + i * 0.5).toFixed(1)}`, t, c));

    shot(sec, "signin", "s1.3", [["s1.3", ...ZOOM]], [
      spot(20, 182, 2840, 120, "s1.3+1.6", "s1.3+3.6"),
      spot(20, 342, 2840, 125, "s1.3+3.6", "s1.5"),
    ], "s1.5");
    row(sec, 100, 812, [["s1.4", "masked", "blue", "s1.5"], ["s1.4+1.2", "symphony login → system keychain", "green", "s1.5"]]);

    const B = D.layer(sec, { in: "s1.5", out: "s1.6" });
    const pl = D.box(B.el, { x: 100, y: 260, w: 420, h: 210, in: "s1.5", cls: "ext", kick: "Tern", title: "Plugin", sub: "token in memory only" });
    const cli = D.box(B.el, { x: 800, y: 260, w: 420, h: 210, in: "s1.5+0.4", cls: "trusted", kick: "the only owner", title: "symphony CLI" });
    const kc = D.box(B.el, { x: 1420, y: 260, w: 400, h: 210, in: "s1.5+0.8", cls: "store", title: "System keychain" });
    const w1 = D.wire(B.el, B.sv, pl, cli, { in: "s1.5+0.6", label: "token print --json", lo: [0, -34], color: "#5cb8ff" });
    D.wire(B.el, B.sv, cli, kc, { in: "s1.5+1.0", bi: true, color: "#b197fc" });
    D.packet(B.sv, w1, "s1.5+1.2", "s1.5+1.8", "#5cb8ff");
    chip(B.el, 100, 520, "s1.5+2.0", "not in save(), not in args", "green");
    chip(B.el, 100, 600, "s1.5+2.8", "a 401 asks the CLI again", "blue");

    shot(sec, "signin-ready", "s1.6", [["s1.6", 1012, 440, 0.85]], [spot(20, 390, 2840, 296, "s1.6+0.8")], "s1.7");
    chip(sec, 100, 812, "s1.6+0.4", "symphony tern install --no-plugin", "blue", "s1.7");

    shot(sec, "disconnected", "s1.7", [["s1.7", 1012, 400, 0.85], ["s1.7+2.0", 1440, 1440, 0.6]], [
      spot(20, 262, 2840, 180, "s1.7+0.4", "s1.7+2.0"),
      spot(2550, 1864, 310, 52, "s1.7+2.6"),
    ], "s1.8");

    shot(sec, "offline", "s1.8", [["s1.8", ...TOP], ["s1.8+1.6", 1440, 1440, 0.6]], [
      spot(2504, 108, 120, 46, "s1.8+0.3", "s1.8+1.6"),
      spot(20, 1748, 975, 50, "s1.8+2.2"),
      spot(2556, 1864, 304, 52, "s1.8+2.8"),
    ]);
  },

  // 2. The Dashboard
  s2(sec) {
    // The top of the page (numbers, rate limits), the last 30 days, then the question.
    shot(sec, "dashboard", "s2", [["s2", ...TOP], ["s2+1.0", 1440, 610, 0.6], ["s2.3", 1440, 2000, 0.6], ["s2.4", ...LEFT(830)]], [
      spot(20, 270, 960, 100, "s2.2", "s2.2+1.6"),
      spot(20, 400, 2840, 240, "s2.2+1.6", "s2.3"),
      spot(20, 1696, 2840, 266, "s2.3+0.3", "s2.3+2.4"),
      spot(2386, 1696, 466, 266, "s2.3+2.4", "s2.3+3.8"),
      spot(20, 1966, 2840, 370, "s2.3+3.8", "s2.4"),
      spot(20, 716, 1860, 228, "s2.4", "s2.5"),
    ], "s2.5");
    key(sec, 100, "s2.1", "ctrl+alt+s", "Symphony: Dashboard", "s2.3");
    chip(sec, 100, 812, "s2.3+0.4", "the last 30 days, a point a day", "blue", "s2.4");
    // The selected session's card, at scale 1, with the keys beside it.
    vshot(sec, SIDE, "dashboard", "s2.5", [["s2.5", 2410, 1320, 1.0]], [
      spot(2138, 1094, 116, 50, "s2.5", "s2.6"),
      spot(2255, 1094, 192, 50, "s2.6", "s2.7"),
      spot(2446, 1094, 130, 50, "s2.7", "s2.8"),
      spot(2576, 1094, 138, 50, "s2.8", "s2.9"),
    ], "s2.9");
    key(sec, 100, "s2.5", "a", "answer", "s2.9", 240);
    key(sec, 100, "s2.6", "m", "message", "s2.9", 320);
    key(sec, 100, "s2.7", "i", "interrupt", "s2.9", 400);
    key(sec, 100, "s2.8", "t", "take over", "s2.9", 480);
    D.abs(sec, "div", { x: 100, y: 560, w: 760, in: "s2.7+0.6", out: "s2.8" }, {
      class: "strip red",
      html: `<b>Interrupt ENG-139?</b><p>The agent stops the current turn right away. The session stays open, and you can send a message later.</p>${keyrow([["enter", "Interrupt"], ["esc", "Cancel"]])}`,
    });
    // The status line (window.luau), drawn: the screenshots do not always show Tern's status bar.
    D.abs(sec, "div", { x: 100, y: 240, in: "s2.9" }, { text: "The status line", style: { fontSize: "32px", fontWeight: "700" } });
    D.abs(sec, "div", { x: 900, y: 320, w: 920, in: "s2.9+0.3" }, {
      class: "strip",
      html: `<b>Tern's status bar</b><p style="font-family: var(--mono)">Symphony · 4 running · 1 needs input</p><p>A click opens the panel.</p>`,
    });
    chip(sec, 100, 320, "s2.9+0.6", "Symphony · 4 running · 1 needs input", "amber");
    chip(sec, 100, 400, "s2.10+1.6", "Symphony · 4 running · paused", "amber");
    chip(sec, 100, 480, "s2.10+2.6", "Symphony · offline", "red");
  },

  // 3. Sessions and live chat
  s3(sec) {
    shot(sec, "sessions", "s3", [["s3", 1440, 560, 0.6]], [
      spot(2286, 270, 568, 60, "s3.1+1.4", "s3.1+4.0"),
      spot(1970, 345, 880, 712, "s3.1+4.0"),
    ], "s3.2");
    key(sec, 100, "s3.1+1.4", "[ ]", "filter", "s3.2");

    shot(sec, "chat", "s3.2", [["s3.2", 1440, 560, 0.6]], [spot(908, 196, 1094, 840, "s3.2+0.6")], "s3.3");
    key(sec, 100, "s3.2", "ctrl+alt+a", "Symphony: Chat with an agent", "s3.3");

    shot(sec, "real-chat", "s3.3", [["s3.3", 1440, 565, 0.9], ["s3.3+1.4", 860, 1625, 1.0], ["s3.3+3.2", 1440, 565, 0.9], ["s3.7", 1440, 1530, 0.9]], [
      spot(548, 430, 1790, 290, "s3.3+0.3", "s3.3+1.4"),
      spot(262, 1864, 580, 52, "s3.3+2.2", "s3.3+3.4"),
      spot(548, 430, 1790, 290, "s3.5", "s3.7"),
      spot(694, 1632, 1492, 192, "s3.7+0.4"),
    ], "s3.8");
    // The chat's first line (acp_map.remote_note) and the tab's empty folder (window.luau chat_dir).
    D.abs(sec, "div", { x: 100, y: 215, w: 1720, in: "s3.4", out: "s3.5" }, {
      class: "strip",
      html: `<b>The chat's first line</b><p><i>Runs in the cluster: pod <span style="font-family: var(--mono)">symphony-eng-142</span> · <span style="font-family: var(--mono)">acme/app</span> · branch <span style="font-family: var(--mono)">symphony/eng-142-csv-export</span>. Nothing runs on this machine: this chat shows the session and sends your messages.</i></p>`,
      style: { borderColor: "var(--blue)", background: "#111822" },
    });
    chip(sec, 100, 812, "s3.4+2.6", "the tab's folder: ~/.cache/symphony/remote/app/ENG-142 (empty)", "blue", "s3.5");
    chip(sec, 100, 812, "s3.5", "running → steer", "blue", "s3.8");
    chip(sec, 440, 812, "s3.6", "idle → follow-up", "violet", "s3.8");
    chip(sec, 790, 812, "s3.6+1.8", "question open → answer", "amber", "s3.8");
    chip(sec, 1260, 812, "s3.7", "/interrupt", "red", "s3.8");

    D.term(sec, { x: 100, y: 205, w: 1720, in: "s3.8" }, "terminal, inside the repository", [
      { cmd: "symphony takeover ENG-142", in: "s3.8+0.2" },
      { c: "# holds the ticket: Symphony stops dispatching, retrying and continuing it", in: "s3.8+1.2" },
      { c: "# interrupts the turn, downloads the transcript and the work branch", in: "s3.8+2.4" },
      { c: "# opens omp --resume with the whole conversation", in: "s3.8+3.6" },
      { cmd: "symphony release ENG-142", in: "s3.9" },
      { c: "# returns the ticket to Symphony", in: "s3.9+1.0" },
    ]);
    D.abs(sec, "div", { x: 100, y: 740, in: "s3.8+0.6" }, {
      class: "keyrow", html: `In Tern, ${kbd("t")} on the Dashboard or in Sessions runs it in a new tab, after a confirmation.`,
    });
  },

  // 4. Tickets and holds
  s4(sec) {
    // The table: names and the state in Symphony, then (in a shorter viewport that stops above the held row) the
    // board, the attempt, the cost and the pull request, then the "Held by a person" row.
    shot(sec, "tickets", "s4", [["s4", ...TOP], ["s4+1.0", 985, 560, 0.9]], [spot(1310, 340, 620, 370, "s4.1+1.4", "s4.1+3.0")], "s4.1+3.0");
    vshot(sec, { ...VP, h: 450 }, "tickets", "s4.1+3.0", [["s4.1+3.0", 1925, 482, 0.9]], [spot(2105, 340, 750, 370, "s4.1+3.4")], "s4.2");
    shot(sec, "tickets", "s4.2", [["s4.2", 985, 700, 0.9]], [
      spot(20, 728, 1470, 92, "s4.2+0.4", "s4.3"),
      spot(430, 772, 250, 50, "s4.3"),
    ]);
    D.abs(sec, "div", { x: 920, y: 520, w: 900, in: "s4.3+1.0" }, {
      class: "strip",
      html: `<b>Return ENG-147 to Symphony?</b><p>The ticket becomes eligible again. Symphony can dispatch a new attempt from what is on the branch.</p>${keyrow([["enter", "Return to Symphony"], ["esc", "Cancel"]])}`,
      style: { borderColor: "var(--green)", background: "#101a15" },
    });
    row(sec, 100, 812, [["s4.2+0.4", "held by a person: no dispatch", "amber"], ["s4.3+1.0", "symphony release", "green"]]);
  },

  // 5. The merger view
  s5(sec) {
    shot(sec, "merger", "s5", [["s5", ...TOP], ["s5.3", ...RIGHT(560)], ["s5.4", 1440, 1150, 0.6], ["s5.5", ...LEFT(1000)], ["s5.6", 1012, 1320, 0.85]], [
      spot(20, 400, 2840, 236, "s5.2", "s5.3"),
      spot(2190, 450, 660, 190, "s5.3+0.3", "s5.4"),
      spot(20, 1395, 2840, 48, "s5.4+0.3", "s5.5"),
      spot(76, 720, 260, 128, "s5.5+0.4", "s5.6"),
      spot(76, 940, 260, 270, "s5.5+0.4", "s5.6"),
      spot(228, 1448, 156, 50, "s5.6", "s5.7"),
      spot(382, 1448, 168, 50, "s5.7", "s5.7+1.2"),
      spot(548, 1448, 160, 50, "s5.7+1.2", "s5.8"),
      spot(20, 1448, 690, 50, "s5.8"),
    ]);
    key(sec, 100, "s5.1+1.6", "ctrl+alt+m", "Symphony: Merger", "s5.3");
    chip(sec, 100, 812, "s5.3+0.4", "every PR · or labeled: symphony, symphony-learnings", "blue", "s5.4");
    chip(sec, 100, 812, "s5.4+0.4", "clean review: waiting for CI → waiting for approval", "amber", "s5.5");
    key(sec, 100, "s5.6", "b", "bypass");
    key(sec, 330, "s5.7", "q", "requeue");
    key(sec, 570, "s5.7+1.2", "l", "release");
    D.abs(sec, "div", { x: 860, y: 230, w: 960, in: "s5.6+1.4", out: "s5.7" }, {
      class: "prompt",
      html: 'Reason to bypass acme/web #821 (at least 8 characters). It goes to the audit log.<div class="in">Approved hotfix for the release</div>',
    });
    chip(sec, 880, 812, "s5.8+0.4", "#821 → acme/web", "violet");
  },

  // 6. The other views
  s6(sec) {
    natural(D.shot(sec, {
      ...VP, in: "s6",
      shots: [
        { src: SHOT("repositories"), in: "s6" },
        { src: SHOT("infrastructure"), in: "s6.4" },
        { src: SHOT("telemetry"), in: "s6.5" },
        { src: SHOT("learnings"), in: "s6.7" },
        { src: SHOT("audit"), in: "s6.8" },
        { src: SHOT("infrastructure"), in: "s6.9" },
      ],
      cam: [
        ["s6", ...TOP], ["s6+1.0", 985, 330, 0.9], // the tab strip
        ["s6.2", 985, 440, 0.9], ["s6.2+2.0", 1925, 520, 0.9], // repositories: names and detected values, then the card
        ["s6.4", 962, 680, 0.85], ["s6.4+1.6", ...RIGHT(680)], // infrastructure: names, then states
        ["s6.5", 1440, 2800, 0.6], // telemetry: by model, by month and by session
        ["s6.6", 1440, 900, 0.6], ["s6.6+1.8", 1440, 1750, 0.6], ["s6.6+3.4", 1440, 900, 0.6], // history: the charts, then the period
        ["s6.7", 860, 400, 0.85], ["s6.7+1.4", ...RIGHT(680)], // learnings: tickets, then origin and result
        ["s6.8", 985, 600, 0.9], // audit: every column fits
        ["s6.9", 962, 680, 0.85], // infrastructure again: the Sources line
      ],
      spots: [
        spot(676, 186, 770, 56, "s6.1+1.0", "s6.2"),
        spot(680, 380, 720, 140, "s6.2+0.6", "s6.2+2.0"),
        spot(2650, 380, 190, 50, "s6.3+0.2", "s6.4"),
        spot(2500, 438, 356, 66, "s6.6+3.6", "s6.7"),
        spot(20, 968, 484, 52, "s6.9+0.4"),
      ],
    }));
    chip(sec, 100, 812, "s6.1+0.5", "viewer, operator and admin", "blue", "s6.2");
    key(sec, 100, "s6.3", "e", "configure", "s6.4");
    chip(sec, 430, 812, "s6.3+0.8", "admin · runtime, base branch, components", "amber", "s6.4");
    key(sec, 100, "s6.6+3.6", "[ ]", "period", "s6.7");
    chip(sec, 420, 812, "s6.6+3.8", "All · 90 d · 30 d · 7 d", "blue", "s6.7");
    chip(sec, 100, 812, "s6.9+0.4", "Sources  Kubernetes: stale (1 min ago)", "amber");
    key(sec, 100, "s6.8+0.4", "m", "load more", "s6.9");
  },

  // 7. Admin: models and providers
  s7(sec) {
    D.shot(sec, {
      ...VP, in: "s7",
      shots: [
        { src: SHOT("admin-models"), in: "s7" },
        { src: SHOT("admin-pick-model"), in: "s7.3", out: "s7.4" },
        { src: SHOT("admin-providers"), in: "s7.7" },
      ],
      cam: [
        ["s7", ...TOP], ["s7+1.0", 960, 330, 0.9],
        ["s7.2", ...LEFT(700)], ["s7.2+1.6", ...RIGHT(700)],
        ["s7.3", 1455, 440, 0.9],
        ["s7.4", ...LEFT(700)],
        ["s7.6", ...LEFT(1250)], ["s7.6+1.6", ...RIGHT(1250)],
        ["s7.7", 1440, 560, 0.6],
      ],
      spots: [
        spot(1432, 190, 124, 52, "s7.1+1.0", "s7.2"),
        spot(20, 460, 1870, 480, "s7.2", "s7.2+1.6"),
        spot(1880, 460, 970, 480, "s7.2+2.0", "s7.3"),
        spot(908, 196, 1094, 490, "s7.3+0.4", "s7.4"),
        spot(20, 555, 1870, 48, "s7.4", "s7.4+1.6"),
        spot(414, 953, 250, 48, "s7.4+1.6", "s7.6"),
        spot(20, 1005, 2840, 490, "s7.6", "s7.7"),
        spot(20, 405, 2840, 236, "s7.7+0.3", "s7.8"),
        spot(155, 660, 165, 48, "s7.8"),
      ],
    });
    key(sec, 100, "s7.3", "enter", "change model", "s7.4");
    key(sec, 100, "s7.4", "p", "publish", "s7.5");
    D.abs(sec, "div", { x: 760, y: 420, w: 1060, in: "s7.4+2.2", out: "s7.6" }, {
      class: "prompt",
      html: 'Reason for publishing (at least 8 characters). It goes to the history.<div class="in">Oracle on a different provider</div>',
    });
    chip(sec, 100, 812, "s7.5", "If-Match: the current generation", "blue", "s7.6");
    chip(sec, 680, 812, "s7.5+1.0", "nothing is overwritten", "green", "s7.6");
    chip(sec, 100, 812, "s7.6+0.6", "applied at the next unit of work", "green", "s7.7");
    key(sec, 100, "s7.8", "c", "set key");
    D.abs(sec, "div", { x: 760, y: 470, w: 1060, in: "s7.8+0.8" }, {
      class: "prompt",
      html: 'API key for Anthropic (write-only)<div class="in">••••••••••••••••••••</div>',
    });
    chip(sec, 400, 812, "s7.8+3.0", "to the CLI on standard input", "blue");
    chip(sec, 960, 812, "s7.9", "next attempt · no restart", "green");
  },

  // 8. Admin: tokens, MCP servers, skills, team omp
  s8(sec) {
    D.shot(sec, {
      ...VP, in: "s8",
      shots: [
        { src: SHOT("admin-tokens"), in: "s8" },
        { src: SHOT("admin-mcp"), in: "s8.5" },
        { src: SHOT("admin-skills"), in: "s8.8" },
        { src: SHOT("admin-omp"), in: "s8.9" },
      ],
      cam: [
        ["s8", ...TOP], ["s8+1.0", ...LEFT(620)], // tokens: the created card and the list
        ["s8.5", 985, 560, 0.9], // MCP: the server table
        ["s8.6", 1925, 720, 0.9], // MCP: the selected server's card
        ["s8.8", 740, 420, 1.2], // skills: the sources
        ["s8.9", 962, 700, 0.85], ["s8.9+3.0", 962, 940, 0.85], // team omp: the settings, then the applied profile
      ],
      spots: [
        spot(20, 655, 1870, 240, "s8.1+1.0", "s8.3"),
        spot(20, 345, 1870, 240, "s8.3", "s8.4"),
        spot(92, 908, 112, 50, "s8.4", "s8.5"),
        spot(20, 405, 1950, 190, "s8.5+0.4", "s8.6"),
        spot(2000, 760, 780, 170, "s8.6", "s8.7"),
        spot(2000, 620, 800, 52, "s8.7", "s8.8"),
        spot(528, 276, 106, 50, "s8.8", "s8.8+1.0"),
        spot(20, 345, 730, 185, "s8.8+1.0", "s8.9"),
        spot(20, 530, 2840, 322, "s8.9+0.3", "s8.9+3.0"),
        spot(20, 920, 2840, 280, "s8.9+3.0"),
      ],
    });
    key(sec, 100, "s8.1", "n", "new token", "s8.2");
    chip(sec, 100, 812, "s8.2", "viewer: reads every view", "blue", "s8.3");
    chip(sec, 560, 812, "s8.2+1.2", "operator: also acts", "green", "s8.3");
    chip(sec, 960, 812, "s8.2+2.4", "admin: tokens, keys, configuration", "amber", "s8.3");
    chip(sec, 100, 812, "s8.3+0.4", "shown once · stored nowhere", "amber", "s8.4");
    key(sec, 100, "s8.4", "x", "revoke", "s8.5");
    key(sec, 100, "s8.6", "enter", "test", "s8.8");
    key(sec, 380, "s8.7", "h", "headers", "s8.8");
    chip(sec, 100, 812, "s8.8+1.6", "global, or only some repositories", "blue", "s8.9");
    key(sec, 100, "s8.9+1.6", "s", "apply to my omp");
    chip(sec, 560, 812, "s8.9+3.0", "symphony omp sync", "blue");
  },

  // 9. The keyboard
  s9(sec) {
    const A = D.layer(sec, { in: "s9", out: "s9.5" });
    const cmds = [
      ["Dashboard", "ctrl+alt+s"], ["Sessions"], ["Tickets"], ["Merger", "ctrl+alt+m"], ["Repositories"], ["Infrastructure"], ["Telemetry"],
      ["Learnings"], ["Audit"], ["Admin"], ["Chat with an agent", "ctrl+alt+a"], ["Sign in"], ["Setup"],
    ];
    const list = card(A.el, { x: 100, y: 205, w: 1720, h: 420, in: "s9.1" }, "<h3>Palette</h3>", { display: "grid", gridTemplateColumns: "1fr 1fr", columnGap: "60px", alignContent: "start" });
    list.firstChild.style.gridColumn = "1 / 3";
    cmds.forEach(([n, k], i) => {
      const r = Engine.h("div", { class: "cmdlist", html: `<div class="r">Symphony: ${n}${k ? `<span class="k">${kbd(k)}</span>` : ""}</div>`,
        style: { gridColumn: i < 7 ? "1" : "2", gridRow: String((i < 7 ? i : i - 7) + 2) } });
      list.append(r);
      Engine.anim(r, { in: Engine.at("s9.1") + 0.15 * i, lift: 6 });
      if (k) Engine.anim(r.querySelector(".kbd"), { in: "s9.2", kind: "none", o: [["s9.1", 0.35], ["s9.2", 1]] });
    });
    D.abs(A.el, "div", { x: 100, y: 660, in: "s9.3" }, {
      class: "keyrow", html: `In the panel &nbsp; ${keyrow([["tab", "views"], ["↑↓ j k", "select"], ["enter", "chat"]])}`,
    });
    D.abs(A.el, "div", { x: 100, y: 730, in: "s9.4" }, { class: "keyrow", html: keyrow([["r", "refresh"], ["⌃P", "pause or resume"]]) });

    const B = D.layer(sec, { in: "s9.5" });
    const foot = card(B.el, { x: 100, y: 205, w: 1720, h: 320, in: "s9.5" }, "<h3>The Dashboard footer</h3>", {});
    foot.classList.add("foot");
    const base = [["⇥", "tabs"], ["↑↓", "session"], ["enter", "chat"], ["r", "refresh"]];
    [["viewer", base], ["operator or admin", [...base, ["a", "answer"], ["m", "message"], ["i", "interrupt"], ["t", "take over"], ["⌃P", "pause"]]]].forEach(([who, keys], i) => {
      const r = Engine.h("div", { html: `<div class="who">${who}</div><div class="keyrow">${keyrow(keys)}</div>` });
      foot.append(r);
      Engine.anim(r, { in: Engine.at("s9.5") + 0.4 + i * 1.0, lift: 6 });
    });
    D.abs(B.el, "div", { x: 100, y: 555, w: 1720, in: "s9.6" }, {
      class: "strip",
      html: `<b>Take over ENG-142?</b><p>Symphony pauses this attempt, downloads the conversation and the branch, and opens omp in a new tab so you can continue from here.</p>${keyrow([["enter", "Take over"], ["esc", "Cancel"]])}`,
    });
    D.abs(B.el, "div", { x: 100, y: 740, w: 1720, in: "s9.7" }, {
      class: "prompt",
      html: 'Reason for the pause (at least 8 characters). It goes to the audit log.<div class="in">Deploy</div><div class="warn">The reason needs at least 8 characters.</div>',
    });
  },
};
