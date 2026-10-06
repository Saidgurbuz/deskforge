// DeskForge project page. All numbers mirror the paper's tables (see README).
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const SVGNS = "http://www.w3.org/2000/svg";
  const fmt = (v, d = 2) => v.toFixed(d);

  /* ---------- theme ---------- */
  const root = document.documentElement;
  const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } } };
  const saved = store.get("df-theme");
  if (saved) root.dataset.theme = saved;
  const isDark = () => root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const themeBtn = $("#theme-btn");
  const paintThemeIcon = () => { themeBtn.innerHTML = isDark()
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M20 14.6A8.2 8.2 0 1 1 9.4 4a6.6 6.6 0 0 0 10.6 10.6Z"/></svg>';
    themeBtn.setAttribute("aria-label", isDark() ? "Switch to light theme" : "Switch to dark theme"); };
  themeBtn.addEventListener("click", () => { root.dataset.theme = isDark() ? "light" : "dark"; store.set("df-theme", root.dataset.theme); paintThemeIcon(); });
  paintThemeIcon();

  /* ---------- nav state + reveal ---------- */
  const nav = $(".nav");
  addEventListener("scroll", () => nav.classList.toggle("scrolled", scrollY > 8), { passive: true });
  const links = $$(".nav-links a");
  const secObs = new IntersectionObserver(es => es.forEach(e => {
    if (e.isIntersecting) links.forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + e.target.id));
  }), { rootMargin: "-45% 0px -50% 0px" });
  $$("section[id]").forEach(s => secObs.observe(s));
  const revObs = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); revObs.unobserve(e.target); } }), { rootMargin: "0px 0px -8% 0px" });
  $$(".reveal").forEach(el => revObs.observe(el));

  /* ---------- tabs (generic) ---------- */
  function tabs(container, onSelect) {
    const btns = $$("button", container);
    const select = i => { btns.forEach((b, j) => { b.setAttribute("aria-selected", i === j); b.tabIndex = i === j ? 0 : -1; }); onSelect(i, btns[i]); };
    btns.forEach((b, i) => {
      b.setAttribute("role", "tab");
      b.addEventListener("click", () => select(i));
      b.addEventListener("keydown", e => {
        const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (d) { const k = (i + d + btns.length) % btns.length; btns[k].focus(); select(k); }
      });
    });
    container.setAttribute("role", "tablist");
    select(Math.max(0, btns.findIndex(b => b.getAttribute("aria-selected") === "true")));
  }

  /* ---------- compare sliders ---------- */
  function compare(el) {
    const input = $("input[type=range]", el);
    const set = v => el.style.setProperty("--pos", v + "%");
    input.addEventListener("input", () => set(input.value));
    set(input.value);
  }
  $$(".compare").forEach(compare);

  const pic = (stem, alt, small = 960, big = 1920, sizes = "(max-width: 1100px) 100vw, 1080px") =>
    `<img src="assets/img/${stem}-${small}.webp" srcset="assets/img/${stem}-${small}.webp ${small}w, assets/img/${stem}-${big}.webp ${big}w" sizes="${sizes}" alt="${alt}" decoding="async">`;

  function swapCompare(el, left, right, altL, altR) {
    const imgs = $$("img", el);
    const apply = (img, stem, alt) => { img.src = `assets/img/${stem}-960.webp`; img.srcset = `assets/img/${stem}-960.webp 960w, assets/img/${stem}-1920.webp 1920w`; img.alt = alt; };
    apply(imgs[0], right, altR); apply(imgs[1], left, altL);
  }

  /* ---------- appearance presets ---------- */
  const presetStage = $("#preset-stage");
  if (presetStage) {
    const imgs = $$("img", presetStage), cap = $("#preset-cap");
    tabs($("#preset-tabs"), (i, b) => { imgs.forEach((im, j) => im.classList.toggle("on", i === j)); cap.textContent = b.dataset.cap; });
  }

  /* ---------- dense annotation samples ---------- */
  const SAMPLES = [
    { apps: "Bluefish, File Roller, Mousepad", n: 79 },
    { apps: "GNOME Logs, GNOME Text Editor, Nautilus, VS Code", n: 185 },
    { apps: "Bluefish, Chromium, HomeBank", n: 116 },
    { apps: "GNOME Calculator, HomeBank, Nautilus, VS Code, Zim", n: 271 },
    { apps: "Seahorse, VS Code", n: 201 },
  ];
  const samp = $("#sample-compare");
  if (samp) tabs($("#sample-tabs"), i => {
    swapCompare(samp, `samples/${i}_clean`, `samples/${i}_annot`, "Captured desktop screenshot", "The same screenshot with its dense element annotations");
    $("#sample-apps").textContent = SAMPLES[i].apps;
    $("#sample-n").textContent = SAMPLES[i].n + " annotated elements";
  });

  /* ---------- detector examples ---------- */
  const det = $("#det-compare");
  if (det) tabs($("#det-tabs"), (i, b) => {
    const k = b.dataset.k;
    swapCompare(det, `detector/${k}_gt`, `detector/${k}_pred`, "GroundCUA human annotation", "DeskForge RT-DETRv4-L detections");
  });

  /* ---------- interaction records ---------- */
  const INTER = [
    { id: "01_008", w: 1920, h: 1080, instr: "Switch the editor to the Run and Debug view.", target: "Run and Debug (Ctrl+Shift+D)", kind: "tab", pt: [134, 273], box: [110, 249, 158, 297],
      apps: "Seahorse, VS Code", theme: "Linux Classic", res: "1920×1080", n: 117, delta: [5, 4, 10] },
    { id: "03_023", w: 2560, h: 1440, instr: "Select the time range dropdown in the HomeBank application to view available date range options.", target: "Last 12 Months", kind: "combo box", pt: [1043, 451], box: [966, 437, 1121, 465],
      apps: "HomeBank, Seahorse, Transmission, Zim", theme: "macOS Tahoe-like", res: "2560×1440", n: 154 },
    { id: "05_037", w: 1920, h: 1080, instr: "Open the project browsing page on PyPI.", target: "browse projects", kind: "link", pt: [595, 527], box: [531, 513, 659, 542],
      apps: "Bluefish, Chromium, File Roller, GNOME Logs, GNOME Text Editor, HomeBank", theme: "Ubuntu-like", res: "1920×1080", n: 177 },
  ];
  const tr = $("#transition");
  if (tr) {
    const before = $("#tr-before"), after = $("#tr-after");
    tabs($("#tr-tabs"), i => {
      const r = INTER[i];
      before.src = `assets/img/interaction/${r.id}_before-1920.webp`;
      after.src = `assets/img/interaction/${r.id}_after-1920.webp`;
      before.parentElement.dataset.full = before.src; after.parentElement.dataset.full = after.src;
      const bx = $("#tr-box"), pt = $("#tr-pt");
      Object.assign(bx.style, { left: 100 * r.box[0] / r.w + "%", top: 100 * r.box[1] / r.h + "%", width: 100 * (r.box[2] - r.box[0]) / r.w + "%", height: 100 * (r.box[3] - r.box[1]) / r.h + "%" });
      Object.assign(pt.style, { left: 100 * r.pt[0] / r.w + "%", top: 100 * r.pt[1] / r.h + "%" });
      $("#tr-instr").textContent = "“" + r.instr + "”";
      $("#tr-act").innerHTML = `click(${r.pt[0]}, ${r.pt[1]})<br><span style="color:var(--muted)">${r.kind}: “${r.target}”</span>`;
      $("#tr-meta").innerHTML = `<span><b>Applications:</b> ${r.apps}</span><span><b>Preset:</b> ${r.theme} · ${r.res} · ${r.n} annotated elements</span>`;
    });
  }

  /* ---------- trajectories / failures ---------- */
  const traj = $("#traj-img");
  if (traj) tabs($("#traj-tabs"), (i, b) => {
    const s = b.dataset.stem;
    traj.src = `assets/img/trajectories/${s}-1100.webp`;
    traj.srcset = `assets/img/trajectories/${s}-1100.webp 1100w, assets/img/trajectories/${s}-2200.webp 2200w`;
    traj.alt = b.textContent + " trajectory"; $("#traj-cap").textContent = b.dataset.cap;
  });

  /* ---------- video ---------- */
  // Play (muted) while on screen, pause when scrolled away, unless the viewer took control.
  // With reduced motion, or where autoplay is refused, the video stays paused on its cover frame.
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const playWhileVisible = (v, threshold) => {
    const cover = () => { if (v.dataset.cover && v.currentTime === 0) v.poster = v.dataset.cover; };
    if (reduce) cover();
    let userPaused = false;
    v.addEventListener("pause", () => { if (!document.hidden && v.dataset.auto !== "1") userPaused = true; v.dataset.auto = ""; });
    v.addEventListener("play", () => { userPaused = false; });
    new IntersectionObserver(es => es.forEach(e => {
      if (e.isIntersecting && !userPaused && !reduce) { v.play().catch(cover); }
      else if (!e.isIntersecting && !v.paused) { v.dataset.auto = "1"; v.pause(); }
    }), { threshold }).observe(v);
  };
  const hero = $("#hero-video");
  if (hero) playWhileVisible(hero, 0.25);
  const vid = $("#demo-video");
  if (vid) {
    const chips = $$(".chapters .chip");
    chips.forEach(c => c.addEventListener("click", () => { vid.currentTime = +c.dataset.t; vid.play().catch(() => {}); }));
    vid.addEventListener("timeupdate", () => {
      let k = 0; chips.forEach((c, i) => { if (vid.currentTime + 0.25 >= +c.dataset.t) k = i; });
      chips.forEach((c, i) => c.classList.toggle("on", i === k));
    });
    playWhileVisible(vid, 0.45);
  }

  /* ---------- lightbox ---------- */
  const lb = $("#lightbox"), lbImg = $("#lightbox img");
  const openLb = (src, alt) => { lbImg.src = src; lbImg.alt = alt || ""; lb.classList.add("on"); document.body.style.overflow = "hidden"; $(".x", lb).focus(); };
  const closeLb = () => { lb.classList.remove("on"); document.body.style.overflow = ""; lbImg.removeAttribute("src"); };
  document.addEventListener("click", e => {
    const z = e.target.closest(".zoomable");
    if (!z) return;
    const img = z.tagName === "IMG" ? z : ($("img.on", z) || $("img", z));
    // Largest candidate from srcset, so the enlarged view is sharp.
    const largest = (img.getAttribute("srcset") || "").split(",").map(s => s.trim().split(/\s+/)).sort((a, b) => parseInt(b[1]) - parseInt(a[1]))[0];
    openLb(z.dataset.full || (largest && largest[0]) || img.currentSrc || img.src, img.alt);
  });
  lb.addEventListener("click", closeLb);
  addEventListener("keydown", e => { if (e.key === "Escape" && lb.classList.contains("on")) closeLb(); });

  /* ---------- BibTeX copy ---------- */
  $$(".copy").forEach(b => b.addEventListener("click", async () => {
    const text = $(b.dataset.target).innerText;
    try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; }
    catch { b.textContent = "Select & copy"; }
    setTimeout(() => (b.textContent = "Copy"), 1600);
  }));

  /* ======================================================================
     Charts: one dumbbell component (base ○ → + DeskForge ●) used three times.
     ====================================================================== */
  const MODELS = ["Gemma4-E4B", "UI-R1-3B", "InternVL3.5-8B", "Qwen3.5-4B"];

  // Table 2: held-out conditions of DeskForge-1M (%)
  const HELDOUT = {
    cols: ["New Scenes", "Theme", "App", "Resolution", "Mean"],
    base: { "Gemma4-E4B": [16.27, 12.10, 14.15, 6.56, 12.27], "UI-R1-3B": [41.34, 40.24, 35.56, 34.90, 38.01], "InternVL3.5-8B": [65.00, 59.28, 61.50, 47.62, 58.35], "Qwen3.5-4B": [78.56, 74.03, 76.11, 76.34, 76.26] },
    ft: { "Gemma4-E4B": [89.01, 85.36, 84.50, 81.62, 85.12], "UI-R1-3B": [88.25, 84.28, 83.77, 82.07, 84.59], "InternVL3.5-8B": [87.37, 83.43, 83.77, 71.10, 81.42], "Qwen3.5-4B": [90.14, 87.36, 87.50, 85.15, 87.54] },
    publics: [
      ["OS-Atlas-Pro-7B", [47.61, 47.27, 45.58, 32.10, 43.14]], ["EvoCUA-8B", [50.49, 45.59, 46.65, 44.93, 46.91]],
      ["UI-TARS-1.5-7B", [67.68, 64.96, 66.68, 61.67, 65.25]], ["GUI-Owl-1.5-32B", [70.87, 62.19, 66.68, 64.65, 66.10]],
      ["UGround-V1-7B", [69.08, 64.61, 67.18, 63.82, 66.17]], ["ScaleCUA-7B", [72.74, 68.28, 70.75, 63.62, 68.85]],
      ["UI-MOPD-8B", [73.04, 67.12, 70.49, 68.07, 69.68]], ["GroundNext-7B", [73.57, 67.67, 72.10, 68.87, 70.55]],
      ["Gemma4-31B", [75.26, 71.00, 73.57, 63.13, 70.74]], ["UI-Venus-2-9B", [81.45, 77.44, 79.40, 77.40, 78.92]],
    ],
  };

  // Table 3: five external GUI grounding benchmarks (%); the mean is computed from the five columns.
  const EXT = {
    cols: ["ScreenSpot-Pro", "ScreenSpot-v2", "OSWorld-G", "UI-Vision", "MMBench-GUI"],
    base: { "Gemma4-E4B": [1.83, 46.86, 10.64, 3.28, 31.39], "UI-R1-3B": [14.48, 81.92, 34.04, 12.09, 56.84], "InternVL3.5-8B": [18.72, 83.33, 44.15, 17.50, 66.58], "Qwen3.5-4B": [30.17, 80.11, 51.24, 17.24, 61.83] },
    ft: { "Gemma4-E4B": [21.95, 79.56, 44.15, 13.87, 58.90], "UI-R1-3B": [27.20, 83.96, 43.97, 15.15, 65.41], "InternVL3.5-8B": [23.78, 84.98, 44.33, 17.55, 67.20], "Qwen3.5-4B": [41.68, 90.33, 61.35, 28.77, 76.82] },
  };
  const mean = a => a.reduce((s, v) => s + v, 0) / a.length;
  EXT.cols.push("Mean of five");
  for (const k of ["base", "ft"]) for (const m of MODELS) EXT[k][m] = [...EXT[k][m], mean(EXT[k][m])];

  // Table 4: tasks solved under a fixed Qwen3.6-27B planner.
  const LH = {
    cols: ["WebArena-Infinity", "OpenApps"], totals: [119, 100],
    base: { "Gemma4-E4B": [5, 2], "UI-R1-3B": [30, 7], "InternVL3.5-8B": [37, 4], "Qwen3.5-4B": [31, 3] },
    ft: { "Gemma4-E4B": [40, 12], "UI-R1-3B": [37, 12], "InternVL3.5-8B": [44, 11], "Qwen3.5-4B": [50, 15] },
  };

  const tip = document.createElement("div"); tip.className = "tip"; tip.setAttribute("role", "tooltip"); document.body.appendChild(tip);
  const showTip = (e, html) => {
    tip.innerHTML = html; tip.classList.add("on");
    const r = tip.getBoundingClientRect();
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  };
  const hideTip = () => tip.classList.remove("on");
  const el = (tag, attrs = {}, parent) => { const n = document.createElementNS(SVGNS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); if (parent) parent.appendChild(n); return n; };

  function dumbbell(host, { rows, max, ticks, unit = "%", digits = 2, ref, deltaUnit = "pt", tipExtra }) {
    const narrow = host.clientWidth < 560;
    const W = Math.max(320, host.clientWidth), labelW = narrow ? 118 : 138, rightW = narrow ? 58 : 74, rowH = 46, top = 22, bottom = 28;
    const H = top + rows.length * rowH + bottom;
    const x0 = labelW, x1 = W - rightW, X = v => x0 + (x1 - x0) * v / max;
    if (narrow) ticks = [0, Math.round(max / 2), max];
    host.innerHTML = "";
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": host.dataset.label || "" }, host);
    ticks.forEach(t => {
      el("line", { class: "grid", x1: X(t), x2: X(t), y1: top - 6, y2: H - bottom + 4 }, svg);
      const tt = el("text", { class: "axis-t", x: X(t), y: H - 8, "text-anchor": "middle" }, svg); tt.textContent = t + (unit === "%" && t === max ? "%" : "");
    });
    if (ref) {
      el("line", { class: "ref", x1: X(ref.v), x2: X(ref.v), y1: top - 12, y2: H - bottom + 4 }, svg);
      const t = el("text", { class: "ref-t", x: X(ref.v) + (narrow ? -4 : 5), y: top - 6, "text-anchor": narrow ? "end" : "start" }, svg); t.textContent = narrow ? "best public" : ref.label;
    }
    rows.forEach((r, i) => {
      const y = top + i * rowH + rowH / 2;
      const g = el("g", { class: "row", tabindex: 0, "aria-label": `${r.name}: base ${fmt(r.b, digits)}, fine-tuned ${fmt(r.f, digits)}` }, svg);
      el("rect", { class: "hl", x: 0, y: y - rowH / 2 + 2, width: W, height: rowH - 4, rx: 8 }, g);
      const lt = el("text", { class: "row-l", x: 8, y: narrow ? y - 2 : y + 4.5 }, g); lt.textContent = r.name;
      if (narrow) { const sv = el("text", { class: "val", x: 8, y: y + 14 }, g); sv.textContent = `${fmt(r.b, digits)} → `; const b2 = el("tspan", { class: "val f" }, sv); b2.textContent = fmt(r.f, digits); }
      el("line", { class: "link", x1: X(r.b), x2: X(r.f), y1: y, y2: y }, g);
      el("circle", { class: "dot-b", cx: X(r.b), cy: y, r: 6 }, g);
      el("circle", { class: "dot-f", cx: X(r.f), cy: y, r: 7 }, g);
      const gap = X(r.f) - X(r.b);
      if (!narrow) {
      const vb = el("text", { class: "val", x: X(r.b) + (gap >= 0 ? -11 : 11), y: y + 4.5, "text-anchor": gap >= 0 ? "end" : "start" }, g); vb.textContent = fmt(r.b, digits);
      const vf = el("text", { class: "val f", x: X(r.f) + (gap >= 0 ? 12 : -12), y: y + 4.5, "text-anchor": gap >= 0 ? "start" : "end" }, g); vf.textContent = fmt(r.f, digits);
      // Keep the fine-tuned label inside the plot when it sits near the right edge.
      if (gap >= 0 && X(r.f) + 12 + (digits ? 38 : 18) > x1 + rightW - 52) { vf.setAttribute("x", X(r.f)); vf.setAttribute("y", y - 11); vf.setAttribute("text-anchor", "middle"); }
      if (gap >= 0 && gap < (digits ? 46 : 26)) { vb.setAttribute("x", X(r.b)); vb.setAttribute("y", y + 21); vb.setAttribute("text-anchor", "middle"); }
      }
      const d = r.f - r.b;
      const dt = el("text", { class: "delta", x: W - 4, y: y + 4.5, "text-anchor": "end" }, g); dt.textContent = (d >= 0 ? "+" : "−") + fmt(Math.abs(d), digits);
      el("rect", { class: "hit", x: 0, y: y - rowH / 2, width: W, height: rowH }, g);
      const html = `<div class="tt">${r.name}</div><span class="k">Base</span> ${fmt(r.b, digits)}${unit}<br><span class="k">+ DeskForge-1M</span> <b>${fmt(r.f, digits)}${unit}</b><br><span class="k">Change</span> ${d >= 0 ? "+" : "−"}${fmt(Math.abs(d), digits)} ${deltaUnit}${tipExtra ? "<br>" + tipExtra(r) : ""}`;
      g.addEventListener("pointermove", e => showTip(e, html));
      g.addEventListener("pointerleave", hideTip);
      g.addEventListener("focus", () => { const b = g.getBoundingClientRect(); showTip({ clientX: b.left + X(r.f), clientY: b.top + rowH / 2 }, html); });
      g.addEventListener("blur", hideTip);
    });
    const dh = el("text", { class: "axis-t", x: W - 4, y: top - 6, "text-anchor": "end" }, svg); dh.textContent = "Δ";
  }

  function tableHTML(cols, rowsFn, note) {
    return `<div class="table-wrap"><table><thead><tr><th>Model</th><th></th>${cols.map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rowsFn()}</tbody></table>${note ? `<div class="table-note">${note}</div>` : ""}</div>`;
  }
  const pairRows = (D, digits = 2) => MODELS.map(m => `<tr><td rowspan="2"><b>${m}</b></td><td class="var">Base</td>${D.base[m].map(v => `<td>${fmt(v, digits)}</td>`).join("")}</tr><tr class="ours"><td class="var">+ DeskForge-1M</td>${D.ft[m].map(v => `<td>${fmt(v, digits)}</td>`).join("")}</tr>`).join("");

  function wireChart(prefix, D, opts) {
    const host = $(`#${prefix}-chart`), tabsEl = $(`#${prefix}-tabs`);
    let cur = 0;
    const draw = () => dumbbell(host, { ...opts(cur), rows: MODELS.map(m => ({ name: m, b: D.base[m][cur], f: D.ft[m][cur] })) });
    tabs(tabsEl, i => { cur = i; draw(); });
    let w = host.clientWidth;
    new ResizeObserver(() => { if (Math.abs(host.clientWidth - w) > 4) { w = host.clientWidth; draw(); } }).observe(host);
  }

  if ($("#ho-chart")) {
    wireChart("ho", HELDOUT, i => ({ max: 100, ticks: [0, 20, 40, 60, 80, 100], ref: { v: HELDOUT.publics[9][1][i], label: "UI-Venus-2-9B (best public)" } }));
    $("#ho-table").innerHTML = tableHTML(HELDOUT.cols, () =>
      `<tr class="group"><td colspan="7">Public VLMs and CUAs (reference; model-specific inference protocols)</td></tr>` +
      HELDOUT.publics.map(([n, v]) => `<tr><td>${n}</td><td class="var"></td>${v.map(x => `<td>${fmt(x)}</td>`).join("")}</tr>`).join("") +
      `<tr class="group"><td colspan="7">Fine-tuning across VLMs</td></tr>` + pairRows(HELDOUT),
      "Grounding accuracy (%) on the four held-out conditions of DeskForge-1M; a prediction is correct if the point falls in the target's annotated visible region.");
  }
  if ($("#ext-chart")) {
    wireChart("ext", EXT, () => ({ max: 100, ticks: [0, 20, 40, 60, 80, 100] }));
    $("#ext-table").innerHTML = tableHTML(EXT.cols, () => pairRows(EXT), "Accuracy (%) under each benchmark's own annotations and scoring. None of the five benchmarks is used for fine-tuning. The mean is the unweighted mean of the five columns.");
  }
  if ($("#lh-chart")) {
    wireChart("lh", LH, i => ({ max: LH.totals[i], ticks: i === 0 ? [0, 20, 40, 60, 80, 100, 119] : [0, 20, 40, 60, 80, 100], unit: "", digits: 0, deltaUnit: "tasks",
      tipExtra: r => `<span class="k">of ${LH.totals[i]} tasks</span>` }));
    $("#lh-table").innerHTML = tableHTML(["WebArena-Infinity (119)", "OpenApps (100)"], () => pairRows(LH, 0), "Tasks solved, judged by each environment's state-based checks. The planner is fixed; only the action-model weights change within each pair.");
  }

  /* ======================================================================
     Corpus composition: three hoverable panels.
     Counts from the paper repo's evidence/corpus_composition_20260911.json.
     ====================================================================== */
  const COMP = {
    N: 1207368, median: 121, mean: 132.29, max: 1161, overflowFrom: 400, overflow: 5413,
    // [lo, hi, observations], 10-element bins
    elements: [[0,10,4501],[10,20,26327],[20,30,35604],[30,40,45689],[40,50,52718],[50,60,54678],[60,70,58965],[70,80,64156],[80,90,65568],[90,100,63148],[100,110,60753],[110,120,62112],[120,130,63212],[130,140,63976],[140,150,57817],[150,160,51656],[160,170,45910],[170,180,40381],[180,190,36365],[190,200,31997],[200,210,28473],[210,220,25584],[220,230,22975],[230,240,21109],[240,250,19177],[250,260,17327],[260,270,15055],[270,280,12605],[280,290,10445],[290,300,8622],[300,310,7406],[310,320,6000],[320,330,4881],[330,340,4155],[340,350,3379],[350,360,2732],[360,370,2179],[370,380,1782],[380,390,1380],[390,400,1156]],
    windows: [[0,32189],[1,78231],[2,298210],[3,269779],[4,192361],[5,143910],[6,97181],[7,65746],[8,29761]],
    // share of each observation's elements that occlusion clips or removes
    occBands: [["none",27572],["0-10%",189830],["10-30%",401806],["30-50%",236358],["50-70%",184112],["70-100%",167690]],
  };
  const pctN = n => 100 * n / COMP.N;
  const intl = n => n.toLocaleString("en-US");

  function frame(host, H, pad) {
    const W = Math.max(240, host.clientWidth);
    host.innerHTML = "";
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": host.dataset.label || "" }, host);
    return { svg, W, H, x0: pad.l, x1: W - pad.r, y0: H - pad.b, y1: pad.t };
  }
  function yAxis(f, ticks, Y, fmtT = t => t) {
    ticks.forEach(t => {
      el("line", { class: "grid", x1: f.x0, x2: f.x1, y1: Y(t), y2: Y(t) }, f.svg);
      const tx = el("text", { class: "axis-t", x: f.x0 - 6, y: Y(t) + 4, "text-anchor": "end" }, f.svg); tx.textContent = fmtT(t);
    });
  }
  function xLabel(f, text) { const t = el("text", { class: "axis-t", x: (f.x0 + f.x1) / 2, y: f.H - 2, "text-anchor": "middle" }, f.svg); t.textContent = text; }
  function hoverRect(f, x, w, html) {
    const r = el("rect", { class: "hit", x, y: f.y1, width: w, height: f.y0 - f.y1 }, f.svg);
    r.addEventListener("pointermove", e => showTip(e, html)); r.addEventListener("pointerleave", hideTip);
    return r;
  }

  function drawDensity(host) {
    const f = frame(host, 230, { l: 34, r: 8, t: 26, b: 40 });
    const ymax = 6, X = v => f.x0 + (f.x1 - f.x0) * v / COMP.overflowFrom, Y = v => f.y0 - (f.y0 - f.y1) * v / ymax;
    yAxis(f, [0, 2, 4, 6], Y, t => t + "%");
    const bars = COMP.elements.map(([lo, hi, n]) => {
      const b = el("rect", { class: "cbar", x: X(lo) + 0.5, y: Y(pctN(n)), width: Math.max(1, X(hi) - X(lo) - 1), height: f.y0 - Y(pctN(n)), rx: 1 }, f.svg);
      return [b, lo, hi, n];
    });
    const mx = X(COMP.median);
    el("line", { class: "cmark", x1: mx, x2: mx, y1: f.y1 - 8, y2: f.y0 }, f.svg);
    const ml = el("text", { class: "cnote", x: mx + 6, y: f.y1 - 2 }, f.svg); ml.textContent = `median ${COMP.median} · mean ${Math.round(COMP.mean)}`;
    [0, 100, 200, 300, 400].forEach(t => { const tx = el("text", { class: "axis-t", x: X(t), y: f.y0 + 16, "text-anchor": "middle" }, f.svg); tx.textContent = t === 400 ? "400+" : t; });
    xLabel(f, "Annotated elements");
    bars.forEach(([b, lo, hi, n]) => {
      const r = hoverRect(f, X(lo), X(hi) - X(lo), `<div class="tt">${lo}–${hi - 1} elements</div>${fmt(pctN(n))}% of screens<br><span class="k">${intl(n)} observations</span>`);
      r.addEventListener("pointerenter", () => b.classList.add("on")); r.addEventListener("pointerleave", () => b.classList.remove("on"));
    });
  }

  function drawWindows(host) {
    const f = frame(host, 230, { l: 34, r: 8, t: 26, b: 40 });
    const n = COMP.windows.length, ymax = 25, slot = (f.x1 - f.x0) / n, Y = v => f.y0 - (f.y0 - f.y1) * v / ymax;
    yAxis(f, [0, 10, 20], Y, t => t + "%");
    const multi = COMP.windows.filter(w => w[0] >= 2).reduce((s, w) => s + w[1], 0);
    COMP.windows.forEach(([k, c], i) => {
      const x = f.x0 + i * slot, w = slot * 0.68, v = pctN(c);
      const b = el("rect", { class: k >= 2 ? "cbar" : "cbar base", x: x + (slot - w) / 2, y: Y(v), width: w, height: f.y0 - Y(v), rx: 3 }, f.svg);
      const tx = el("text", { class: "axis-t", x: x + slot / 2, y: f.y0 + 16, "text-anchor": "middle" }, f.svg); tx.textContent = k;
      const r = hoverRect(f, x, slot, `<div class="tt">${k} application window${k === 1 ? "" : "s"}</div>${fmt(v)}% of screens<br><span class="k">${intl(c)} observations</span>`);
      r.addEventListener("pointerenter", () => b.classList.add("on")); r.addEventListener("pointerleave", () => b.classList.remove("on"));
    });
    const t = el("text", { class: "cnote f", x: f.x1, y: f.y1 - 2, "text-anchor": "end" }, f.svg); t.textContent = `${Math.round(pctN(multi))}% show two or more`;
    xLabel(f, "Application windows open");
  }

  function drawOcclusion(host) {
    const f = frame(host, 230, { l: 38, r: 14, t: 26, b: 40 });
    const counts = COMP.occBands.map(b => b[1]), th = [0, 10, 30, 50, 70];
    const surv = th.map((_, i) => pctN(counts.slice(i + 1).reduce((s, v) => s + v, 0)));
    const X = v => f.x0 + (f.x1 - f.x0) * v / 70, Y = v => f.y0 - (f.y0 - f.y1) * v / 100;
    yAxis(f, [0, 25, 50, 75, 100], Y, t => t + "%");
    const pts = th.map((t, i) => [X(t), Y(surv[i])]);
    el("path", { class: "carea", d: `M${pts[0][0]},${f.y0} L${pts.map(p => p.join(",")).join(" L")} L${pts[pts.length - 1][0]},${f.y0} Z` }, f.svg);
    el("path", { class: "cline", d: "M" + pts.map(p => p.join(",")).join(" L") }, f.svg);
    th.forEach(t => { const tx = el("text", { class: "axis-t", x: X(t), y: f.y0 + 16, "text-anchor": "middle" }, f.svg); tx.textContent = t + "%"; });
    const t = el("text", { class: "cnote f", x: f.x1, y: f.y1 - 2, "text-anchor": "end" }, f.svg); t.textContent = `${Math.round(surv[0])}% contain a covered element`;
    th.forEach((v, i) => {
      const dot = el("circle", { class: "cdot", cx: pts[i][0], cy: pts[i][1], r: 5 }, f.svg);
      const lo = i === 0 ? pts[0][0] : (pts[i - 1][0] + pts[i][0]) / 2, hi = i === th.length - 1 ? f.x1 + 10 : (pts[i][0] + pts[i + 1][0]) / 2;
      const r = hoverRect(f, lo - (i === 0 ? 10 : 0), hi - lo + (i === 0 ? 10 : 0), `<div class="tt">More than ${v}% of elements occluded</div>${fmt(surv[i])}% of screens`);
      r.addEventListener("pointerenter", () => dot.setAttribute("r", 7)); r.addEventListener("pointerleave", () => dot.setAttribute("r", 5));
    });
    xLabel(f, "Occluded elements (%)");
  }

  [["comp-density", drawDensity], ["comp-windows", drawWindows], ["comp-occlusion", drawOcclusion]].forEach(([id, fn]) => {
    const host = $("#" + id); if (!host) return;
    fn(host); let w = host.clientWidth;
    new ResizeObserver(() => { if (Math.abs(host.clientWidth - w) > 4) { w = host.clientWidth; fn(host); } }).observe(host);
  });
})();
