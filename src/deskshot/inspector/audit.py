"""Human audit campaigns: a fixed queue, a rubric, and an append-only log.

The inspector already browses the corpus. What it could not do is *record a
judgement* about it: `/api/corpus/*` is read-only by construction and every
marking affordance is keyed to a run under `--root`. This module is the missing
half, and it is deliberately shaped around three facts about a real labelling
session.

**The queue is drawn once, before any labelling.** A campaign is a frozen list
of items with the seed and filters that produced it, so the sample cannot drift
while it is being worked through and cannot be quietly re-drawn after seeing the
answers. `scripts/build_audit_queue.py` writes it; nothing here changes it.

**Judgements are appended, never rewritten.** One row per answer *event*, so a
session that is killed mid-item loses one keystroke instead of the session, and
the log doubles as the audit trail (who, when, how long they looked). The
current answer to a question is the last row that mentions it, which also makes
"change my mind" free. A torn trailing line - what a `kill -9` mid-write leaves
behind - is dropped on read and the next append repairs the missing newline,
because appending after a newline-less line glues two records into one.

**The corpus is never written to.** Nothing in this module resolves a path
under the corpus root for writing. Labels, corrections and statistics live in
the campaign directory, which is in the repository and is the deliverable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from deskshot.inspector import overlay as overlay_mod

SCHEMA = "deskshot.audit.campaign/1"
LABEL_SCHEMA = "deskshot.audit.label/1"
QUEUE_SCHEMA = "deskshot.audit.item/1"

MANIFEST_NAME = "manifest.json"
QUEUE_NAME = "queue.jsonl"
LABELS_NAME = "labels.jsonl"
CORRECTIONS_DIRNAME = "corrections"
RUBRICS_DIRNAME = "rubrics"

#: The element fields the release actually publishes, and therefore the only
#: ones a human is asked to judge. The raw `.elements.leaf.json` carries ~40
#: fields per element and runs to 480KB a capture; most of them are the
#: pipeline's own bookkeeping (`_dom_index`, `_source_parent_dom_index`,
#: `interaction`, `attrs`), which nobody can audit by looking at a screenshot
#: and which would be a third of a megabyte per item over a tunnel.
ELEMENT_FIELDS = (
    "uid",
    "type",
    "role",
    "kind",
    "name",
    "visible_text",
    "visible_text_status",
    "rect",
    "visible_fragments",
    "is_occluded",
    "occlusion_state",
    "app_name",
    "reading_order_index",
    "_window_stack_index",
)

#: What a label row's `scope` may be.
#:
#: - `screen` answers a whole-capture rubric question;
#: - `element` judges one element;
#: - `missed` records points a rater clicked on elements with no annotation;
#: - `sweep` declares every element on a screen correct except the ones flagged;
#: - `ground` is where a rater clicked from the instruction alone, before seeing
#:   the answer - the one judgement the machine can score for itself;
#: - `item` is bookkeeping about the item itself (done, skipped, dwell).
SCOPES = ("screen", "element", "missed", "sweep", "ground", "item")

#: How an element came to be judged, recorded on every element row.
#:
#: `sampled` means a rater was shown that element on its own, zoomed, and had
#: to answer about it - one row per element, whatever the verdict. `sweep` means
#: they were working through a whole screen, and a row exists only where they
#: stopped: the screen's other elements carry their verdict on the sweep row.
#: `audit_stats.py` reads them differently for that reason - a rate over sweep
#: rows alone would be a rate over the exceptions - and not because one
#: procedure is worth less than the other.
ELEMENT_SOURCES = ("sampled", "sweep")

#: The two ways to work through a queue.
#:
#: `sample` judges a fixed few elements per screen, individually. `sweep` shows
#: the whole dense annotation and asks only for the exceptions: one verdict per
#: element, correct unless flagged. Declaring the rest correct is a judgement on
#: each of them, so a sweep is a census of the screen and not a sample of it.
#: What a campaign's items are.
#:
#: `observation` is a screen and its dense annotation - is every element right.
#: `instruction` is one action sample: a screen, the natural-language
#: instruction generated for it, and the single target the model is trained to
#: click. Different question, different rubric, same machinery underneath.
KINDS = ("observation", "instruction")
DEFAULT_KIND = "observation"

MODES = ("sweep", "sample")

#: What a new campaign gets when nothing is asked for.
DEFAULT_MODE = "sweep"

#: What a campaign whose manifest predates the field *was*. Not the same
#: constant: a queue drawn before sweeping existed was worked through in sample
#: mode, and its answers mean what they meant when they were given. Letting a
#: change of default reinterpret finished work is how a record stops being one.
LEGACY_MODE = "sample"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AuditError(Exception):
    def __init__(self, status: int, message: str, extra: Optional[Dict[str, Any]] = None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_name(name: str, what: str = "name") -> str:
    """Campaign and rater names end up in paths and in every label row."""
    name = (name or "").strip()
    if not _SAFE_NAME.match(name):
        raise AuditError(400, "invalid %s %r" % (what, name))
    return name


# --------------------------------------------------------------------------
# the rubric


#: The questions asked by default. Kept in code so a campaign can be started
#: without authoring a file first, and copied into the campaign directory at
#: build time so an answered campaign always carries the exact questions that
#: were asked.
#:
#: Element flags are a *set*, not one choice: an element can be both
#: misclassified and mislocalized, and collapsing that to a single verdict
#: throws away the per-class error the audit exists to measure. `ok` is
#: mutually exclusive with the rest and the store enforces it.
DEFAULT_RUBRIC: Dict[str, Any] = {
    "id": "annotation_v1",
    "title": "DeskForge annotation audit",
    "element_sample": 8,
    "questions": [
        {
            "id": "screen_usable",
            "scope": "screen",
            "prompt": "Overall, is this screen's annotation usable?",
            "help": "Judge the annotation as a whole, not individual boxes.",
            "options": [
                {"key": "1", "value": "good", "label": "good"},
                {"key": "2", "value": "partial", "label": "partly wrong"},
                {"key": "3", "value": "unusable", "label": "unusable"},
            ],
        },
        {
            "id": "screen_missing_region",
            "scope": "screen",
            "prompt": "Is a whole window or panel left unannotated?",
            "options": [
                {"key": "q", "value": "no", "label": "no"},
                {"key": "w", "value": "yes", "label": "yes"},
            ],
        },
        {
            "id": "screen_occlusion",
            "scope": "screen",
            "prompt": "Is the occlusion resolution plausible?",
            "help": "Are covered elements marked covered, and visible ones not?",
            "options": [
                {"key": "a", "value": "ok", "label": "plausible"},
                {"key": "s", "value": "wrong", "label": "wrong"},
                {"key": "d", "value": "na", "label": "nothing occluded"},
            ],
        },
        {
            "id": "element",
            "scope": "element",
            "multi": True,
            "exclusive": ["ok"],
            "prompt": "Judge this element.",
            "options": [
                {"key": "a", "value": "ok", "label": "correct"},
                {"key": "s", "value": "wrong_class", "label": "wrong class"},
                {"key": "d", "value": "bad_geometry", "label": "box wrong"},
                {"key": "f", "value": "phantom", "label": "nothing there"},
                {"key": "g", "value": "wrong_text", "label": "text wrong"},
                {"key": "h", "value": "wrong_occlusion", "label": "occlusion wrong"},
                {"key": "x", "value": "unsure", "label": "unsure"},
            ],
        },
    ],
}


#: The instruction audit.
#:
#: The first question is not a rubric question at all: the rater is shown the
#: screen and the instruction with no box, and clicks where they would click.
#: That is scored against the recorded target automatically, and it is the
#: strongest evidence available about whether an instruction is answerable and
#: unambiguous - stronger than asking, because it cannot be answered
#: charitably. Everything else is asked afterwards, with the target revealed.
INSTRUCTION_RUBRIC: Dict[str, Any] = {
    "id": "instruction_v1",
    "title": "DeskForge instruction and target audit",
    "kind": "instruction",
    "questions": [
        {
            "id": "instruction_ok",
            "scope": "screen",
            "prompt": "Is the instruction a legitimate thing to ask?",
            "help": ("Fluent, means something, and describes something a person "
                     "could act on from this screen."),
            "options": [
                {"key": "1", "value": "good", "label": "good", "accept": True},
                {"key": "2", "value": "vague", "label": "too vague to act on"},
                {"key": "3", "value": "not_visible",
                 "label": "refers to something not on screen"},
                {"key": "4", "value": "leaks",
                 "label": "mentions coordinates or annotation internals"},
                {"key": "5", "value": "unnatural", "label": "not natural English"},
            ],
        },
        {
            "id": "target_match",
            "scope": "screen",
            "prompt": "Does the marked target match the instruction?",
            "help": "Now that the box is revealed.",
            "options": [
                {"key": "q", "value": "match", "label": "yes, that is the thing",
                 "accept": True},
                {"key": "w", "value": "other_element",
                 "label": "no — it means a different element"},
                {"key": "e", "value": "too_big",
                 "label": "the box is a container, not the widget"},
                {"key": "r", "value": "off", "label": "the box is off the thing entirely"},
            ],
        },
        {
            "id": "unique",
            "scope": "screen",
            "prompt": "How many things on this screen satisfy the instruction?",
            "help": "Ambiguity is the failure a grounding model cannot recover from.",
            "options": [
                {"key": "a", "value": "one", "label": "exactly one", "accept": True},
                {"key": "s", "value": "several", "label": "several, equally well"},
                {"key": "d", "value": "none", "label": "none"},
            ],
        },
        {
            "id": "target_visible",
            "scope": "screen",
            "prompt": "Is the target actually visible and clickable?",
            "options": [
                {"key": "z", "value": "visible", "label": "fully visible",
                 "accept": True},
                {"key": "x", "value": "partly", "label": "partly covered but findable"},
                {"key": "c", "value": "hidden", "label": "not really visible"},
            ],
        },
        {
            "id": "effect",
            "scope": "screen",
            "optional": True,
            "prompt": "Does what happened next match what the instruction promised?",
            "help": ("Press o to see the screen after the click. Skip this one "
                     "when the instruction only names a target."),
            "options": [
                {"key": "j", "value": "match", "label": "yes", "accept": True},
                {"key": "k", "value": "mismatch", "label": "no, something else happened"},
                {"key": "l", "value": "nothing", "label": "nothing visibly happened"},
                {"key": "n", "value": "na", "label": "instruction makes no promise"},
            ],
        },
    ],
}


def accept_answers(spec: Dict[str, Any], scope: str = "screen") -> List[Dict[str, Any]]:
    """The answers "nothing is wrong here" stands for, read off the rubric.

    Most samples are correct, so the common case has to be one keystroke rather
    than five. Which value each question means by that is marked in the rubric
    with `accept`, not hardcoded here, so a changed question changes the fast
    path with it - and the page shows the reader exactly what the keystroke is
    about to assert, because a single key that records five judgements has to
    be an informed one.
    """
    out: List[Dict[str, Any]] = []
    for question in spec.get("questions") or []:
        if question.get("scope") != scope:
            continue
        for option in question.get("options") or []:
            if option.get("accept"):
                out.append({"question": question["id"], "value": option["value"],
                            "label": option.get("label") or option["value"],
                            "prompt": question.get("prompt")})
                break
    return out


def rubric_for_kind(kind: str) -> Dict[str, Any]:
    return INSTRUCTION_RUBRIC if kind == "instruction" else DEFAULT_RUBRIC


def rubric_digest(spec: Dict[str, Any]) -> str:
    payload = json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def rubric_ref(spec: Dict[str, Any]) -> str:
    """`<id>@<digest>` - stamped into every label row.

    A campaign whose questions changed halfway through is not one dataset, and
    the only way to notice afterwards is to have recorded which questions each
    answer was an answer to.
    """
    return "%s@%s" % (spec.get("id") or "rubric", rubric_digest(spec))


def validate_rubric(spec: Any) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        raise AuditError(400, "a rubric is a JSON object")
    if not spec.get("id"):
        raise AuditError(400, "the rubric needs an id")
    questions = spec.get("questions")
    if not isinstance(questions, list) or not questions:
        raise AuditError(400, "the rubric needs a non-empty questions list")
    seen_ids: Dict[str, bool] = {}
    for question in questions:
        if not isinstance(question, dict):
            raise AuditError(400, "each question is an object")
        qid = question.get("id")
        if not qid or qid in seen_ids:
            raise AuditError(400, "each question needs a unique id (%r)" % (qid,))
        seen_ids[qid] = True
        if question.get("scope") not in ("screen", "element"):
            raise AuditError(400, "question %s: scope is screen or element" % qid)
        options = question.get("options")
        if not isinstance(options, list) or not options:
            raise AuditError(400, "question %s: needs options" % qid)
        keys: Dict[str, bool] = {}
        for option in options:
            if not isinstance(option, dict) or not option.get("value"):
                raise AuditError(400, "question %s: each option needs a value" % qid)
            key = option.get("key") or ""
            if key and key in keys:
                raise AuditError(400, "question %s: hotkey %r used twice" % (qid, key))
            keys[key] = True
    return spec


def load_rubric(path: Path) -> Dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            spec = json.load(handle)
    except OSError as error:
        raise AuditError(404, "cannot read rubric %s: %s" % (path, error))
    except ValueError as error:
        raise AuditError(400, "rubric %s is not valid JSON: %s" % (path, error))
    return validate_rubric(spec)


def question_map(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {question["id"]: question for question in spec.get("questions") or []}


def allowed_values(question: Dict[str, Any]) -> List[str]:
    return [option["value"] for option in question.get("options") or []]


# --------------------------------------------------------------------------
# element projection and sampling


def project_element(element: Dict[str, Any]) -> Dict[str, Any]:
    """Only what the release publishes, which is all a human can judge."""
    projected = {field: element[field] for field in ELEMENT_FIELDS if field in element}
    projected["key"] = overlay_mod.element_key(element)
    return projected


def project_elements(elements: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keys = overlay_mod.element_keys(elements)
    out = []
    for key, element in zip(keys, elements):
        projected = project_element(element)
        projected["key"] = key
        out.append(projected)
    return out


def sample_element_keys(
    elements: Sequence[Dict[str, Any]], count: int, seed_material: str
) -> List[str]:
    """Pick which elements a rater judges - seeded, and spread over classes.

    Uniform sampling would spend every judgement on `Button` and `Text` and
    never reach the classes the release itself flags as least reliable
    (window controls are synthesised from the title bar rather than read from
    the tree). So the draw is round-robin over `type`: one from each class in
    turn, ordered by a hash of the key so it is reproducible from the campaign
    seed alone and independent of the order the file happens to be in.
    """
    keys = overlay_mod.element_keys(elements)
    by_type: Dict[str, List[Tuple[str, str]]] = {}
    for key, element in zip(keys, elements):
        kind = str(element.get("type") or element.get("role") or "?")
        rank = hashlib.sha256(("%s|%s" % (seed_material, key)).encode("utf-8")).hexdigest()
        by_type.setdefault(kind, []).append((rank, key))
    for rows in by_type.values():
        rows.sort()
    order = sorted(by_type)
    picked: List[str] = []
    depth = 0
    while len(picked) < count:
        added = False
        for kind in order:
            if depth < len(by_type[kind]):
                picked.append(by_type[kind][depth][1])
                added = True
                if len(picked) >= count:
                    break
        if not added:
            break
        depth += 1
    return picked


# --------------------------------------------------------------------------
# append-only storage


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=".tmp-", suffix=".json", delete=False
    )
    try:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, str(path))
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def read_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Every well-formed row, plus how many trailing bytes were unusable.

    A session killed mid-write leaves a last line with no newline. Reading it
    as a record fails; appending after it *joins* the next record onto it and
    silently loses both. So the reader drops an unparseable final line and
    reports it, and `append_jsonl` repairs the missing newline before writing.
    """
    rows: List[Dict[str, Any]] = []
    if not Path(path).is_file():
        return rows, 0
    with Path(path).open("r", encoding="utf-8") as handle:
        lines = handle.read().splitlines(True)
    torn = 0
    for position, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            if position == len(lines) - 1:
                torn = len(line)
                break
            raise AuditError(500, "%s line %d is not JSON" % (path, position + 1))
        if isinstance(row, dict):
            rows.append(row)
    if lines and not lines[-1].endswith("\n") and not torn:
        # Parsed, but the write never completed its newline. Treat it as good
        # data; `append_jsonl` adds the separator.
        pass
    return rows, torn


def append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if path.is_file() and path.stat().st_size:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            needs_newline = handle.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as handle:
        if needs_newline:
            handle.write("\n")
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------
# the campaign


class Campaign(object):
    """One frozen queue, its rubric, and the labels answered against it."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.name = self.root.name
        self._manifest: Optional[Dict[str, Any]] = None
        self._queue: Optional[List[Dict[str, Any]]] = None
        self._by_key: Dict[str, int] = {}
        self._labels: Optional[List[Dict[str, Any]]] = None
        self._event_ids: Dict[str, bool] = {}
        self._torn = 0

    # -- paths ------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def queue_path(self) -> Path:
        return self.root / QUEUE_NAME

    @property
    def labels_path(self) -> Path:
        return self.root / LABELS_NAME

    @property
    def corrections_root(self) -> Path:
        return self.root / CORRECTIONS_DIRNAME

    def exists(self) -> bool:
        return self.manifest_path.is_file() and self.queue_path.is_file()

    # -- loading ----------------------------------------------------------

    @property
    def manifest(self) -> Dict[str, Any]:
        if self._manifest is None:
            try:
                with self.manifest_path.open("r", encoding="utf-8") as handle:
                    self._manifest = json.load(handle)
            except OSError:
                raise AuditError(404, "no campaign at %s" % self.root)
            except ValueError as error:
                raise AuditError(500, "%s is not valid JSON: %s" % (self.manifest_path, error))
        return self._manifest

    @property
    def kind(self) -> str:
        kind = self.manifest.get("kind")
        return kind if kind in KINDS else DEFAULT_KIND

    @property
    def mode(self) -> str:
        mode = self.manifest.get("mode")
        if not mode:
            return LEGACY_MODE
        return mode if mode in MODES else LEGACY_MODE

    @property
    def rubric(self) -> Dict[str, Any]:
        spec = self.manifest.get("rubric")
        if not isinstance(spec, dict):
            raise AuditError(500, "%s carries no rubric" % self.manifest_path)
        return spec

    @property
    def queue(self) -> List[Dict[str, Any]]:
        if self._queue is None:
            rows, torn = read_jsonl(self.queue_path)
            if torn:
                raise AuditError(500, "%s has a truncated final row" % self.queue_path)
            self._queue = rows
            self._by_key = {
                str(row.get("observation_key")): position
                for position, row in enumerate(rows)
            }
        return self._queue

    def item(self, index: int) -> Dict[str, Any]:
        queue = self.queue
        if not isinstance(index, int) or index < 0 or index >= len(queue):
            raise AuditError(404, "no item %r in %s" % (index, self.name))
        return queue[index]

    def index_of(self, observation_key: str) -> int:
        self.queue  # noqa: B018 - populates the lookup
        if observation_key not in self._by_key:
            raise AuditError(404, "%s is not in %s" % (observation_key, self.name))
        return self._by_key[observation_key]

    # -- labels -----------------------------------------------------------

    def labels(self) -> List[Dict[str, Any]]:
        if self._labels is None:
            rows, torn = read_jsonl(self.labels_path)
            self._torn = torn
            self._labels = rows
            self._event_ids = {
                str(row.get("event_id")): True for row in rows if row.get("event_id")
            }
        return self._labels

    @property
    def torn_bytes(self) -> int:
        self.labels()
        return self._torn

    def append_label(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Record one answer. Idempotent on `event_id`.

        Idempotency rather than compare-and-set, because two raters answering
        the same item is the point - it is where the agreement number comes
        from - while the failure worth defending against is one rater's
        keystroke arriving twice after a retry.
        """
        rows = self.labels()
        event_id = str(row.get("event_id") or "")
        if event_id and event_id in self._event_ids:
            return {"stored": False, "duplicate": True, "event_id": event_id}
        append_jsonl(self.labels_path, [row])
        rows.append(row)
        if event_id:
            self._event_ids[event_id] = True
        self._torn = 0
        return {"stored": True, "duplicate": False, "event_id": event_id}

    # -- state ------------------------------------------------------------

    def item_state(self, index: int, rater: Optional[str] = None) -> Dict[str, Any]:
        """The current answers for one item: last row per question wins."""
        screen: Dict[str, Any] = {}
        elements: Dict[str, Dict[str, Any]] = {}
        missed: List[Dict[str, Any]] = []
        sweep: Optional[Dict[str, Any]] = None
        ground: Optional[Dict[str, Any]] = None
        item: Dict[str, Any] = {}
        for row in self.labels():
            if row.get("index") != index:
                continue
            if rater is not None and row.get("rater") != rater:
                continue
            scope = row.get("scope")
            if scope == "screen":
                screen[str(row.get("question"))] = row
            elif scope == "element":
                elements[str(row.get("element_key"))] = row
            elif scope == "missed":
                missed = list(row.get("points") or [])
                item["missed_at"] = row.get("at")
            elif scope == "sweep":
                sweep = row
            elif scope == "ground":
                ground = row
            elif scope == "item":
                item.update({key: row[key] for key in ("status", "reason", "dwell_ms", "at")
                             if key in row})
        return {"screen": screen, "elements": elements, "missed": missed,
                "sweep": sweep, "ground": ground, "item": item}

    def progress(self, rater: Optional[str] = None) -> Dict[str, Any]:
        """How far through, and which item to open next.

        Computed from the in-memory label list, so a save costs no filesystem
        work at all. The runs page rescans its whole run after every save -
        0.9s and a 387KB cache rewrite, once per labelled sample - and that is
        precisely what makes a long session feel broken.
        """
        total = len(self.queue)
        done: Dict[int, bool] = {}
        skipped: Dict[int, bool] = {}
        raters: Dict[str, int] = {}
        answers = 0
        element_answers = 0
        for row in self.labels():
            who = str(row.get("rater") or "?")
            if rater is not None and who != rater:
                continue
            raters[who] = raters.get(who, 0) + 1
            scope = row.get("scope")
            if scope in ("screen", "element", "missed", "sweep", "ground"):
                answers += 1
            if scope == "element":
                element_answers += 1
            if scope == "sweep":
                # Committing a sweep is the assertion that every element on
                # that screen now carries a verdict, which is what finishing
                # the screen means. Counting it here rather than relying only
                # on the item row also survives the misattribution bug that
                # left three swept screens of two hundred without one.
                index = row.get("index")
                if isinstance(index, int):
                    done.setdefault(index, True)
            if scope == "item":
                index = row.get("index")
                if not isinstance(index, int):
                    continue
                if row.get("status") == "done":
                    done[index] = True
                    skipped.pop(index, None)
                elif row.get("status") == "skipped":
                    skipped[index] = True
                    done.pop(index, None)
        next_index = None
        for position in range(total):
            if position not in done and position not in skipped:
                next_index = position
                break
        return {
            "campaign": self.name,
            "total": total,
            "done": len(done),
            "skipped": len(skipped),
            "answers": answers,
            "element_answers": element_answers,
            "next": next_index,
            "done_indices": sorted(done),
            "skipped_indices": sorted(skipped),
            "raters": raters,
            "rubric": rubric_ref(self.rubric),
        }

    # -- writing ----------------------------------------------------------

    def write_manifest(self, manifest: Dict[str, Any]) -> None:
        _atomic_write_json(self.manifest_path, manifest)
        self._manifest = manifest

    def write_queue(self, rows: Sequence[Dict[str, Any]]) -> None:
        if self.queue_path.exists():
            raise AuditError(409, "%s already has a queue; campaigns are frozen"
                             % self.name)
        append_jsonl(self.queue_path, rows)
        self._queue = list(rows)
        self._by_key = {str(row.get("observation_key")): n for n, row in enumerate(rows)}

    def correction_path(self, observation_key: str) -> Path:
        key = safe_name(observation_key.replace("__", "-"), "observation key")
        return self.corrections_root / ("%s.json" % key)


def list_campaigns(audit_root: Path) -> List[Dict[str, Any]]:
    root = Path(audit_root)
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name == RUBRICS_DIRNAME:
            continue
        campaign = Campaign(entry)
        if not campaign.exists():
            continue
        try:
            progress = campaign.progress()
        except AuditError:
            continue
        out.append({
            "name": campaign.name,
            "title": campaign.manifest.get("title") or campaign.name,
            "created_at": campaign.manifest.get("created_at"),
            "seed": campaign.manifest.get("seed"),
            "progress": progress,
        })
    return out


def _inside(box: Sequence[float], x: float, y: float) -> bool:
    """`[x0, y0, x1, y1]`, the frame `action_target_bbox_px` is recorded in."""
    if not box or len(box) != 4:
        return False
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def new_event_id() -> str:
    return uuid.uuid4().hex[:16]


def build_label_row(
    campaign: Campaign,
    index: int,
    payload: Dict[str, Any],
    rater: str,
) -> Dict[str, Any]:
    """Validate one answer against the campaign's own rubric."""
    item = campaign.item(index)
    scope = payload.get("scope")
    if scope not in SCOPES:
        raise AuditError(400, "scope must be one of %s" % (", ".join(SCOPES),))
    questions = question_map(campaign.rubric)
    row: Dict[str, Any] = {
        "schema": LABEL_SCHEMA,
        "event_id": str(payload.get("event_id") or new_event_id()),
        "campaign": campaign.name,
        "index": index,
        "observation_key": item.get("observation_key"),
        "rater": rater,
        "at": now_stamp(),
        "rubric": rubric_ref(campaign.rubric),
        "scope": scope,
    }
    dwell = payload.get("dwell_ms")
    if isinstance(dwell, (int, float)) and dwell >= 0:
        # Kept in the log, never reported as a statistic: a screen can sit open
        # across an interruption, so elapsed time does not measure what a
        # reader would take "seconds per screen" to mean.
        row["dwell_ms"] = int(dwell)
    note = payload.get("note")
    if isinstance(note, str) and note.strip():
        row["note"] = note.strip()[:2000]

    if scope in ("screen", "element"):
        question_id = str(payload.get("question") or ("element" if scope == "element" else ""))
        question = questions.get(question_id)
        if question is None:
            raise AuditError(400, "no question %r in this campaign's rubric" % question_id)
        if question.get("scope") != scope:
            raise AuditError(400, "question %s is scope %s, not %s"
                             % (question_id, question.get("scope"), scope))
        row["question"] = question_id
        if payload.get("accepted"):
            # Recorded because it is real provenance about how the judgement
            # was made: one keystroke accepting the whole sample is not the
            # same act as answering five questions one at a time, even though
            # it produces the same five answers.
            row["accepted"] = True
        permitted = allowed_values(question)
        if question.get("multi"):
            values = payload.get("values")
            if values is None:
                values = [payload.get("value")] if payload.get("value") else []
            if not isinstance(values, list) or not values:
                raise AuditError(400, "question %s needs at least one value" % question_id)
            chosen = []
            for value in values:
                if value not in permitted:
                    raise AuditError(400, "question %s: %r is not one of %s"
                                     % (question_id, value, ", ".join(permitted)))
                if value not in chosen:
                    chosen.append(value)
            exclusive = [value for value in (question.get("exclusive") or []) if value in chosen]
            if exclusive and len(chosen) > 1:
                raise AuditError(400, "question %s: %s cannot be combined with another flag"
                                 % (question_id, exclusive[0]))
            row["values"] = chosen
        else:
            value = payload.get("value")
            if value not in permitted:
                raise AuditError(400, "question %s: %r is not one of %s"
                                 % (question_id, value, ", ".join(permitted)))
            row["value"] = value

    if scope == "element":
        element_key = str(payload.get("element_key") or "")
        if not element_key:
            raise AuditError(400, "an element answer needs element_key")
        sampled = [str(key) for key in (item.get("element_keys") or [])]
        row["element_key"] = element_key
        row["sampled"] = element_key in sampled
        source = payload.get("source") or ("sampled" if row["sampled"] else "sweep")
        if source not in ELEMENT_SOURCES:
            raise AuditError(400, "source must be one of %s" % (", ".join(ELEMENT_SOURCES),))
        row["source"] = source
        for field in ("element_type", "element_role", "element_app"):
            value = payload.get(field)
            if isinstance(value, str) and value:
                row[field] = value[:120]

    if scope == "sweep":
        count = payload.get("n_elements")
        if not isinstance(count, int) or count < 0:
            raise AuditError(400, "a sweep needs n_elements")
        flagged = payload.get("flagged")
        if not isinstance(flagged, list):
            raise AuditError(400, "a sweep needs a flagged list, empty when nothing was wrong")
        census = payload.get("census")
        if not isinstance(census, dict):
            raise AuditError(400, "a sweep needs a census of the screen's element classes")
        cleaned: Dict[str, int] = {}
        for name, value in list(census.items())[:200]:
            if not isinstance(value, int) or value < 0:
                raise AuditError(400, "census counts must be non-negative integers")
            cleaned[str(name)[:120]] = value
        if sum(cleaned.values()) != count:
            raise AuditError(400, "the census sums to %d but n_elements is %d"
                             % (sum(cleaned.values()), count))
        row["n_elements"] = count
        row["flagged"] = [str(key)[:200] for key in flagged[:500]]
        row["census"] = cleaned
        row["declared_correct"] = max(0, count - len(row["flagged"]))
        opened = payload.get("opened")
        if isinstance(opened, int) and opened >= 0:
            # How many elements the rater actually opened on this screen. The
            # only in-band evidence of how the sweep was done, and cheaper than
            # a row per element that was looked at and found fine.
            row["opened"] = opened

    if scope == "missed":
        points = payload.get("points")
        if not isinstance(points, list):
            raise AuditError(400, "missed needs a points list")
        width = int(item.get("width") or 0) or None
        height = int(item.get("height") or 0) or None
        cleaned = []
        for point in points[:200]:
            if not isinstance(point, dict):
                raise AuditError(400, "each missed point is an object")
            try:
                x, y = int(round(float(point["x"]))), int(round(float(point["y"])))
            except (KeyError, TypeError, ValueError):
                raise AuditError(400, "each missed point needs numeric x and y")
            if width and height and not (0 <= x < width and 0 <= y < height):
                raise AuditError(400, "missed point (%d, %d) is outside the screen" % (x, y))
            entry: Dict[str, Any] = {"x": x, "y": y}
            inside = point.get("inside_key")
            if isinstance(inside, str) and inside:
                entry["inside_key"] = inside[:200]
            kind = point.get("kind")
            if isinstance(kind, str) and kind:
                entry["kind"] = kind[:60]
            cleaned.append(entry)
        row["points"] = cleaned
        row["n_points"] = len(cleaned)

    if scope == "ground":
        point = payload.get("point")
        if not isinstance(point, dict):
            raise AuditError(400, "a grounding click needs a point")
        try:
            x, y = int(round(float(point["x"]))), int(round(float(point["y"])))
        except (KeyError, TypeError, ValueError):
            raise AuditError(400, "a grounding click needs numeric x and y")
        width = int(item.get("width") or 0) or None
        height = int(item.get("height") or 0) or None
        if width and height and not (0 <= x < width and 0 <= y < height):
            raise AuditError(400, "the click (%d, %d) is outside the screen" % (x, y))
        row["point"] = {"x": x, "y": y}
        # Scored here rather than in the browser: the answer travels with the
        # item, and a client that computed its own verdict could not be checked
        # against the record afterwards.
        target = item.get("target") or {}
        box = target.get("bbox_px")
        row["in_bbox"] = bool(box) and _inside(box, x, y)
        row["in_fragment"] = any(
            _inside([f["x"], f["y"], f["x"] + f["w"], f["y"] + f["h"]], x, y)
            for f in (target.get("visible_fragments") or [])
        ) if target.get("visible_fragments") else None
        recorded = target.get("point_px")
        if isinstance(recorded, (list, tuple)) and len(recorded) == 2:
            row["distance_px"] = round(
                ((x - recorded[0]) ** 2 + (y - recorded[1]) ** 2) ** 0.5, 1)
        if payload.get("gave_up"):
            # "I could not find it" is an answer about the instruction, and a
            # different one from clicking the wrong thing.
            row["gave_up"] = True
            row.pop("in_bbox", None)
            row.pop("in_fragment", None)
            row.pop("distance_px", None)

    if scope == "item":
        status = payload.get("status")
        if status not in ("done", "skipped", "reopened"):
            raise AuditError(400, "item status is done, skipped or reopened")
        row["status"] = status
        reason = payload.get("reason")
        if isinstance(reason, str) and reason.strip():
            row["reason"] = reason.strip()[:200]
        if status == "skipped" and not row.get("reason"):
            raise AuditError(400, "a skip needs a reason")

    return row
