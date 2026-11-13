// Operating Symphony from Tern: nine scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/tern.md, docs/cli.md, docs/security.md and tern-plugin/ (the footers are the LEGENDS of
// lib/views.luau; the confirm and prompt texts are those of lib/app.luau). The screenshots are docs/tern/*-dark.png
// (2880x1920); spots are in image pixels.

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
const shot = (parent, name, ref, cam, spots = [], out = null) =>
  D.shot(parent, { ...VP, in: ref, out, shots: [{ src: SHOT(name), in: ref }], cam, spots });
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
  D.shot(parent, { ...vp, in: ref, out, shots: [{ src: SHOT(name), in: ref }], cam, spots });

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
    chip(sec, 100, 812, "s1.6+0.4", "symphony tern install", "blue", "s1.7");

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
    // The top of the page: numbers, rate limits and the question (all within x 1880).
    shot(sec, "dashboard", "s2", [["s2", ...TOP], ["s2+1.0", ...LEFT(560)]], [
      spot(20, 266, 960, 104, "s2.2", "s2.2+1.6"),
      spot(20, 390, 1860, 250, "s2.2+1.6", "s2.3"),
      spot(20, 706, 1860, 234, "s2.3", "s2.4"),
    ], "s2.4");
    key(sec, 100, "s2.1", "ctrl+alt+s", "Symphony: Dashboard", "s2.4");
    // The selected session's card, at scale 1, with the keys beside it.
    vshot(sec, SIDE, "dashboard", "s2.4", [["s2.4", 2413, 1290, 1.0]], [
      spot(2188, 1082, 124, 50, "s2.4", "s2.5"),
      spot(2304, 1082, 198, 50, "s2.5", "s2.6"),
      spot(2494, 1082, 138, 50, "s2.6", "s2.7"),
      spot(2626, 1082, 138, 50, "s2.7", "s2.8"),
    ], "s2.8");
    key(sec, 100, "s2.4", "a", "answer", "s2.8", 240);
    key(sec, 100, "s2.5", "m", "message", "s2.8", 320);
    key(sec, 100, "s2.6", "i", "interrupt", "s2.8", 400);
    key(sec, 100, "s2.7", "t", "take over", "s2.8", 480);
    D.abs(sec, "div", { x: 100, y: 560, w: 760, in: "s2.6+0.6", out: "s2.7" }, {
      class: "strip red",
      html: `<b>Interrupt ENG-139?</b><p>The agent stops the current turn right away. The session stays open, and you can send a message later.</p>${keyrow([["enter", "Interrupt"], ["esc", "Cancel"]])}`,
    });
    // The status line at the bottom right, at scale 1, with its states beside it.
    vshot(sec, SIDE, "dashboard", "s2.8", [["s2.8", 2420, 1625, 1.0]], [spot(2310, 1864, 550, 52, "s2.8+0.6")]);
    D.abs(sec, "div", { x: 100, y: 240, in: "s2.8" }, { text: "The status line", style: { fontSize: "32px", fontWeight: "700" } });
    chip(sec, 100, 320, "s2.8+0.6", "Symphony · 4 running · 1 needs input", "amber");
    chip(sec, 100, 400, "s2.9+1.6", "Symphony · 4 running · paused", "amber");
    chip(sec, 100, 480, "s2.9+2.6", "Symphony · offline", "red");
  },

  // 3. Sessions and live chat
  s3(sec) {
    shot(sec, "sessions", "s3", [["s3", 1440, 560, 0.6]], [
      spot(2262, 262, 596, 64, "s3.1+1.4", "s3.1+4.0"),
      spot(1972, 340, 884, 716, "s3.1+4.0"),
    ], "s3.2");
    key(sec, 100, "s3.1+1.4", "[ ]", "filter", "s3.2");

    shot(sec, "chat", "s3.2", [["s3.2", 1440, 560, 0.6]], [spot(908, 196, 1094, 840, "s3.2+0.6")], "s3.3");
    key(sec, 100, "s3.2", "ctrl+alt+a", "Symphony: Chat with an agent", "s3.3");

    shot(sec, "real-chat", "s3.3", [["s3.3", 1440, 565, 0.9], ["s3.3+1.4", 860, 1625, 1.0], ["s3.3+3.2", 1440, 565, 0.9], ["s3.6", 1440, 1530, 0.9]], [
      spot(548, 430, 1790, 290, "s3.3+0.3", "s3.3+1.4"),
      spot(262, 1864, 580, 52, "s3.3+2.2", "s3.3+3.4"),
      spot(548, 430, 1790, 290, "s3.3+4.0", "s3.6"),
      spot(694, 1632, 1492, 192, "s3.6+0.4"),
    ], "s3.7");
    chip(sec, 100, 812, "s3.4", "running → steer", "blue", "s3.7");
    chip(sec, 440, 812, "s3.5", "idle → follow-up", "violet", "s3.7");
    chip(sec, 790, 812, "s3.5+1.8", "question open → answer", "amber", "s3.7");
    chip(sec, 1260, 812, "s3.6", "/interrupt", "red", "s3.7");

    D.term(sec, { x: 100, y: 205, w: 1720, in: "s3.7" }, "terminal, inside the repository", [
      { cmd: "symphony takeover ENG-142", in: "s3.7+0.2" },
      { c: "# holds the ticket: Symphony stops dispatching, retrying and continuing it", in: "s3.7+1.2" },
      { c: "# interrupts the turn, downloads the transcript and the work branch", in: "s3.7+2.4" },
      { c: "# opens omp --resume with the whole conversation", in: "s3.7+3.6" },
      { cmd: "symphony release ENG-142", in: "s3.8" },
      { c: "# returns the ticket to Symphony", in: "s3.8+1.0" },
    ]);
    D.abs(sec, "div", { x: 100, y: 740, in: "s3.7+0.6" }, {
      class: "keyrow", html: `In Tern, ${kbd("t")} on the Dashboard or in Sessions runs it in a new tab, after a confirmation.`,
    });
  },

  // 4. Tickets and holds
  s4(sec) {
    // The table: names and the state in Symphony, then (in a shorter viewport that stops above the held row) the
    // board, the attempt, the cost and the pull request, then the "Held by a person" row.
    shot(sec, "tickets", "s4", [["s4", ...TOP], ["s4+1.0", 985, 560, 0.9]], [spot(1310, 326, 600, 386, "s4.1+1.4", "s4.1+3.0")], "s4.1+3.0");
    vshot(sec, { ...VP, h: 450 }, "tickets", "s4.1+3.0", [["s4.1+3.0", 1925, 482, 0.9]], [spot(2110, 326, 760, 386, "s4.1+3.4")], "s4.2");
    shot(sec, "tickets", "s4.2", [["s4.2", 985, 700, 0.9]], [
      spot(20, 724, 1470, 104, "s4.2+0.4", "s4.3"),
      spot(428, 768, 256, 52, "s4.3"),
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
    shot(sec, "merger", "s5", [["s5", ...TOP], ["s5+1.0", ...LEFT(600)], ["s5.3", ...LEFT(997)], ["s5.4", 1012, 1320, 0.85]], [
      spot(20, 392, 1870, 240, "s5.2", "s5.3"),
      spot(76, 704, 254, 148, "s5.3+0.4", "s5.4"),
      spot(76, 926, 254, 284, "s5.3+0.4", "s5.4"),
      spot(228, 1432, 156, 50, "s5.4", "s5.5"),
      spot(382, 1432, 168, 50, "s5.5", "s5.5+1.2"),
      spot(546, 1432, 150, 50, "s5.5+1.2", "s5.6"),
      spot(20, 1432, 680, 50, "s5.6"),
    ]);
    key(sec, 100, "s5.1+1.6", "ctrl+alt+m", "Symphony: Merger", "s5.4");
    key(sec, 100, "s5.4", "b", "bypass");
    key(sec, 330, "s5.5", "q", "requeue");
    key(sec, 570, "s5.5+1.2", "l", "release");
    D.abs(sec, "div", { x: 860, y: 230, w: 960, in: "s5.4+1.4", out: "s5.5" }, {
      class: "prompt",
      html: 'Reason to bypass acme/web #821 (at least 8 characters). It goes to the audit log.<div class="in">Approved hotfix for the release</div>',
    });
    chip(sec, 880, 812, "s5.6+0.4", "→ the merger of acme/web", "violet");
  },

  // 6. The read-only views
  s6(sec) {
    D.shot(sec, {
      ...VP, in: "s6",
      shots: [
        { src: SHOT("repositories"), in: "s6" },
        { src: SHOT("infrastructure"), in: "s6.3" },
        { src: SHOT("telemetry"), in: "s6.4" },
        { src: SHOT("learnings"), in: "s6.5" },
        { src: SHOT("audit"), in: "s6.6" },
        { src: SHOT("infrastructure"), in: "s6.7" },
      ],
      cam: [
        ["s6", ...TOP], ["s6+1.0", 985, 330, 0.9], // the tab strip
        ["s6.2", 985, 420, 0.9], ["s6.2+1.4", 1925, 600, 0.9], // repositories: names, then runtime and arrival
        ["s6.3", 962, 680, 0.85], ["s6.3+1.6", ...RIGHT(680)], // infrastructure: names, then states
        ["s6.4", ...LEFT(620)], ["s6.4+1.6", ...RIGHT(620)], // telemetry: by model and by month, names then numbers
        ["s6.5", 860, 400, 0.85], ["s6.5+1.4", ...RIGHT(680)], // learnings: tickets, then origin and result
        ["s6.6", 985, 600, 0.9], // audit: every column fits
        ["s6.7", 962, 680, 0.85], // infrastructure again: the Sources line
      ],
      spots: [spot(686, 176, 858, 60, "s6.1+1.0", "s6.2"), spot(20, 968, 484, 52, "s6.7+0.4")],
    });
    chip(sec, 100, 812, "s6.1+0.5", "viewer, operator and admin", "blue", "s6.2");
    chip(sec, 100, 812, "s6.7+0.4", "Sources  Kubernetes: stale (1 min ago)", "amber");
    key(sec, 100, "s6.6+0.4", "m", "load more", "s6.7");
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
        ["s7.2", ...LEFT(640)], ["s7.2+1.6", ...RIGHT(790)],
        ["s7.3", 1455, 440, 0.9],
        ["s7.4", ...LEFT(640)],
        ["s7.6", ...LEFT(1200)], ["s7.6+1.6", ...RIGHT(1200)],
        ["s7.7", ...LEFT(750)],
      ],
      spots: [
        spot(1508, 176, 136, 60, "s7.1+1.0", "s7.2"),
        spot(20, 452, 1870, 448, "s7.2", "s7.2+1.6"),
        spot(1880, 452, 970, 448, "s7.2+2.0", "s7.3"),
        spot(908, 196, 1094, 480, "s7.3+0.4", "s7.4"),
        spot(20, 536, 1870, 56, "s7.4", "s7.4+1.6"),
        spot(412, 908, 256, 52, "s7.4+1.6", "s7.6"),
        spot(20, 966, 2840, 484, "s7.6", "s7.7"),
        spot(20, 396, 1870, 230, "s7.7+0.3", "s7.8"),
        spot(154, 630, 208, 52, "s7.8"),
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

  // 8. Admin: tokens, MCP servers, team omp
  s8(sec) {
    D.shot(sec, {
      ...VP, in: "s8",
      shots: [
        { src: SHOT("admin-tokens"), in: "s8" },
        { src: SHOT("admin-mcp"), in: "s8.5" },
        { src: SHOT("admin-omp"), in: "s8.8" },
      ],
      cam: [
        ["s8", ...TOP], ["s8+1.0", ...LEFT(600)], // tokens: the created card and the list
        ["s8.5", 985, 560, 0.9], // MCP: the server table
        ["s8.6", 1925, 720, 0.9], // MCP: the selected server's card
        ["s8.8", 962, 725, 0.85], ["s8.8+3.0", 962, 900, 0.85], // team omp: the settings, then the applied profile
      ],
      spots: [
        spot(20, 648, 1870, 224, "s8.1+1.0", "s8.3"),
        spot(20, 336, 1870, 240, "s8.3", "s8.4"),
        spot(90, 880, 118, 52, "s8.4", "s8.5"),
        spot(20, 400, 1948, 196, "s8.5+0.4", "s8.6"),
        spot(2040, 745, 712, 176, "s8.6", "s8.7"),
        spot(2040, 584, 800, 84, "s8.7", "s8.8"),
        spot(20, 526, 2840, 318, "s8.8+0.3", "s8.8+3.0"),
        spot(20, 848, 2840, 334, "s8.8+3.0"),
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
    key(sec, 100, "s8.8+1.6", "s", "apply to my omp");
    chip(sec, 560, 812, "s8.8+3.0", "symphony omp sync", "blue");
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
