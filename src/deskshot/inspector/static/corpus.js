/* Lazy browser for a corpus that must never be listed in full.
 *
 * Every navigation is one /api/corpus/ls call for one directory. Nothing is
 * prefetched and nothing is cached across levels, because the point of the
 * endpoint is that a level costs the same whether it holds 3 entries or 986.
 *
 * A capture is rendered by asking the server for a figure. The bytes are not
 * stored anywhere - the <img> src is the render - so what you see is always
 * drawn from the annotation on disk at the moment you clicked. */

var st = {
  path: "",
  sample: null,
  detail: null,
  tab: "facts",
  seed: 1,
  results: null
};

function el(tag, attrs, kids) {
  var node = document.createElement(tag);
  Object.keys(attrs || {}).forEach(function (k) {
    if (k === "class") node.className = attrs[k];
    else if (k === "text") node.textContent = attrs[k];
    else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), attrs[k]);
    else node.setAttribute(k, attrs[k]);
  });
  (kids || []).forEach(function (kid) { if (kid) node.appendChild(kid); });
  return node;
}

function get(url) {
  return fetch(url).then(function (r) {
    return r.json().then(function (body) {
      if (!r.ok) throw new Error(body.error || r.statusText);
      return body;
    });
  });
}

function bytes(n) {
  if (n == null) return "";
  if (n > 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n > 1024) return Math.round(n / 1024) + " KB";
  return n + " B";
}

/* ------------------------------------------------------------- browsing */

function browse(path) {
  st.path = path || "";
  st.results = null;
  location.hash = st.path ? "#" + st.path : "";
  document.getElementById("listing-title").textContent = "browse";
  return get("/api/corpus/ls?path=" + encodeURIComponent(st.path) + "&limit=400")
    .then(render)
    .catch(function (e) { note(e.message); });
}

function crumbs(parents) {
  var host = document.getElementById("crumbs");
  host.textContent = "";
  parents.forEach(function (crumb, i) {
    if (i) host.appendChild(el("span", { class: "sep", text: "/" }));
    host.appendChild(el("a", { text: crumb.name, onclick: function () { browse(crumb.path); } }));
  });
}

function render(page) {
  crumbs(page.parents || []);
  var host = document.getElementById("entries");
  host.textContent = "";

  if (page.path) {
    var up = page.parents[page.parents.length - 2];
    host.appendChild(el("button", {
      class: "entry", onclick: function () { browse(up ? up.path : ""); }
    }, [el("span", { class: "name", text: "‹ up" })]));
  }

  (page.dirs || []).forEach(function (dir) {
    var sub = [];
    if (dir.captures) sub.push(dir.captures + " captures");
    if (dir.dirs) sub.push(dir.dirs + " folders");
    if (!sub.length && dir.children != null) sub.push(dir.children + " entries");
    host.appendChild(el("button", {
      class: "entry", onclick: function () { browse(dir.path); }
    }, [
      el("span", { class: "name" }, [
        el("span", { class: "dirmark", text: "▸" }),
        document.createTextNode(dir.name)
      ]),
      el("span", { class: "sub", text: sub.join(" · ") })
    ]));
  });

  (page.samples || []).forEach(function (sample) { host.appendChild(sampleRow(sample)); });

  var tail = [];
  if (page.total_dirs > (page.dirs || []).length + page.offset)
    tail.push((page.total_dirs - page.dirs.length) + " more folders not shown");
  (page.other_files || []).forEach(function (f) {
    tail.push(f.count + " " + f.extension);
  });
  document.getElementById("more").textContent = "";
  if (tail.length)
    document.getElementById("more").appendChild(
      el("p", { class: "muted", text: tail.join("  ·  "), style: "padding:6px 12px" }));

  var scope = [];
  if (page.total_dirs) scope.push(page.total_dirs + " folders");
  if (page.total_samples) scope.push(page.total_samples + " captures");
  document.getElementById("scope").textContent = scope.join(" · ");
}

function sampleRow(sample) {
  var chips = [];
  if (sample.split) chips.push(el("span", { class: "chip split-" + sample.split, text: sample.split }));
  if (sample.publishable === false) chips.push(el("span", { class: "chip warn", text: "rejected" }));
  else if (sample.train_eligible === false) chips.push(el("span", { class: "chip", text: "not for training" }));

  var facts = [];
  if (sample.width) facts.push(sample.width + "×" + sample.height);
  if (sample.n_elements != null) facts.push(sample.n_elements + " elem");
  if (sample.n_windows != null) facts.push(sample.n_windows + " win");
  if (sample.occluded_ratio != null) facts.push(Math.round(sample.occluded_ratio * 100) + "% occl");
  if (!facts.length) facts.push(bytes(sample.bytes));

  var sub = el("span", { class: "sub" });
  chips.forEach(function (c) { sub.appendChild(c); });
  sub.appendChild(document.createTextNode(facts.join(" · ")));

  var row = el("button", {
    class: "entry" + (st.sample === sample.path ? " on" : ""),
    onclick: function () { openCapture(sample.path); }
  }, [el("span", { class: "name", text: sample.stem }), sub]);
  row.dataset.path = sample.path;
  return row;
}

/* ------------------------------------------------------------- one sample */

function openCapture(path) {
  st.sample = path;
  Array.prototype.forEach.call(document.querySelectorAll(".entry"), function (node) {
    node.classList.toggle("on", node.dataset.path === path);
  });
  get("/api/corpus/sample?path=" + encodeURIComponent(path)).then(function (detail) {
    st.detail = detail;
    var view = document.getElementById("c-view");
    view.textContent = "";
    (detail.views || ["leaf"]).forEach(function (name) {
      view.appendChild(el("option", { value: name, text: name }));
    });
    view.value = detail.view || "leaf";
    draw();
    panel();
  }).catch(function (e) { note(e.message); });
}

function figureUrl() {
  var canvas = document.getElementById("canvas");
  var width = Math.max(640, Math.min(Math.round(canvas.clientWidth * 1.6), 3200));
  return "/api/corpus/figure?path=" + encodeURIComponent(st.sample)
    + "&mode=" + document.getElementById("c-mode").value
    + "&labels=" + document.getElementById("c-labels").value
    + "&style=" + document.getElementById("c-style").value
    + "&view=" + document.getElementById("c-view").value
    + "&occlusion=" + (document.getElementById("c-occ").checked ? 1 : 0)
    + "&legend=" + (document.getElementById("c-legend").checked ? 1 : 0)
    + "&width=" + width;
}

function draw() {
  if (!st.sample) return;
  var canvas = document.getElementById("canvas");
  canvas.classList.add("busy");
  document.getElementById("c-state").textContent = "rendering…";
  var started = Date.now();
  var img = new Image();
  img.onload = function () {
    canvas.textContent = "";
    canvas.appendChild(img);
    canvas.classList.remove("busy");
    document.getElementById("c-state").textContent =
      "rendered on the fly in " + (Date.now() - started) + " ms · not stored";
  };
  img.onerror = function () {
    canvas.classList.remove("busy");
    document.getElementById("c-state").textContent = "render failed";
  };
  img.src = figureUrl();
}

/* ------------------------------------------------------------------ panel */

function panel() {
  var host = document.getElementById("panel");
  host.textContent = "";
  var detail = st.detail;
  if (!detail) return;

  if (st.tab === "tag") {
    host.appendChild(el("pre", { text: detail.screentag || "(no .screentag.txt)" }));
    return;
  }
  if (st.tab === "meta") {
    host.appendChild(el("pre", { text: JSON.stringify(detail.meta || {}, null, 1) }));
    return;
  }

  var rows = [];
  var facts = detail.facts || {};
  rows.push(["stem", detail.stem]);
  rows.push(["path", detail.dir]);
  if (detail.width) rows.push(["viewport", detail.width + " × " + detail.height]);
  ["split", "theme", "resolution", "n_elements", "n_windows"].forEach(function (k) {
    if (facts[k] != null) rows.push([k, String(facts[k])]);
  });
  if (facts.occluded_ratio != null)
    rows.push(["occluded", Math.round(facts.occluded_ratio * 1000) / 10 + " %"]);
  if (facts.apps) rows.push(["apps", facts.apps.join(", ")]);
  if (facts.publishable != null) rows.push(["publishable", String(facts.publishable)]);
  if (facts.train_eligible != null) rows.push(["train eligible", String(facts.train_eligible)]);
  if (detail.episode) rows.push(["episode steps", String((detail.episode.steps || []).length)]);
  if (!detail.facts)
    rows.push(["index", "none — build plan/index.sqlite for split and occlusion"]);

  var table = el("table");
  rows.forEach(function (row) {
    table.appendChild(el("tr", {}, [
      el("td", { class: "k", text: row[0] }),
      el("td", { text: row[1] == null ? "" : row[1] })
    ]));
  });
  host.appendChild(table);

  if (detail.verdict && detail.verdict.hard_failures &&
      detail.verdict.hard_failures.length) {
    host.appendChild(el("h2", { text: "hard failures" }));
    host.appendChild(el("pre", { text: detail.verdict.hard_failures.join("\n") }));
  }
}

/* ----------------------------------------------------------------- search */

function note(text) { document.getElementById("f-note").textContent = text || ""; }

function fillFacets(facets) {
  if (!facets.indexed) {
    note("no plan/index.sqlite — browsing works, filters need the index "
         + "(scripts/build_corpus_index.py)");
    ["f-split", "f-app", "f-theme", "f-resolution", "f-occ", "f-win", "f-go", "f-rand"]
      .forEach(function (id) { document.getElementById(id).disabled = true; });
    return;
  }
  [["f-split", "split"], ["f-app", "app"], ["f-theme", "theme"],
   ["f-resolution", "resolution"]].forEach(function (pair) {
    var select = document.getElementById(pair[0]);
    select.appendChild(el("option", { value: "", text: "any" }));
    (facets[pair[1]] || []).forEach(function (item) {
      select.appendChild(el("option", {
        value: item.value, text: item.value + "  (" + item.count.toLocaleString() + ")"
      }));
    });
  });
  note(facets.total.toLocaleString() + " captures indexed");
}

function search(shuffle) {
  var params = ["limit=200"];
  [["f-split", "split"], ["f-app", "app"], ["f-theme", "theme"],
   ["f-resolution", "resolution"]].forEach(function (pair) {
    var value = document.getElementById(pair[0]).value;
    if (value) params.push(pair[1] + "=" + encodeURIComponent(value));
  });
  var occ = document.getElementById("f-occ").value;
  if (occ !== "") params.push("min_occlusion=" + occ);
  var win = document.getElementById("f-win").value;
  if (win !== "") params.push("min_windows=" + win);
  if (shuffle) { st.seed += 1; params.push("random=1&seed=" + st.seed); }

  get("/api/corpus/search?" + params.join("&")).then(function (found) {
    st.results = found;
    document.getElementById("listing-title").textContent = "search";
    document.getElementById("crumbs").textContent = "";
    var host = document.getElementById("entries");
    host.textContent = "";
    found.results.forEach(function (sample) { host.appendChild(sampleRow(sample)); });
    document.getElementById("more").textContent = "";
    note(found.total.toLocaleString() + " match"
         + (found.total === 1 ? "" : "es") + ", showing " + found.results.length);
    document.getElementById("scope").textContent = found.results.length + " results";
  }).catch(function (e) { note(e.message); });
}

/* ------------------------------------------------------------------- wire */

["c-mode", "c-labels", "c-style", "c-view", "c-occ", "c-legend"].forEach(function (id) {
  document.getElementById(id).addEventListener("change", draw);
});
document.getElementById("c-reload").addEventListener("click", draw);
document.getElementById("f-go").addEventListener("click", function () { search(false); });
document.getElementById("f-rand").addEventListener("click", function () { search(true); });
document.getElementById("f-clear").addEventListener("click", function () {
  ["f-split", "f-app", "f-theme", "f-resolution", "f-occ", "f-win"].forEach(function (id) {
    document.getElementById(id).value = "";
  });
  browse(st.path);
  note("");
});
Array.prototype.forEach.call(document.querySelectorAll("#tabs button"), function (button) {
  button.addEventListener("click", function () {
    st.tab = button.dataset.tab;
    Array.prototype.forEach.call(document.querySelectorAll("#tabs button"), function (other) {
      other.classList.toggle("on", other === button);
    });
    panel();
  });
});
window.addEventListener("hashchange", function () {
  var want = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (want !== st.path) browse(want);
});

get("/api/corpus/facets").then(fillFacets).catch(function (e) { note(e.message); });
browse(decodeURIComponent(location.hash.replace(/^#/, "")));
