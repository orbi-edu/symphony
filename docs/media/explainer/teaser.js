// A 12 s silent loop for the top of the README. The first and the last frame are the same.
const TEASER = { duration: 12, fps: 15 };

function buildTeaser() {
  Engine.setTiming({ duration: TEASER.duration, scenes: [] });
  const stage = document.getElementById("stage");
  const sec = Engine.h("section", { id: "teaser" });
  const sv = Engine.svg("svg", { class: "wires", viewBox: "0 0 1920 1080" });
  sec.append(sv);
  stage.append(sec);
  Engine.section(sec, -1, null);

  const OFF = 10.8; // everything dims back to the start state
  const lit = (t) => ({ o: [[0, 0.28], [t, 1], [OFF, 0.28]], tdur: 0.6 });
  const big = { fontSize: "60px" };
  const sub = { fontSize: "40px" };

  D.abs(sec, "div", { x: 100, y: 80 }, { class: "big-word", text: "Symphony", style: { fontSize: "84px" } });
  D.abs(sec, "div", { x: 520, y: 112 }, { text: "coding agents on Linear or GitHub tickets", style: { fontSize: "46px", color: "var(--muted)" } });

  const steps = [
    { title: "Ticket", sub: "ENG-1", cls: "ext", t: 0.4 },
    { title: "Agent", sub: "own pod", cls: "ws", t: 2.0 },
    { title: "PR", sub: "#1 ready", cls: "ext", t: 4.4 },
    { title: "Review", sub: "2 models", cls: "merger", t: 5.8 },
    { title: "Merge", sub: "tested head", cls: "good", t: 7.8 },
  ];
  const boxes = steps.map((s, i) => {
    const b = D.box(sec, { x: 100 + i * 355, y: 300, w: 300, h: 250, cls: `center ${s.cls}`, title: s.title, sub: s.sub, style: { justifyContent: "flex-start", paddingTop: "44px" }, ...lit(s.t) });
    b.el.querySelector(".t").style.fontSize = big.fontSize;
    b.el.querySelector(".s").style.fontSize = sub.fontSize;
    return b;
  });
  const runs = [[1.2, 1.9], [3.7, 4.4], [5.1, 5.8], [7.1, 7.8]];
  boxes.slice(0, -1).forEach((b, i) => {
    const w = D.wire(sec, sv, b, boxes[i + 1], { width: 5 });
    D.packet(sv, w, runs[i][0], runs[i][1], i === 2 ? "#b197fc" : "#5cb8ff", 14);
  });

  // The agent works: three bars fill in turn.
  [0, 1, 2].forEach((k) => {
    D.abs(sec, "div", { x: 480, y: 490 + k * 18, w: 250, h: 10, in: 2.3 + k * 0.45, out: OFF, kind: "wipe", dur: 0.5 }, { style: { background: "var(--amber)", borderRadius: "5px" } });
  });
  // Two reviews agree.
  D.abs(sec, "div", { x: 1196, y: 478, in: 6.2, out: OFF, kind: "pop" }, { html: '<span class="chip green" style="font-size:36px">✓</span> <span class="chip green" style="font-size:36px">✓</span>' });

  const states = ["Todo", "In Progress", "In Review", "Merging", "Done"];
  const when = [0.4, 2.0, 4.4, 7.0, 8.8];
  states.forEach((s, i) => {
    D.abs(sec, "div", { x: 100 + i * 355, y: 650, w: 300, h: 90, ...lit(when[i]) }, {
      class: "state", text: s, style: { fontSize: "40px", borderColor: i === 4 ? "var(--green)" : "#3b4a60" },
    });
  });
  D.abs(sec, "div", { x: 0, y: 840, w: 1920, ...lit(8.8) }, {
    text: "People follow, steer and take over from Tern.", style: { textAlign: "center", fontSize: "50px", color: "var(--amber)" },
  });
}
