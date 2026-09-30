/* The offline bundle: the same audit page, with the server taken out.
 *
 * This file loads *after* audit.js and replaces only the functions that talk to
 * a server - `get`, `post`, `imageUrl` and the native-pixel path. Everything
 * else, including every keystroke, every canvas rule and the whole three-pass
 * flow, is the same code that runs live. That is deliberate: a second copy of
 * the UI would drift from the first, and the offline session is the one that a
 * second rater uses, so it is the one that must behave identically.
 *
 * Answers go to IndexedDB rather than localStorage: 600 items is a couple of
 * megabytes of answers, localStorage is a ~5MB per-origin budget shared with
 * everything else on that origin, and losing a rater's afternoon to a quota
 * error is not a trade worth making.
 *
 * `bundle.json` carries each item's SHA-256, and every exported answer carries
 * the bundle's rubric reference, so scripts/merge_audit_labels.py can refuse a
 * file whose items are not the ones the answers were given against. */

(function () {
"use strict";

var params = new URLSearchParams(location.search);
var BUNDLE = null;
var RATER = "";
var DB = null;
var ROWS = [];
var SEEN = {};

/* ------------------------------------------------------------- storage */

function openDb() {
  return new Promise(function (resolve, reject) {
    var request = indexedDB.open("deskshot-audit", 1);
    request.onupgradeneeded = function () {
      var db = request.result;
      if (!db.objectStoreNames.contains("labels")) {
        db.createObjectStore("labels", { keyPath: "event_id" });
      }
      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta", { keyPath: "key" });
      }
    };
    request.onsuccess = function () { resolve(request.result); };
    request.onerror = function () { reject(request.error); };
  });
}

function put(store, value) {
  return new Promise(function (resolve, reject) {
    var transaction = DB.transaction([store], "readwrite");
    transaction.objectStore(store).put(value);
    transaction.oncomplete = resolve;
    transaction.onerror = function () { reject(transaction.error); };
  });
}

function all(store) {
  return new Promise(function (resolve, reject) {
    var request = DB.transaction([store], "readonly").objectStore(store).getAll();
    request.onsuccess = function () { resolve(request.result || []); };
    request.onerror = function () { reject(request.error); };
  });
}

/* ---------------------------------------------------------------- state */

function forCampaign(rows) {
  return rows.filter(function (row) { return row.campaign === BUNDLE.campaign; });
}

function statePayload(index, rater) {
  var screen = {}, elements = {}, missed = [], item = {}, others = {};
  var sweep = null, ground = null;
  forCampaign(ROWS).forEach(function (row) {
    if (row.index !== index) return;
    if (row.rater !== rater) { others[row.rater] = 1; return; }
    if (row.scope === "screen") screen[row.question] = row;
    else if (row.scope === "element") elements[row.element_key] = row;
    else if (row.scope === "missed") { missed = row.points || []; item.missed_at = row.at; }
    else if (row.scope === "sweep") sweep = row;
    else if (row.scope === "ground") ground = row;
    else if (row.scope === "item") {
      item.status = row.status;
      if (row.reason) item.reason = row.reason;
      item.at = row.at;
    }
  });
  return {
    screen: screen, elements: elements, missed: missed, sweep: sweep,
    ground: ground, item: item,
    other_raters: Object.keys(others).sort(),
    any_rater_answers: 0
  };
}

function progressPayload(rater) {
  var done = {}, skipped = {}, raters = {}, answers = 0, elementAnswers = 0;
  forCampaign(ROWS).forEach(function (row) {
    if (rater && row.rater !== rater) return;
    raters[row.rater] = (raters[row.rater] || 0) + 1;
    if (row.scope === "screen" || row.scope === "element" || row.scope === "missed") answers += 1;
    if (row.scope === "element") elementAnswers += 1;
    if (row.scope === "sweep" && !skipped[row.index]) done[row.index] = 1;
    if (row.scope === "item") {
      if (row.status === "done") { done[row.index] = 1; delete skipped[row.index]; }
      else if (row.status === "skipped") { skipped[row.index] = 1; delete done[row.index]; }
    }
  });
  var next = null;
  for (var i = 0; i < BUNDLE.items.length; i++) {
    if (!done[i] && !skipped[i]) { next = i; break; }
  }
  return {
    campaign: BUNDLE.campaign,
    total: BUNDLE.items.length,
    done: Object.keys(done).length,
    skipped: Object.keys(skipped).length,
    answers: answers,
    element_answers: elementAnswers,
    next: next,
    done_indices: Object.keys(done).map(Number).sort(function (a, b) { return a - b; }),
    skipped_indices: Object.keys(skipped).map(Number).sort(function (a, b) { return a - b; }),
    raters: raters,
    rubric: BUNDLE.rubric_ref
  };
}

/* -------------------------------------------------- the replaced fetches */

function query(url) {
  var out = {};
  var mark = url.indexOf("?");
  if (mark < 0) return out;
  url.slice(mark + 1).split("&").forEach(function (pair) {
    var bits = pair.split("=");
    out[decodeURIComponent(bits[0])] = decodeURIComponent(bits.slice(1).join("=") || "");
  });
  return out;
}

function itemPayload(index) {
  var row = BUNDLE.items[index];
  if (!row) return Promise.reject(new Error("no item " + index + " in this bundle"));
  return fetch(row.payload).then(function (response) {
    if (!response.ok) throw new Error("missing " + row.payload);
    return response.json();
  }).then(function (payload) {
    /* Shape it exactly like /api/audit/item, so the page cannot tell. */
    return {
      campaign: BUNDLE.campaign,
      index: index,
      count: BUNDLE.items.length,
      item: payload.item,
      rater: RATER,
      read_only: false,
      elements: payload.elements,
      sampled: payload.sampled,
      sampled_missing: (payload.item.element_keys || []).filter(function (key) {
        return payload.sampled.indexOf(key) < 0;
      }),
      screentag: null,
      automated: { verdict: null, shard_pixel_audit: null },
      offline: { image: payload.image, crops: payload.crops || {} }
    };
  });
}

function offlineGet(url, options) {
  if (options && options.method === "POST") return offlinePost(url, options);
  var parameters = query(url);
  if (url.indexOf("/api/audit/campaigns") === 0) {
    return Promise.resolve({
      campaigns: [{
        name: BUNDLE.campaign,
        title: (BUNDLE.source_manifest || {}).title || BUNDLE.campaign,
        created_at: BUNDLE.packed_at,
        seed: (BUNDLE.source_manifest || {}).seed,
        progress: progressPayload(RATER)
      }],
      rater: RATER,
      read_only: false
    });
  }
  if (url.indexOf("/api/audit/queue") === 0) {
    return Promise.resolve({
      campaign: BUNDLE.campaign,
      title: (BUNDLE.source_manifest || {}).title || BUNDLE.campaign,
      rubric: BUNDLE.rubric,
      rubric_ref: BUNDLE.rubric_ref,
      mode: BUNDLE.mode || "sample",
      element_sample: BUNDLE.element_sample,
      rater: RATER,
      read_only: false,
      from: 0,
      items: BUNDLE.items.map(function (row) {
        var state = statePayload(row.index, RATER);
        return {
          index: row.index,
          observation_key: row.observation_key,
          stratum: row.stratum,
          apps: row.apps,
          n_elements: row.n_elements,
          occluded_ratio: row.occluded_ratio,
          draw: row.draw,
          status: state.item.status,
          answered: Object.keys(state.screen).length + Object.keys(state.elements).length,
          missed: state.missed.length
        };
      }),
      progress: progressPayload(RATER),
      torn_bytes: 0
    });
  }
  if (url.indexOf("/api/audit/item") === 0) {
    return itemPayload(parseInt(parameters.index, 10));
  }
  if (url.indexOf("/api/audit/state") === 0) {
    var indices = String(parameters.indices || "").split(",")
      .filter(function (part) { return part !== ""; }).map(Number);
    var states = {};
    indices.forEach(function (index) { states[String(index)] = statePayload(index, RATER); });
    return Promise.resolve({
      campaign: BUNDLE.campaign, rater: RATER, states: states,
      progress: progressPayload(RATER)
    });
  }
  return Promise.reject(new Error("offline bundle has no " + url));
}

function offlinePost(url, options) {
  var payload = JSON.parse(options.body);
  if (url.indexOf("/api/audit/label") !== 0) {
    return Promise.reject(new Error("offline bundle has no " + url));
  }
  /* Several answers in one request, the same shape the server takes. Accepting
   * a sample is five judgements and a done row, and a sweep sends its census
   * and its done row together so the two cannot be separated by a rater moving
   * on mid-flight. */
  if (Array.isArray(payload.answers)) {
    var base = { campaign: payload.campaign, index: payload.index,
                 rater: payload.rater };
    return payload.answers.reduce(function (chain, answer) {
      return chain.then(function () {
        return storeRow(Object.assign({}, base, answer));
      });
    }, Promise.resolve()).then(function () {
      var index = payload.index;
      return { stored: true, written: payload.answers.length, duplicate: 0,
               index: index, state: statePayload(index, RATER),
               progress: progressPayload(RATER) };
    });
  }
  return storeRow(payload).then(function (result) {
    return Object.assign(result, {
      index: payload.index,
      state: statePayload(payload.index, RATER),
      progress: progressPayload(RATER)
    });
  });
}

function storeRow(payload) {
  var row = Object.assign({}, payload, {
    schema: "deskshot.audit.label/1",
    campaign: BUNDLE.campaign,
    rater: RATER,
    rubric: BUNDLE.rubric_ref,
    at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    observation_key: (BUNDLE.items[payload.index] || {}).observation_key,
    offline: true
  });
  delete row.rubric_ref;
  if (row.scope === "element") {
    row.sampled = ((BUNDLE.items[row.index] || {}).element_keys || [])
      .indexOf(row.element_key) >= 0;
    row.source = row.source || (row.sampled ? "sampled" : "sweep");
  }
  if (row.scope === "missed") row.n_points = (row.points || []).length;
  if (row.scope === "sweep") {
    row.declared_correct = Math.max(0, (row.n_elements || 0) - (row.flagged || []).length);
  }
  if (SEEN[row.event_id]) {
    return Promise.resolve({ stored: false, duplicate: true });
  }
  return put("labels", row).then(function () {
    ROWS.push(row);
    SEEN[row.event_id] = 1;
    return { stored: true, duplicate: false };
  });
}

/* Images are files in the bundle. A region request is the live page asking for
 * native pixels of an arbitrary viewport rectangle, which a folder of files
 * cannot answer - so that path is switched off (below) and the sampled
 * element's packed lossless crop is used instead, which is the only place the
 * extra resolution was doing any work. */
function offlineImageUrl(index) {
  var row = BUNDLE.items[index];
  return row ? row.image : "";
}

/* ---------------------------------------------------------------- export */

function exportLabels() {
  var mine = forCampaign(ROWS);
  if (!mine.length) {
    window.alert("nothing to export yet");
    return;
  }
  var body = mine.map(function (row) { return JSON.stringify(row); }).join("\n") + "\n";
  var blob = new Blob([body], { type: "application/x-ndjson" });
  var link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "labels-" + BUNDLE.campaign + "-" + (RATER || "rater") + ".jsonl";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  setTimeout(function () { URL.revokeObjectURL(link.href); }, 4000);
}

var NAME_OK = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/* Who is labelling, without a modal dialog.
 *
 * `?rater=` wins, then whatever this browser stored last, then a placeholder.
 * A `window.prompt` on load would be worse for the rater and unusable for a
 * headless browser, which auto-dismisses or blocks on dialogs - and the whole
 * point of shipping the same UI offline is that the same probe can drive it. */
function resolveRater() {
  var wanted = params.get("rater") || "";
  if (NAME_OK.test(wanted)) {
    return put("meta", { key: "rater", value: wanted }).then(function () { return wanted; });
  }
  return all("meta").then(function (rows) {
    var stored = rows.filter(function (row) { return row.key === "rater"; })[0];
    var name = stored && stored.value;
    return NAME_OK.test(name || "") ? name : "unnamed";
  });
}

/* ------------------------------------------------------------------ boot */

function chrome() {
  var button = document.getElementById("export");
  if (button) button.addEventListener("click", exportLabels);
  var header = document.getElementById("rater");
  if (!header) return;
  header.textContent = "";
  var field = document.createElement("input");
  field.type = "text";
  field.value = RATER;
  field.size = 12;
  field.title = "who is labelling; stamped into every answer";
  field.setAttribute("aria-label", "rater");
  field.addEventListener("change", function () {
    if (!NAME_OK.test(field.value)) {
      field.value = RATER;
      return;
    }
    put("meta", { key: "rater", value: field.value }).then(function () {
      location.search = "?rater=" + encodeURIComponent(field.value);
    });
  });
  header.appendChild(field);
  header.appendChild(document.createTextNode(" · offline bundle"));
  if (RATER === "unnamed") {
    var note = document.getElementById("board-note");
    if (note) note.textContent = "Put your name in the header before you start — " +
      "it is how a second rater's answers are told from the first's.";
  }
}

fetch("bundle.json").then(function (response) {
  if (!response.ok) throw new Error("no bundle.json beside this page");
  return response.json();
}).then(function (bundle) {
  BUNDLE = bundle;
  return openDb();
}).then(function (db) {
  DB = db;
  return all("labels");
}).then(function (rows) {
  ROWS = rows;
  rows.forEach(function (row) { SEEN[row.event_id] = 1; });
  return resolveRater();
}).then(function (name) {
  RATER = name;
  window.get = offlineGet;
  window.post = function (url, payload) {
    return offlineGet(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  };
  window.imageUrl = offlineImageUrl;
  /* Never ask for a crop of an arbitrary region: there is no server to make
     one. The packed per-element crop is installed by the focus hook instead. */
  window.DETAIL_TRIGGER = Infinity;
  var focus = window.focusElement;
  window.focusElement = function () {
    focus();
    var payload = window.st.offlineItem;
    var key = window.st.sampled[window.st.element];
    var crop = payload && payload.crops && payload.crops[key];
    if (!crop) { window.st.detail = null; return; }
    var image = new Image();
    image.onload = function () {
      window.st.detail = { x: crop.x, y: crop.y, w: crop.w, h: crop.h, img: image };
      window.draw();
    };
    image.src = crop.file;
  };
  /* Remember the payload the page just loaded, so the image and crop lookups
     above can find the files it named. */
  var fetchItem = window.fetchItem;
  window.fetchItem = function (index) {
    return fetchItem(index).then(function (payload) {
      if (index === window.st.index || !window.st.offlineItem) {
        window.st.offlineItem = payload.offline;
      }
      return payload;
    });
  };
  return window.auditStart();
}).then(chrome).catch(function (error) {
  var note = document.getElementById("board-note");
  if (note) note.textContent = "offline bundle: " + error.message;
  else window.alert("offline bundle: " + error.message);
});
})();
