/* Queue-driven human audit of released annotations.
 *
 * Three things make this page different from the corpus browser next door,
 * and all three exist because the corpus browser is a browser and this is an
 * instrument somebody has to use for six hundred items in a row.
 *
 * 1. Boxes are drawn HERE, on a canvas, from the element list. The corpus
 *    browser asks the server to render an annotated figure, which for a 4K
 *    capture is a 569ms render and 2.9MB of PNG that it then forbids caching -
 *    so toggling labels costs a second and a half and three megabytes. Drawing
 *    client-side makes every toggle, zoom and pan free, and reduces the item to
 *    one cacheable WebP frame plus the element list: 129KB for a 400-element 4K
 *    capture, measured.
 *
 * 2. Every answer is saved the instant the key is pressed, and saving costs the
 *    server no filesystem scan: the label log is append-only and progress is
 *    counted in memory. Nothing here rescans a directory after a write.
 *
 * 3. The three neighbouring items are prefetched, because on a tunnel the
 *    latency you feel is the one between deciding and seeing the next screen.
 *
 * The missed-element pass runs FIRST, with the boxes hidden. Asking "what has
 * no box" while the boxes are on screen does not measure anything: you cannot
 * see what is not drawn. */

/* Two ways to work through a queue, and the passes each one has.
 *
 * `sample` judges a fixed few elements per screen, one at a time and zoomed.
 * `sweep` puts the whole dense annotation on screen and asks only for the
 * exceptions: click what is wrong, mark what is missing, then declare the rest
 * correct. When precision is high - and 22 of the first 22 elements judged here
 * were correct - sweeping is both far faster and a census rather than a sample.
 * What it gives up is per-element evidence: an element nobody stopped on is
 * judged correct by not having been flagged. The store records which procedure
 * produced each verdict so the two are never pooled. */
var MODE_PASSES = {
  sweep: ["sweep"],
  sample: ["missed", "screen", "elements"]
};

/* An instruction campaign asks about one action sample rather than a whole
 * annotation: the screen, the instruction, and the target it names, all shown
 * at once, and the questions are whether the instruction is a legitimate thing
 * to ask and whether the target is the thing it asks for.
 *
 * There is an optional first pass in which the target is hidden and the rater
 * clicks from the instruction alone, which the server can then score by itself.
 * It is off unless the queue was drawn with --with-grounding: it makes every
 * item cost a click that has to be right, and a misclick becomes a data point
 * the rater did not mean. */
var INSTRUCTION_PASSES = ["judge"];
var INSTRUCTION_PASSES_WITH_GROUNDING = ["ground", "judge"];
var IMAGE_CAP = 2048;          /* long edge of the base frame we ask for */
var DETAIL_TRIGGER = 1.15;     /* fetch native pixels past this much zoom */
var DETAIL_GRID = 64;          /* quantise crop requests so ETags hit */
var PREFETCH = 3;

var st = {
  campaign: null, rubric: null, rubricRef: null, rater: "", readOnly: false,
  mode: "sweep", kind: "observation", selected: null, marking: false,
  flagged: {}, opened: {},
  grounding: false, expanded: false, guess: null, revealedTarget: false,
  after: null, showAfter: false,
  count: 0, queue: [], progress: null,
  index: 0, item: null, elements: [], byKey: {}, sampled: [], state: null,
  stateIndex: null,
  pass: "missed", question: 0, element: 0,
  pending: [], missed: [], revealed: false,
  natural: { w: 0, h: 0 }, img: null, imgScale: 1,
  detail: null, detailWanted: null, detailTimer: null,
  view: { scale: 1, x: 0, y: 0 }, fitted: true,
  opts: { boxes: true, labels: false, frags: true, loupe: false },
  cursor: null, hover: null,
  dwell: 0, elementDwell: 0,
  saving: 0, chain: Promise.resolve(),
  itemCache: {}
};

var ROLE_COLOURS = {
  window: "#8bb7ff", button: "#7ee0a0", text: "#ffd479", entry: "#ff9ecb",
  menu: "#c9a0ff", list: "#79e0e0", image: "#ffa07a", other: "#9aa4b2"
};

/* --------------------------------------------------------------- helpers */

function $(id) { return document.getElementById(id); }

function el(tag, attrs, kids) {
  var node = document.createElement(tag);
  Object.keys(attrs || {}).forEach(function (k) {
    if (k === "class") node.className = attrs[k];
    else if (k === "text") node.textContent = attrs[k];
    else if (k === "html") node.innerHTML = attrs[k];
    else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), attrs[k]);
    else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
  });
  (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function get(url, options) {
  return fetch(url, options || {}).then(function (r) {
    if (r.status === 304) return null;
    return r.json().then(function (body) {
      if (!r.ok) throw new Error(body.error || r.statusText);
      return body;
    });
  });
}

function post(url, payload) {
  return get(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

function saved(text, kind) {
  var node = $("saved");
  node.textContent = text;
  node.className = "saved" + (kind ? " " + kind : "");
}

function colourFor(element) {
  var kind = String(element.kind || "").toLowerCase();
  if (ROLE_COLOURS[kind]) return ROLE_COLOURS[kind];
  var role = String(element.role || "").toLowerCase();
  var keys = Object.keys(ROLE_COLOURS);
  for (var i = 0; i < keys.length; i++) {
    if (role.indexOf(keys[i]) >= 0) return ROLE_COLOURS[keys[i]];
  }
  return ROLE_COLOURS.other;
}

function boxesOf(element) {
  if (st.opts.frags && element.visible_fragments && element.visible_fragments.length) {
    return element.visible_fragments;
  }
  return element.rect ? [element.rect] : [];
}

function area(rect) { return Math.max(1, rect.w) * Math.max(1, rect.h); }

function contains(rect, x, y) {
  return x >= rect.x && y >= rect.y && x < rect.x + rect.w && y < rect.y + rect.h;
}

/* Smallest element whose drawn geometry covers a point. Smallest-first is the
 * only workable rule: a table cell sits under three containers, and the
 * outermost one is never the answer to "what is here". */
function elementAt(x, y) {
  var best = null, bestArea = Infinity;
  for (var i = 0; i < st.elements.length; i++) {
    var element = st.elements[i];
    var rects = boxesOf(element);
    for (var j = 0; j < rects.length; j++) {
      if (contains(rects[j], x, y) && area(rects[j]) < bestArea) {
        best = element;
        bestArea = area(rects[j]);
      }
    }
  }
  return best;
}

/* ------------------------------------------------------------ the rubric */

function passes() {
  if (st.kind === "instruction") {
    return st.grounding ? INSTRUCTION_PASSES_WITH_GROUNDING : INSTRUCTION_PASSES;
  }
  return MODE_PASSES[st.mode] || MODE_PASSES.sample;
}

function questions(scope) {
  return (st.rubric && st.rubric.questions || []).filter(function (q) {
    return q.scope === scope;
  });
}

function activeQuestion() {
  if (st.pass === "screen") return questions("screen")[st.question] || null;
  if (st.pass === "elements") return questions("element")[0] || null;
  /* In a sweep both question sets are live at once, so the selection decides:
   * with an element selected the keys flag that element, with nothing selected
   * they answer the screen. One rule, and the panel shows which set is armed. */
  if (st.pass === "sweep") {
    if (st.selected) return questions("element")[0] || null;
    return questions("screen")[st.question] || null;
  }
  if (st.pass === "judge") return questions("screen")[st.question] || null;
  return null;
}

function elementQuestion() {
  return questions("element")[0] || null;
}

/* Keys the active question owns. A rubric is a data file and may claim any
 * letter, so its keys win over the page's own and the page reports the
 * fallback it ended up with instead of silently doing nothing. */
function claimedKeys() {
  var question = activeQuestion();
  var claimed = {};
  (question && question.options || []).forEach(function (option) {
    if (option.key) claimed[option.key.toLowerCase()] = option;
  });
  return claimed;
}

/* Every key the rubric uses anywhere, with the question it belongs to.
 *
 * The blind pass asks no rubric question, so a key that answers pass 2 does
 * nothing there - and a key that does nothing is indistinguishable from a
 * broken keyboard. This is what lets an unhandled key say which pass it
 * belongs to instead of being swallowed. */
function allClaimedKeys() {
  var out = {};
  (st.rubric && st.rubric.questions || []).forEach(function (question) {
    (question.options || []).forEach(function (option) {
      if (!option.key) return;
      var key = option.key.toLowerCase();
      (out[key] = out[key] || []).push({ question: question, option: option });
    });
  });
  return out;
}

//: Which pass answers a given rubric question.
function passOf(question) {
  return question.scope === "screen" ? "screen" : "elements";
}

function unhandledKey(lower) {
  var entries = allClaimedKeys()[lower] || [];
  var elsewhere = entries.filter(function (entry) {
    return passOf(entry.question) !== st.pass;
  });
  if (elsewhere.length) {
    var target = passOf(elsewhere[0].question);
    var position = passes().indexOf(target) + 1;
    hint(
      "\u2018" + lower + "\u2019 answers " + elsewhere[0].question.id +
      ", which is pass " + position + " (" + target + "). " +
      (st.pass === "missed"
        ? "Finish this pass first: press Enter (nothing marked = nothing missing)."
        : "Press Tab to go there."));
    return;
  }
  if (lower.length === 1) {
    hint("\u2018" + lower + "\u2019 does nothing here. Press ? for the keys that do.");
  }
}

/* A message where the eye already is - beside the question - and on the image,
 * because which one you are looking at depends on what you were doing. */
function hint(text) {
  stageNote(text);
  var host = $("pass-extra");
  var existing = host.querySelector(".hint");
  if (existing) host.removeChild(existing);
  host.appendChild(el("p", { class: "hint", text: text }));
  if (hint.timer) clearTimeout(hint.timer);
  hint.timer = setTimeout(function () {
    stageNote("");
    var node = $("pass-extra").querySelector(".hint");
    if (node) node.parentNode.removeChild(node);
  }, 6000);
}

/* --------------------------------------------------------------- loading */

function boot() {
  return get("/api/audit/campaigns").then(function (body) {
    st.rater = body.rater;
    st.readOnly = body.read_only;
    $("rater").textContent = body.rater + (body.read_only ? " (read-only)" : "");
    st.rater = body.rater;
    var select = $("campaign");
    clear(select);
    (body.campaigns || []).forEach(function (campaign) {
      select.appendChild(el("option", {
        value: campaign.name,
        text: campaign.name + "  " + campaign.progress.done + "/" + campaign.progress.total
      }));
    });
    if (!body.campaigns || !body.campaigns.length) {
      stageNote("No campaigns under the audit root. Draw one with " +
                "scripts/build_audit_queue.py.");
      return null;
    }
    var wanted = (location.hash || "").replace("#", "").split("|")[0];
    var name = wanted && [].some.call(select.options, function (o) { return o.value === wanted; })
      ? wanted : body.campaigns[0].name;
    select.value = name;
    return openCampaign(name);
  }).catch(function (error) { stageNote(error.message); });
}

function openCampaign(name) {
  return get("/api/audit/queue?campaign=" + encodeURIComponent(name)).then(function (body) {
    st.campaign = body.campaign;
    st.rubric = body.rubric;
    st.rubricRef = body.rubric_ref;
    st.mode = body.mode || "sample";
    st.kind = body.kind || "observation";
    st.grounding = !!body.grounding;
    st.queue = body.items || [];
    st.count = st.queue.length;
    st.progress = body.progress;
    st.readOnly = body.read_only;
    st.itemCache = {};
    if (body.torn_bytes) {
      stageNote("Recovered from an interrupted session: " + body.torn_bytes +
                " unterminated bytes at the end of labels.jsonl were ignored.");
    }
    renderTrack();
    renderHelpKeys();
    $("counter").title = st.mode === "sweep"
      ? "sweep mode: every element on a screen is judged, by exception"
      : "sample mode: a fixed few elements per screen, judged individually";
    var wanted = parseInt((location.hash || "").split("|")[1], 10);
    var index = isNaN(wanted) ? (body.progress.next === null ? 0 : body.progress.next) : wanted;
    return openItem(index);
  });
}

function itemUrl(index) {
  return "/api/audit/item?campaign=" + encodeURIComponent(st.campaign) + "&index=" + index;
}

function imageUrl(index, region, cap, fmt) {
  var url = "/api/audit/image?campaign=" + encodeURIComponent(st.campaign) +
    "&index=" + index + "&fmt=" + (fmt || "webp");
  if (region) {
    url += "&x=" + region.x + "&y=" + region.y + "&w=" + region.w + "&h=" + region.h;
  }
  if (cap) url += "&max=" + cap;
  return url;
}

function fetchItem(index) {
  if (st.itemCache[index]) return Promise.resolve(st.itemCache[index]);
  return get(itemUrl(index)).then(function (body) {
    if (body) st.itemCache[index] = body;
    return st.itemCache[index];
  });
}

function openItem(index) {
  if (index < 0 || index >= st.count) return Promise.resolve();
  st.index = index;
  location.hash = "#" + st.campaign + "|" + index;
  st.detail = null;
  st.detailWanted = null;
  st.state = null;
  st.stateIndex = null;
  st.pending = [];
  st.revealed = false;
  st.hover = null;
  return fetchItem(index).then(function (body) {
    st.item = body.item;
    st.elements = body.elements || [];
    st.byKey = {};
    st.elements.forEach(function (element) { st.byKey[element.key] = element; });
    st.sampled = (body.sampled || []).filter(function (key) { return st.byKey[key]; });
    st.natural = { w: body.item.width || 0, h: body.item.height || 0 };
    st.screentag = body.screentag;
    st.automated = body.automated;
    renderFacts(body);
    loadImage(index);
    return refreshState([index]);
  }).then(function () {
    var done = st.state && st.state.item && st.state.item.status === "done";
    if (st.kind === "instruction") {
      var ground = st.state && st.state.ground;
      st.guess = ground ? ground.point : null;
      // Without the grounding pass there is nothing to reveal: the target is
      // on screen from the moment the item opens.
      st.revealedTarget = !st.grounding || !!ground;
      st.showAfter = false;
      st.after = null;
      st.expanded = false;
      st.pass = (st.grounding && !ground) ? "ground" : "judge";
      st.question = firstUnansweredScreenQuestion();
    } else if (st.mode === "sweep") {
      st.pass = "sweep";
      st.selected = null;
      st.marking = false;
      st.flagged = {};
      st.opened = {};
      Object.keys((st.state && st.state.elements) || {}).forEach(function (key) {
        var answer = st.state.elements[key];
        var values = answer.values || [];
        if (values.length && values.join() !== "ok") st.flagged[key] = values;
      });
    } else {
      st.pass = done ? "screen" : firstUnansweredPass();
    }
    st.question = 0;
    st.element = firstUnjudgedElement();
    st.missed = (st.state && st.state.missed || []).slice();
    st.dwell = Date.now();
    st.elementDwell = Date.now();
    renderAll();
    if (st.pass === "elements") focusElement();
    prefetch(index);
  }).catch(function (error) { stageNote(error.message); });
}

function firstUnansweredPass() {
  var answers = st.state && st.state.screen || {};
  if (!(st.state && st.state.missed && st.state.missed.length) &&
      !(st.state && st.state.item && st.state.item.missed_at)) return "missed";
  var screen = questions("screen");
  for (var i = 0; i < screen.length; i++) {
    if (!answers[screen[i].id]) return "screen";
  }
  return "elements";
}

function firstUnansweredScreenQuestion() {
  var answers = (st.state && st.state.screen) || {};
  var list = questions("screen");
  for (var i = 0; i < list.length; i++) {
    if (!answers[list[i].id]) return i;
  }
  return 0;
}

function firstUnjudgedElement() {
  var judged = st.state && st.state.elements || {};
  for (var i = 0; i < st.sampled.length; i++) {
    if (!judged[st.sampled[i]]) return i;
  }
  return 0;
}

function refreshState(indices) {
  return get("/api/audit/state?campaign=" + encodeURIComponent(st.campaign) +
             "&indices=" + indices.join(",")).then(function (body) {
    st.state = body.states[String(st.index)] || null;
    /* Which item `st.state` is about. `st.index` moves the instant the rater
     * does, so without this there is no way to tell a fresh state from the
     * previous item's - and code that assumed otherwise read the wrong one. */
    st.stateIndex = st.index;
    st.progress = body.progress;
    applyProgress();
    return body;
  });
}

function applyProgress() {
  var progress = st.progress || {};
  $("counter").textContent = (st.index + 1) + " / " + st.count +
    "  ·  " + (progress.done || 0) + " done" +
    (progress.skipped ? ", " + progress.skipped + " skipped" : "");
  renderTrack();
}

function prefetch(index) {
  for (var offset = 1; offset <= PREFETCH; offset++) {
    var next = index + offset;
    if (next >= st.count) break;
    /* The item payload has an ETag and no live state in it, and the image is
     * immutable for the capture's mtime, so both of these are reusable rather
     * than merely early. */
    fetchItem(next).catch(function () {});
    var image = new Image();
    image.src = imageUrl(next, null, IMAGE_CAP);
  }
}

function loadImage(index) {
  var image = new Image();
  image.decoding = "async";
  image.onload = function () {
    if (st.index !== index) return;
    st.img = image;
    st.imgScale = st.natural.w ? image.naturalWidth / st.natural.w : 1;
    if (st.fitted) fit();
    draw();
  };
  image.onerror = function () {
    if (st.index === index) stageNote("could not load the screenshot for this item");
  };
  st.img = null;
  image.src = imageUrl(index, null, IMAGE_CAP);
  draw();
}

/* ------------------------------------------------------------- rendering */

function canvasSize() {
  var canvas = $("canvas");
  var ratio = window.devicePixelRatio || 1;
  var width = canvas.clientWidth, height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio) ||
      canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  return { w: width, h: height, ratio: ratio };
}

function fit() {
  var size = canvasSize();
  if (!st.natural.w || !st.natural.h) return;
  var scale = Math.min(size.w / st.natural.w, size.h / st.natural.h);
  st.view.scale = scale;
  st.view.x = (size.w - st.natural.w * scale) / 2;
  st.view.y = (size.h - st.natural.h * scale) / 2;
  st.fitted = true;
  st.detail = null;
  draw();
}

function zoomTo(rect, pad) {
  var size = canvasSize();
  pad = pad || 2.6;
  var w = Math.max(rect.w * pad, 320), h = Math.max(rect.h * pad, 200);
  var scale = Math.min(size.w / w, size.h / h, 8);
  st.view.scale = scale;
  st.view.x = size.w / 2 - (rect.x + rect.w / 2) * scale;
  st.view.y = size.h / 2 - (rect.y + rect.h / 2) * scale;
  st.fitted = false;
  wantDetail();
  draw();
}

function toNatural(clientX, clientY) {
  var box = $("canvas").getBoundingClientRect();
  return {
    x: (clientX - box.left - st.view.x) / st.view.scale,
    y: (clientY - box.top - st.view.y) / st.view.scale
  };
}

/* Ask for real pixels once the view is showing more than the downscaled frame
 * has. The region is quantised so panning a little re-uses the same URL, which
 * is the difference between a cache hit and a fresh crop per mouse move. */
function wantDetail() {
  if (!st.natural.w) return;
  if (st.view.scale <= st.imgScale * DETAIL_TRIGGER) {
    st.detail = null;
    st.detailWanted = null;
    return;
  }
  var size = canvasSize();
  var x = Math.floor(Math.max(0, -st.view.x / st.view.scale) / DETAIL_GRID) * DETAIL_GRID;
  var y = Math.floor(Math.max(0, -st.view.y / st.view.scale) / DETAIL_GRID) * DETAIL_GRID;
  var w = Math.ceil((size.w / st.view.scale) / DETAIL_GRID) * DETAIL_GRID + DETAIL_GRID;
  var h = Math.ceil((size.h / st.view.scale) / DETAIL_GRID) * DETAIL_GRID + DETAIL_GRID;
  w = Math.min(w, st.natural.w - x);
  h = Math.min(h, st.natural.h - y);
  if (w <= 0 || h <= 0) return;
  var key = [x, y, w, h].join(",");
  if (st.detailWanted === key) return;
  st.detailWanted = key;
  if (st.detailTimer) clearTimeout(st.detailTimer);
  var index = st.index;
  st.detailTimer = setTimeout(function () {
    var image = new Image();
    image.onload = function () {
      if (st.index !== index || st.detailWanted !== key) return;
      st.detail = { x: x, y: y, w: w, h: h, img: image };
      draw();
    };
    image.src = imageUrl(index, { x: x, y: y, w: w, h: h }, null, "webp_exact");
  }, 160);
}

function draw() {
  var canvas = $("canvas");
  var size = canvasSize();
  var ctx = canvas.getContext("2d");
  ctx.setTransform(size.ratio, 0, 0, size.ratio, 0, 0);
  ctx.clearRect(0, 0, size.w, size.h);
  ctx.fillStyle = "#14161b";
  ctx.fillRect(0, 0, size.w, size.h);
  if (!st.natural.w) return;

  var scale = st.view.scale;
  ctx.imageSmoothingEnabled = scale < 1.5;
  var frame = (st.showAfter && st.after) ? st.after : st.img;
  if (frame) {
    ctx.drawImage(frame, 0, 0, frame.naturalWidth, frame.naturalHeight,
                  st.view.x, st.view.y, st.natural.w * scale, st.natural.h * scale);
  }
  if (st.detail) {
    ctx.drawImage(st.detail.img, 0, 0, st.detail.img.naturalWidth, st.detail.img.naturalHeight,
                  st.view.x + st.detail.x * scale, st.view.y + st.detail.y * scale,
                  st.detail.w * scale, st.detail.h * scale);
  }

  var blind = st.pass === "missed" && !st.revealed;
  if (st.pass === "sweep") $("board-note").className = st.marking ? "marking" : "";
  if (st.kind === "instruction") {
    /* The dense annotation is not what is being audited here and drawing it
     * during the grounding pass would hand over the answer, so it is off unless
     * asked for and never before the reveal. */
    if (st.opts.boxes && st.revealedTarget) drawBoxes(ctx, scale);
    drawInstructionTarget(ctx, scale);
  } else {
    if (st.opts.boxes && !blind) drawBoxes(ctx, scale);
  }
  drawMissed(ctx, scale);
  drawLoupe();
  $("zoomlevel").textContent = Math.round(scale * 100) + "%" +
    (st.detail ? " · native pixels" : "");
}

function drawBoxes(ctx, scale) {
  var focus = st.pass === "elements" ? st.sampled[st.element]
    : (st.pass === "sweep" ? st.selected : null);
  var judged = st.state && st.state.elements || {};
  for (var i = 0; i < st.elements.length; i++) {
    var element = st.elements[i];
    var isFocus = element.key === focus;
    var isHover = st.hover && element.key === st.hover;
    if (focus && !isFocus && !isHover && st.pass !== "sweep") {
      ctx.globalAlpha = 0.20;
      ctx.lineWidth = 1;
    } else if (focus && !isFocus && !isHover) {
      ctx.globalAlpha = 0.75;
      ctx.lineWidth = 1.2;
    } else {
      ctx.globalAlpha = 1;
      ctx.lineWidth = isFocus ? 3 : 1.4;
    }
    ctx.strokeStyle = isFocus ? "#ffffff" : colourFor(element);
    if (st.pass === "sweep") {
      /* In a sweep the only colour that carries information is "this one is
       * wrong": everything else is correct by not having been flagged, so
       * tinting it would say something the rater has not said. */
      if (st.flagged[element.key]) {
        ctx.strokeStyle = "#ff6b6b";
        ctx.lineWidth = Math.max(ctx.lineWidth, 2.5);
      }
    } else if (judged[element.key]) {
      var values = judged[element.key].values || [];
      ctx.strokeStyle = isFocus ? "#ffffff"
        : (values.length === 1 && values[0] === "ok" ? "#4caf72" : "#e06c6c");
    }
    ctx.setLineDash(element.is_occluded ? [4, 3] : []);
    var rects = boxesOf(element);
    for (var j = 0; j < rects.length; j++) {
      var rect = rects[j];
      ctx.strokeRect(st.view.x + rect.x * scale, st.view.y + rect.y * scale,
                     rect.w * scale, rect.h * scale);
    }
    if (isFocus && element.rect && st.opts.frags) {
      /* The amodal rect beside the visible parts: the point of the geometry
       * question is whether the box claims pixels something else is covering. */
      ctx.save();
      ctx.globalAlpha = 0.85;
      ctx.setLineDash([2, 4]);
      ctx.strokeStyle = "#ffd479";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(st.view.x + element.rect.x * scale, st.view.y + element.rect.y * scale,
                     element.rect.w * scale, element.rect.h * scale);
      ctx.restore();
    }
    if ((st.opts.labels || isFocus) && rects.length) {
      label(ctx, element, rects[0], scale, isFocus);
    }
  }
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
}

function label(ctx, element, rect, scale, isFocus) {
  var text = String(element.type || element.role || "?");
  if (isFocus && element.name) text += " · " + String(element.name).slice(0, 40);
  ctx.font = (isFocus ? "600 12px " : "11px ") + "ui-monospace, monospace";
  var width = ctx.measureText(text).width + 6;
  var x = st.view.x + rect.x * scale;
  var y = st.view.y + rect.y * scale - 14;
  if (y < 2) y = st.view.y + rect.y * scale + 2;
  ctx.fillStyle = isFocus ? "rgba(255,255,255,.92)" : "rgba(20,22,27,.82)";
  ctx.fillRect(x, y, width, 13);
  ctx.fillStyle = isFocus ? "#14161b" : colourFor(element);
  ctx.fillText(text, x + 3, y + 10);
}

function drawInstructionTarget(ctx, scale) {
  var spec = target();
  if (st.guess) {
    var gx = st.view.x + st.guess.x * scale, gy = st.view.y + st.guess.y * scale;
    ctx.beginPath();
    ctx.arc(gx, gy, 9, 0, Math.PI * 2);
    ctx.strokeStyle = "#6ea8fe";
    ctx.lineWidth = 2.5;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(gx - 14, gy); ctx.lineTo(gx + 14, gy);
    ctx.moveTo(gx, gy - 14); ctx.lineTo(gx, gy + 14);
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  if (!st.revealedTarget) return;

  (spec.visible_fragments || []).forEach(function (rect) {
    ctx.strokeStyle = "rgba(255, 212, 121, .55)";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([3, 3]);
    ctx.strokeRect(st.view.x + rect.x * scale, st.view.y + rect.y * scale,
                   rect.w * scale, rect.h * scale);
  });
  ctx.setLineDash([]);
  var box = spec.bbox_px;
  if (box && box.length === 4) {
    ctx.strokeStyle = "#ffd479";
    ctx.lineWidth = 3;
    ctx.strokeRect(st.view.x + box[0] * scale, st.view.y + box[1] * scale,
                   (box[2] - box[0]) * scale, (box[3] - box[1]) * scale);
  }
  var point = spec.point_px;
  if (point && point.length === 2) {
    ctx.beginPath();
    ctx.arc(st.view.x + point[0] * scale, st.view.y + point[1] * scale, 5, 0, Math.PI * 2);
    ctx.fillStyle = "#ffd479";
    ctx.fill();
  }
}

function drawMissed(ctx, scale) {
  for (var i = 0; i < st.missed.length; i++) {
    var point = st.missed[i];
    var x = st.view.x + point.x * scale, y = st.view.y + point.y * scale;
    ctx.beginPath();
    ctx.arc(x, y, 7, 0, Math.PI * 2);
    ctx.lineWidth = 2;
    ctx.strokeStyle = point.inside_key ? "#d8a13a" : "#ff6b6b";
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(x, y, 2, 0, Math.PI * 2);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fill();
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillText(String(i + 1), x + 9, y + 3);
  }
}

function drawLoupe() {
  var loupe = $("loupe");
  if (!st.opts.loupe || !st.cursor || !st.img) {
    loupe.className = "";
    return;
  }
  loupe.className = "on";
  var box = $("canvas").getBoundingClientRect();
  loupe.style.left = Math.round(st.cursor.clientX - box.left + 18) + "px";
  loupe.style.top = Math.round(st.cursor.clientY - box.top - 240) + "px";
  var ctx = loupe.getContext("2d");
  var factor = 6;
  var span = loupe.width / factor;
  var source = st.detail ? st.detail.img : st.img;
  var sourceScale = st.detail ? (st.detail.img.naturalWidth / st.detail.w) : st.imgScale;
  var originX = st.detail ? st.detail.x : 0;
  var originY = st.detail ? st.detail.y : 0;
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = "#14161b";
  ctx.fillRect(0, 0, loupe.width, loupe.height);
  ctx.drawImage(
    source,
    (st.cursor.x - originX - span / 2) * sourceScale,
    (st.cursor.y - originY - span / 2) * sourceScale,
    span * sourceScale, span * sourceScale,
    0, 0, loupe.width, loupe.height);
  ctx.strokeStyle = "rgba(110,168,254,.9)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(loupe.width / 2, 0); ctx.lineTo(loupe.width / 2, loupe.height);
  ctx.moveTo(0, loupe.height / 2); ctx.lineTo(loupe.width, loupe.height / 2);
  ctx.stroke();
}

function stageNote(text) {
  $("board-note").textContent = text || "";
}

/* ------------------------------------------------------------ the panels */

function renderAll() {
  renderPasses();
  renderPrompt();
  renderElement();
  renderAnswers();
  draw();
}

function renderPasses() {
  var host = $("passes");
  clear(host);
  passes().forEach(function (pass) {
    var done = passDone(pass);
    host.appendChild(el("b", {
      class: (pass === st.pass ? "on" : "") + (done ? " done" : ""),
      text: (passes().indexOf(pass) + 1) + " " + pass + (pass === "elements"
        ? " " + judgedCount() + "/" + st.sampled.length : ""),
      onclick: function () { goPass(pass); }
    }));
  });
}

function passDone(pass) {
  if (!st.state) return false;
  if (pass === "sweep") return !!st.state.sweep;
  if (pass === "ground") return !!st.state.ground;
  if (pass === "judge") {
    var answers = st.state.screen || {};
    return questions("screen").every(function (q) {
      return q.optional || !!answers[q.id];
    });
  }
  if (pass === "missed") {
    return !!(st.state.item && st.state.item.missed_at) ||
      (st.state.missed && st.state.missed.length > 0);
  }
  if (pass === "screen") {
    var answers = st.state.screen || {};
    return questions("screen").every(function (q) { return !!answers[q.id]; });
  }
  return judgedCount() >= st.sampled.length && st.sampled.length > 0;
}

function judgedCount() {
  var judged = st.state && st.state.elements || {};
  return st.sampled.filter(function (key) { return !!judged[key]; }).length;
}

function renderPrompt() {
  var title = $("pass-title"), help = $("pass-help"), host = $("options");
  var extra = $("pass-extra");
  clear(host);
  clear(extra);
  $("viewport").classList.toggle("judging",
    !(st.pass === "missed" && !st.revealed) && !(st.pass === "sweep" && st.marking));
  $("viewport").classList.toggle("marking", st.pass === "sweep" && st.marking);

  if (st.pass === "sweep") {
    renderSweep(title, help, host, extra);
    return;
  }

  if (st.pass === "ground" || st.pass === "judge") {
    renderInstruction(title, help, host, extra);
    return;
  }

  if (st.pass === "missed") {
    title.textContent = "1 · anything with no box?";
    help.textContent = st.revealed
      ? "Revealed. Amber points landed inside a box that does exist; red ones did not. "
        + "Press Enter for pass 2 — still this same screenshot."
      : "Boxes are hidden on purpose — you cannot see what is not drawn. " +
        "Click every visible element that has no annotation, then press Enter. " +
        "Most screens have none: just press Enter. All three passes are about " +
        "this one screenshot; the next item comes after pass 3.";
    extra.appendChild(el("p", {
      class: "muted",
      text: st.missed.length + " point" + (st.missed.length === 1 ? "" : "s") +
        " marked. Click a point again to remove it."
    }));
    host.appendChild(el("button", {
      text: st.revealed ? "continue → screen" : "nothing missing / done  ⏎",
      onclick: commitMissed
    }));
    if (st.missed.length && !st.revealed) {
      host.appendChild(el("button", {
        class: "ghost", text: "clear the points",
        onclick: function () { st.missed = []; renderAll(); }
      }));
    }
    return;
  }

  var question = activeQuestion();
  if (!question) {
    title.textContent = "done";
    help.textContent = "";
    return;
  }
  if (st.pass === "elements" && !st.sampled.length) {
    title.textContent = "3 · no elements to judge";
    help.textContent = "None of this item's sampled element keys resolve in the " +
      "capture on disk, so there is nothing to answer. That is itself a finding: " +
      "it means the annotation changed under the queue.";
    host.appendChild(el("button", { text: "mark the item done  ⏎", onclick: markDone }));
    return;
  }
  if (st.pass === "screen") {
    title.textContent = "2 · " + question.prompt;
    help.textContent = question.help || "";
  } else {
    var key = st.sampled[st.element];
    title.textContent = "3 · " + (question.prompt || "judge this element");
    help.textContent = key
      ? "Element " + (st.element + 1) + " of " + st.sampled.length +
        ". One key is enough; flags add up and Enter commits."
      : "No sampled elements resolve in this capture.";
  }

  var previous = currentAnswer(question);
  (question.options || []).forEach(function (option) {
    var picked = st.pending.indexOf(option.value) >= 0;
    var was = previous && (previous.values || [previous.value]).indexOf(option.value) >= 0;
    host.appendChild(el("button", {
      class: (picked ? "picked" : "") + (was ? " was" : ""),
      onclick: function () { chooseOption(question, option); }
    }, [
      el("kbd", { text: option.key || "" }),
      el("span", { text: option.label || option.value }),
      was ? el("span", { class: "muted tiny", text: "· recorded" }) : null
    ]));
  });
  if (question.multi) {
    host.appendChild(el("button", {
      text: "commit " + (st.pending.length ? st.pending.join(" + ") : "") + "  ⏎",
      onclick: function () { commitPending(question); }
    }));
  }
}

function target() {
  return (st.item && st.item.target) || {};
}

function renderInstruction(title, help, host, extra) {
  var ground = st.state && st.state.ground;

  extra.appendChild(el("blockquote", { class: "instruction",
                                       text: st.item.instruction || "(no instruction)" }));

  if (st.pass === "ground") {
    title.textContent = "1 · where would you click?";
    help.textContent = st.revealedTarget
      ? "Revealed. Blue is where you clicked, amber is the recorded target."
      : "Read the instruction and click the thing it asks for. The target is " +
        "hidden on purpose — an instruction you cannot act on is the finding, " +
        "and asking whether one is clear invites a kinder answer than acting " +
        "on it does.";
    if (st.revealedTarget && ground) {
      extra.appendChild(el("p", {
        class: ground.gave_up ? "verdict miss"
          : (ground.in_bbox ? "verdict hit" : "verdict miss"),
        text: ground.gave_up ? "you could not find it"
          : (ground.in_bbox ? "your click is inside the recorded target"
             : "your click is outside the recorded target" +
               (ground.distance_px != null
                ? " (" + Math.round(ground.distance_px) + " px away)" : ""))
      }));
      host.appendChild(el("button", { class: "primary",
                                      text: "on to the questions  \u23ce",
                                      onclick: function () { goPass("judge"); } }));
      return;
    }
    host.appendChild(el("button", {
      class: st.guess ? "primary" : "",
      text: st.guess ? "that is my answer  \u23ce" : "click the screen first",
      onclick: commitGround
    }));
    host.appendChild(el("button", {
      class: "ghost",
      onclick: function () { commitGround(true); }
    }, [el("kbd", { text: "0" }),
        el("span", { text: "I cannot find it from this instruction" })]));
    return;
  }

  var list = questions("screen");
  var answered = st.state && st.state.screen || {};
  var anyAnswer = Object.keys(answered).length > 0;

  /* The common case is that nothing is wrong, so that is the one keystroke.
   * The detail only opens when the rater says something is, or presses the key
   * for the thing that is - which is the key they would have pressed anyway. */
  if (!st.expanded && !anyAnswer) {
    title.textContent = "is this sample correct?";
    help.textContent = "Enter accepts it. Press the key for anything that is "
      + "wrong instead" + (detailKey() ? ", or " + detailKey() +
        " to see all the questions." : ".");
    var accepts = acceptAnswers();
    host.appendChild(el("button", {
      class: "primary", text: "everything is correct  \u23ce",
      onclick: acceptSample
    }));
    var detail = detailKey();
    host.appendChild(el("button", {
      class: "ghost", onclick: function () { st.expanded = true; renderAll(); }
    }, [detail ? el("kbd", { text: detail }) : null,
        el("span", { text: "something is wrong" })]));
    var says = el("div", { class: "accepts" });
    says.appendChild(el("p", { class: "muted",
                              text: "Enter records all of this:" }));
    accepts.forEach(function (answer) {
      says.appendChild(el("div", { class: "a" }, [
        el("span", { text: answer.question }),
        el("b", { text: answer.label })
      ]));
    });
    extra.appendChild(says);
    if (st.item.after_source_path) {
      extra.appendChild(el("button", {
        class: st.showAfter ? "picked" : "ghost",
        onclick: toggleAfter
      }, [el("kbd", { text: "o" }),
          el("span", { text: st.showAfter ? "showing the screen after the click"
                                          : "show the screen after the click" })]));
    }
    return;
  }

  var question = activeQuestion();
  var step = st.grounding ? "2 · " : "";
  title.textContent = step + (question ? question.prompt : "done");
  help.textContent = question ? (question.help || "") : "";
  if (question) {
    var previous = (st.state && st.state.screen || {})[question.id];
    (question.options || []).forEach(function (option) {
      var was = previous && previous.value === option.value;
      host.appendChild(el("button", {
        class: was ? "was" : "",
        onclick: function () { chooseOption(question, option); }
      }, [el("kbd", { text: option.key || "" }),
          el("span", { text: option.label || option.value }),
          was ? el("span", { class: "muted tiny", text: "· recorded" }) : null]));
    });
    if (question.optional) {
      host.appendChild(el("button", {
        class: "ghost", text: "skip this question",
        onclick: function () { advanceScreenQuestion(); }
      }));
    }
  } else {
    host.appendChild(el("button", { class: "primary", text: "next item  \u23ce",
                                    onclick: nextItem }));
  }

  var progressRow = el("div", { class: "qprogress" });
  list.forEach(function (each, position) {
    var answered = (st.state && st.state.screen || {})[each.id];
    progressRow.appendChild(el("i", {
      class: (position === st.question ? "here " : "") + (answered ? "done" : ""),
      title: each.prompt,
      onclick: function () { st.question = position; renderAll(); }
    }));
  });
  extra.appendChild(progressRow);

  if (st.item.after_source_path) {
    extra.appendChild(el("button", {
      class: st.showAfter ? "picked" : "ghost",
      onclick: toggleAfter
    }, [el("kbd", { text: "o" }),
        el("span", { text: st.showAfter ? "showing the screen after the click"
                                        : "show the screen after the click" })]));
  }
}

//: Preference order for "open the detail". The rubric may claim any letter -
//: the instruction one takes w for "a different element" - so the key is
//: computed and the button shows whichever it ended up with.
var DETAIL_KEYS = ["w", "t", "y", "i", "v", "6"];

function detailKey() {
  var claimed = allClaimedKeys();
  for (var i = 0; i < DETAIL_KEYS.length; i++) {
    if (!claimed[DETAIL_KEYS[i]] && !TOGGLES[DETAIL_KEYS[i]]) return DETAIL_KEYS[i];
  }
  return null;
}

function acceptAnswers() {
  var out = [];
  (st.rubric && st.rubric.questions || []).forEach(function (question) {
    if (question.scope !== "screen") return;
    var chosen = (question.options || []).filter(function (option) {
      return option.accept;
    })[0];
    if (chosen) {
      out.push({ question: question.id, value: chosen.value,
                 label: chosen.label || chosen.value });
    }
  });
  return out;
}

function acceptSample() {
  if (st.readOnly) { saved("read-only", "err"); return; }
  var accepts = acceptAnswers();
  if (!accepts.length) { hint("this rubric marks no answer as 'nothing wrong'"); return; }
  var answers = accepts.map(function (answer) {
    return { scope: "screen", question: answer.question, value: answer.value,
             accepted: true };
  });
  answers.push({ scope: "item", status: "done", dwell_ms: Date.now() - st.dwell });
  queueWriteMany(answers, function () {
    saved("accepted", "ok");
    nextItem();
  });
}

function toggleAfter() {
  st.showAfter = !st.showAfter;
  if (st.showAfter && !st.after) {
    var image = new Image();
    var index = st.index;
    image.onload = function () {
      if (st.index === index) { st.after = image; draw(); }
    };
    image.src = imageUrl(index, null, IMAGE_CAP) + "&which=after";
  }
  renderAll();
}

function commitGround(gaveUp) {
  if (st.readOnly) { saved("read-only", "err"); return; }
  if (!gaveUp && !st.guess) { hint("click the screen where the instruction points"); return; }
  var payload = { scope: "ground", dwell_ms: Date.now() - st.dwell };
  if (gaveUp) payload.gave_up = true;
  payload.point = gaveUp ? { x: 0, y: 0 } : { x: Math.round(st.guess.x),
                                              y: Math.round(st.guess.y) };
  queueWrite(payload, function (body) {
    var ground = (body.state || {}).ground;
    st.guess = ground ? ground.point : st.guess;
    st.revealedTarget = true;
    renderAll();
  });
}

function advanceScreenQuestion() {
  var list = questions("screen");
  if (st.question < list.length - 1) {
    st.question += 1;
    st.dwell = Date.now();
    renderAll();
  } else {
    markDone();
  }
}

function renderSweep(title, help, host, extra) {
  var flagged = Object.keys(st.flagged);
  var element = st.selected ? st.byKey[st.selected] : null;

  if (element) {
    title.textContent = element.type || element.role || "element";
    help.textContent = (element.name || "(no name)") +
      " — press a flag, or a again to clear it. Esc deselects.";
    var question = elementQuestion();
    (question && question.options || []).forEach(function (option) {
      var on = (st.flagged[st.selected] || []).indexOf(option.value) >= 0;
      var pendingOn = st.pending.indexOf(option.value) >= 0;
      host.appendChild(el("button", {
        class: (pendingOn ? "picked" : "") + (on ? " was" : ""),
        onclick: function () { flagSelected(option.value); }
      }, [
        el("kbd", { text: option.key || "" }),
        el("span", { text: option.label || option.value })
      ]));
    });
    return;
  }

  title.textContent = "the whole screen";
  help.textContent = st.marking
    ? "Marking what is MISSING: click where an element should be and has no box. " +
      "Press m again to go back to flagging."
    : "Every box is drawn. Click any box that is wrong, mark anything missing " +
      "with m, then declare the rest correct. That records a verdict for all " +
      String(st.elements.length) + " elements on this screen, not a sample of them.";

  host.appendChild(el("button", {
    class: "primary",
    text: "the rest is correct — " + (st.elements.length - flagged.length) +
      " elements  \u23ce",
    onclick: commitSweep
  }));
  host.appendChild(el("button", {
    class: st.marking ? "picked" : "ghost",
    onclick: function () { st.marking = !st.marking; renderAll(); }
  }, [el("kbd", { text: "m" }),
      el("span", { text: st.marking ? "marking what is missing" : "mark something missing" })]));

  var summary = el("div", { class: "sweepstat" });
  summary.appendChild(el("p", {
    text: flagged.length + " flagged wrong · " + st.missed.length + " marked missing · " +
      (st.elements.length - flagged.length) + " would be declared correct"
  }));
  extra.appendChild(summary);

  flagged.forEach(function (key) {
    var row = st.byKey[key];
    extra.appendChild(el("div", {
      class: "a flagged",
      onclick: function () { select(key); }
    }, [
      el("span", { text: row ? (row.type || row.role) : key }),
      el("b", { text: st.flagged[key].join(" + ") })
    ]));
  });

  /* The screen questions stay reachable without leaving the sweep: they are
   * three keystrokes and they are the only judgement about the annotation as a
   * whole rather than element by element. */
  questions("screen").forEach(function (question) {
    var answer = (st.state && st.state.screen || {})[question.id];
    var row = el("div", { class: "screenq" });
    row.appendChild(el("span", { class: "muted", text: question.prompt }));
    var buttons = el("div", { class: "opts" });
    (question.options || []).forEach(function (option) {
      buttons.appendChild(el("button", {
        class: answer && answer.value === option.value ? "was" : "",
        title: question.help || "",
        onclick: function () {
          send({ scope: "screen", question: question.id, value: option.value }, null);
        }
      }, [el("kbd", { text: option.key || "" }),
          el("span", { text: option.label || option.value })]));
    });
    row.appendChild(buttons);
    extra.appendChild(row);
  });
}

function select(key) {
  if (key) (st.opened = st.opened || {})[key] = 1;
  st.selected = key === st.selected ? null : key;
  st.pending = [];
  renderAll();
}

function flagSelected(value) {
  if (!st.selected) return;
  var key = st.selected;
  var element = st.byKey[key] || {};
  var current = st.flagged[key] || [];
  var next;
  if (value === "ok") {
    next = [];
  } else if (current.indexOf(value) >= 0) {
    next = current.filter(function (each) { return each !== value; });
  } else {
    next = current.concat([value]);
  }
  if (next.length) st.flagged[key] = next; else delete st.flagged[key];
  queueWrite({
    scope: "element", question: (elementQuestion() || {}).id || "element",
    values: next.length ? next : ["ok"], element_key: key, source: "sweep",
    element_type: element.type, element_role: element.role,
    element_app: element.app_name, dwell_ms: Date.now() - st.elementDwell
  }, function () {
    st.elementDwell = Date.now();
    renderAll();
  });
}

function census() {
  var out = {};
  st.elements.forEach(function (element) {
    var kind = String(element.type || element.role || "?");
    out[kind] = (out[kind] || 0) + 1;
  });
  return out;
}

function commitSweep() {
  if (st.readOnly) { saved("read-only", "err"); return; }
  var flagged = Object.keys(st.flagged);
  /* The sweep row and the item-done row go out together.
   *
   * They used to be two requests, the second sent from the first's callback -
   * and `queueWrite` stamps the item index when it runs, so pressing Enter and
   * moving on before the callback fired put the done row on the *next* item.
   * That is not a hypothetical: it happened 22 times in a 200-screen campaign,
   * leaving 14 items with several done rows and 3 with none. One request has no
   * window to race in.
   *
   * No elapsed time on a sweep: a screen can sit open across an interruption,
   * so the number would not measure what a reader would take it to mean. */
  queueWriteMany([
    { scope: "sweep", n_elements: st.elements.length, flagged: flagged,
      census: census(), opened: Object.keys(st.opened || {}).length },
    { scope: "item", status: "done" }
  ], function () {
    saved("screen swept", "ok");
    renderAll();
  });
}

function currentAnswer(question) {
  if (!st.state) return null;
  if (question.scope === "screen") return (st.state.screen || {})[question.id] || null;
  var key = st.sampled[st.element];
  return key ? (st.state.elements || {})[key] || null : null;
}

function renderElement() {
  var section = $("element"), table = $("element-table");
  var key = st.pass === "sweep" ? st.selected
    : (st.pass === "elements" && st.sampled.length ? st.sampled[st.element] : null);
  if (!key) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  $("element-count").textContent = st.pass === "sweep"
    ? "selected" : (st.element + 1) + " / " + st.sampled.length;
  var element = st.byKey[key];
  clear(table);
  if (!element) return;
  var rows = [
    ["class", element.type],
    ["role", element.role],
    ["name", element.name],
    ["visible text", element.visible_text],
    ["text status", element.visible_text_status],
    ["app", element.app_name],
    ["box", element.rect ? [element.rect.x, element.rect.y, element.rect.w, element.rect.h].join(", ") : ""],
    ["visible parts", (element.visible_fragments || []).length],
    ["occlusion", element.occlusion_state],
    ["reading order", element.reading_order_index],
    ["key", element.key]
  ];
  rows.forEach(function (row) {
    if (row[1] === undefined || row[1] === null || row[1] === "") return;
    table.appendChild(el("tr", {}, [
      el("td", { text: row[0] }), el("td", { text: String(row[1]) })
    ]));
  });
}

function renderAnswers() {
  var host = $("answer-list");
  clear(host);
  if (!st.state) return;
  if (st.kind === "instruction") {
    var ground = st.state.ground;
    host.appendChild(el("div", { class: "a" }, [
      el("span", { text: "your click" }),
      el("b", { text: !ground ? "—" : (ground.gave_up ? "could not find it"
                                       : (ground.in_bbox ? "on target" : "off target")) })
    ]));
    questions("screen").forEach(function (question) {
      var answer = (st.state.screen || {})[question.id];
      host.appendChild(el("div", {
        class: "a",
        onclick: function () {
          goPass("judge");
          st.question = questions("screen").indexOf(question);
          renderAll();
        }
      }, [
        el("span", { text: question.id }),
        el("b", { text: answer ? answer.value : "—" })
      ]));
    });
    if (st.state.other_raters && st.state.other_raters.length) {
      host.appendChild(el("div", { class: "a" }, [
        el("span", { text: "also rated by" }),
        el("b", { text: st.state.other_raters.join(", ") })
      ]));
    }
    return;
  }
  if (st.mode === "sweep") {
    var sweep = st.state.sweep;
    host.appendChild(el("div", { class: "a" }, [
      el("span", { text: "swept" }),
      el("b", { text: sweep ? (sweep.declared_correct + " of " + sweep.n_elements +
                               " declared correct") : "—" })
    ]));
    host.appendChild(el("div", { class: "a" }, [
      el("span", { text: "flagged wrong" }),
      el("b", { text: String(Object.keys(st.flagged).length) })
    ]));
    host.appendChild(el("div", { class: "a" }, [
      el("span", { text: "marked missing" }),
      el("b", { text: String((st.state.missed || []).length) })
    ]));
    if (st.state.other_raters && st.state.other_raters.length) {
      host.appendChild(el("div", { class: "a" }, [
        el("span", { text: "also rated by" }),
        el("b", { text: st.state.other_raters.join(", ") })
      ]));
    }
    return;
  }
  questions("screen").forEach(function (question) {
    var answer = (st.state.screen || {})[question.id];
    host.appendChild(el("div", { class: "a" }, [
      el("span", { text: question.id }),
      el("b", { text: answer ? (answer.value || (answer.values || []).join("+")) : "—" })
    ]));
  });
  host.appendChild(el("div", { class: "a" }, [
    el("span", { text: "missed points" }),
    el("b", { text: String((st.state.missed || []).length) })
  ]));
  host.appendChild(el("div", { class: "a" }, [
    el("span", { text: "elements judged" }),
    el("b", { text: judgedCount() + " / " + st.sampled.length })
  ]));
  var judged = st.state.elements || {};
  st.sampled.forEach(function (key, position) {
    var answer = judged[key];
    var element = st.byKey[key];
    host.appendChild(el("div", {
      class: "a",
      onclick: function () { goPass("elements"); st.element = position; st.pending = []; renderAll(); }
    }, [
      el("span", { text: (position + 1) + " " + (element ? (element.type || element.role) : "?") }),
      el("b", { text: answer ? (answer.values || []).join(" + ") : "—" })
    ]));
  });
  if (st.state.other_raters && st.state.other_raters.length) {
    host.appendChild(el("div", { class: "a" }, [
      el("span", { text: "also rated by" }),
      el("b", { text: st.state.other_raters.join(", ") })
    ]));
  }
}

function renderFacts(body) {
  var table = $("fact-table");
  clear(table);
  var item = body.item;
  if (body.kind === "instruction") {
    var spec = item.target || {};
    [["instruction", item.instruction],
     ["style", item.style],
     ["target class", spec.kind || spec.role],
     ["target text", spec.text],
     ["target app", spec.app],
     ["box covers", spec.area_share == null ? null
       : (100 * spec.area_share).toFixed(2) + "% of the screen"],
     ["visible parts", (spec.visible_fragments || []).length],
     ["sample", item.sample_id],
     ["transition", item.transition_id],
     ["size band", (item.stratum || {}).target_size]
    ].forEach(function (row) {
      if (row[1] === undefined || row[1] === null || row[1] === "") return;
      table.appendChild(el("tr", {}, [
        el("td", { text: row[0] }), el("td", { text: String(row[1]) })
      ]));
    });
    renderAutomated(body);
    return;
  }
  var rows = [
    ["key", item.observation_key],
    ["split", item.stratum.split],
    ["theme", item.stratum.theme],
    ["size", item.width + "x" + item.height],
    ["apps", (item.apps || []).join(", ")],
    ["elements", item.n_elements + (body.elements.length !== item.n_elements
      ? " (" + body.elements.length + " now)" : "")],
    ["windows", item.n_windows],
    ["occluded", item.occluded_ratio],
    ["drawn from", item.draw]
  ];
  rows.forEach(function (row) {
    table.appendChild(el("tr", {}, [
      el("td", { text: row[0] }), el("td", { text: String(row[1]) })
    ]));
  });
  if (body.sampled_missing && body.sampled_missing.length) {
    table.appendChild(el("tr", {}, [
      el("td", { text: "missing" }),
      el("td", { text: body.sampled_missing.length + " sampled elements no longer resolve" })
    ]));
  }

  renderAutomated(body);
}

function renderAutomated(body) {
  var item = body.item;
  var automated = $("automated-body");
  clear(automated);
  var verdict = (body.automated || {}).verdict;
  var pixel = (body.automated || {}).shard_pixel_audit;
  var flat = el("table");
  if (verdict) {
    flat.appendChild(row2("verdict", verdict.status +
      (verdict.failed_checks && verdict.failed_checks.length
        ? " (" + verdict.failed_checks.join(", ") + ")" : "")));
  }
  Object.keys(pixel || {}).forEach(function (key) {
    flat.appendChild(row2(key.replace(/_/g, " "), String(pixel[key])));
  });
  automated.appendChild(flat);

  var tag = $("screentag-body");
  clear(tag);
  tag.appendChild(el("pre", { text: body.screentag || "(no screentag.txt)" }));

  var provenance = $("provenance-body");
  clear(provenance);
  var table2 = el("table");
  table2.appendChild(row2("source", item.source_path));
  Object.keys(item.digests || {}).forEach(function (kind) {
    var digest = item.digests[kind];
    table2.appendChild(row2(kind, digest.sha256.slice(0, 16) + "…  " +
      Math.round(digest.bytes / 1024) + " KB" +
      (digest.release_sha256_match === true ? "  ✓ matches the release"
        : digest.release_sha256_match === false ? "  ✗ DIFFERS from the release" : "")));
  });
  provenance.appendChild(table2);
}

function row2(a, b) {
  return el("tr", {}, [el("td", { text: a }), el("td", { text: b })]);
}

function renderTrack() {
  var host = $("track");
  clear(host);
  var done = {}, skipped = {};
  (st.progress && st.progress.done_indices || []).forEach(function (i) { done[i] = 1; });
  (st.progress && st.progress.skipped_indices || []).forEach(function (i) { skipped[i] = 1; });
  for (var i = 0; i < st.count; i++) {
    host.appendChild(el("i", {
      class: i === st.index ? "here" : (done[i] ? "done" : (skipped[i] ? "skipped" : "")),
      title: "item " + (i + 1),
      "data-index": i
    }));
  }
}

/* --------------------------------------------------------------- answering */

function chooseOption(question, option) {
  if (st.readOnly) { saved("read-only", "err"); return; }
  if (!question.multi) {
    send({ scope: question.scope === "screen" ? "screen" : "element",
           question: question.id, value: option.value }, question);
    return;
  }
  var exclusive = question.exclusive || [];
  var at = st.pending.indexOf(option.value);
  if (at >= 0) {
    st.pending.splice(at, 1);
  } else if (exclusive.indexOf(option.value) >= 0) {
    /* `ok` is the common case and the one that must be one keystroke: it is
     * exclusive by definition, so choosing it commits immediately. */
    st.pending = [option.value];
    commitPending(question);
    return;
  } else {
    st.pending = st.pending.filter(function (value) {
      return exclusive.indexOf(value) < 0;
    });
    st.pending.push(option.value);
  }
  renderPrompt();
}

function commitPending(question) {
  if (!st.pending.length) { saved("pick a flag first", "busy"); return; }
  send({ scope: "element", question: question.id, values: st.pending.slice() }, question);
  st.pending = [];
}

/* Every write goes through here, so `st.saving` is the truth about whether
 * anything is in flight. It used to be the truth about *some* writes: the
 * item-done row went out on its own and the indicator could read "saved" while
 * it was still on the wire, which is exactly the row you cannot afford to lose
 * because it is what makes the item count as finished. */
function queueWrite(payload, done) {
  payload.campaign = st.campaign;
  // Stamped now, not when the chain reaches this write: `st.index` moves as
  // soon as the rater does, and a write that read it late landed on the wrong
  // item.
  if (payload.index === undefined) payload.index = st.index;
  payload.rater = st.rater;
  payload.rubric_ref = st.rubricRef;
  payload.event_id = payload.event_id || eventId();
  st.saving += 1;
  saved("saving…", "busy");
  st.chain = st.chain.then(function () {
    return post("/api/audit/label", payload).then(function (body) {
      if (body.state) st.state = body.state;
      if (body.progress) st.progress = body.progress;
      applyProgress();
      st.saving -= 1;
      if (!st.saving) saved("saved", "ok");
      if (done) done(body);
    });
  }).catch(function (error) {
    st.saving = Math.max(0, st.saving - 1);
    saved(error.message.slice(0, 60), "err");
    stageNote(error.message);
  });
  return st.chain;
}

/* Several answers in one request. Accepting a sample is five judgements and an
 * item-done row; six sequential round trips per item is the latency this page
 * exists to avoid. */
function queueWriteMany(answers, done) {
  var index = st.index;
  st.saving += 1;
  saved("saving…", "busy");
  st.chain = st.chain.then(function () {
    return post("/api/audit/label", {
      campaign: st.campaign, index: index, rater: st.rater,
      rubric_ref: st.rubricRef,
      answers: answers.map(function (answer) {
        return Object.assign({ event_id: eventId(), index: index }, answer);
      })
    }).then(function (body) {
      if (body.state) st.state = body.state;
      if (body.progress) st.progress = body.progress;
      applyProgress();
      st.saving -= 1;
      if (!st.saving) saved("saved", "ok");
      if (done) done(body);
    });
  }).catch(function (error) {
    st.saving = Math.max(0, st.saving - 1);
    saved(error.message.slice(0, 60), "err");
    stageNote(error.message);
  });
  return st.chain;
}

function send(payload, question) {
  if (payload.scope === "element") {
    var key = st.sampled[st.element];
    var element = st.byKey[key] || {};
    payload.element_key = key;
    payload.element_type = element.type;
    payload.element_role = element.role;
    payload.element_app = element.app_name;
    payload.dwell_ms = Date.now() - st.elementDwell;
  } else {
    payload.dwell_ms = Date.now() - st.dwell;
  }
  return queueWrite(payload, function () { advance(question); });
}

function eventId() {
  return (Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
}

function advance(question) {
  if (!question) { renderAll(); return; }
  if (question.scope === "screen") {
    var list = questions("screen");
    if (st.question < list.length - 1) {
      st.question += 1;
      st.dwell = Date.now();
    } else if (st.kind === "instruction") {
      markDone();
      renderAll();
      return;
    } else if (st.pass === "sweep") {
      st.question = 0;
    } else {
      goPass("elements");
      return;
    }
  } else {
    if (st.element < st.sampled.length - 1) {
      st.element += 1;
      st.elementDwell = Date.now();
      focusElement();
    } else {
      // Pinned to the item the answer was about, not to whatever is on screen
      // by the time this write reaches the front of the queue.
      markDone(st.index);
    }
  }
  renderAll();
}

function focusElement() {
  var element = st.byKey[st.sampled[st.element]];
  if (!element) return;
  var rects = boxesOf(element);
  var rect = rects.length ? rects[0] : element.rect;
  if (rect) zoomTo(rect);
}

function goPass(pass) {
  st.pass = pass;
  st.pending = [];
  st.question = 0;
  if (pass === "ground" || pass === "judge") {
    if (pass === "judge") st.question = firstUnansweredScreenQuestion();
    st.dwell = Date.now();
    renderAll();
    return;
  }
  if (pass === "sweep") { st.selected = null; st.marking = false; fit(); renderAll(); return; }
  if (pass === "missed") {
    st.revealed = passDone("missed");
    fit();
  }
  if (pass === "screen") { st.dwell = Date.now(); fit(); }
  if (pass === "elements") {
    st.element = firstUnjudgedElement();
    st.elementDwell = Date.now();
    focusElement();
  }
  renderAll();
}

function commitMissed() {
  if (st.revealed) { goPass("screen"); return; }
  if (st.readOnly) { saved("read-only", "err"); return; }
  var points = st.missed.map(function (point) {
    var inside = elementAt(point.x, point.y);
    return { x: Math.round(point.x), y: Math.round(point.y),
             inside_key: inside ? inside.key : null };
  });
  queueWrite({ scope: "missed", points: points, dwell_ms: Date.now() - st.dwell },
             function (body) {
    st.missed = ((body.state || {}).missed || []).slice();
    st.revealed = true;
    renderAll();
  });
}

function markDone(index) {
  if (st.readOnly) return;
  var payload = { scope: "item", status: "done" };
  if (index !== undefined) payload.index = index;
  return queueWrite(payload, function () {
    saved("item done", "ok");
    renderAll();
  });
}

function skipItem() {
  if (st.readOnly) { saved("read-only", "err"); return; }
  var reason = window.prompt("Skip this item. Why? (recorded with the skip)");
  if (!reason) return;
  return queueWrite({ scope: "item", status: "skipped", reason: reason },
                    function () { nextItem(); });
}

function nextItem() {
  var progress = st.progress || {};
  var done = {}, skipped = {};
  (progress.done_indices || []).forEach(function (i) { done[i] = 1; });
  (progress.skipped_indices || []).forEach(function (i) { skipped[i] = 1; });
  for (var i = st.index + 1; i < st.count; i++) {
    if (!done[i] && !skipped[i]) return openItem(i);
  }
  if (progress.next !== null && progress.next !== undefined) return openItem(progress.next);
  return openItem(Math.min(st.index + 1, st.count - 1));
}

function back() {
  /* Not a retract: the log is append-only and the last answer to a question is
   * the one that counts, so stepping back and answering again supersedes the
   * previous answer and keeps both, with timestamps. */
  st.pending = [];
  if (st.pass === "elements" && st.element > 0) { st.element -= 1; focusElement(); }
  else if (st.pass === "elements") goPass("screen");
  else if (st.pass === "screen" && st.question > 0) st.question -= 1;
  else if (st.pass === "screen") goPass("missed");
  renderAll();
}

/* ------------------------------------------------------------- interaction */

function onCanvasClick(event) {
  var point = toNatural(event.clientX, event.clientY);
  if (point.x < 0 || point.y < 0 || point.x >= st.natural.w || point.y >= st.natural.h) return;
  if (st.pass === "ground" && !st.revealedTarget) {
    st.guess = { x: point.x, y: point.y };
    renderAll();
    return;
  }
  if (st.pass === "sweep") {
    if (st.marking) {
      toggleMissedPoint(point);
      renderAll();
      return;
    }
    var hit = elementAt(point.x, point.y);
    if (!hit) { hint("no box here — press m to mark it as missing"); return; }
    select(hit.key);
    return;
  }
  if (st.pass === "missed" && !st.revealed) {
    toggleMissedPoint(point);
    renderAll();
    return;
  }
  var element = elementAt(point.x, point.y);
  if (!element) return;
  var at = st.sampled.indexOf(element.key);
  if (at >= 0) {
    goPass("elements");
    st.element = at;
    st.pending = [];
    st.elementDwell = Date.now();
    renderAll();
  } else {
    st.hover = element.key;
    stageNote(element.type + " · " + (element.name || "(no name)") +
              " — not one of this item's sampled elements");
    draw();
  }
}

/* Click near an existing point to take it back, otherwise add one. */
function toggleMissedPoint(point) {
  for (var i = 0; i < st.missed.length; i++) {
    var existing = st.missed[i];
    var dx = (existing.x - point.x) * st.view.scale;
    var dy = (existing.y - point.y) * st.view.scale;
    if (dx * dx + dy * dy < 100) { st.missed.splice(i, 1); return; }
  }
  st.missed.push({ x: point.x, y: point.y });
  if (st.pass === "sweep") saveMissed();
}

/* A sweep has no separate commit for the missing marks, so each one is stored
 * as it is made rather than waiting for a pass that never comes. */
function saveMissed() {
  var points = st.missed.map(function (point) {
    var inside = elementAt(point.x, point.y);
    return { x: Math.round(point.x), y: Math.round(point.y),
             inside_key: inside ? inside.key : null };
  });
  queueWrite({ scope: "missed", points: points, dwell_ms: Date.now() - st.dwell },
             function (body) {
    st.missed = ((body.state || {}).missed || []).slice();
    renderAll();
  });
}

function bindBoard() {
  var viewport = $("viewport");
  var canvas = $("canvas");
  canvas.addEventListener("click", onCanvasClick);
  canvas.addEventListener("mousemove", function (event) {
    var point = toNatural(event.clientX, event.clientY);
    st.cursor = { x: point.x, y: point.y, clientX: event.clientX, clientY: event.clientY };
    if (st.opts.loupe) drawLoupe();
  });
  canvas.addEventListener("mouseleave", function () { st.cursor = null; drawLoupe(); });
  canvas.addEventListener("wheel", function (event) {
    event.preventDefault();
    var box = canvas.getBoundingClientRect();
    var cx = event.clientX - box.left, cy = event.clientY - box.top;
    var before = { x: (cx - st.view.x) / st.view.scale, y: (cy - st.view.y) / st.view.scale };
    var factor = Math.exp(-event.deltaY * 0.0016);
    st.view.scale = Math.max(0.05, Math.min(st.view.scale * factor, 16));
    st.view.x = cx - before.x * st.view.scale;
    st.view.y = cy - before.y * st.view.scale;
    st.fitted = false;
    wantDetail();
    draw();
  }, { passive: false });

  var dragging = null;
  canvas.addEventListener("mousedown", function (event) {
    if (event.button !== 1 && !(event.button === 0 && event.shiftKey)) return;
    event.preventDefault();
    dragging = { x: event.clientX, y: event.clientY };
    viewport.classList.add("panning");
  });
  window.addEventListener("mousemove", function (event) {
    if (!dragging) return;
    st.view.x += event.clientX - dragging.x;
    st.view.y += event.clientY - dragging.y;
    dragging = { x: event.clientX, y: event.clientY };
    st.fitted = false;
    wantDetail();
    draw();
  });
  window.addEventListener("mouseup", function () {
    dragging = null;
    viewport.classList.remove("panning");
  });
  window.addEventListener("resize", function () {
    if (st.fitted) fit(); else draw();
  });
}

var TOGGLES = {
  b: ["boxes", "opt-boxes"],
  l: ["labels", "opt-labels"],
  v: ["frags", "opt-frags"],
  x: ["loupe", "opt-loupe"]
};

function toggle(name) {
  st.opts[name] = !st.opts[name];
  var entry = Object.keys(TOGGLES).filter(function (k) { return TOGGLES[k][0] === name; })[0];
  if (entry) $(TOGGLES[entry][1]).checked = st.opts[name];
  draw();
}

function onKey(event) {
  if (event.target && /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  var key = event.key;
  if (key === "?" ) { showHelp(true); return; }
  if (key === "Escape") {
    showHelp(false);
    st.pending = [];
    if (st.selected) { st.selected = null; renderAll(); return; }
    if (st.expanded && !Object.keys((st.state && st.state.screen) || {}).length) {
      st.expanded = false;
      renderAll();
      return;
    }
    renderPrompt();
    return;
  }
  if (!$("help").hidden) return;

  var claimed = claimedKeys();
  var lower = key.length === 1 ? key.toLowerCase() : key;
  var shifted = event.shiftKey && key.length === 1;

  /* A rubric is a data file and can claim any letter, so its keys win. The
   * page's own toggles are always reachable with Shift, and the checkbox
   * labels are rewritten to show whichever form is actually free. */
  if (!shifted && claimed[lower]) {
    event.preventDefault();
    if (st.pass === "sweep" && st.selected) flagSelected(claimed[lower].value);
    else chooseOption(activeQuestion(), claimed[lower]);
    return;
  }
  /* On the accept screen the questions are collapsed, so no rubric key is
   * "active" - but pressing the key for the thing that is wrong is the natural
   * way to say so. It opens the detail and answers that question. */
  if (!shifted && st.kind === "instruction" && st.pass === "judge" && !st.expanded) {
    var entries = allClaimedKeys()[lower] || [];
    var screenEntry = entries.filter(function (entry) {
      return entry.question.scope === "screen";
    })[0];
    if (screenEntry) {
      event.preventDefault();
      st.expanded = true;
      st.question = questions("screen").indexOf(screenEntry.question);
      renderAll();
      chooseOption(screenEntry.question, screenEntry.option);
      return;
    }
    if (lower === detailKey()) {
      event.preventDefault();
      st.expanded = true;
      renderAll();
      return;
    }
  }
  if (TOGGLES[lower] && (shifted || !claimed[lower])) {
    event.preventDefault();
    toggle(TOGGLES[lower][0]);
    return;
  }
  switch (lower) {
    case " ":
    case "Enter":
      event.preventDefault();
      if (st.pass === "ground") {
        if (st.revealedTarget) goPass("judge"); else commitGround(false);
      }
      else if (st.pass === "judge" && st.kind === "instruction" && !st.expanded
               && !Object.keys((st.state && st.state.screen) || {}).length) {
        acceptSample();
      }
      else if (st.pass === "judge" && !activeQuestion()) nextItem();
      else if (st.pass === "sweep") {
        if (st.selected) { st.selected = null; renderAll(); }
        else commitSweep();
      }
      else if (st.pass === "missed") commitMissed();
      else if (st.pass === "elements" && !st.sampled.length) markDone();
      else if (activeQuestion() && activeQuestion().multi) commitPending(activeQuestion());
      else nextItem();
      return;
    case "Tab":
      event.preventDefault();
      var order = passes();
      goPass(order[(order.indexOf(st.pass) + (event.shiftKey ? order.length - 1 : 1)) % order.length]);
      return;
    case "Backspace":
      event.preventDefault();
      back();
      return;
    case "ArrowRight": event.preventDefault(); openItem(st.index + 1); return;
    case "ArrowLeft": event.preventDefault(); openItem(st.index - 1); return;
  }
  if (shifted || !claimed[lower]) {
    if (lower === "f") { event.preventDefault(); fit(); return; }
    if (lower === "n") { event.preventDefault(); nextItem(); return; }
    if (lower === "p") { event.preventDefault(); openItem(st.index - 1); return; }
    if (lower === "u") { event.preventDefault(); back(); return; }
    if (lower === "s") { event.preventDefault(); skipItem(); return; }
    if (lower === "g") { event.preventDefault(); $("automated").open = !$("automated").open; return; }
    if (lower === "z") { event.preventDefault(); focusElement(); return; }
    if (lower === "o" && st.kind === "instruction") {
      event.preventDefault();
      toggleAfter();
      return;
    }
    if (lower === "0" && st.pass === "ground" && !st.revealedTarget) {
      event.preventDefault();
      commitGround(true);
      return;
    }
    if (lower === "m" && st.pass === "sweep") {
      event.preventDefault();
      st.marking = !st.marking;
      st.selected = null;
      renderAll();
      return;
    }
  }
  /* Nothing took it. Say so: a key that silently does nothing reads as a broken
   * page, and the commonest case - a pass-2 key pressed during the blind pass -
   * has a specific answer. */
  unhandledKey(lower);
}

function renderHelpKeys() {
  var claimed = {};
  (st.rubric && st.rubric.questions || []).forEach(function (question) {
    (question.options || []).forEach(function (option) {
      if (option.key) {
        claimed[option.key.toLowerCase()] = (claimed[option.key.toLowerCase()] || [])
          .concat([question.id + " → " + (option.label || option.value)]);
      }
    });
  });
  Object.keys(TOGGLES).forEach(function (key) {
    var node = $(TOGGLES[key][1]);
    var kbd = node.parentNode.querySelector("kbd");
    if (kbd) kbd.textContent = claimed[key] ? "⇧" + key.toUpperCase() : key;
  });
  /* A letter the rubric claimed is reachable as ⇧letter and nowhere else, so
   * the sheet has to print the form that actually works rather than the one
   * the code would like to offer. */
  function effective(key) {
    return claimed[key] ? "⇧" + key.toUpperCase() : key;
  }

  var table = $("help-keys");
  clear(table);
  var rows = [
    ["⏎", st.kind === "instruction"
      ? "accept the sample as correct, and move on"
      : "commit / next item"],
    ["Tab", "next pass (⇧Tab previous)"],
    ["← →", "previous / next item"],
    ["Backspace or " + effective("u"), "step back and answer again"],
    [effective("s"), "skip this item, with a reason"],
    [effective("f"), "fit"],
    [effective("b"), "boxes"],
    [effective("l"), "labels"],
    [effective("v"), "visible parts vs whole element"],
    [effective("x"), "loupe"],
    [effective("z"), "zoom to the current element"],
    [effective("g"), "the automated audits for this capture"],
    ["wheel", "zoom at the cursor"], ["⇧drag", "pan"],
    ["Esc", "close this sheet, deselect, or collapse the questions"],
    ["?", "this sheet"]
  ];
  Object.keys(claimed).sort().forEach(function (key) {
    rows.push([key, claimed[key].join("; ")]);
  });
  rows.splice(rows.length - Object.keys(claimed).length, 0,
              ["", "— the rubric's own keys —"]);
  rows.forEach(function (row) {
    table.appendChild(el("tr", {}, [
      el("td", {}, [el("kbd", { text: row[0] })]),
      el("td", { text: row[1] })
    ]));
  });
}

function showHelp(open) { $("help").hidden = !open; }

/* ------------------------------------------------------------------- boot */

function bindChrome() {
  $("campaign").addEventListener("change", function () {
    openCampaign(this.value);
  });
  $("fit").addEventListener("click", fit);
  $("skip").addEventListener("click", skipItem);
  $("back").addEventListener("click", back);
  $("next").addEventListener("click", nextItem);
  $("help-open").addEventListener("click", function () { showHelp(true); });
  $("help-close").addEventListener("click", function () { showHelp(false); });
  Object.keys(TOGGLES).forEach(function (key) {
    var entry = TOGGLES[key];
    $(entry[1]).addEventListener("change", function () {
      st.opts[entry[0]] = this.checked;
      draw();
    });
  });
  $("track").addEventListener("click", function (event) {
    var index = event.target && event.target.getAttribute("data-index");
    if (index !== null && index !== undefined) openItem(parseInt(index, 10));
  });
  window.addEventListener("keydown", onKey);
  bindBoard();
}

/* The offline bundle loads this same file and then replaces the four functions
 * that talk to a server, so bootstrap is a named hook rather than a bare call:
 * the shim has to install its overrides before anything fetches. */
window.auditStart = function () {
  bindChrome();
  return boot();
};

if (!window.AUDIT_DEFER) window.auditStart();
