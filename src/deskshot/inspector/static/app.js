/* DeskShot annotation inspector - vanilla, no build step, no CDN.
 *
 * The edit model mirrors the server's: the sample as served is
 * "source elements + overlay applied", and every local change goes into a
 * pending edit set that is applied the same way. Saving posts that set. The
 * source list is reconstructed from `_golden.original`, which is why the server
 * sends it - without that the client would re-apply edits on top of edits.
 */
(function () {
"use strict";

var cfg = null;
var st = {
  /* Navigation is a tree of folders over `incremental_checks/`: 325 runs under
     141 top-level directories. The tree itself is cheap (one cached-index read,
     no capture opened); a run's samples are fetched only when that run is
     opened, and their thumbnails only when the row scrolls into view. */
  tree: [], open: {}, runSamples: {}, runQuality: {}, pendingRuns: {}, sort: "recent",
  run: null, sample: null,
  view: "leaf", kind: "screenshot",
  fmt: "png", exact: "png",          // lossy / lossless variants the server has
  baseCap: 2048, baseDensity: 1,
  source: [], elements: [], byKey: {},
  edits: { modified: {}, deleted: [], added: [] },
  mark: { status: "unmarked", tags: [], note: "" },
  baseVersion: null, conflict: null,
  dirty: false, selected: null, filter: { role: null, app: null, text: "" },
  zoom: 1, tx: 0, ty: 0, addArmed: false,
  /* The ScreenTag pane and the image are two views of one list. `screentag`
     holds the server's index - the tag text plus the character range each
     element produced - `tagBlock` is the block being read, and `tagPeek` is the
     box a hovered block flashes without disturbing the selection. */
  screentag: null, tagBlock: null, tagPeek: null, tagHover: null, tagNodes: [],
  boxNodes: {}, addSeq: 0
};

/* Set while a tag click drives the selection, so the selection's own "reveal
   the block" reflex does not fight the click that caused it. */
var tagSyncing = false;

var $ = function (id) { return document.getElementById(id); };
function on(node, evt, fn, opts) { node.addEventListener(evt, fn, opts || false); }
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function make(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function qs(params) {
  var out = [];
  for (var k in params) if (params[k] !== null && params[k] !== undefined && params[k] !== "")
    out.push(encodeURIComponent(k) + "=" + encodeURIComponent(params[k]));
  return out.join("&");
}
function api(path, params, opts) {
  var url = path + (params ? "?" + qs(params) : "");
  return fetch(url, opts || {}).then(function (r) {
    return r.json().then(function (body) {
      if (!r.ok) {
        // Keep the body: a 409 carries the overlay that won, and refusing a
        // save is only useful if the UI can say what it lost to.
        var error = new Error(body && body.error ? body.error : r.status);
        error.status = r.status;
        error.body = body;
        throw error;
      }
      return body;
    }, function () { throw new Error("HTTP " + r.status); });
  });
}
function status(msg, cls) {
  var node = $("status");
  node.textContent = msg || "";
  node.className = "dim " + (cls || "");
}
function pct(x, digits) {
  if (x === null || x === undefined) return "-";
  return (100 * x).toFixed(digits === undefined ? 2 : digits) + "%";
}
function shortStem(stem) { return stem.replace(/^scene-/, ""); }

/* ------------------------------------------------------------ edit model */

function sourceOf(merged) {
  /* Undo the overlay the server applied, so local edits apply to source. */
  var out = [];
  for (var i = 0; i < merged.length; i++) {
    var e = merged[i], g = e._golden;
    if (g && g.status === "added") continue;
    var copy = {};
    for (var k in e) if (k !== "_golden") copy[k] = e[k];
    if (g && g.original) for (var f in g.original) copy[f] = g.original[f];
    out.push(copy);
  }
  return out;
}

function applyEdits(source, edits) {
  var deleted = {}, i;
  for (i = 0; i < edits.deleted.length; i++) deleted[edits.deleted[i]] = true;
  var out = [];
  for (i = 0; i < source.length; i++) {
    var src = source[i], key = src._key, item = {};
    for (var k in src) item[k] = src[k];
    var change = edits.modified[key];
    if (change) {
      var original = {};
      for (var f in change) { original[f] = src[f]; item[f] = change[f]; }
      item._golden = { status: "modified", fields: Object.keys(change), original: original };
    }
    if (deleted[key]) item._golden = { status: "deleted", original: (item._golden || {}).original };
    out.push(item);
  }
  for (i = 0; i < edits.added.length; i++) {
    var add = {}, raw = edits.added[i];
    for (var a in raw) add[a] = raw[a];
    add._key = "add:" + raw.golden_id;
    add._golden = { status: "added" };
    out.push(add);
  }
  return out;
}

function rebuild() {
  st.elements = applyEdits(st.source, st.edits);
  st.byKey = {};
  for (var i = 0; i < st.elements.length; i++) st.byKey[st.elements[i]._key] = st.elements[i];
  renderBoxes();
  renderStats();
  renderElement();
  markDirty(st.dirty);
}

function markDirty(flag) {
  st.dirty = flag;
  $("save").disabled = !flag || (cfg && cfg.read_only);
  $("save").className = flag ? "primary" : "";
}

function editCount() {
  return Object.keys(st.edits.modified).length + st.edits.deleted.length + st.edits.added.length;
}

function setField(key, field, value) {
  var src = null;
  for (var i = 0; i < st.source.length; i++) if (st.source[i]._key === key) src = st.source[i];
  if (src === null) {                       // an added element edits in place
    for (var j = 0; j < st.edits.added.length; j++)
      if ("add:" + st.edits.added[j].golden_id === key) st.edits.added[j][field] = value;
  } else {
    var change = st.edits.modified[key] || {};
    if (JSON.stringify(src[field] === undefined ? null : src[field]) === JSON.stringify(value)) {
      delete change[field];                 // back to the pipeline's value: not an edit
    } else {
      change[field] = value;
    }
    /* Moving a rect invalidates the fragment geometry the occlusion stage
       computed for the old one, and coverage is measured as rect UNION
       fragments - so keeping both would credit this element with area the
       person editing it just said it does not cover. The server applies the
       same rule; doing it here too means the boxes, the statistics and the
       document that gets saved all agree before anything is written. */
    if (field === "rect" && (src.visible_fragments || []).length) {
      if (change.rect) change.visible_fragments = [];
      else delete change.visible_fragments;
    }
    if (Object.keys(change).length) st.edits.modified[key] = change;
    else delete st.edits.modified[key];
  }
  markDirty(true);
  rebuild();
}

function revert(key) {
  delete st.edits.modified[key];
  st.edits.deleted = st.edits.deleted.filter(function (k) { return k !== key; });
  st.edits.added = st.edits.added.filter(function (a) { return "add:" + a.golden_id !== key; });
  markDirty(true);
  rebuild();
}

function toggleDelete(key) {
  if (st.edits.added.some(function (a) { return "add:" + a.golden_id === key; })) {
    revert(key);
    st.selected = null;
    return;
  }
  var at = st.edits.deleted.indexOf(key);
  if (at >= 0) st.edits.deleted.splice(at, 1); else st.edits.deleted.push(key);
  markDirty(true);
  rebuild();
}

/* --------------------------------------------------------------- loading */

function loadTree(refresh) {
  status(refresh ? "rescanning…" : "loading runs…", "busy");
  return api("/api/tree", refresh ? { refresh: 1 } : null).then(function (data) {
    st.tree = data.tree || [];
    if (refresh) st.runSamples = {};
    renderTree();
    status("");
    return data;
  }).catch(fail);
}

/* Two independent sequence numbers, because run listings and sample loads race
   in different ways.

   A run listing is keyed by its own run, so several may be in flight at once
   (two folders expanded) and none of them may cancel another - each just fills
   in its own bucket. A sample load is singular: only the newest one may reach
   the screen, or a slow response from the run you just left overwrites the one
   you are looking at.

   The residual bug this replaces was the crossing of the two. Opening a run
   auto-loads its first sample, and a click landing in that window used to lose:
   the click's fetch went out first, then the run listing arrived and fired the
   auto-load on top of it. So the auto-load now compares the sample counter
   against what it was when the listing was asked for, and stands down if
   anything asked for a sample in between. */
var nav = { sample: 0 };

function fetchRunSamples(run, force) {
  if (!force && st.runSamples[run]) return Promise.resolve(st.runSamples[run]);
  if (!force && st.pendingRuns[run]) return st.pendingRuns[run];
  var request = api("/api/run", { run: run })
    .then(function (data) {
      delete st.pendingRuns[run];
      st.runSamples[run] = data.samples;
      st.runQuality[run] = data.quality;
      renderTree();
      if (st.sample && st.sample.run === run) renderAudit();
      return data.samples;
    }, function (err) {
      delete st.pendingRuns[run];
      throw err;
    });
  st.pendingRuns[run] = request;
  return request;
}

/* Open a run in the tree, and - unless the user has meanwhile asked for a
   particular sample - show its first one. */
function openRun(run, autoload) {
  st.open[run] = true;
  var sampleSeq = nav.sample;
  renderTree();
  return fetchRunSamples(run).then(function (samples) {
    if (!autoload || !samples.length) return;
    if (nav.sample !== sampleSeq) return;             // a click won the race
    if (st.sample && st.sample.run === run) return;   // already showing this run
    var wanted = readHash().stem;
    var found = samples.filter(function (s) { return s.stem === wanted; })[0];
    loadSample(run, (found || samples[0]).stem);
  }).catch(fail);
}

function loadSample(run, stem, keepPending) {
  if (!run || !stem) return Promise.resolve();
  if (!keepPending && st.dirty && !confirm("Discard unsaved edits?")) return Promise.resolve();
  var mine = ++nav.sample;
  status("loading sample…", "busy");
  st.run = run;
  st.open[run] = true;
  return api("/api/sample", { run: run, stem: stem, view: st.view }).then(function (data) {
    if (mine !== nav.sample) return;
    st.sample = data;
    st.run = data.run;
    st.screentag = null;
    st.tagBlock = null;
    st.tagPeek = null;
    st.tagHover = null;
    st.tagNodes = [];
    st.baseVersion = data.overlay_version || null;
    st.conflict = null;
    st.source = sourceOf(data.elements);
    st.edits = normaliseEdits(data.overlay);
    st.mark = (data.overlay && data.overlay.mark) || { status: "unmarked", tags: [], note: "" };
    st.selected = null;
    st.addSeq = st.edits.added.length;
    st.filter.role = st.filter.app = st.filter.kind = st.filter.type = null;
    if (data.views.indexOf(st.view) < 0) st.view = data.views[0];
    fillSelect($("view-select"), data.views, st.view);
    fillSelect($("image-kind"), data.image.kinds, st.kind);
    st.kind = $("image-kind").value;
    location.hash = run + "|" + stem;   // the run too, so a reload lands here
    $("run-label").textContent = run;
    markDirty(false);
    rebuild();
    renderAudit();
    renderMeta();
    renderMark();
    renderScreentag();
    renderTree();
    setImage(true);
    status("");
    // Fetched only for somebody who is actually using the tag pane; once they
    // are, every sample they step through arrives with it linked.
    if (tagTabOpen()) loadScreentag();
    if (!st.runSamples[run]) fetchRunSamples(run);
  }).catch(fail);
}

/* "#<run>|<stem>": the stem alone is not an address, because the same stem can
   only be found by first opening the run it lives in. */
function readHash() {
  var raw = decodeURIComponent(location.hash.replace(/^#/, ""));
  var split = raw.lastIndexOf("|");
  if (split < 0) return { run: null, stem: raw };
  return { run: raw.slice(0, split), stem: raw.slice(split + 1) };
}

function normaliseEdits(overlay) {
  var edits = (overlay && overlay.edits) || {};
  return {
    modified: JSON.parse(JSON.stringify(edits.modified || {})),
    deleted: (edits.deleted || []).slice(),
    added: JSON.parse(JSON.stringify(edits.added || []))
  };
}

function fail(err) {
  status(String(err && err.message ? err.message : err), "error");
  console.error(err);
}

function fillSelect(sel, values, current) {
  clear(sel);
  values.forEach(function (value) {
    var opt = make("option", null, value);
    opt.value = value;
    sel.appendChild(opt);
  });
  sel.value = current;
  if (sel.selectedIndex < 0 && values.length) sel.selectedIndex = 0;
}

/* ------------------------------------------------------------------- tree */

function filterText() { return $("sample-filter").value.trim().toLowerCase(); }

function sampleMatches(sample) {
  var text = filterText();
  if (text) {
    var hay = (sample.stem + " " + (sample.apps || []).join(" ") + " " +
               (sample.theme || "") + " " + (sample.seed || "")).toLowerCase();
    if (hay.indexOf(text) < 0) return false;
  }
  var want = $("mark-filter").value;
  if (!want) return true;
  var overlay = sample.overlay, mark = (overlay && overlay.mark && overlay.mark.status) || "unmarked";
  if (want === "marked") return !!overlay;
  if (want === "unmarked") return !overlay || mark === "unmarked";
  return mark === want;
}

function shownSamples(run) {
  return (st.runSamples[run] || []).filter(sampleMatches);
}

/* A folder survives the filter if its own path matches - in which case
   everything under it is shown - or if something under it does. */
function nodeMatches(node) {
  var want = $("mark-filter").value;
  if (want && want !== "unmarked" && !node.marked && !node.edited) return false;
  var text = filterText();
  if (!text) return true;
  if (node.path.toLowerCase().indexOf(text) >= 0) return true;
  for (var i = 0; i < node.children.length; i++) if (nodeMatches(node.children[i])) return true;
  // Only for a run already open: matching on sample text must never make the
  // tree fetch 325 run listings because somebody typed a letter.
  if (node.run && st.runSamples[node.path]) return shownSamples(node.path).length > 0;
  return false;
}

/* Thumbnails are 3KB each and there can be 34 in one run, so they are fetched
   when the row is actually on screen and never during a tree render. */
var thumbObserver = new IntersectionObserver(function (entries) {
  entries.forEach(function (entry) {
    if (!entry.isIntersecting) return;
    var img = entry.target;
    if (img.dataset.src) { img.src = img.dataset.src; delete img.dataset.src; }
    thumbObserver.unobserve(img);
  });
}, { root: null, rootMargin: "300px" });

function twisty(open) {
  return make("span", "twisty", open ? "▾" : "▸");
}

function sortNodes(nodes) {
  var copy = nodes.slice();
  copy.sort(st.sort === "name"
    ? function (a, b) { return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0); }
    : function (a, b) { return (b.mtime || 0) - (a.mtime || 0); });
  return copy;
}

function badge(host, cls, text) { host.appendChild(make("span", "badge " + cls, text)); }

function renderFolder(host, node, depth) {
  // While a filter is on, folders that do not match themselves open so the
  // match inside them is visible. Runs stay shut: opening one costs a request.
  var text = filterText();
  var open = !!st.open[node.path] ||
             (!!text && !node.run && node.path.toLowerCase().indexOf(text) < 0);
  var row = make("div", "tnode" + (st.run === node.path ? " current" : ""));
  row.style.paddingLeft = (4 + depth * 12) + "px";
  row.dataset.path = node.path;
  if (node.run) row.dataset.runNode = "1";
  row.appendChild(twisty(open));
  row.appendChild(make("span", "tname", node.name));
  row.appendChild(make("span", "tcount dim", node.total));
  if (node.marked) badge(row, "verified", node.marked + "✓");
  if (node.edited) badge(row, "edits", node.edited + "✎");
  on(row, "click", function () {
    if (st.open[node.path]) delete st.open[node.path];
    else if (node.run) { openRun(node.path, true); return; }
    else st.open[node.path] = true;
    renderTree();
  });
  host.appendChild(row);
  if (!open) return;
  sortNodes(node.children).forEach(function (child) {
    if (!nodeMatches(child)) return;
    renderFolder(host, child, depth + 1);
  });
  if (node.run) renderSamples(host, node, depth + 1);
}

function renderSamples(host, node, depth) {
  var samples = st.runSamples[node.path];
  if (!samples) {
    var row = make("div", "tnode dim", "loading…");
    row.style.paddingLeft = (4 + depth * 12) + "px";
    host.appendChild(row);
    fetchRunSamples(node.path).catch(fail);
    return;
  }
  var shown = samples.filter(sampleMatches);
  if (!shown.length) {
    var none = make("div", "tnode dim", samples.length ? "no sample matches" : "no samples");
    none.style.paddingLeft = (4 + depth * 12) + "px";
    host.appendChild(none);
    return;
  }
  shown.forEach(function (sample) {
    var active = st.sample && st.sample.run === node.path && st.sample.stem === sample.stem;
    var row = make("div", "sample" + (active ? " active" : ""));
    row.style.paddingLeft = (4 + depth * 12) + "px";
    row.dataset.run = node.path;
    row.dataset.stem = sample.stem;
    var img = make("img");
    img.dataset.src = "/api/image?" + qs({ run: node.path, stem: sample.stem,
                                           max: 220, fmt: st.fmt });
    img.alt = "";
    thumbObserver.observe(img);
    row.appendChild(img);

    var meta = make("div", "meta");
    meta.appendChild(make("div", "name", shortStem(sample.stem)));
    var counts = (sample.counts || {})[st.view];
    meta.appendChild(make("div", "dim",
      (counts === undefined || counts === null ? "?" : counts) + " el · " +
      (sample.width || "?") + "×" + (sample.height || "?")));
    meta.appendChild(make("div", "dim", (sample.apps || []).join(", ")));
    var overlay = sample.overlay;
    if (overlay) {
      var mark = (overlay.mark && overlay.mark.status) || "unmarked";
      if (mark !== "unmarked") meta.appendChild(make("span", "badge " + mark, mark.replace("_", " ")));
      var n = overlay.counts.modified + overlay.counts.deleted + overlay.counts.added;
      if (n) meta.appendChild(make("span", "badge edits", n + " edits"));
    }
    row.appendChild(meta);
    on(row, "click", function () { loadSample(node.path, sample.stem); });
    host.appendChild(row);
  });
}

function renderTree() {
  var host = $("tree");
  clear(host);
  var runs = 0, captures = 0;
  sortNodes(st.tree).forEach(function (node) {
    if (!nodeMatches(node)) return;
    runs += 1;
    captures += node.total;
    renderFolder(host, node, 0);
  });
  var open = st.sample ? shownSamples(st.sample.run).length : 0;
  $("sidebar-foot").textContent =
    runs + " folders · " + captures + " captures" +
    (st.run && st.runSamples[st.run] ? " · " + open + " shown here" : "");
}

/* Expand every folder on the way to the open sample, so a link pasted into a
   message lands with its place in the tree visible. */
function revealRun(run) {
  if (!run) return;
  var parts = run.split("/"), path = "";
  parts.forEach(function (part) {
    path = path ? path + "/" + part : part;
    st.open[path] = true;
  });
}

/* --------------------------------------------------------------- viewer */

function imageSize() {
  var image = st.sample && st.sample.image;
  return { w: (image && image.width) || 0, h: (image && image.height) || 0 };
}

/* Never ask for more pixels than the screen can show. The frame arrives capped
   to the size of the viewer (a 3840x2160 capture is 86KB of WebP at 2048px
   against 681KB of PNG), and anything sharper than that comes later, as a crop
   of just the region being looked at. */
function baseCap() {
  var wrap = $("stage-wrap").getBoundingClientRect();
  var want = Math.max(wrap.width, wrap.height) * Math.min(window.devicePixelRatio || 1, 2);
  var step = 256;                       // quantised so the cache is reused
  return Math.max(768, Math.min(2560, Math.ceil((want || 1024) / step) * step));
}

function setImage(fit) {
  var size = imageSize();
  if (!size.w) return;
  $("viewer-empty").classList.add("hidden");
  var base = $("base-image");
  base.style.width = size.w + "px";
  base.style.height = size.h + "px";
  st.baseCap = baseCap();
  st.baseDensity = Math.min(1, st.baseCap / Math.max(size.w, size.h));
  var url = "/api/image?" + qs({ run: st.sample.run, stem: st.sample.stem, kind: st.kind,
                                 max: st.baseCap, fmt: st.fmt });
  // An unchanged URL is an unchanged image: re-assigning src would restart the
  // decode and blank the viewer for a frame on every zoom reset.
  if (base.getAttribute("src") !== url) base.src = url;
  $("detail-image").style.display = "none";
  if (fit) fitView(); else applyTransform();
}

function fitView() {
  var size = imageSize(), wrap = $("stage-wrap").getBoundingClientRect();
  if (!size.w) return;
  st.zoom = Math.min(wrap.width / size.w, wrap.height / size.h) * 0.98;
  st.tx = (wrap.width - size.w * st.zoom) / 2;
  st.ty = (wrap.height - size.h * st.zoom) / 2;
  applyTransform();
}

function applyTransform() {
  var stage = $("stage");
  stage.style.transform = "translate(" + st.tx + "px," + st.ty + "px) scale(" + st.zoom + ")";
  stage.style.setProperty("--bw", (1 / st.zoom) + "px");
  stage.style.setProperty("--fs", (11 / st.zoom) + "px");
  stage.style.setProperty("--hs", (9 / st.zoom) + "px");
  $("zoom-level").textContent = Math.round(st.zoom * 100) + "%";
  scheduleDetail();
}

function toImage(clientX, clientY) {
  var rect = $("stage-wrap").getBoundingClientRect();
  return { x: (clientX - rect.left - st.tx) / st.zoom, y: (clientY - rect.top - st.ty) / st.zoom };
}

function zoomTo(rect, factor) {
  var wrap = $("stage-wrap").getBoundingClientRect();
  var want = Math.min(wrap.width / Math.max(rect.w, 8), wrap.height / Math.max(rect.h, 8));
  st.zoom = Math.max(0.05, Math.min(16, want * (factor || 0.4)));
  st.tx = wrap.width / 2 - (rect.x + rect.w / 2) * st.zoom;
  st.ty = wrap.height / 2 - (rect.y + rect.h / 2) * st.zoom;
  applyTransform();
}

/* A sharper copy of whatever is on screen, once the fitted frame stops being
   enough. Both the region and the scale are quantised - the region to a 128px
   grid, the scale to quarters - so panning and wheeling produce a handful of
   cacheable URLs instead of one per animation frame. At scale 1 the crop is
   native and lossless, because at that zoom the pixels are the thing being
   inspected. */
var DETAIL_STEPS = [0.25, 0.5, 0.75, 1];
var detailTimer = null;
function scheduleDetail() {
  if (detailTimer) clearTimeout(detailTimer);
  detailTimer = setTimeout(updateDetail, 180);
}
function detailScale() {
  for (var i = 0; i < DETAIL_STEPS.length; i++)
    if (st.zoom <= DETAIL_STEPS[i]) return DETAIL_STEPS[i];
  return 1;
}
function updateDetail() {
  var detail = $("detail-image"), size = imageSize();
  // The fitted frame already carries `baseDensity` image pixels per unit; only
  // ask for more when the zoom has gone past what it can show.
  if (!size.w || !st.sample || st.zoom <= st.baseDensity * 1.02) {
    detail.style.display = "none";
    return;
  }
  var wrap = $("stage-wrap").getBoundingClientRect(), grid = 128;
  var x = Math.max(0, Math.floor((-st.tx / st.zoom) / grid) * grid);
  var y = Math.max(0, Math.floor((-st.ty / st.zoom) / grid) * grid);
  var w = Math.min(size.w - x, Math.ceil((wrap.width / st.zoom + grid) / grid) * grid);
  var h = Math.min(size.h - y, Math.ceil((wrap.height / st.zoom + grid) / grid) * grid);
  if (w < 1 || h < 1) { detail.style.display = "none"; return; }
  var scale = detailScale();
  var params = { run: st.sample.run, stem: st.sample.stem, kind: st.kind,
                 x: x, y: y, w: w, h: h, fmt: scale >= 1 ? st.exact : st.fmt };
  if (scale < 1) params.max = Math.ceil(Math.max(w, h) * scale);
  var url = "/api/image?" + qs(params);
  detail.style.left = x + "px"; detail.style.top = y + "px";
  detail.style.width = w + "px"; detail.style.height = h + "px";
  if (detail.getAttribute("src") !== url) {
    detail.onload = function () { detail.style.display = "block"; };
    detail.src = url;
  } else {
    detail.style.display = "block";
  }
}

/* ----------------------------------------------------------------- boxes */

var ROLE_COLOURS = [
  ["push button|toggle button|button", "#61b3ff"],
  ["menu item|menu|menu bar|check menu item|radio menu item", "#7fd6c4"],
  ["text|entry|password text|document text|paragraph", "#f2a35e"],
  ["table cell|table row|table|tree|tree item|list item|list", "#c8d76a"],
  ["static|label|heading", "#9aa7bd"],
  ["frame|window|dialog|panel|filler|title bar", "#5f7185"],
  ["icon|image|graphic", "#e08cc0"],
  ["link", "#8fb3ff"],
  ["check box|radio button|combo box|spin button|slider|page tab", "#6fcf7f"]
];
function roleColour(role) {
  role = role || "";
  for (var i = 0; i < ROLE_COLOURS.length; i++)
    if (new RegExp("^(" + ROLE_COLOURS[i][0] + ")$").test(role)) return ROLE_COLOURS[i][1];
  return "#b0b8c6";
}

/* Deleted elements stay on screen in red: a deletion has to be visible to be
   reviewable, and undoing one is the common case after a mis-click. */
function visibleElements() {
  return st.elements.filter(function (e) {
    if (st.filter.role && e.role !== st.filter.role) return false;
    if (st.filter.kind && (e.kind || "(none)") !== st.filter.kind) return false;
    if (st.filter.type && (e.type || "?") !== st.filter.type) return false;
    if (st.filter.app && e.app_name !== st.filter.app) return false;
    return true;
  });
}

function renderBoxes() {
  var host = $("boxes");
  clear(host);
  st.boxNodes = {};
  if (!$("show-boxes").checked) return;
  var labels = $("show-labels").checked;
  visibleElements().forEach(function (e) {
    var rect = e.rect || {};
    if (!rect.w || !rect.h) return;
    var g = e._golden || {};
    var node = make("div", "box" + (g.status ? " " + g.status : "") +
                    (e.is_occluded ? " occluded" : ""));
    // The element key on the node itself: it is what the ScreenTag spans are
    // keyed by, so "which box is this block" is answerable from the DOM.
    node.dataset.key = e._key;
    node.style.left = rect.x + "px"; node.style.top = rect.y + "px";
    node.style.width = rect.w + "px"; node.style.height = rect.h + "px";
    if (!g.status) node.style.setProperty("--box", roleColour(e.role));
    if (labels) {
      var text = (e.role || "?") + (e.name ? " " + e.name.slice(0, 28) : "");
      node.appendChild(make("span", "lab", text));
    }
    host.appendChild(node);
    st.boxNodes[e._key] = node;
  });
  paintSelection();
}

function paintSelection() {
  for (var key in st.boxNodes) {
    var node = st.boxNodes[key];
    node.classList.toggle("selected", key === st.selected);
    node.classList.toggle("peek", key === st.tagPeek && key !== st.selected);
    node.style.pointerEvents = (key === st.selected && $("edit-mode").checked) ? "auto" : "none";
    var handles = node.querySelectorAll(".handle");
    for (var i = 0; i < handles.length; i++) node.removeChild(handles[i]);
  }
  if (!st.selected || !$("edit-mode").checked) return;
  var host = st.boxNodes[st.selected];
  if (!host) return;
  [["nw", 0, 0], ["ne", 1, 0], ["sw", 0, 1], ["se", 1, 1]].forEach(function (spec) {
    var handle = make("div", "handle");
    handle.dataset.corner = spec[0];
    handle.style.left = "calc(" + (spec[1] * 100) + "% - var(--hs)/2)";
    handle.style.top = "calc(" + (spec[2] * 100) + "% - var(--hs)/2)";
    host.appendChild(handle);
  });
}

function select(key, scroll) {
  st.selected = key;
  paintSelection();
  renderElement();
  if (scroll && key && st.byKey[key] && st.byKey[key].rect) zoomTo(st.byKey[key].rect, 0.35);
  // Picking a box in the image is half of the link; the other half is the tag
  // pane scrolling to the markup that box came from.
  if (!tagSyncing) revealTagBlockFor(key);
}

/* Bring a rect into view without re-framing when it is already on screen:
   stepping through neighbouring blocks should not re-centre the camera on
   every step, but a block whose box is off screen has to be found. */
function ensureVisible(rect) {
  if (!rect || !rect.w || !rect.h) return;
  var wrap = $("stage-wrap").getBoundingClientRect();
  var x0 = rect.x * st.zoom + st.tx, y0 = rect.y * st.zoom + st.ty;
  var x1 = (rect.x + rect.w) * st.zoom + st.tx, y1 = (rect.y + rect.h) * st.zoom + st.ty;
  if (x0 >= 0 && y0 >= 0 && x1 <= wrap.width && y1 <= wrap.height) return;
  zoomTo(rect, 0.35);
}

function hitTest(point) {
  var hits = visibleElements().filter(function (e) {
    var r = e.rect;
    return r && point.x >= r.x && point.x <= r.x + r.w && point.y >= r.y && point.y <= r.y + r.h;
  });
  hits.sort(function (a, b) { return (a.rect.w * a.rect.h) - (b.rect.w * b.rect.h); });
  return hits;
}

/* --------------------------------------------------------------- panels */

function renderElement() {
  var host = $("tab-element");
  clear(host);
  var element = st.selected ? st.byKey[st.selected] : null;
  if (!element) {
    host.appendChild(make("p", "dim", "Click a box. Overlapping boxes cycle smallest first."));
    return;
  }
  var editing = $("edit-mode").checked && !cfg.read_only;
  var g = element._golden || {};
  if (g.status) {
    var note = make("div", "note" + (g.status === "deleted" ? " bad" : ""),
                    "overlay: " + g.status + (g.fields ? " (" + g.fields.join(", ") + ")" : ""));
    host.appendChild(note);
  }
  var dropped = ((g.original || {}).visible_fragments || []).length;
  if (dropped && !(element.visible_fragments || []).length) {
    host.appendChild(make("div", "note",
      "the " + dropped + " visible fragments this element had were dropped when its " +
      "rect was edited, so coverage now counts this box alone. Revert to get them back."));
  }

  var fields = ["role", "name", "visible_text", "visible_text_status", "app_name"];
  fields.forEach(function (field) {
    var wrap = make("div", "field");
    wrap.appendChild(make("label", null, field));
    var input;
    if (field === "visible_text") {
      input = make("textarea");
      input.rows = 3;
    } else {
      input = make("input");
      input.type = "text";
      input.setAttribute("list", "values-" + field);
      host.appendChild(datalist("values-" + field, field));
    }
    input.dataset.field = field;
    input.value = element[field] === null || element[field] === undefined ? "" : element[field];
    input.disabled = !editing;
    on(input, "change", function () { setField(element._key, field, input.value); });
    wrap.appendChild(input);
    host.appendChild(wrap);
  });

  var rect = element.rect || { x: 0, y: 0, w: 0, h: 0 };
  var rectWrap = make("div", "field");
  rectWrap.appendChild(make("label", null, "rect (x, y, w, h)"));
  var grid = make("div", "rect-grid");
  ["x", "y", "w", "h"].forEach(function (part) {
    var input = make("input");
    input.type = "number";
    input.value = rect[part];
    input.disabled = !editing;
    on(input, "change", function () {
      var next = { x: rect.x, y: rect.y, w: rect.w, h: rect.h };
      next[part] = Math.round(Number(input.value));
      if (next.w > 0 && next.h > 0) setField(element._key, "rect", next);
    });
    grid.appendChild(input);
  });
  rectWrap.appendChild(grid);
  host.appendChild(rectWrap);

  var row = make("div", "row");
  if (editing) {
    var del = make("button", "danger", g.status === "deleted" ? "undelete" : "delete");
    on(del, "click", function () { toggleDelete(element._key); });
    row.appendChild(del);
    if (g.status) {
      var undo = make("button", null, "revert");
      on(undo, "click", function () { revert(element._key); });
      row.appendChild(undo);
    }
  }
  var locate = make("button", null, "zoom to");
  on(locate, "click", function () { if (element.rect) zoomTo(element.rect, 0.35); });
  row.appendChild(locate);
  host.appendChild(row);

  var facts = make("div", "kv");
  [["key", element._key],
   ["type", element.type],
   ["occluded", String(!!element.is_occluded) + " (" + (element.occlusion_state || "-") + ")"],
   ["fragments", (element.visible_fragments || []).length],
   ["text status", element.visible_text_status || "-"],
   ["text conf", element.visible_text_confidence],
   ["dom index", element._dom_index + " ← " + element._parent_dom_index],
   ["window z", element._window_stack_index],
   ["actionable", String(!!(element.interaction || {}).actionable)]
  ].forEach(function (pair) {
    facts.appendChild(make("div", null, pair[0]));
    facts.appendChild(make("div", null, pair[1] === undefined ? "-" : String(pair[1])));
  });
  host.appendChild(make("h4", null, "facts"));
  host.appendChild(facts);

  var raw = make("details");
  raw.appendChild(make("summary", null, "raw element json"));
  raw.appendChild(make("pre", null, JSON.stringify(element, null, 1)));
  host.appendChild(raw);
}

function datalist(id, field) {
  var node = make("datalist");
  node.id = id;
  var seen = {};
  st.elements.forEach(function (e) {
    var value = e[field];
    if (typeof value === "string" && value && !seen[value] && Object.keys(seen).length < 200) {
      seen[value] = true;
      node.appendChild(make("option", null, value));
    }
  });
  return node;
}

function bars(host, pairs, kind) {
  var total = pairs.reduce(function (acc, pair) { return Math.max(acc, pair[1]); }, 1);
  var grid = make("div", "bars");
  pairs.forEach(function (pair) {
    var name = make("div", "bar-name" + (st.filter[kind] === pair[0] ? " picked" : ""));
    name.style.setProperty("--pct", (100 * pair[1] / total) + "%");
    name.appendChild(make("span", null, pair[0]));
    name.title = "click to isolate";
    on(name, "click", function () {
      st.filter[kind] = st.filter[kind] === pair[0] ? null : pair[0];
      renderBoxes();
      renderStats();
    });
    grid.appendChild(name);
    grid.appendChild(make("div", "bar-count", pair[1]));
  });
  host.appendChild(grid);
}

function renderStats() {
  var host = $("tab-stats");
  clear(host);
  if (!st.sample) return;
  var live = st.elements.filter(function (e) { return (e._golden || {}).status !== "deleted"; });
  var stats = {
    elements: live.length, occluded: 0, actionable: 0, with_text: 0, characters: 0,
    fragmented: 0
  };
  var roles = {}, apps = {}, statuses = {}, kinds = {}, types = {};
  live.forEach(function (e) {
    if (e.is_occluded) stats.occluded += 1;
    if ((e.interaction || {}).actionable) stats.actionable += 1;
    if ((e.visible_fragments || []).length > 1) stats.fragmented += 1;
    var text = e.visible_text || "";
    if (text) { stats.with_text += 1; stats.characters += text.length; }
    roles[e.role || "?"] = (roles[e.role || "?"] || 0) + 1;
    // Both vocabularies at once: `kind` is the 26 that replace the legacy 55
    // `type`s, and the only way to judge the replacement is to see them side by
    // side on real captures.
    kinds[e.kind || "(none)"] = (kinds[e.kind || "(none)"] || 0) + 1;
    types[e.type || "?"] = (types[e.type || "?"] || 0) + 1;
    apps[e.app_name || "?"] = (apps[e.app_name || "?"] || 0) + 1;
    var status = e.visible_text_status;
    if (status) statuses[status] = (statuses[status] || 0) + 1;
  });

  var kv = make("div", "kv");
  var image = imageSize();
  [["elements", stats.elements],
   ["occluded", stats.occluded],
   ["fragmented", stats.fragmented],
   ["actionable", stats.actionable],
   ["with text", stats.with_text],
   ["characters", stats.characters],
   ["image", image.w + "×" + image.h],
   ["edits", editCount()]
  ].forEach(function (pair) {
    kv.appendChild(make("div", null, pair[0]));
    kv.appendChild(make("div", null, String(pair[1])));
  });
  host.appendChild(kv);

  var sorted = function (obj) {
    return Object.keys(obj).map(function (k) { return [k, obj[k]]; })
      .sort(function (a, b) { return b[1] - a[1]; });
  };
  host.appendChild(make("h4", null, "kinds"));
  bars(host, sorted(kinds), "kind");
  host.appendChild(make("h4", null, "legacy types"));
  bars(host, sorted(types), "type");
  host.appendChild(make("h4", null, "roles"));
  bars(host, sorted(roles), "role");
  host.appendChild(make("h4", null, "apps"));
  bars(host, sorted(apps), "app");
  if (Object.keys(statuses).length) {
    host.appendChild(make("h4", null, "visible text status"));
    var grid = make("div", "kv");
    sorted(statuses).forEach(function (pair) {
      grid.appendChild(make("div", null, pair[0]));
      grid.appendChild(make("div", null, String(pair[1])));
    });
    host.appendChild(grid);
  }
  if (st.filter.role || st.filter.app || st.filter.kind || st.filter.type) {
    var clearBtn = make("button", null, "clear filter");
    on(clearBtn, "click", function () {
      st.filter.role = st.filter.app = st.filter.kind = st.filter.type = null;
      renderBoxes(); renderStats();
    });
    host.appendChild(clearBtn);
  }
}

function flagTable(host, title, rows, colour) {
  if (!rows || !rows.length) return;
  host.appendChild(make("h4", null, title + " (" + rows.length + ")"));
  var table = make("table", "flag");
  rows.forEach(function (row) {
    var tr = make("tr");
    var left = make("td");
    left.appendChild(make("div", null, (row.role || "?") + " · " +
      (row.text || "").slice(0, 40)));
    var rect = row.rect || {};
    left.appendChild(make("div", "dim",
      [rect.x, rect.y, rect.w, rect.h].join(", ") +
      (row.ink_ratio !== undefined ? "  ink " + pct(row.ink_ratio) : "")));
    tr.appendChild(left);
    var act = make("td", "act");
    var go = make("button", null, "⌕");
    go.title = "zoom to this box";
    on(go, "click", function () {
      if (!rect.w) return;
      zoomTo(rect, 0.25);
      var hits = hitTest({ x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 });
      if (hits.length) select(hits[0]._key, false);
    });
    act.appendChild(go);
    tr.appendChild(act);
    table.appendChild(tr);
  });
  host.appendChild(table);
  void colour;
}

function renderAudit() {
  var host = $("tab-audit");
  clear(host);
  if (!st.sample) return;
  var audit = st.sample.audit || {};
  var names = Object.keys(audit);
  if (!names.length) host.appendChild(make("p", "dim", "No audit rows for this capture."));
  names.forEach(function (name) {
    var row = audit[name];
    host.appendChild(make("h4", null, name.replace(/_/g, " ")));
    var kv = make("div", "kv");
    Object.keys(row).forEach(function (key) {
      if (Array.isArray(row[key]) || key === "capture") return;
      kv.appendChild(make("div", null, key));
      kv.appendChild(make("div", null, String(row[key])));
    });
    host.appendChild(kv);
    flagTable(host, "phantoms", row.phantoms);
    flagTable(host, "drifted", row.drifted);
    flagTable(host, "uncovered", row.regions || row.uncovered);
  });

  var quality = st.runQuality[st.sample.run];
  if (quality) {
    host.appendChild(make("h4", null, "run quality"));
    var kv = make("div", "kv");
    ["captures", "elements_per_capture", "fp_blank_rate", "fn_uncovered_ink", "phantom_rate",
     "drift_rate", "text_unsupported_rate", "window_control_coverage"].forEach(function (key) {
      if (quality[key] === undefined) return;
      kv.appendChild(make("div", null, key));
      kv.appendChild(make("div", null, String(quality[key])));
    });
    host.appendChild(kv);
  }
}

function jsonTree(value, key) {
  if (value !== null && typeof value === "object") {
    var node = make("details");
    var count = Array.isArray(value) ? value.length + " items" : Object.keys(value).length + " keys";
    node.appendChild(make("summary", null, key + "  (" + count + ")"));
    var keys = Array.isArray(value) ? value.map(function (_, i) { return i; }) : Object.keys(value);
    keys.slice(0, 200).forEach(function (child) {
      node.appendChild(jsonTree(value[child], String(child)));
    });
    return node;
  }
  var line = make("div", "kv");
  line.appendChild(make("div", null, key));
  line.appendChild(make("div", null, String(value)));
  return line;
}

function renderMeta() {
  var host = $("tab-meta");
  clear(host);
  if (!st.sample) return;
  var meta = st.sample.meta || {};
  var scene = meta.scene || {};
  var kv = make("div", "kv");
  [["stem", st.sample.stem],
   ["run", st.sample.run],
   ["seed", scene.seed],
   ["theme", scene.theme_preset],
   ["display", scene.display_preset],
   ["layout", scene.layout],
   ["panel", scene.panel_variant],
   ["apps", (meta.launched_apps || []).join(", ")],
   ["desktop", meta.desktop_env],
   ["walk sec", (meta.capture_timing || {}).atspi_walk_duration_sec],
   ["shot sec", (meta.capture_timing || {}).screenshot_duration_sec],
   ["png bytes", st.sample.image.bytes]
  ].forEach(function (pair) {
    kv.appendChild(make("div", null, pair[0]));
    kv.appendChild(make("div", null, pair[1] === undefined ? "-" : String(pair[1])));
  });
  host.appendChild(kv);

  host.appendChild(make("h4", null, "source file"));
  var source = st.sample.source || {};
  var src = make("div", "kv");
  [["path", source.path], ["sha256", (source.sha256 || "").slice(0, 16) + "…"],
   ["size", source.size]].forEach(function (pair) {
    src.appendChild(make("div", null, pair[0]));
    src.appendChild(make("div", null, String(pair[1])));
  });
  host.appendChild(src);

  host.appendChild(make("h4", null, "meta.json"));
  Object.keys(meta).forEach(function (key) {
    host.appendChild(jsonTree(meta[key], key));
  });
}

function renderMark() {
  var host = $("tab-mark");
  clear(host);
  if (!st.sample) return;
  if (st.sample.stale) {
    host.appendChild(make("div", "note bad",
      "The capture changed since this overlay was written. Its edits may not " +
      "line up with these pixels - check before trusting them."));
  }
  var applied = st.sample.applied || {};
  if (applied.unmatched && applied.unmatched.length) {
    host.appendChild(make("div", "note",
      applied.unmatched.length + " stored edit(s) match no element in this capture."));
  }
  if (st.conflict) {
    var clash = make("div", "note bad");
    clash.appendChild(make("div", null,
      "Somebody else saved this overlay while you were editing it, so nothing " +
      "was written. Their version is on disk; yours is still in this page."));
    var theirs = st.conflict.current || {};
    clash.appendChild(make("div", "dim",
      "theirs: " + (theirs.updated_by || "?") + " at " + (theirs.updated_at || "?") +
      " · mark " + ((theirs.mark || {}).status || "unmarked")));
    var choice = make("div", "row");
    var take = make("button", null, "load theirs (lose mine)");
    on(take, "click", function () {
      st.dirty = false;
      loadSample(st.sample.run, st.sample.stem, true);
    });
    var mine = make("button", "danger", "overwrite with mine");
    on(mine, "click", function () { saveOverlay(true); });
    choice.appendChild(take);
    choice.appendChild(mine);
    clash.appendChild(choice);
    host.appendChild(clash);
  }

  var statusWrap = make("div", "field");
  statusWrap.appendChild(make("label", null, "mark (v / w / r / u)"));
  var row = make("div", "row");
  (cfg.mark_statuses || []).forEach(function (value) {
    var button = make("button", st.mark.status === value ? "primary" : null, value.replace("_", " "));
    on(button, "click", function () {
      st.mark = { status: value, tags: st.mark.tags, note: st.mark.note };
      markDirty(true);
      renderMark();
    });
    row.appendChild(button);
  });
  statusWrap.appendChild(row);
  host.appendChild(statusWrap);

  var tags = make("div", "field");
  tags.appendChild(make("label", null, "tags (comma separated)"));
  var tagsInput = make("input");
  tagsInput.type = "text";
  tagsInput.value = (st.mark.tags || []).join(", ");
  on(tagsInput, "change", function () {
    st.mark.tags = tagsInput.value.split(",").map(function (t) { return t.trim(); })
      .filter(function (t) { return t; });
    markDirty(true);
  });
  tags.appendChild(tagsInput);
  host.appendChild(tags);

  var note = make("div", "field");
  note.appendChild(make("label", null, "note"));
  var noteInput = make("textarea");
  noteInput.rows = 4;
  noteInput.value = st.mark.note || "";
  on(noteInput, "change", function () { st.mark.note = noteInput.value; markDirty(true); });
  note.appendChild(noteInput);
  host.appendChild(note);

  var actions = make("div", "row");
  var save = make("button", "primary", "save overlay");
  on(save, "click", saveOverlay);
  actions.appendChild(save);
  var drop = make("button", "danger", "discard overlay");
  on(drop, "click", discardOverlay);
  actions.appendChild(drop);
  host.appendChild(actions);

  host.appendChild(make("h4", null, "pending edits"));
  var kv = make("div", "kv");
  [["modified", Object.keys(st.edits.modified).length],
   ["deleted", st.edits.deleted.length],
   ["added", st.edits.added.length]].forEach(function (pair) {
    kv.appendChild(make("div", null, pair[0]));
    kv.appendChild(make("div", null, String(pair[1])));
  });
  host.appendChild(kv);

  var overlay = st.sample.overlay;
  if (overlay) {
    host.appendChild(make("h4", null, "stored overlay"));
    var stored = make("div", "kv");
    [["updated", overlay.updated_at], ["by", overlay.updated_by],
     ["created", overlay.created_at], ["revisions", (overlay.history || []).length]
    ].forEach(function (pair) {
      stored.appendChild(make("div", null, pair[0]));
      stored.appendChild(make("div", null, String(pair[1])));
    });
    host.appendChild(stored);
  }
  host.appendChild(make("p", "dim", "Overlays are written under " +
    (cfg.golden_root || "golden/") + " and are what you commit."));
}

/* ------------------------------------------------------------- screentag
 *
 * The tag is one flat string and the boxes are a list, and the question a
 * person actually has in front of a capture is "which of these is that". The
 * server answers it: `/api/screentag?spans=1` returns the text together with
 * the character range every element produced, keyed the same way the boxes are.
 * Everything here is presentation over that index - the browser never parses
 * the tag and never guesses at the order.
 */

function loadScreentag() {
  if (!st.sample || !st.sample.has_screentag) return Promise.resolve();
  if (st.screentag && !st.screentag.error) return Promise.resolve();
  var run = st.run, stem = st.sample.stem, mine = nav.sample;
  st.screentag = { loading: true };
  renderScreentag();
  return api("/api/screentag", { run: run, stem: stem, spans: 1 }).then(function (data) {
    if (mine !== nav.sample) return;
    data.byKey = {};
    for (var i = 0; i < data.spans.length; i++) {
      var key = data.spans[i].key;
      if (!key) continue;
      if (!data.byKey[key]) data.byKey[key] = [];
      data.byKey[key].push(i);
    }
    st.screentag = data;
    st.tagBlock = null;
    renderScreentag();
    if (st.selected) revealTagBlockFor(st.selected);
  }).catch(function (err) {
    if (mine !== nav.sample) return;
    st.screentag = { error: String((err && err.message) || err) };
    renderScreentag();
  });
}

function tagSpans() {
  return (st.screentag && st.screentag.spans) || [];
}

function renderScreentag() {
  var host = $("tab-screentag");
  clear(host);
  st.tagNodes = [];
  st.tagHover = null;
  if (!st.sample) return;
  if (!st.sample.has_screentag) {
    host.appendChild(make("p", "dim", "No screentag.txt for this capture."));
    return;
  }
  if (st.screentag === null) {
    var load = make("button", null, "load screentag");
    on(load, "click", loadScreentag);
    host.appendChild(load);
    return;
  }
  if (st.screentag.loading) {
    host.appendChild(make("p", "dim", "loading…"));
    return;
  }
  if (st.screentag.error) {
    host.appendChild(make("div", "note bad", st.screentag.error));
    var retry = make("button", null, "retry");
    on(retry, "click", function () { st.screentag = null; loadScreentag(); });
    host.appendChild(retry);
    return;
  }

  var data = st.screentag, stats = data.stats || {};
  if (data.unlinked) {
    host.appendChild(make("div", "note", "blocks are not linked to boxes: " + data.unlinked));
  } else if (data.view !== st.view) {
    host.appendChild(make("div", "note",
      "the tag is serialized from the " + data.view + " view; you are looking at " + st.view +
      ", so a block may have no box to point at. Switch the view to " + data.view + " to link them."));
  }
  if (stats.unmatched_blocks) {
    host.appendChild(make("div", "note bad", stats.unmatched_blocks +
      " block(s) match no element - those are shown struck through and cannot be linked."));
  }

  host.appendChild(make("h4", null, "block"));
  var panel = make("div", null);
  panel.id = "tag-block";
  host.appendChild(panel);

  host.appendChild(make("h4", null, "screentag"));
  var bar = make("div", "row");
  var prev = make("button", null, "‹ block");
  var next = make("button", null, "block ›");
  on(prev, "click", function () { stepTagBlock(-1); });
  on(next, "click", function () { stepTagBlock(1); });
  bar.appendChild(prev);
  bar.appendChild(next);
  bar.appendChild(make("span", "dim",
    (stats.blocks || 0) + " blocks · " + (stats.matched || 0) + " linked" +
    (stats.unmatched_elements ? " · " + stats.unmatched_elements + " element(s) never serialized" : "")));
  host.appendChild(bar);
  host.appendChild(buildTagCode(data));
  paintTagBlock();
  renderTagBlockPanel();
}

/* Rebuild the tag as nested spans, one per block, so a click can be resolved to
   a block by walking up from the node under the pointer and the innermost one
   wins - the same "smallest first" rule the image uses for stacked boxes.
   Concatenating every piece reproduces the file exactly; the browser check
   asserts that, because a rendering that drops a character would put every
   offset after it out by one. */
function buildTagCode(data) {
  var pre = make("pre", "tagcode");
  pre.id = "tag-code";
  pre.tabIndex = 0;
  var spans = data.spans, text = data.text, kids = {}, roots = [], i;
  for (i = 0; i < spans.length; i++) {
    var parent = spans[i].parent;
    if (parent >= 0) { if (!kids[parent]) kids[parent] = []; kids[parent].push(i); }
    else roots.push(i);
  }

  function emit(host, index) {
    var span = spans[index];
    var node = make("span", "tgb" + (span.element === null ? " orphan" : ""));
    node.dataset.block = String(index);
    node.appendChild(make("span", "tghead", text.slice(span.start, span.head_end)));
    if (span.text_end > span.head_end)
      node.appendChild(make("span", "tgtext", text.slice(span.head_end, span.text_end)));
    var cursor = span.text_end, children = kids[index] || [];
    for (var k = 0; k < children.length; k++) {
      var child = spans[children[k]];
      if (child.start > cursor) node.appendChild(document.createTextNode(text.slice(cursor, child.start)));
      emit(node, children[k]);
      cursor = child.end;
    }
    if (span.end > cursor) node.appendChild(make("span", "tgclose", text.slice(cursor, span.end)));
    host.appendChild(node);
    st.tagNodes[index] = node;
  }

  var at = 0;
  for (i = 0; i < roots.length; i++) {
    var root = spans[roots[i]];
    if (root.start > at) pre.appendChild(document.createTextNode(text.slice(at, root.start)));
    emit(pre, roots[i]);
    at = root.end;
  }
  if (at < text.length) pre.appendChild(document.createTextNode(text.slice(at)));

  on(pre, "click", function (event) {
    var index = blockUnder(event.target);
    if (index !== null) selectTagBlock(index, false);
  });
  on(pre, "mouseover", function (event) {
    var index = blockUnder(event.target);
    peekTagBlock(index === null ? null : index);
  });
  on(pre, "mouseleave", function () { peekTagBlock(null); });
  on(pre, "keydown", function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowRight") stepTagBlock(1);
    else if (event.key === "ArrowUp" || event.key === "ArrowLeft") stepTagBlock(-1);
    else return;
    event.preventDefault();
  });
  return pre;
}

function blockUnder(node) {
  while (node && node !== document) {
    if (node.nodeType === 1 && node.dataset && node.dataset.block !== undefined)
      return Number(node.dataset.block);
    node = node.parentNode;
  }
  return null;
}

/* The caret, for the case the pointer is not the instrument: a click leaves a
   collapsed selection in the pre, and with caret browsing on the arrow keys
   move it. Either way "where the cursor is" resolves to a block the same way. */
function tagCaretBlock() {
  var pre = $("tag-code");
  if (!pre) return null;
  var selection = window.getSelection();
  if (!selection || !selection.anchorNode || !pre.contains(selection.anchorNode)) return null;
  return blockUnder(selection.anchorNode);
}

function peekTagBlock(index) {
  if (index === st.tagHover) return;
  if (st.tagNodes[st.tagHover]) st.tagNodes[st.tagHover].classList.remove("hovered");
  st.tagHover = index;
  if (index !== null && st.tagNodes[index]) st.tagNodes[index].classList.add("hovered");
  var span = index === null ? null : tagSpans()[index];
  var key = span ? span.key : null;
  if (key === st.tagPeek) return;
  st.tagPeek = key;
  paintSelection();
}

function selectTagBlock(index, scrollTag) {
  var span = tagSpans()[index];
  if (!span) return;
  st.tagBlock = index;
  paintTagBlock();
  renderTagBlockPanel();
  if (scrollTag) scrollTagBlockIntoView(index);
  if (!span.key || !st.byKey[span.key]) return;
  tagSyncing = true;
  select(span.key, false);
  tagSyncing = false;
  ensureVisible(st.byKey[span.key].rect);
}

function stepTagBlock(delta) {
  var spans = tagSpans();
  if (!spans.length) return;
  var at = st.tagBlock === null ? (delta > 0 ? -1 : spans.length) : st.tagBlock;
  var next = Math.max(0, Math.min(spans.length - 1, at + delta));
  selectTagBlock(next, true);
}

/* The reverse link: a box picked in the image scrolls the tag to its markup. */
function revealTagBlockFor(key) {
  var data = st.screentag;
  if (!data || !data.spans || !key) return;
  var list = data.byKey && data.byKey[key];
  if (!list || !list.length) return;
  st.tagBlock = list[0];
  paintTagBlock();
  renderTagBlockPanel();
  scrollTagBlockIntoView(list[0]);
}

function paintTagBlock() {
  for (var i = 0; i < st.tagNodes.length; i++) {
    var node = st.tagNodes[i];
    if (node) node.classList.toggle("active", i === st.tagBlock);
  }
}

function scrollTagBlockIntoView(index) {
  var pre = $("tag-code"), node = st.tagNodes[index];
  if (!pre || !node) return;
  var box = node.getBoundingClientRect(), frame = pre.getBoundingClientRect();
  if (box.top >= frame.top && box.bottom <= frame.bottom) return;
  pre.scrollTop += box.top - frame.top - (frame.height - Math.min(box.height, frame.height)) / 2;
}

/* One block, spelled out: the markup itself, what its `<loc_>` tokens decode
   to, and how that compares with the rect of the element it was matched to.
   Both rects are shown because the decode is lossy by construction - the
   serializer truncates each edge onto a 0-500 grid, so a decoded edge can sit
   up to one cell short of the real one and a small difference is not a bug. */
function renderTagBlockPanel() {
  var host = $("tag-block");
  if (!host) return;
  clear(host);
  var data = st.screentag, span = st.tagBlock === null ? null : tagSpans()[st.tagBlock];
  if (!span) {
    host.appendChild(make("p", "dim",
      "Click a block below (or a box on the screenshot) to read one element at a time."));
    return;
  }
  var element = span.key ? st.byKey[span.key] : null;
  var hasChildren = tagSpans().some(function (other) { return other.parent === st.tagBlock; });
  var body = data.text.slice(span.start, hasChildren ? span.text_end : span.end);
  if (hasChildren) body += "…</" + span.tag + ">";

  var head = make("div", "row");
  head.appendChild(make("span", "chip", "<" + span.tag + ">"));
  head.appendChild(make("span", "dim", "block " + (st.tagBlock + 1) + " of " + tagSpans().length +
                                       " · chars " + span.start + "–" + span.end));
  if (element && element.rect) {
    var zoom = make("button", null, "zoom to");
    on(zoom, "click", function () { zoomTo(element.rect, 0.35); });
    head.appendChild(zoom);
  }
  host.appendChild(head);
  host.appendChild(make("pre", "tagblock", body));

  var cell = data.viewport && data.viewport.width
    ? (data.viewport.width / data.grid).toFixed(2) + "×" + (data.viewport.height / data.grid).toFixed(2) + "px"
    : "?";
  var kv = make("div", "kv");
  function row(name, value) {
    kv.appendChild(make("div", null, name));
    kv.appendChild(make("div", null, value));
  }
  row("loc tokens", span.loc.length ? span.loc.join(", ") : "none");
  row("decodes to", span.rect ? rectText(span.rect) : "-");
  row("element rect", element && element.rect ? rectText(element.rect) : (span.key ? "not in this view" : "-"));
  row("grid cell", cell + " (of " + data.grid + ")");
  if (span.states.length) row("state", span.states.join(" "));
  // Labelled tokens (a window's `<title>`) describe the element but have no box
  // of their own, so they belong here rather than as a block to click on.
  (span.labels || []).forEach(function (label) { row(label[0], label[1]); });
  if (span.fragments.length)
    row("fragments", span.fragments.map(rectText).join("  ·  "));
  row("element", span.key || "no element matches this block");
  if (span.repeat) row("note", "this element is serialized more than once");
  host.appendChild(kv);
}

function rectText(rect) {
  return rect.x + "," + rect.y + " " + rect.w + "×" + rect.h;
}

/* ---------------------------------------------------------------- saving */

/* `base_version` is the hash of the overlay as it was when this sample was
   opened. The server refuses the write if what is on disk no longer matches,
   which is the difference between two people curating the same capture and one
   of them losing an afternoon without being told. */
function saveOverlay(force) {
  if (!st.sample || cfg.read_only) return Promise.resolve();
  status("saving…", "busy");
  var payload = {
    run: st.sample.run, stem: st.sample.stem, view: st.view,
    mark: st.mark, edits: st.edits,
    base_version: force ? (st.conflict && st.conflict.current_version) || null : st.baseVersion
  };
  return api("/api/overlay", null, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(function (result) {
    markDirty(false);
    st.sample.overlay = result.overlay || null;
    st.baseVersion = result.overlay_version || null;
    st.conflict = null;
    st.sample.stale = false;
    var cleared = (result.fragments_cleared || []).length;
    renderMark();
    status(
      (result.saved ? "saved " + result.path.split("/").slice(-2).join("/") : "overlay removed") +
      (cleared ? " · fragments cleared on " + cleared + " element(s)" : ""),
      "saved");
    return refreshMarks();
  }).catch(function (err) {
    if (err && err.status === 409 && err.body && err.body.conflict) {
      st.conflict = err.body.conflict;
      renderMark();
      activateTab("mark");
      status(err.message, "error");
      return;
    }
    fail(err);
  });
}

/* Re-read the badges from `golden/` rather than patching them locally. The
   local patch was wrong whenever a sample load was still in flight over it,
   and the run listing is a cached-index read - cheap enough to just ask. */
function refreshMarks() {
  var run = st.sample && st.sample.run;
  if (!run) return Promise.resolve();
  return fetchRunSamples(run, true).catch(function (err) { void err; });
}

function discardOverlay() {
  if (!st.sample || !confirm("Delete the stored overlay for this sample?")) return;
  var run = st.sample.run, stem = st.sample.stem;
  api("/api/overlay", { run: run, stem: stem, view: st.view }, { method: "DELETE" })
    .then(function () { loadSample(run, stem, true); status("overlay deleted", "saved"); })
    .catch(fail);
}

/* -------------------------------------------------------------- pointers */

var drag = null;

function beginDrag(event) {
  if (event.button !== 0 && event.button !== 1) return;
  var wrap = $("stage-wrap");
  var point = toImage(event.clientX, event.clientY);
  var editing = $("edit-mode").checked && !cfg.read_only;

  if (editing && event.target.classList.contains("handle")) {
    var element = st.byKey[st.selected];
    drag = { mode: "resize", corner: event.target.dataset.corner, start: point,
             rect: Object.assign({}, element.rect), key: st.selected };
  } else if (st.addArmed && editing) {
    drag = { mode: "create", start: point, rect: { x: point.x, y: point.y, w: 0, h: 0 } };
  } else if (editing && st.selected && event.target.classList.contains("box")) {
    drag = { mode: "move", start: point, rect: Object.assign({}, st.byKey[st.selected].rect),
             key: st.selected };
  } else {
    drag = { mode: "pan", startX: event.clientX, startY: event.clientY, tx: st.tx, ty: st.ty,
             point: point, moved: false };
    wrap.classList.add("panning");
  }
  event.preventDefault();
  try { wrap.setPointerCapture(event.pointerId); } catch (err) { void err; }
}

function moveDrag(event) {
  var point = toImage(event.clientX, event.clientY);
  $("hover-readout").textContent = Math.round(point.x) + ", " + Math.round(point.y);
  if (!drag) return;
  if (drag.mode === "pan") {
    st.tx = drag.tx + (event.clientX - drag.startX);
    st.ty = drag.ty + (event.clientY - drag.startY);
    if (Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY) > 3)
      drag.moved = true;
    applyTransform();
    return;
  }
  var rect;
  if (drag.mode === "create") {
    rect = normRect(drag.start, point);
    drawDraft(rect);
    drag.rect = rect;
    return;
  }
  if (drag.mode === "move") {
    rect = {
      x: Math.round(drag.rect.x + point.x - drag.start.x),
      y: Math.round(drag.rect.y + point.y - drag.start.y),
      w: drag.rect.w, h: drag.rect.h
    };
  } else {
    var corner = drag.corner, base = drag.rect;
    var x0 = corner.indexOf("w") >= 0 ? point.x : base.x;
    var y0 = corner.indexOf("n") >= 0 ? point.y : base.y;
    var x1 = corner.indexOf("e") >= 0 ? point.x : base.x + base.w;
    var y1 = corner.indexOf("s") >= 0 ? point.y : base.y + base.h;
    rect = normRect({ x: x0, y: y0 }, { x: x1, y: y1 });
  }
  drag.live = rect;
  var node = st.boxNodes[drag.key];
  if (node) {
    node.style.left = rect.x + "px"; node.style.top = rect.y + "px";
    node.style.width = rect.w + "px"; node.style.height = rect.h + "px";
  }
}

function endDrag(event) {
  var wrap = $("stage-wrap");
  wrap.classList.remove("panning");
  if (!drag) return;
  var finished = drag;
  drag = null;
  try { wrap.releasePointerCapture(event.pointerId); } catch (err) { void err; }

  if (finished.mode === "pan") {
    if (!finished.moved) {
      var hits = hitTest(finished.point);
      if (!hits.length) select(null, false);
      else {
        var at = hits.map(function (e) { return e._key; }).indexOf(st.selected);
        select(hits[(at + 1) % hits.length]._key, false);
      }
    }
    return;
  }
  if (finished.mode === "create") {
    clear($("draft"));
    st.addArmed = false;
    wrap.classList.remove("drawing");
    var rect = finished.rect;
    if (rect.w < 3 || rect.h < 3) return;
    st.addSeq += 1;
    var golden_id = "add-" + Date.now().toString(36) + "-" + st.addSeq;
    st.edits.added.push({ golden_id: golden_id, rect: rect, role: "static", name: "" });
    markDirty(true);
    rebuild();
    select("add:" + golden_id, false);
    activateTab("element");
    return;
  }
  if (finished.live) setField(finished.key, "rect", finished.live);
}

function normRect(a, b) {
  return {
    x: Math.round(Math.min(a.x, b.x)), y: Math.round(Math.min(a.y, b.y)),
    w: Math.round(Math.abs(b.x - a.x)), h: Math.round(Math.abs(b.y - a.y))
  };
}

function drawDraft(rect) {
  var host = $("draft");
  clear(host);
  var node = make("div", "box");
  node.style.left = rect.x + "px"; node.style.top = rect.y + "px";
  node.style.width = rect.w + "px"; node.style.height = rect.h + "px";
  host.appendChild(node);
}

/* ------------------------------------------------------------------ wire */

function tagTabOpen() {
  var tab = $("tab-screentag");
  return !!tab && tab.classList.contains("active");
}

function activateTab(name) {
  var buttons = document.querySelectorAll(".tabs button");
  for (var i = 0; i < buttons.length; i++)
    buttons[i].classList.toggle("active", buttons[i].dataset.tab === name);
  var tabs = document.querySelectorAll(".tab");
  for (var j = 0; j < tabs.length; j++)
    tabs[j].classList.toggle("active", tabs[j].id === "tab-" + name);
  if (name === "screentag") loadScreentag();
}

function step(delta) {
  if (!st.sample) return;
  var run = st.sample.run, shown = shownSamples(run);
  var at = shown.map(function (s) { return s.stem; }).indexOf(st.sample.stem);
  var next = shown[Math.max(0, Math.min(shown.length - 1, at + delta))];
  if (next && next.stem !== st.sample.stem) loadSample(run, next.stem);
}

function setMark(value) {
  if (!st.sample) return;
  st.mark = { status: value, tags: st.mark.tags, note: st.mark.note };
  markDirty(true);
  renderMark();
  activateTab("mark");
}

function wire() {
  on($("rescan"), "click", function () {
    var run = st.sample && st.sample.run;
    loadTree(true).then(function () { if (run) { revealRun(run); fetchRunSamples(run, true); } });
  });
  on($("collapse-all"), "click", function () { st.open = {}; renderTree(); });
  on($("reveal"), "click", function () {
    revealRun(st.sample && st.sample.run);
    renderTree();
  });
  on($("tree-sort"), "change", function () { st.sort = this.value; renderTree(); });
  on($("view-select"), "change", function () {
    st.view = this.value;
    if (st.sample) loadSample(st.sample.run, st.sample.stem);
  });
  on($("image-kind"), "change", function () { st.kind = this.value; setImage(false); });
  on($("show-boxes"), "change", renderBoxes);
  on($("show-labels"), "change", renderBoxes);
  on($("fit"), "click", fitView);
  on($("zoom-one"), "click", function () {
    var wrap = $("stage-wrap").getBoundingClientRect();
    var centre = { x: (wrap.width / 2 - st.tx) / st.zoom, y: (wrap.height / 2 - st.ty) / st.zoom };
    st.zoom = 1;
    st.tx = wrap.width / 2 - centre.x;
    st.ty = wrap.height / 2 - centre.y;
    applyTransform();
  });
  on($("edit-mode"), "change", function () {
    $("add-box").classList.toggle("hidden", !this.checked);
    paintSelection();
    renderElement();
  });
  on($("add-box"), "click", function () {
    st.addArmed = true;
    $("stage-wrap").classList.add("drawing");
    status("drag on the screenshot to draw the new element", "busy");
  });
  on($("save"), "click", function () { saveOverlay(false); });
  on($("sample-filter"), "input", renderTree);
  on($("mark-filter"), "change", renderTree);

  var buttons = document.querySelectorAll(".tabs button");
  for (var i = 0; i < buttons.length; i++) {
    (function (button) {
      on(button, "click", function () { activateTab(button.dataset.tab); });
    })(buttons[i]);
  }

  var wrap = $("stage-wrap");
  on(wrap, "pointerdown", beginDrag);
  on(wrap, "pointermove", moveDrag);
  on(wrap, "pointerup", endDrag);
  on(wrap, "pointercancel", endDrag);
  on(wrap, "wheel", function (event) {
    event.preventDefault();
    var before = toImage(event.clientX, event.clientY);
    var factor = Math.exp(-event.deltaY * (event.deltaMode === 1 ? 0.05 : 0.0015));
    st.zoom = Math.max(0.02, Math.min(24, st.zoom * factor));
    var rect = wrap.getBoundingClientRect();
    st.tx = event.clientX - rect.left - before.x * st.zoom;
    st.ty = event.clientY - rect.top - before.y * st.zoom;
    applyTransform();
  }, { passive: false });

  /* A caret inside the tag selects the element that block came from. This is
     the same path a click takes, but it also covers moving the caret with the
     keyboard, which is how somebody reads a tag block by block. */
  on(document, "selectionchange", function () {
    var index = tagCaretBlock();
    if (index !== null && index !== st.tagBlock) selectTagBlock(index, false);
  });

  on(window, "resize", function () { scheduleDetail(); });
  on(window, "beforeunload", function (event) {
    if (st.dirty) { event.preventDefault(); event.returnValue = ""; }
  });
  on(document, "keydown", function (event) {
    var tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault(); saveOverlay(false); return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    switch (event.key) {
      case "j": step(1); break;
      case "k": step(-1); break;
      case "f": fitView(); break;
      case "b": $("show-boxes").checked = !$("show-boxes").checked; renderBoxes(); break;
      case "l": $("show-labels").checked = !$("show-labels").checked; renderBoxes(); break;
      case "e": $("edit-mode").checked = !$("edit-mode").checked;
                $("edit-mode").dispatchEvent(new Event("change")); break;
      case "t": activateTab("screentag"); break;
      case "n": if ($("edit-mode").checked) $("add-box").click(); break;
      case "v": setMark("verified"); break;
      case "w": setMark("needs_work"); break;
      case "r": setMark("rejected"); break;
      case "u": setMark("unmarked"); break;
      case "Escape": select(null, false); break;
      case "Delete":
      case "Backspace":
        if (st.selected && $("edit-mode").checked) toggleDelete(st.selected);
        break;
      default: return;
    }
    event.preventDefault();
  });
}

/* The newest run, depth-first through the sorted tree - what to open when the
   URL does not name one. */
function firstRun(nodes) {
  var sorted = sortNodes(nodes || []);
  for (var i = 0; i < sorted.length; i++) {
    if (sorted[i].run) return sorted[i].path;
    var deeper = firstRun(sorted[i].children);
    if (deeper) return deeper;
  }
  return null;
}

api("/api/config").then(function (config) {
  cfg = config;
  st.view = config.views[0];
  var formats = config.image_formats || ["png"];
  st.fmt = formats.indexOf("webp") >= 0 ? "webp" : "png";
  st.exact = formats.indexOf("webp_exact") >= 0 ? "webp_exact" : "png";
  wire();
  return loadTree(false);
}).then(function (data) {
  if (!data || !data.tree || !data.tree.length) return;
  var wanted = readHash().run;
  var run = wanted || firstRun(data.tree);
  if (!run) return;
  revealRun(run);
  openRun(run, true);
}).catch(fail);

})();
