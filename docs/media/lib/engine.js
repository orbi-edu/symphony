// A tiny deterministic animation engine. Engine.seek(t) puts every element in its state at time t.
// Nothing depends on the wall clock, so each captured frame is exact and the render is reproducible.
const Engine = (() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  const anims = [];
  const sections = [];
  let timing = { duration: 12, scenes: [] };
  let captionEl = null;
  let progress = [];
  let lastCaption = null;

  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const easeOut = (p) => 1 - Math.pow(1 - p, 3);
  const easeInOut = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
  const lerp = (a, b, p) => a + (b - a) * p;

  function setTiming(t) { timing = t; }

  // Time references: a number, "s2" (scene start), "s2e" (scene end), "s2.3" (cue start),
  // "s2.3e" (cue end), each with an optional "+1.5" or "-0.4".
  function at(ref) {
    if (ref == null) return null;
    if (typeof ref === "number") return ref;
    const m = /^(s\d+)(?:\.(\d+))?(e?)([+-]\d*\.?\d+)?$/.exec(ref);
    if (!m) throw new Error(`bad time ref ${ref}`);
    const scene = timing.scenes.find((s) => s.id === m[1]);
    if (!scene) throw new Error(`no scene ${m[1]}`);
    let t;
    if (m[2]) {
      const cue = scene.cues[Number(m[2]) - 1];
      if (!cue) throw new Error(`no cue ${ref}`);
      t = m[3] ? cue.end : cue.start;
    } else {
      t = m[3] ? scene.end : scene.start;
    }
    return t + (m[4] ? parseFloat(m[4]) : 0);
  }

  function h(tag, props = {}, children = []) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "style") Object.assign(el.style, v);
      else if (k === "html") el.innerHTML = v;
      else if (k === "text") el.textContent = v;
      else if (k === "class") el.className = v;
      else el.setAttribute(k, v);
    }
    for (const c of [].concat(children)) if (c) el.append(c);
    return el;
  }

  function svg(tag, attrs = {}) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  // Register an element. opts: in, out (time refs), kind ("fade" | "pop" | "none" | "draw" | "wipe" | "type"),
  // dur, dx/dy/s/o tracks ([[ref, value], ...]: offset, scale, opacity), cam ([[ref, fx, fy, scale], ...]), tdur (track duration).
  function anim(el, opts = {}) {
    const a = {
      el,
      tin: opts.in, tout: opts.out, kind: opts.kind || "fade", dur: opts.dur ?? 0.5,
      tracks: {}, tdur: opts.tdur ?? 0.8, cam: opts.cam, vw: opts.vw, vh: opts.vh,
      text: opts.kind === "type" ? el.textContent : null, along: opts.along, t0: opts.t0, t1: opts.t1,
      lift: opts.lift ?? 18,
    };
    for (const [p, k] of [["x", "dx"], ["y", "dy"], ["s", "s"], ["o", "o"]]) if (opts[k]) a.tracks[p] = opts[k];
    anims.push(a);
    return a;
  }

  function resolve() {
    for (const a of anims) {
      a.tin = at(a.tin);
      a.tout = at(a.tout);
      a.t0 = at(a.t0);
      a.t1 = at(a.t1);
      for (const k of Object.keys(a.tracks)) a.tracks[k] = a.tracks[k].map(([r, v]) => [at(r), v]).sort((p, q) => p[0] - q[0]);
      if (a.cam) a.cam = a.cam.map(([r, ...v]) => [at(r), ...v]).sort((p, q) => p[0] - q[0]);
      if (a.kind === "draw" || a.along) {
        const path = a.along || a.el;
        a.len = path.getTotalLength();
        if (a.kind === "draw") a.el.style.strokeDasharray = `${a.len} ${a.len}`;
      }
    }
    for (const s of sections) { s.tin = at(s.tin); s.tout = at(s.tout); }
  }

  function trackAt(keys, t, dur) {
    if (!keys.length) return null;
    if (t <= keys[0][0]) return keys[0][1];
    let i = 0;
    while (i + 1 < keys.length && keys[i + 1][0] <= t) i++;
    if (i === 0) return keys[0][1];
    const p = easeInOut(clamp((t - keys[i][0]) / dur));
    return lerp(keys[i - 1][1], keys[i][1], p);
  }

  function camAt(keys, t, dur) {
    if (t <= keys[0][0]) return keys[0].slice(1);
    let i = 0;
    while (i + 1 < keys.length && keys[i + 1][0] <= t) i++;
    if (i === 0) return keys[0].slice(1);
    const p = easeInOut(clamp((t - keys[i][0]) / dur));
    const [ax, ay, as] = keys[i - 1].slice(1);
    const [bx, by, bs] = keys[i].slice(1);
    // Interpolate the scale in log space so that zooms feel even.
    return [lerp(ax, bx, p), lerp(ay, by, p), Math.exp(lerp(Math.log(as), Math.log(bs), p))];
  }

  function render(a, t) {
    const pin = a.tin == null ? 1 : clamp((t - a.tin) / a.dur);
    const pout = a.tout == null ? 1 : 1 - clamp((t - a.tout) / 0.35);
    const ein = easeOut(pin);
    let vis = (a.kind === "draw" || a.kind === "type" || a.kind === "wipe" ? (pin > 0 ? 1 : 0) : ein) * easeOut(pout);
    if (a.along) {
      const span = a.t1 - a.t0;
      const p = clamp((t - a.t0) / span);
      vis = t < a.t0 || t > a.t1 + 0.25 ? 0 : clamp((t - a.t0) / 0.15) * (1 - clamp((t - a.t1) / 0.25));
      const pt = a.along.getPointAtLength(a.len * easeInOut(p));
      a.el.setAttribute("cx", pt.x);
      a.el.setAttribute("cy", pt.y);
      a.el.style.opacity = vis;
      return;
    }
    const o = a.tracks.o ? trackAt(a.tracks.o, t, a.tdur) : 1;
    a.el.style.opacity = vis * o;
    a.el.style.visibility = vis * o <= 0.001 ? "hidden" : "visible";
    if (a.kind === "draw") {
      a.el.style.strokeDashoffset = a.len * (1 - easeInOut(pin));
      if (a.marker) a.el.setAttribute("marker-end", pin > 0.96 ? a.marker : "");
      if (a.markerStart) a.el.setAttribute("marker-start", pin > 0.96 ? a.markerStart : "");
      return;
    }
    if (a.kind === "wipe") {
      a.el.style.clipPath = `inset(0 ${100 - 100 * easeInOut(pin)}% 0 0)`;
    }
    if (a.kind === "type") {
      const n = Math.round(a.text.length * pin);
      const s = a.text.slice(0, n);
      if (a.el.textContent !== s) a.el.textContent = s;
    }
    if (a.cam) {
      const [fx, fy, s] = camAt(a.cam, t, a.tdur);
      a.el.style.transform = `translate(${a.vw / 2 - fx * s}px, ${a.vh / 2 - fy * s}px) scale(${s})`;
      return;
    }
    const x = a.tracks.x ? trackAt(a.tracks.x, t, a.tdur) : 0;
    const y = (a.tracks.y ? trackAt(a.tracks.y, t, a.tdur) : 0) + (a.kind === "fade" ? (1 - ein) * a.lift : 0);
    let s = a.tracks.s ? trackAt(a.tracks.s, t, a.tdur) : 1;
    if (a.kind === "pop") s *= 0.9 + 0.1 * ein;
    if (x || y || s !== 1) a.el.style.transform = `translate(${x}px, ${y}px) scale(${s})`;
    else a.el.style.transform = "";
  }

  function section(el, tin, tout) {
    sections.push({ el, tin, tout });
    return el;
  }

  function mountChrome(stage, withCaptions) {
    if (withCaptions) {
      const bar = h("div", { id: "progress" });
      progress = timing.scenes.map((s) => {
        const fill = h("div", { class: "fill" });
        bar.append(h("div", { class: "seg" }, fill));
        return { s, fill };
      });
      stage.append(bar);
      captionEl = h("div", { id: "caption" });
      stage.append(captionEl);
    }
  }

  function caption(t) {
    if (!captionEl) return;
    let cur = null;
    for (const s of timing.scenes) {
      s.cues.forEach((c, i) => {
        const next = s.cues[i + 1];
        const end = next ? next.start : Math.min(c.end + 0.6, s.end);
        if (t >= c.start && t < end) cur = c;
      });
    }
    if (!cur) { captionEl.style.opacity = 0; return; }
    if (lastCaption !== cur.id) {
      captionEl.textContent = cur.text;
      captionEl.classList.remove("wrap");
      if (captionEl.scrollWidth > 1640) captionEl.classList.add("wrap");
      lastCaption = cur.id;
    }
    captionEl.style.opacity = easeOut(clamp((t - cur.start + 0.08) / 0.18));
  }

  function seek(t) {
    // A scene fades out in the 0.3 s before its end; the next one fades in during its first 0.3 s.
    for (const s of sections) {
      const pin = s.tin == null ? 1 : easeOut(clamp((t - s.tin) / 0.3));
      const pout = s.tout == null ? 1 : 1 - easeOut(clamp((t - s.tout + 0.3) / 0.3));
      const v = Math.min(pin, pout);
      s.el.style.opacity = v;
      s.el.style.display = v <= 0 ? "none" : "block";
    }
    for (const a of anims) {
      const sec = a.el.closest("section");
      if (sec && sec.style.display === "none") continue;
      render(a, t);
    }
    for (const { s, fill } of progress) fill.style.width = `${100 * clamp((t - s.start) / (s.end - s.start))}%`;
    caption(t);
  }

  async function ready() {
    resolve();
    await document.fonts.ready;
    await Promise.all([...document.images].map((img) => img.decode().catch(() => { throw new Error(`image ${img.src} did not load`); })));
    seek(0);
    return { duration: timing.duration };
  }

  return { at, h, svg, anim, section, mountChrome, seek, ready, setTiming, clamp, get timing() { return timing; } };
})();

// ---------- Diagram helpers shared by the scenes and the teaser ----------

const D = (() => {
  const { h, svg, anim } = Engine;
  let markerSeq = 0;

  function header(sec, scene, n, total) {
    const k = h("div", { class: "kicker", text: `${String(n).padStart(2, "0")} / ${String(total).padStart(2, "0")} · ${scene.kicker}` });
    const t = h("div", { class: "title", text: scene.title });
    sec.append(k, t);
    return { k, t };
  }

  // A box with a title, an optional subtitle and optional extra content.
  function box(parent, o) {
    const kids = [];
    if (o.kick) kids.push(h("div", { class: "k", text: o.kick }));
    if (o.title) kids.push(h("div", { class: "t", html: o.title }));
    if (o.sub) kids.push(h("div", { class: "s", html: o.sub }));
    if (o.body) kids.push(o.body);
    const el = h("div", { class: `node ${o.cls || ""}`, style: { left: `${o.x}px`, top: `${o.y}px`, width: `${o.w}px`, height: `${o.h}px`, ...(o.style || {}) } }, kids);
    parent.append(el);
    const a = anim(el, o);
    return { el, a, x: o.x, y: o.y, w: o.w, h: o.h };
  }

  function point(b, side, f = 0.5) {
    if (Array.isArray(b)) return b;
    switch (side) {
      case "l": return [b.x, b.y + b.h * f];
      case "r": return [b.x + b.w, b.y + b.h * f];
      case "t": return [b.x + b.w * f, b.y];
      case "b": return [b.x + b.w * f, b.y + b.h];
      default: throw new Error(side);
    }
  }
  const normal = { l: [-1, 0], r: [1, 0], t: [0, -1], b: [0, 1] };

  function defsFor(svgEl) {
    let defs = svgEl.querySelector("defs");
    if (!defs) { defs = svg("defs"); svgEl.prepend(defs); }
    return defs;
  }

  function marker(svgEl, color) {
    const id = `m${++markerSeq}`;
    const m = svg("marker", { id, viewBox: "0 0 10 10", refX: "8", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse", markerUnits: "strokeWidth" });
    m.append(svg("path", { d: "M0,0 L10,5 L0,10 z", fill: color }));
    defsFor(svgEl).append(m);
    return `url(#${id})`;
  }

  // A connector between two boxes (or points). Returns the path for packets.
  function wire(sec, svgEl, A, B, o = {}) {
    const p0 = point(A, o.from || "r", o.ff ?? 0.5);
    const p3 = point(B, o.to || "l", o.tf ?? 0.5);
    const dist = Math.hypot(p3[0] - p0[0], p3[1] - p0[1]);
    const d = o.bend ?? Math.max(30, Math.min(160, dist * 0.4));
    const n0 = Array.isArray(A) ? [0, 0] : normal[o.from || "r"];
    const n3 = Array.isArray(B) ? [0, 0] : normal[o.to || "l"];
    const p1 = [p0[0] + n0[0] * d, p0[1] + n0[1] * d];
    const p2 = [p3[0] + n3[0] * d, p3[1] + n3[1] * d];
    const color = o.color || "#5b6b82";
    const path = svg("path", {
      d: o.straight ? `M${p0} L${p3}` : `M${p0} C${p1} ${p2} ${p3}`,
      fill: "none", stroke: color, "stroke-width": o.width || 3, "stroke-linecap": "round",
    });
    if (o.dash) path.setAttribute("stroke-dasharray", "10 10");
    svgEl.append(path);
    const a = anim(path, { in: o.in, out: o.out, kind: o.dash ? "fade" : "draw", dur: o.dur ?? 0.7, o: o.o, lift: 0 });
    if (!o.noArrow) a.marker = marker(svgEl, color);
    if (o.bi) a.markerStart = marker(svgEl, color);
    if (o.dash) {
      if (!o.noArrow) path.setAttribute("marker-end", a.marker);
      if (o.bi) path.setAttribute("marker-start", a.markerStart);
    }
    if (o.label) {
      const m = o.straight
        ? [(p0[0] + p3[0]) / 2, (p0[1] + p3[1]) / 2]
        : [(p0[0] + 3 * p1[0] + 3 * p2[0] + p3[0]) / 8, (p0[1] + 3 * p1[1] + 3 * p2[1] + p3[1]) / 8];
      const lo = o.lo || [0, 0];
      const lab = h("div", { class: "wl", html: o.label, style: { left: `${m[0] + lo[0]}px`, top: `${m[1] + lo[1]}px`, ...(o.lstyle || {}) } });
      sec.append(lab);
      // The label appears a little after the line starts.
      anim(lab, { in: o.in == null ? null : Engine.at(o.in) + 0.35, out: o.out, dur: 0.5, o: o.o, lift: 8 });
    }
    return path;
  }

  // A glowing dot that runs along a path between t0 and t1.
  function packet(svgEl, path, t0, t1, color = "#5cb8ff", r = 10) {
    const g = svg("circle", { r, fill: color, style: `filter: drop-shadow(0 0 10px ${color})` });
    svgEl.append(g);
    anim(g, { along: path, t0, t1, kind: "none" });
    return g;
  }

  function chip(text, cls = "") { return h("span", { class: `chip ${cls}`, text }); }
  function chips(list) { return h("div", { class: "chips" }, list.map(([t, c]) => chip(t, c))); }

  function abs(parent, tag, o, props = {}) {
    const el = h(tag, { ...props, style: { left: `${o.x}px`, top: `${o.y}px`, ...(o.w ? { width: `${o.w}px` } : {}), ...(o.h ? { height: `${o.h}px` } : {}), ...(props.style || {}) } });
    el.classList.add("abs");
    parent.append(el);
    anim(el, o);
    return el;
  }

  // A screenshot viewport with a camera. shots: [{src, in, out}], cam: [[ref, fx, fy, s]].
  function shot(parent, o) {
    const vp = h("div", { class: "shot", style: { left: `${o.x}px`, top: `${o.y}px`, width: `${o.w}px`, height: `${o.h}px` } });
    const cam = h("div", { class: "cam" });
    vp.append(cam);
    for (const s of o.shots) {
      const img = h("img", { src: s.src, alt: "" });
      cam.append(img);
      anim(img, { in: s.in, out: s.out, kind: "fade", dur: 0.5, lift: 0 });
    }
    for (const s of o.spots || []) {
      const sp = h("div", { class: "spot", style: { left: `${s.x}px`, top: `${s.y}px`, width: `${s.w}px`, height: `${s.h}px` } });
      cam.append(sp);
      anim(sp, { in: s.in, out: s.out, kind: "fade", dur: 0.4, lift: 0 });
    }
    parent.append(vp);
    anim(vp, { in: o.in, out: o.out, kind: "fade", lift: 24 });
    anim(cam, { kind: "none", cam: o.cam, vw: o.w, vh: o.h, tdur: o.tdur ?? 1.1 });
    return vp;
  }

  function callout(parent, o, html) {
    const el = h("div", { class: "callout", html, style: { left: `${o.x}px`, top: `${o.y}px`, ...(o.style || {}) } });
    parent.append(el);
    anim(el, { in: o.in, out: o.out, kind: "pop" });
    return el;
  }

  function term(parent, o, title, lines) {
    const el = h("div", { class: "term", style: { left: `${o.x}px`, top: `${o.y}px`, width: `${o.w}px`, height: o.h ? `${o.h}px` : "auto" } });
    el.append(h("div", { class: "bar", text: title }));
    parent.append(el);
    anim(el, { in: o.in, out: o.out });
    lines.forEach((ln, i) => {
      const row = h("div", { class: "ln" });
      if (ln.c) {
        row.append(h("span", { class: "c", text: ln.c }));
      } else {
        row.append(h("span", { class: "p", text: "$ " }));
        const cmd = h("span", { text: ln.cmd });
        row.append(cmd);
        anim(cmd, { in: ln.in, kind: "type", dur: ln.dur ?? Math.min(1.2, 0.025 * ln.cmd.length) });
      }
      el.append(row);
      if (ln.c) anim(row, { in: ln.in, kind: "fade", lift: 6 });
    });
    return el;
  }

  // A layer (div + svg) that can fade as a whole.
  function layer(parent, o) {
    const el = abs(parent, "div", { x: 0, y: 0, w: 1920, h: 1080, kind: "none", ...o });
    const sv = svg("svg", { class: "wires", viewBox: "0 0 1920 1080" });
    el.append(sv);
    return { el, sv };
  }

  // Chips at one place, each replacing the previous one: steps [[ref, text, cls], ...].
  function swap(parent, x, y, steps, end = null) {
    steps.forEach(([ref, text, cls], i) => {
      const next = steps[i + 1];
      const out = next ? Engine.at(next[0]) - 0.3 : end;
      abs(parent, "span", { x, y, in: ref, out, kind: "pop", dur: 0.35 }, { class: `chip ${cls || ""}`, text });
    });
  }

  // A narrated video: one section per scene of `timing`, built by scenes[scene.id](sec, svg, header).
  function video(timing, scenes) {
    Engine.setTiming(timing);
    const stage = document.getElementById("stage");
    const total = timing.scenes.length;
    timing.scenes.forEach((scene, i) => {
      const sec = h("section", { id: scene.id });
      const sv = svg("svg", { class: "wires", viewBox: "0 0 1920 1080" });
      sec.append(sv);
      stage.append(sec);
      Engine.section(sec, i === 0 ? -1 : scene.id, i === total - 1 ? null : `${scene.id}e`);
      const head = header(sec, scene, i + 1, total);
      scenes[scene.id](sec, sv, head);
    });
    Engine.mountChrome(stage, true);
  }

  return { header, box, wire, packet, chip, chips, abs, shot, callout, term, point, layer, swap, video };
})();
