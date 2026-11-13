// Install and set up: nine scenes. Every time reference points at a narration cue (see ../lib/engine.js `at`).
// The facts come from docs/install.md, docs/cli.md, docs/api.md (Setup), docs/security.md, docs/tern.md,
// src/symphony/api/setup.py, api/setup_checks.py, cli/setup.py, setup_secrets*.py, edge.py and entry/*.py.
// The terminal texts are the formats of cli/setup.py (Wizard, status_text, tracker_text) and cli/render.py.

const SHOT = (name) => `../../tern/${name}-dark.png`;

const card = (parent, o, html, style = {}) => D.abs(parent, "div", o, { class: "card", html, style });
const chip = (parent, x, y, ref, text, cls = "", out = null) =>
  D.abs(parent, "span", { x, y, in: ref, out, kind: "pop" }, { class: `chip ${cls}`, text });
const t = (ref, d) => Engine.at(ref) + d;
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// A terminal whose rows are HTML (colors), each fading in at its ref: rows [[ref, html, out?], ...].
function tty(parent, o, title, rows) {
  const el = D.abs(parent, "div", { x: o.x, y: o.y, w: o.w, in: o.in, out: o.out }, { class: "term" });
  el.append(Engine.h("div", { class: "bar", text: title }));
  for (const [ref, html, out] of rows) {
    const row = Engine.h("div", { class: "ln", html });
    el.append(row);
    Engine.anim(row, { in: ref, out, kind: "fade", lift: 6 });
  }
  return el;
}
const G = (s) => `<span style="color:var(--green)">${s}</span>`;
const Y = (s) => `<span style="color:var(--amber)">${s}</span>`;
const R = (s) => `<span style="color:var(--red)">${s}</span>`;
const M = (s) => `<span style="color:#6f7f94">${s}</span>`;
const B = (s) => `<b>${s}</b>`;
const P = (cmd) => `${G("$ ")}${esc(cmd)}`;

const BANNER = [
  "   ___                 _",
  "  / __|_  _ _ __  _ __| |_  ___ _ _ _  _",
  "  \\__ \\ || | '  \\| '_ \\ ' \\/ _ \\ ' \\ || |",
  "  |___/\\_, |_|_|_| .__/_||_\\___/_||_\\_, |",
  "       |__/      |_|                |__/",
];

// A row of `symphony setup --status` (status_text: names padded to "Notifications (optional)").
function statusRow(mark, color, name, optional, status, summary) {
  const pad = " ".repeat(24 - name.length - (optional ? 11 : 0));
  const n = optional ? `${esc(name)}${M(" (optional)")}` : esc(name);
  const m = { green: G, amber: Y, red: R, dim: M }[color](mark);
  return `  ${m} ${n}${pad}  ${status.padEnd(7)}  ${esc(summary)}`;
}

const SCENES = {
  // 1. Two ways to run
  s1(sec) {
    card(sec, { x: 100, y: 230, w: 820, h: 430, in: "s1.1+0.3" },
      '<h3 style="font-size:40px">Kubernetes</h3><p>A cluster runs the API, the daemon, the mergers and a pod for each ticket.</p>', { borderColor: "#2f74c0" });
    D.abs(sec, "div", { x: 130, y: 420, in: "s1.2" }, { html: '<span class="chip blue">k3s on one VM</span> <span style="font-size:28px;color:var(--muted)">&nbsp;production</span>' });
    D.abs(sec, "div", { x: 130, y: 490, in: "s1.2+1.4" }, { html: '<span class="chip blue">kind</span> <span style="font-size:28px;color:var(--muted)">&nbsp;evaluation, end-to-end tests</span>' });
    D.abs(sec, "div", { x: 130, y: 570, in: "s1.3" }, { html: '<span class="chip green">a pod per ticket</span> <span class="chip green">NetworkPolicy</span>' });

    card(sec, { x: 1000, y: 230, w: 820, h: 430, in: "s1.4" },
      '<h3 style="font-size:40px">Local mode</h3><p>One process runs the API, the gateway, the daemon and the runners. Attempts drive a local <span class="mono">omp</span>.</p>', { borderColor: "var(--amber)", borderStyle: "dashed" });
    D.abs(sec, "div", { x: 1030, y: 470, w: 760, in: "s1.4+0.5" }, { class: "mono", text: "python -m symphony.entry.local --root ~/symphony-local", style: { fontSize: "28px", color: "var(--muted)" } });
    D.abs(sec, "div", { x: 1030, y: 570, in: "s1.4+1.5" }, { html: '<span class="chip red">agent runs as the host user</span> <span class="chip red">no isolation</span>' });

    D.abs(sec, "div", { x: 100, y: 710, w: 1720, h: 90, in: "s1.5", kind: "fade" }, {
      class: "node good center", html: '<div style="font-size:32px;font-weight:680">Same first-run setup: <span class="mono" style="color:var(--green)">symphony setup</span> &nbsp;or&nbsp; Tern › Setup</div>',
    });
  },

  // 2. Minimal install
  s2(sec) {
    tty(sec, { x: 100, y: 205, w: 1000, in: "s2.2" }, "Minimal install on Kubernetes", [
      ["s2.2", P("scripts/build_images.sh")],
      ["s2.2+1.0", P("kubectl apply -k deploy/")],
      ["s2.3", P("cp deploy/secrets.example.yaml deploy/secrets.yaml")],
      ["s2.3+0.6", M("# symphony-api: SYMPHONY_BOOTSTRAP_TOKEN=sym_…")],
      ["s2.3+1.2", M("# model-provider: host model credential (optional)")],
      ["s2.3+1.8", P("kubectl apply -n symphony -f deploy/secrets.yaml")],
      ["s2.4", P("kubectl -n symphony port-forward svc/symphony-api 8080")],
      ["s2.4+1.0", P("symphony login --url http://127.0.0.1:8080 --token -")],
      ["s2.4+2.2", P("symphony setup")],
    ]);

    D.abs(sec, "div", { x: 1160, y: 205, in: "s2.1" }, { text: "Namespace symphony", style: { fontSize: "28px", color: "var(--muted)", fontWeight: "650" } });
    D.box(sec, { x: 1160, y: 250, w: 310, h: 100, cls: "store center", in: "s2.1+0.3", title: "Redis" });
    D.box(sec, { x: 1510, y: 250, w: 310, h: 100, cls: "trusted center", in: "s2.1+0.6", title: "API" });
    ["gateway", "daemon", "merger", "assistant"].forEach((n, i) =>
      D.abs(sec, "div", { x: 1160 + (i % 2) * 350, y: 375 + Math.floor(i / 2) * 78, w: 310, h: 64, in: t("s2.2", 0.6 + i * 0.25), o: [["s2.2", 1], ["s2.3", 0.45]] }, {
        class: "node center", html: `<div class="mono" style="font-size:28px">${n}</div>`, style: { padding: "12px 10px" },
      }));
    D.abs(sec, "div", { x: 1160, y: 540, w: 660, in: "s2.2+2.0" }, {
      html: '<div style="font-size:28px;color:var(--muted);margin-bottom:8px">Empty, for setup to fill:</div><div class="chips" style="margin-top:0;gap:8px"><span class="chip">linear-api-key</span><span class="chip">github-app</span><span class="chip">symphony-slack</span><span class="chip">symphony-discord</span><span class="chip">symphony-providers</span><span class="chip">ConfigMap symphony-workflow</span></div>',
    });
    D.callout(sec, { x: 100, y: 790, in: "s2.5" }, 'Local mode: the process prints an admin token at its first start');
  },

  // 3. Nothing crash-loops
  s3(sec) {
    const rows = [
      ["symphony-daemon", "daemon_setup_incomplete reason=…", "≤ once a minute", "s3.2", "heartbeat · /healthz 200"],
      ["symphony-merger", "merger_not_configured missing=[…]", "≤ once a minute", "s3.3", "/healthz 200"],
      ["symphony-assistant", "assistant_not_configured missing=[…]", "once a minute", "s3.4", "heartbeat state: idle"],
    ];
    rows.forEach(([name, event, rate, ref, health], i) => {
      const y = 215 + i * 125;
      D.abs(sec, "div", { x: 100, y, w: 1720, h: 108, in: ref }, {
        class: "node", style: { padding: "0 26px", justifyContent: "center" },
        html: `<div style="display:flex;align-items:center;gap:26px;font-size:28px"><b class="mono" style="min-width:330px">${name}</b><span class="mono" style="color:var(--amber)">${esc(event)}</span><span style="color:var(--muted)">${rate}</span></div>`,
      });
      D.abs(sec, "span", { x: 1340, y: y + 30, in: t("s3.5", i * 0.35), kind: "pop" }, { class: "chip green", text: health });
    });
    D.abs(sec, "div", { x: 100, y: 610, w: 1720, h: 90, in: "s3.6" }, {
      class: "node", style: { padding: "0 26px", justifyContent: "center" },
      html: '<div style="display:flex;align-items:center;gap:20px;font-size:28px"><b style="min-width:330px">Jobs end with</b><span class="chip">sweep_setup_incomplete</span><span class="chip">runner_setup_incomplete</span><span class="chip">reflector_setup_incomplete</span></div>',
    });
    D.abs(sec, "div", { x: 100, y: 725, w: 1720, in: "s3.2+2.5" }, { html: '<span style="font-size:28px;color:var(--muted)">The overview shows</span> <span class="mono" style="font-size:28px">daemon.setup: <span style="color:var(--amber)">"incomplete"</span></span><span style="font-size:28px;color:var(--muted)">. The daemon re-reads WORKFLOW and the tracker every step.</span>' });
  },

  // 4. symphony setup in the terminal
  s4(sec) {
    const A = D.layer(sec, { in: "s4", out: "s4.4" });
    tty(A.el, { x: 100, y: 200, w: 1720, in: "s4.1" }, "Terminal", [
      ["s4.1", P("symphony setup")],
      ...BANNER.map((line, i) => [t("s4.1", 1.0 + i * 0.12), `<span style="color:var(--blue)">${esc(line)}</span>`]),
      ["s4.1+1.8", "Setup walks you through every step Symphony needs. Each value is verified"],
      ["s4.1+1.8", "against the real service before it is saved."],
      ["s4.2", `■□□□□□□ ${B("Step 1 of 7: GitHub App")}`],
      ["s4.3", G("  ✓ done · Installed on 1 repository: acme/app")],
      ["s4.3+0.9", "  Change it? [y/N] "],
    ]);

    const W = D.layer(sec, { in: "s4.4", out: "s4.6" });
    tty(W.el, { x: 100, y: 200, w: 1720, in: "s4.4" }, "Terminal", [
      ["s4.4", `■■□□□□□ ${B("Step 2 of 7: Tracker")}`],
      ["s4.4+0.2", "  Tracker [LINEAR/github] linear"],
      ["s4.4+0.4", "  Linear team key (leave empty to use a project instead): OPS"],
      ["s4.4+0.7", `  Linear API key (hidden): ${M("▍")}`],
      ["s4.5", Y("⠹ Verifying the tracker…"), "s4.5+1.2"],
      ["s4.5+1.2", R("✗ Linear has no team with this key. Details: Linear has no team with key 'OPS'")],
      ["s4.5+2.4", "  Try this step again? [Y/n] "],
    ]);
    D.abs(W.el, "div", { x: 100, y: 610, in: "s4.4+0.9" }, { html: '<span class="chip amber">hidden prompt: nothing is echoed</span> &nbsp;<span class="chip">the App private key: a path to the .pem file</span>' });

    const S = D.layer(sec, { in: "s4.6" });
    tty(S.el, { x: 100, y: 200, w: 1720, in: "s4.6" }, "One step at a time", [
      ["s4.6", P('printf \'%s\\n\' "$LINEAR_API_KEY" | symphony setup tracker --kind linear --team ENG')],
      ["s4.6+1.2", G("Tracker saved: Linear.")],
      ["s4.6+1.4", "States on the board: Todo, In Progress, Done."],
      ["s4.6+1.6", "Restarted to apply it: symphony-daemon, symphony-merger, symphony-api."],
      ["s4.7", P("symphony setup --status")],
      ["s4.7+0.6", `${Y(B("Symphony setup is not complete: 2 required steps left."))} (mode: kubernetes)`],
      ["s4.7+0.8", statusRow("✓", "green", "Tracker", false, "done", "Linear, team ENG")],
      ["s4.7+1.0", statusRow("✓", "green", "GitHub App", false, "done", "Installed on 1 repository: acme/app")],
      ["s4.7+1.2", statusRow("○", "amber", "Repositories", false, "to do", "No default repository and no ticket names one.")],
      ["s4.7+1.4", statusRow("✗", "red", "Models", false, "error", "No credential for provider anthropic")],
      ["s4.7+1.6", statusRow("–", "dim", "Notifications", true, "skipped", "No channel is configured.")],
    ]);
    D.abs(S.el, "div", { x: 1080, y: 400, in: "s4.6+2.4", out: "s4.7" }, { html: '<span class="chip amber">secrets: stdin or a hidden prompt · never in argv</span>' });
  },

  // 5. The Setup view in Tern
  s5(sec) {
    // The page: a short overview, then the parts the narration names, at a readable scale.
    const W = D.layer(sec, { in: "s5", out: "s5.4" });
    D.shot(W.el, {
      x: 100, y: 200, w: 1720, h: 580, in: "s5.1",
      shots: [
        { src: SHOT("setup-tracker"), in: "s5.1", out: "s5.2" },
        { src: SHOT("setup-banner"), in: "s5.2", out: "s5.2+2.0" },
        { src: SHOT("setup-tracker"), in: "s5.2+2.0" },
      ],
      cam: [["s5.1", 1440, 650, 0.59], ["s5.1+1.0", 990, 450, 0.9], ["s5.2", 1000, 330, 0.9], ["s5.2+2.0", 990, 450, 0.9],
        ["s5.3+1.3", 1720, 390, 0.75], ["s5.3+2.6", 1925, 560, 0.9]],
      spots: [
        { x: 28, y: 266, w: 1960, h: 118, in: "s5.2+0.4", out: "s5.2+2.0" },
        { x: 28, y: 290, w: 1928, h: 316, in: "s5.2+2.4", out: "s5.3+1.3" },
        { x: 26, y: 176, w: 2830, h: 46, in: "s5.3+1.5", out: "s5.3+2.6" },
        { x: 1972, y: 242, w: 884, h: 1210, in: "s5.3+2.8" },
      ],
    });
    D.swap(W.el, 100, 800, [
      ["s5.1+0.3", "Tern › Symphony: Setup", "blue"],
      ["s5.2", "everyone else: a banner on the Dashboard", "amber"],
      ["s5.2+2.0", "an admin: Setup opens by itself", "green"],
    ]);

    // The selected step's card, beside what it shows.
    const C = D.layer(sec, { in: "s5.4" });
    D.shot(C.el, {
      x: 920, y: 200, w: 900, h: 600, in: "s5.4",
      shots: [
        { src: SHOT("setup-verifying"), in: "s5.4", out: "s5.5" },
        { src: SHOT("setup-error"), in: "s5.5", out: "s5.6" },
        { src: SHOT("setup-github-app"), in: "s5.6" },
      ],
      cam: [["s5.4", 2413, 600, 0.95], ["s5.5", 2413, 800, 0.95], ["s5.6", 2413, 880, 0.95]],
      spots: [
        { x: 2040, y: 798, w: 800, h: 92, in: "s5.4+0.4", out: "s5.5" },
        { x: 2040, y: 996, w: 800, h: 114, in: "s5.5+0.4", out: "s5.6" },
        { x: 2040, y: 970, w: 800, h: 200, in: "s5.6+0.4" },
      ],
    });
    card(C.el, { x: 100, y: 220, w: 780, h: 300, in: "s5.4", out: "s5.5" },
      '<h3>Masked</h3><p>The private key field is hidden and write-only.</p><p style="margin-top:14px"><b>Verify and save</b> runs <span class="mono">symphony setup &lt;step&gt; … --json</span>. The secret goes on standard input.</p>');
    card(C.el, { x: 100, y: 220, w: 780, h: 300, in: "s5.5", out: "s5.6" },
      '<h3 style="color:var(--red)">Not saved</h3><p>The card shows the reason from the API:</p><p class="mono" style="margin-top:14px;color:var(--amber)">Linear has no team with the key OPS.</p>', { borderColor: "var(--red)" });
    card(C.el, { x: 100, y: 220, w: 780, h: 300, in: "s5.6" },
      '<h3 style="color:var(--green)">Verified and saved</h3><p>What the API found: the repositories of the App installation.</p><p style="margin-top:14px">And the Deployments it restarted to apply the value.</p>', { borderColor: "var(--green)" });
  },

  // 6. The seven steps
  s6(sec) {
    const row = (parent, y, ref, name, required, sub) => D.abs(parent, "div", { x: 100, y, w: 860, h: 118, in: ref }, {
      class: `node ${required ? "trusted" : ""}`, style: { padding: "14px 24px" },
      html: `<div style="display:flex;align-items:center;gap:16px"><span style="font-size:32px;font-weight:700">${name}</span><span class="chip ${required ? "blue" : ""}" style="font-size:26px">${required ? "required" : "optional"}</span></div><div style="font-size:28px;color:#c8d3e1;margin-top:8px">${sub}</div>`,
    });
    const panel = { x: 1000, y: 200, w: 820, h: 600 };
    const panelCard = (parent, ref, out, html) => card(parent, { ...panel, in: ref, out }, html);
    const panelShot = (parent, src, ref, out, fy, spots = []) => D.shot(parent, {
      ...panel, in: ref, out, shots: [{ src: SHOT(src), in: ref }], cam: [[ref, 2413, fy, 0.92]], spots,
    });

    const A = D.layer(sec, { in: "s6", out: "s6.8" });
    D.abs(A.el, "div", { x: 100, y: 200, in: "s6.2" }, { text: "Required", style: { fontSize: "30px", fontWeight: "700", color: "var(--blue)" } });
    row(A.el, 250, "s6.3", "Tracker", true, "Linear key + team, or GitHub board + status field");
    row(A.el, 380, "s6.4", "GitHub App", true, "Mints a token, lists repos, reports missing permissions");
    row(A.el, 510, "s6.5", "Repositories", true, "The App is installed on the default repository");
    D.abs(A.el, "span", { x: 560, y: 518, in: "s6.6", kind: "pop" }, { class: "chip amber", text: "+ a running merger each", style: { fontSize: "26px" } });
    row(A.el, 640, "s6.7", "Models", true, "Each role has a stored key or a host credential");
    panelCard(A.el, "s6.1", "s6.3",
      '<h3>Verify, then store</h3><p>The value goes to the API.</p><p style="margin-top:14px">The API calls the real service: Linear, GitHub, Slack or Discord.</p><p style="margin-top:14px"><span class="tick">✓</span> it works: the API stores it</p><p style="margin-top:8px"><span class="cross">✗</span> it fails: nothing is stored</p><p style="margin-top:14px;color:var(--muted)">Only an admin can save. Any token can read the checklist.</p>');
    panelShot(A.el, "setup-tracker", "s6.3", "s6.4", 560, [{ x: 2040, y: 490, w: 800, h: 140, in: "s6.3+0.6" }]);
    panelShot(A.el, "setup-github-app", "s6.4", "s6.5", 880, [{ x: 2040, y: 1020, w: 800, h: 150, in: "s6.4+0.6" }]);
    panelCard(A.el, "s6.5", "s6.6",
      '<h3>Repositories</h3><p class="mono" style="color:var(--muted)">default_repo: acme/app</p><p style="margin-top:12px">Not installed: <span class="mono" style="color:var(--red)">repo_not_installed</span></p><p style="margin-top:12px">Without a default, each ticket names its repository with a <span class="mono">repo:</span> label.</p><p style="margin-top:12px;color:var(--amber)">Kubernetes: a repository without a beating merger gives <span class="mono">merger_missing</span>.</p>');
    panelShot(A.el, "setup-ready", "s6.6", "s6.7", 880, [{ x: 2040, y: 828, w: 800, h: 60, in: "s6.6+0.6" }]);
    panelCard(A.el, "s6.7", null,
      '<h3>Models</h3><p>Each role of the team configuration needs a credential:</p><p style="margin-top:12px"><span class="chip green">stored</span> a key saved through the API, in <span class="mono">symphony-providers</span></p><p style="margin-top:12px"><span class="chip blue">host</span> a key in the Secret <span class="mono">model-provider</span></p><p style="margin-top:12px"><span class="chip red">none</span> <span class="mono">model_credential_missing</span></p>');

    const O = D.layer(sec, { in: "s6.8" });
    D.abs(O.el, "div", { x: 100, y: 200, in: "s6.8" }, { text: "Optional", style: { fontSize: "30px", fontWeight: "700", color: "var(--muted)" } });
    row(O.el, 250, "s6.9", "Notifications", false, "A test message reaches each new channel");
    row(O.el, 380, "s6.10", "Assistant", false, "Slack tokens · Discord Message Content Intent");
    row(O.el, 510, "s6.11", "Webhooks", false, "One signed delivery from each source in use");
    panelCard(O.el, "s6.8", "s6.9", '<h3>Optional</h3><p>A step that is not set up shows <span class="mono">skipped</span> or <span class="mono">to do</span>.</p><p style="margin-top:14px">It does not block setup.</p>');
    panelCard(O.el, "s6.9", "s6.10",
      '<h3>Test message</h3><p class="mono" style="color:var(--amber)">Symphony is connected: this is the setup test message.</p><p style="margin-top:14px">A bot token and a channel, or a webhook URL.</p><p style="margin-top:14px">If the message fails, nothing is stored: <span class="mono">notification_unverified</span>.</p>');
    panelShot(O.el, "setup-assistant", "s6.10", "s6.11", 900, [{ x: 2040, y: 838, w: 800, h: 80, in: "s6.10+0.6" }]);
    panelShot(O.el, "setup-webhooks", "s6.11", null, 880, [{ x: 2040, y: 806, w: 800, h: 84, in: "s6.11+0.6" }]);
    D.abs(O.el, "div", { x: 100, y: 660, w: 860, h: 110, in: "s6.12", kind: "fade" }, {
      class: "node good center", html: '<div style="font-size:30px"><span class="mono">complete</span> = every required step is <span class="mono" style="color:var(--green)">done</span></div>',
    });
  },

  // 7. Secrets
  s7(sec) {
    const A = D.layer(sec, { in: "s7", out: "s7.5" });
    card(A.el, { x: 100, y: 215, w: 820, h: 150, in: "s7.1" },
      '<h3 style="font-size:38px"><span class="lock">●</span> Write-only</h3><p>The checklist shows only whether a value is set.</p>');
    D.abs(A.el, "div", { x: 100, y: 395, w: 820, in: "s7.1+1.6" }, { html: '<div class="chips" style="margin-top:0"><span class="chip red">✗ API responses</span><span class="chip red">✗ logs</span><span class="chip red">✗ audit entries</span><span class="chip red">✗ command lines</span></div>' });
    card(A.el, { x: 100, y: 560, w: 820, h: 160, in: "s7.4" }, '<h3>Redis</h3><p>No integration credential. The 30 s check cache stays in process memory.</p>');
    card(A.el, { x: 1000, y: 215, w: 820, h: 300, in: "s7.2" },
      '<h3>Kubernetes</h3><p>A merge patch of named Secrets. The manifests made them empty. The API may only get and patch them.</p><div class="chips"><span class="chip">linear-api-key</span><span class="chip">github-app</span><span class="chip">symphony-api</span><span class="chip">symphony-slack</span><span class="chip">symphony-discord</span></div>');
    card(A.el, { x: 1000, y: 545, w: 820, h: 135, in: "s7.3" },
      '<h3>Local mode</h3><p><span class="mono" style="color:var(--amber)">&lt;root&gt;/secrets.env</span> · mode <span class="mono">0600</span></p>');

    const W = D.layer(sec, { in: "s7.5" });
    D.shot(W.el, {
      x: 920, y: 200, w: 900, h: 600, in: "s7.5",
      shots: [{ src: SHOT("setup-webhooks"), in: "s7.5" }],
      cam: [["s7.5", 2413, 900, 0.95], ["s7.5+1.3", 2413, 1180, 0.95], ["s7.6+0.4", 2413, 900, 0.95]],
      spots: [
        { x: 2040, y: 886, w: 800, h: 56, in: "s7.5+0.3", out: "s7.5+1.3" },
        { x: 2040, y: 1090, w: 800, h: 84, in: "s7.5+1.8", out: "s7.6+0.4" },
        { x: 2040, y: 1238, w: 800, h: 156, in: "s7.5+1.8", out: "s7.6+0.4" },
        { x: 2566, y: 660, w: 290, h: 50, in: "s7.6+1.2" },
      ],
    });
    card(W.el, { x: 100, y: 220, w: 780, h: 290, in: "s7.5" },
      '<h3 style="color:var(--amber)">Shown once</h3><p>Setup makes a Linear and a GitHub secret, and shows each with its URL. GitHub also gets the eight events to subscribe.</p><p class="mono" style="margin-top:14px;font-size:26px;color:var(--muted)">POST /v1/setup/webhooks · Cache-Control: no-store</p>', { borderColor: "var(--amber)" });
    card(W.el, { x: 100, y: 540, w: 780, h: 200, in: "s7.6" },
      '<h3>Rotate</h3><p class="mono" style="color:var(--blue)">symphony setup webhooks --rotate</p><p style="margin-top:10px">The old secrets stop working.</p>');
  },

  // 8. Restarts and the public URL
  s8(sec) {
    const A = D.layer(sec, { in: "s8", out: "s8.4" });
    const api = D.box(A.el, { x: 100, y: 220, w: 400, h: 130, cls: "trusted", in: "s8.1", title: "Setup API", sub: "verifies, then stores" });
    const secret = D.box(A.el, { x: 620, y: 220, w: 400, h: 130, cls: "store", in: "s8.1+0.6", title: "Secret", sub: "merge patch" });
    D.wire(A.el, A.sv, api, secret, { in: "s8.1+0.6" });
    const names = ["symphony-daemon", "symphony-merger*", "symphony-assistant", "symphony-api"];
    const subs = ["first", "each merger", "Slack, Discord", "itself, last"];
    const chain = names.map((n, i) => D.box(A.el, { x: 100 + i * 435, y: 440, w: 390, h: 130, cls: i === 3 ? "trusted" : i === 1 ? "merger" : "", in: t("s8.2", i * 0.7), kick: String(i + 1), title: `<span class="mono" style="font-size:28px">${n}</span>`, sub: subs[i] }));
    D.wire(A.el, A.sv, secret, chain[0], { from: "b", to: "t", in: "s8.1+1.6", label: "restart", lo: [130, 0] });
    chain.slice(0, -1).forEach((b, i) => D.wire(A.el, A.sv, b, chain[i + 1], { in: t("s8.2", 0.7 * (i + 1)), color: "#5b6b82" }));
    D.abs(A.el, "div", { x: 100, y: 610, in: "s8.2+2.8" }, { class: "mono", html: '<span style="font-size:28px">"restarted": ["symphony-daemon", "symphony-merger", "symphony-api"]</span>' });
    D.abs(A.el, "div", { x: 100, y: 680, in: "s8.2+3.4" }, { text: "Runner and reflector Jobs read the Secrets when they start. They need no restart.", style: { fontSize: "28px", color: "var(--muted)" } });
    chip(A.el, 100, 750, "s8.3", "local mode: \"restarted\": [] · the change applies at once", "green");

    const U = D.layer(sec, { in: "s8.4" });
    card(U.el, { x: 100, y: 215, w: 1720, h: 140, in: "s8.4" },
      '<h3 class="mono" style="color:var(--amber)">SYMPHONY_PUBLIC_URL</h3><p>The address people and vendors reach the API at. ConfigMap <span class="mono">symphony-api-config</span>. No default.</p>');
    D.abs(U.el, "div", { x: 100, y: 385, w: 1720, h: 76, in: "s8.5" }, {
      class: "node good", style: { justifyContent: "center", padding: "0 24px" },
      html: '<div style="font-size:28px"><b>set</b> &nbsp;<span class="mono">https://symphony.example.com/v1/webhooks/github</span></div>',
    });
    D.abs(U.el, "div", { x: 100, y: 475, w: 1720, h: 76, in: "s8.5+2.4" }, {
      class: "node", style: { justifyContent: "center", padding: "0 24px" },
      html: '<div style="font-size:28px"><b>unset</b> &nbsp;the request\'s address: <span class="mono">http://127.0.0.1:8080/v1/webhooks/github</span></div>',
    });
    tty(U.el, { x: 100, y: 575, w: 1720, in: "s8.4+2.0" }, "Behind a proxy or a tunnel: set it, then restart the API", [
      ["s8.4+2.0", P("kubectl -n symphony patch configmap symphony-api-config --type merge -p")],
      ["s8.4+2.0", esc(`    '{"data":{"SYMPHONY_PUBLIC_URL":"https://symphony.example.com"}}'`)],
      ["s8.5+3.4", P("kubectl -n symphony rollout restart deploy/symphony-api")],
    ]);
  },

  // 9. Ready
  s9(sec) {
    const A = D.layer(sec, { in: "s9", out: "s9.4" });
    D.shot(A.el, {
      x: 100, y: 200, w: 1720, h: 580, in: "s9.1",
      shots: [{ src: SHOT("setup-ready"), in: "s9.1" }],
      cam: [["s9.1", 1440, 640, 0.59], ["s9.1+1.0", 1200, 420, 0.75], ["s9.2", 910, 430, 0.95]],
      spots: [{ x: 26, y: 244, w: 2830, h: 338, in: "s9.1+0.6", out: "s9.2" }, { x: 90, y: 400, w: 1290, h: 50, in: "s9.2+0.6" }],
    });
    D.swap(A.el, 100, 800, [
      ["s9.1+0.3", "Symphony is ready", "green"],
      ["s9.2", "first ticket → an active state on the board", "blue"],
      ["s9.3", "symphony status · Tern › Dashboard", "blue"],
    ]);

    const B = D.layer(sec, { in: "s9.4" });
    D.abs(B.el, "div", { x: 100, y: 300, w: 1720, h: 240, in: "s9.4", kind: "fade" }, {
      class: "node trusted center", style: { justifyContent: "center" },
      html: '<div style="font-size:34px;color:var(--muted)">The full guide</div><div class="mono" style="font-size:64px;font-weight:700;margin-top:10px;color:var(--blue)">docs/install.md</div>',
    });
    D.abs(B.el, "div", { x: 100, y: 580, w: 1720, in: "s9.4+0.8" }, { html: '<div style="text-align:center;font-size:30px;color:var(--muted)">Commands: <span class="mono">docs/cli.md</span> · Routes: <span class="mono">docs/api.md</span> · Credentials: <span class="mono">docs/security.md</span></div>' });
  },
};
