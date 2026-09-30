#!/usr/bin/env python3
"""Turn a human-audit campaign into statistics.

    PYTHONPATH=src python scripts/audit_stats.py --campaign audit/pilot40

Writes `stats.json` and `stats.md` into the campaign directory. Reads only the
campaign: the queue for the denominators and the strata, `labels.jsonl` for the
answers, and the manifest for the rubric the answers were given against.

Three things this is careful about, because getting them wrong is how an audit
produces a confident wrong number.

**The last answer wins.** The log is append-only and a rater who changes their
mind answers again, so every reduction here keys on
`(index, rater, question)` and takes the latest row. Counting rows instead
would count revisions as extra observations.

**The population estimate and the coverage sample are separate.** The queue is
a uniform draw plus a floor under every stratum (see
`scripts/build_audit_queue.py`), so items are not equally likely to be in it.
Rates over the `population` items estimate the corpus; rates over everything
describe the queue and are reported per slice with their denominators, never
pooled into the headline.

**An interval is a Wilson interval on the items actually judged.** Not on the
queue, not on the corpus, and there is no attempt to propagate the element
sampling design into a screen-level confidence claim.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskshot.inspector import audit as audit_mod  # noqa: E402

#: Element flags that mean the annotation was wrong about something. `unsure`
#: is deliberately not one of them: it is a missing observation, not an error,
#: and folding it either way would bias the rate.
ERROR_FLAGS = ("phantom", "wrong_class", "bad_geometry", "wrong_text", "wrong_occlusion")


def wilson(successes: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """A Wilson score interval - usable at the small counts an audit has.

    The normal approximation is wrong exactly where an annotation audit lives:
    a precision of 49/50 has a normal interval that runs past 1.
    """
    if total <= 0:
        return (0.0, 0.0)
    p = successes / float(total)
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = (z / denominator) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def rate(successes: int, total: int) -> Dict[str, Any]:
    if successes < 0 or successes > total:
        # Loud and named, rather than a `math domain error` from inside sqrt.
        # Every counting mistake in here shows up as this shape, and the two
        # numbers are enough to find it.
        raise ValueError("%d of %d is not a rate; a count is being "
                         "double-subtracted somewhere" % (successes, total))
    low, high = wilson(successes, total)
    return {
        "n": total,
        "k": successes,
        "rate": (successes / float(total)) if total else None,
        "ci95": [round(low, 4), round(high, 4)],
    }


def latest(rows: Iterable[Dict[str, Any]], key) -> List[Dict[str, Any]]:
    """One row per key, the last one to arrive.

    Rows are ordered by append, and `at` has one-second resolution, so position
    in the file is the tie-break rather than the timestamp.
    """
    out: "Dict[Any, Dict[str, Any]]" = {}
    for row in rows:
        out[key(row)] = row
    return list(out.values())


def flags_of(row: Dict[str, Any]) -> List[str]:
    values = row.get("values")
    if isinstance(values, list):
        return [str(value) for value in values]
    return [str(row.get("value"))] if row.get("value") else []


class Report(object):
    def __init__(self, campaign: audit_mod.Campaign,
                 raters: Optional[Sequence[str]] = None,
                 primary: Optional[str] = None):
        self.campaign = campaign
        self.manifest = campaign.manifest
        self.rubric = campaign.rubric
        self.queue = campaign.queue
        self.by_index = {position: item for position, item in enumerate(self.queue)}
        rows = campaign.labels()
        if raters:
            allowed = set(raters)
            rows = [row for row in rows if row.get("rater") in allowed]
        self.rows = rows
        self.raters = sorted({str(row.get("rater")) for row in rows})
        self.primary = primary or self._busiest()

    def _busiest(self) -> Optional[str]:
        """Whose ratings the headline numbers are computed from.

        A double-rated element has two ratings and is still one element. Pooling
        both would count it twice, inflate every denominator and shrink every
        interval on the strength of no extra evidence - so the rates come from
        one rater and the second rating is used for agreement, which is what it
        was collected for. The busiest rater is the default because that is who
        worked through the queue; `--primary-rater` overrides it.
        """
        counts = Counter(str(row.get("rater")) for row in self.rows)
        return counts.most_common(1)[0][0] if counts else None

    def mine(self, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self.primary is None:
            return list(rows)
        return [row for row in rows if str(row.get("rater")) == self.primary]

    # -- selection --------------------------------------------------------

    def population_indices(self) -> List[int]:
        return [position for position, item in self.by_index.items()
                if item.get("draw") == "population"]

    def stratum_of(self, index: int, field: str) -> str:
        item = self.by_index.get(index) or {}
        return str((item.get("stratum") or {}).get(field))

    # -- element level ----------------------------------------------------

    def element_rows(self, everyone: bool = False,
                     source: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = [row for row in self.rows if row.get("scope") == "element"]
        if not everyone:
            rows = self.mine(rows)
        rows = latest(rows, lambda row: (row.get("index"), row.get("rater"),
                                         row.get("element_key")))
        if source is not None:
            rows = [row for row in rows
                    if str(row.get("source") or "sampled") == source]
        return rows

    def exceptions(self) -> Dict[str, Any]:
        """The elements a rater stopped on during a sweep.

        Not a sample of anything: they are the ones that looked wrong, so a
        precision computed over them is a precision over elements selected for
        being wrong. Reported as findings - what was flagged, and as what - and
        the rate that means something is the census above.
        """
        rows = self.element_rows(source="sweep")
        flagged: Counter = Counter()
        by_class: Counter = Counter()
        examples: List[Dict[str, Any]] = []
        cleared = 0
        for row in rows:
            values = [flag for flag in flags_of(row) if flag != "ok"]
            if not values:
                cleared += 1
                continue
            for flag in values:
                flagged[flag] += 1
            by_class[str(row.get("element_type") or "?")] += 1
            examples.append({
                "index": row.get("index"),
                "element_key": row.get("element_key"),
                "type": row.get("element_type"),
                "flags": values,
            })
        return {
            "stopped_on": len(rows),
            "flagged": len(examples),
            "opened_then_cleared": cleared,
            "by_flag": dict(flagged.most_common()),
            "by_class": dict(by_class.most_common()),
            "examples": examples[:60],
        }

    def element_summary(self, rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        total = 0
        correct = 0
        unsure = 0
        misidentified = 0
        mislocalized = 0
        flag_counts: Counter = Counter()
        for row in rows:
            flags = flags_of(row)
            if "unsure" in flags:
                unsure += 1
                continue
            total += 1
            if flags == ["ok"]:
                correct += 1
            # Counted per element, not per flag. An element can carry two at
            # once - misclassified *and* mislocalized is a real and common
            # verdict - and subtracting overlapping flag totals from the
            # element count drove it below zero.
            if any(flag in ("phantom", "wrong_class") for flag in flags):
                misidentified += 1
            if any(flag in ("phantom", "bad_geometry") for flag in flags):
                mislocalized += 1
            for flag in flags:
                if flag in ERROR_FLAGS:
                    flag_counts[flag] += 1
        wrong = total - correct
        summary = {
            "judged": total,
            "unsure": unsure,
            "correct": correct,
            "wrong": wrong,
            "precision": rate(correct, total),
            "flags": {flag: rate(flag_counts.get(flag, 0), total) for flag in ERROR_FLAGS},
        }
        # Localization and identification separately: "is this the right thing"
        # and "is this the right box" fail for different reasons and a single
        # accuracy number hides which.
        summary["identified"] = rate(total - misidentified, total)
        summary["localized"] = rate(total - mislocalized, total)
        return summary

    def element_breakdown(self, rows: Sequence[Dict[str, Any]], field: str,
                          value_of) -> Dict[str, Any]:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(value_of(row))].append(row)
        return {name: self.element_summary(members)
                for name, members in sorted(groups.items())}

    # -- sweeps -----------------------------------------------------------

    def sweep_rows(self) -> List[Dict[str, Any]]:
        rows = self.mine([row for row in self.rows if row.get("scope") == "sweep"])
        return latest(rows, lambda row: (row.get("index"), row.get("rater")))

    def swept(self, indices: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        """Every element on every swept screen.

        A sweep is one verdict per element: correct unless flagged. Declaring
        the rest correct is the rater's judgement on each of them, not an
        absence of one, so the denominator is the screen's own element count
        and the per-class denominators come from the census the sweep recorded.
        200 screens is 25,466 elements, not 1,600.

        Which elements are wrong comes from the element rows, not from the
        sweep row's `flagged` list. A rater who flags an element and then clears
        it writes a second element row; trusting the list would keep counting
        the retraction.
        """
        keep = set(indices) if indices is not None else None
        by_index: Dict[int, Dict[str, List[str]]] = defaultdict(dict)
        types: Dict[int, Dict[str, str]] = defaultdict(dict)
        for row in self.element_rows():
            index = row.get("index")
            key = str(row.get("element_key"))
            by_index[index][key] = flags_of(row)
            types[index][key] = str(row.get("element_type") or "?")

        screens = 0
        total = 0
        unsure = 0
        wrong = 0
        flag_counts: Counter = Counter()
        census: Counter = Counter()
        wrong_by_class: Counter = Counter()
        unsure_by_class: Counter = Counter()
        for row in self.sweep_rows():
            index = row.get("index")
            if keep is not None and index not in keep:
                continue
            screens += 1
            count = int(row.get("n_elements") or 0)
            total += count
            census.update({str(name): int(value)
                           for name, value in (row.get("census") or {}).items()})
            for key, flags in (by_index.get(index) or {}).items():
                kind = types[index].get(key, "?")
                if "unsure" in flags:
                    unsure += 1
                    unsure_by_class[kind] += 1
                    continue
                if flags == ["ok"] or not flags:
                    continue
                wrong += 1
                wrong_by_class[kind] += 1
                for flag in flags:
                    if flag in ERROR_FLAGS:
                        flag_counts[flag] += 1
        judged = max(0, total - unsure)
        correct = max(0, judged - wrong)
        by_class = {}
        for kind, count in sorted(census.items()):
            denominator = max(0, count - unsure_by_class.get(kind, 0))
            by_class[kind] = {
                "judged": denominator,
                "wrong": wrong_by_class.get(kind, 0),
                "precision": rate(max(0, denominator - wrong_by_class.get(kind, 0)),
                                  denominator),
            }
        return {
            "screens": screens,
            "elements": total,
            "judged": judged,
            "unsure": unsure,
            "correct": correct,
            "wrong": wrong,
            "precision": rate(correct, judged),
            "flags": {flag: rate(flag_counts.get(flag, 0), judged) for flag in ERROR_FLAGS},
            "by_class": by_class,
            "note": ("a census: every element on every swept screen carries a "
                     "verdict. Correct unless flagged, and declaring the rest "
                     "correct is a judgement on each of them"),
        }

    # -- screen level -----------------------------------------------------

    def screen_rows(self, everyone: bool = False) -> List[Dict[str, Any]]:
        rows = [row for row in self.rows if row.get("scope") == "screen"]
        if not everyone:
            rows = self.mine(rows)
        return latest(rows, lambda row: (row.get("index"), row.get("rater"),
                                         row.get("question")))

    def screen_summary(self, rows: Sequence[Dict[str, Any]],
                       indices: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        keep = set(indices) if indices is not None else None
        out: Dict[str, Any] = {}
        for question in self.rubric.get("questions") or []:
            if question.get("scope") != "screen":
                continue
            counts: Counter = Counter()
            for row in rows:
                if row.get("question") != question["id"]:
                    continue
                if keep is not None and row.get("index") not in keep:
                    continue
                for value in flags_of(row):
                    counts[value] += 1
            total = sum(counts.values())
            out[question["id"]] = {
                "answered": total,
                "counts": dict(counts),
                "shares": {value: (count / float(total)) if total else None
                           for value, count in sorted(counts.items())},
            }
        return out

    # -- instruction campaigns --------------------------------------------

    #: What has to be true for one action sample to be sound supervision. Not a
    #: rubric question - a conjunction of four of them, stated once here so the
    #: headline number has a definition rather than a vibe.
    SOUND = {
        "instruction_ok": {"good"},
        "target_match": {"match"},
        "unique": {"one"},
        "target_visible": {"visible", "partly"},
    }

    def ground_rows(self) -> List[Dict[str, Any]]:
        rows = self.mine([row for row in self.rows if row.get("scope") == "ground"])
        return latest(rows, lambda row: (row.get("index"), row.get("rater")))

    def grounding(self, indices: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        """Could a person act on the instruction, and did they land on the target?

        The one number here that nobody had to be asked for. A rater shown the
        instruction with no box either clicks inside the recorded target or does
        not, and an instruction that a careful human cannot ground is not
        supervision a model should be asked to learn.
        """
        keep = set(indices) if indices is not None else None
        attempted = 0
        hit = 0
        fragment_hit = 0
        gave_up = 0
        distances: List[float] = []
        by_style: Dict[str, List[int]] = defaultdict(list)
        by_size: Dict[str, List[int]] = defaultdict(list)
        for row in self.ground_rows():
            index = row.get("index")
            if keep is not None and index not in keep:
                continue
            if row.get("gave_up"):
                gave_up += 1
                continue
            attempted += 1
            landed = 1 if row.get("in_bbox") else 0
            hit += landed
            if row.get("in_fragment"):
                fragment_hit += 1
            if not landed and isinstance(row.get("distance_px"), (int, float)):
                # Only the misses: the distance on a hit is a measure of where
                # inside the box somebody clicked, which is not a quality signal.
                distances.append(float(row["distance_px"]))
            item = self.by_index.get(index) or {}
            stratum = item.get("stratum") or {}
            by_style[str(stratum.get("style"))].append(landed)
            by_size[str(stratum.get("target_size"))].append(landed)
        distances.sort()
        return {
            "shown": attempted + gave_up,
            "attempted": attempted,
            "gave_up": rate(gave_up, attempted + gave_up),
            "hit": rate(hit, attempted),
            "hit_visible_fragment": rate(fragment_hit, attempted),
            "misses": len(distances),
            "median_miss_distance_px": (distances[len(distances) // 2]
                                        if distances else None),
            "by_style": {name: rate(sum(values), len(values))
                         for name, values in sorted(by_style.items())},
            "by_target_size": {name: rate(sum(values), len(values))
                               for name, values in sorted(by_size.items())},
            "note": ("a click made from the instruction alone, before the target "
                     "was shown, scored by the server against the recorded box. "
                     "'gave up' is its own outcome: not finding the target is a "
                     "different failure from clicking the wrong thing"),
        }

    def instruction_quality(self, indices: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        keep = set(indices) if indices is not None else None
        answers: Dict[int, Dict[str, str]] = defaultdict(dict)
        for row in self.screen_rows():
            index = row.get("index")
            if keep is not None and index not in keep:
                continue
            values = flags_of(row)
            if values:
                answers[index][str(row.get("question"))] = values[0]
        complete = 0
        sound = 0
        failures: Counter = Counter()
        for index, given in answers.items():
            if not all(question in given for question in self.SOUND):
                continue
            complete += 1
            bad = [question for question, allowed in self.SOUND.items()
                   if given[question] not in allowed]
            if bad:
                for question in bad:
                    failures["%s=%s" % (question, given[question])] += 1
            else:
                sound += 1
        return {
            "fully_answered": complete,
            "sound": rate(sound, complete),
            "why_not": dict(failures.most_common()),
            "definition": {question: sorted(values)
                           for question, values in self.SOUND.items()},
            "note": ("sound means all four of the definition above held for the "
                     "same sample; a sample can fail on more than one and is "
                     "counted once in the rate and once per reason below it"),
        }

    # -- recall -----------------------------------------------------------

    def missed_summary(self, indices: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        rows = latest(self.mine([row for row in self.rows if row.get("scope") == "missed"]),
                      lambda row: (row.get("index"), row.get("rater")))
        keep = set(indices) if indices is not None else None
        screens = 0
        real = 0
        slips = 0
        with_any = 0
        per_screen: List[int] = []
        for row in rows:
            if keep is not None and row.get("index") not in keep:
                continue
            screens += 1
            points = row.get("points") or []
            genuine = [point for point in points if not point.get("inside_key")]
            slips += len(points) - len(genuine)
            real += len(genuine)
            per_screen.append(len(genuine))
            if genuine:
                with_any += 1
        per_screen.sort()
        return {
            "screens_checked": screens,
            "missing_elements_found": real,
            "clicks_that_hit_an_existing_box": slips,
            "screens_with_at_least_one_miss": rate(with_any, screens),
            "misses_per_screen_mean": (real / float(screens)) if screens else None,
            "misses_per_screen_median": (per_screen[len(per_screen) // 2]
                                         if per_screen else None),
            "misses_per_screen_max": (per_screen[-1] if per_screen else None),
        }

    def recall_estimate(self, indices: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        """Element recall on the screens where the blind pass actually ran.

        The denominator is the annotated elements on those screens plus the
        elements a human found that had no annotation. It is a *human-perceived*
        denominator: an element neither the pipeline nor the rater saw is in
        neither term, so this is an upper bound on recall, and it is reported as
        one rather than as recall full stop.
        """
        rows = latest(self.mine([row for row in self.rows if row.get("scope") == "missed"]),
                      lambda row: (row.get("index"), row.get("rater")))
        keep = set(indices) if indices is not None else None
        annotated = 0
        missed = 0
        screens = 0
        swept = {row.get("index") for row in self.sweep_rows()}
        for row in rows:
            index = row.get("index")
            if keep is not None and index not in keep:
                continue
            item = self.by_index.get(index)
            if not item:
                continue
            screens += 1
            annotated += int(item.get("n_elements") or 0)
            missed += len([point for point in (row.get("points") or [])
                           if not point.get("inside_key")])
        total = annotated + missed
        return {
            "screens": screens,
            "annotated_elements": annotated,
            "human_found_unannotated": missed,
            "recall_upper_bound": rate(annotated, total),
            "swept_screens": len(swept & set(
                row.get("index") for row in rows)) if rows else 0,
            "note": ("denominator is annotated + human-found-unannotated on the "
                     "screens whose missing-element pass was completed; an element "
                     "nobody saw is in neither term, so this bounds recall from "
                     "above. On a swept screen the bound is looser still: the "
                     "boxes were already drawn, which anchors what a reader "
                     "notices is absent - only the sample mode's blind pass "
                     "estimates this without that anchor"),
        }

    def _headline_precision(self, inspected: Dict[str, Any]) -> Dict[str, Any]:
        """The precision the campaign actually measured.

        On a swept campaign the element rows are the exceptions, so pairing
        their rate with recall produces a number about nothing. The census is
        the precision there.
        """
        swept = self.swept()
        if swept["judged"]:
            return swept["precision"]
        return inspected["precision"]

    def f1(self, precision: Dict[str, Any], recall: Dict[str, Any]) -> Optional[float]:
        p = precision.get("rate")
        r = recall.get("rate")
        if not p or not r:
            return None
        return 2 * p * r / (p + r)

    # -- agreement --------------------------------------------------------

    def agreement(self) -> Dict[str, Any]:
        """Percent agreement and Cohen's kappa where two raters overlap.

        Only pairs that judged the same unit count. A kappa on a handful of
        units is noise, so the count is always reported beside it.
        """
        out: Dict[str, Any] = {"raters": self.raters, "pairs": {}}
        if len(self.raters) < 2:
            out["note"] = "one rater; nothing to compare"
            return out
        elements = self.element_rows(everyone=True)
        screens = self.screen_rows(everyone=True)
        for first in range(len(self.raters)):
            for second in range(first + 1, len(self.raters)):
                a, b = self.raters[first], self.raters[second]
                pair: Dict[str, Any] = {}
                pair["element"] = self._kappa(
                    elements, a, b,
                    unit=lambda row: (row.get("index"), row.get("element_key")),
                    value=lambda row: "+".join(sorted(flags_of(row))))
                for question in self.rubric.get("questions") or []:
                    if question.get("scope") != "screen":
                        continue
                    rows = [row for row in screens if row.get("question") == question["id"]]
                    pair[question["id"]] = self._kappa(
                        rows, a, b,
                        unit=lambda row: row.get("index"),
                        value=lambda row: "+".join(sorted(flags_of(row))))
                out["pairs"]["%s|%s" % (a, b)] = pair
        return out

    @staticmethod
    def _kappa(rows, a, b, unit, value) -> Dict[str, Any]:
        first = {unit(row): value(row) for row in rows if row.get("rater") == a}
        second = {unit(row): value(row) for row in rows if row.get("rater") == b}
        shared = sorted(set(first) & set(second), key=lambda key: str(key))
        n = len(shared)
        if not n:
            return {"n": 0, "agreement": None, "kappa": None}
        same = sum(1 for key in shared if first[key] == second[key])
        observed = same / float(n)
        counts_a: Counter = Counter(first[key] for key in shared)
        counts_b: Counter = Counter(second[key] for key in shared)
        expected = sum((counts_a[label] / float(n)) * (counts_b[label] / float(n))
                       for label in set(counts_a) | set(counts_b))
        kappa = None if expected >= 1 else (observed - expected) / (1 - expected)
        return {"n": n, "agreement": round(observed, 4),
                "kappa": None if kappa is None else round(kappa, 4),
                "expected_agreement": round(expected, 4)}

    # -- the automated proxies -------------------------------------------

    def proxy_calibration(self) -> Dict[str, Any]:
        """How the automated verdict lines up with the human one.

        The corpus already ships automated estimates of this - a per-capture
        `verdict.json` and a per-shard `pixel_audit.json`. They are what the
        release's quality claim rests on, so the useful thing a human audit adds
        is not only its own rate but whether those estimates were right.
        """
        screens = self.screen_rows()
        usable = {row.get("index"): flags_of(row)[0]
                  for row in screens if row.get("question") == "screen_usable"}
        by_flag: Dict[str, Counter] = defaultdict(Counter)
        for index, verdict in usable.items():
            item = self.by_index.get(index) or {}
            flags = item.get("flags") or {}
            key = "near_duplicate" if flags.get("near_duplicate") else (
                "no_op_frame" if flags.get("no_op_frame") else "ordinary")
            by_flag[key][verdict] += 1
        elements = self.element_rows()
        occlusion: Dict[str, Counter] = defaultdict(Counter)
        for row in elements:
            band = self.stratum_of(row.get("index"), "occlusion")
            flags = flags_of(row)
            occlusion[band]["judged"] += 1
            if flags == ["ok"]:
                occlusion[band]["correct"] += 1
        return {
            "screen_verdict_by_queue_flag": {key: dict(value)
                                             for key, value in sorted(by_flag.items())},
            "element_correct_by_occlusion_band": {
                band: rate(counts.get("correct", 0), counts.get("judged", 0))
                for band, counts in sorted(occlusion.items())},
            "note": ("the queue's own flags and strata, against the human verdict; "
                     "per-shard pixel_audit.json rates are shown in the app beside "
                     "each item and are not re-derived here"),
        }

    # -- session ---------------------------------------------------------

    def session(self) -> Dict[str, Any]:
        done: Dict[Tuple[int, str], str] = {}
        for row in self.rows:
            # A committed sweep is the assertion that the screen is finished;
            # counting only the item row missed three swept screens of two
            # hundred, whose done rows had landed on their neighbours.
            if row.get("scope") == "sweep":
                done.setdefault((row.get("index"), str(row.get("rater"))), "done")
            if row.get("scope") == "item":
                done[(row.get("index"), str(row.get("rater")))] = str(row.get("status"))
        stamps = sorted(str(row.get("at")) for row in self.rows if row.get("at"))
        finished = sorted({index for (index, _), status in done.items() if status == "done"})
        skipped = sorted({index for (index, _), status in done.items() if status == "skipped"})
        return {
            "queue": len(self.queue),
            "items_done": len(finished),
            "items_skipped": len(skipped),
            "skip_reasons": dict(Counter(
                str(row.get("reason")) for row in self.rows
                if row.get("scope") == "item" and row.get("status") == "skipped")),
            "answers": len([row for row in self.rows
                            if row.get("scope") in ("screen", "element", "missed")]),
            "raters": dict(Counter(str(row.get("rater")) for row in self.rows)),
            "first_answer": stamps[0] if stamps else None,
            "last_answer": stamps[-1] if stamps else None,
            "coverage": {
                field: {
                    "queue": dict(Counter(self.stratum_of(index, field)
                                          for index in self.by_index)),
                    "done": dict(Counter(self.stratum_of(index, field)
                                         for index in finished)),
                }
                for field in (self.manifest.get("stratum_fields") or [])
            },
        }

    # -- assembly --------------------------------------------------------

    def build(self) -> Dict[str, Any]:
        elements = self.element_rows(source="sampled")
        screens = self.screen_rows()
        population = self.population_indices()
        population_set = set(population)
        population_elements = [row for row in elements if row.get("index") in population_set]

        overall = self.element_summary(elements)
        population_summary = self.element_summary(population_elements)
        recall = self.recall_estimate()
        recall_population = self.recall_estimate(population)

        return {
            "schema": "deskshot.audit.stats/1",
            "campaign": self.campaign.name,
            "generated_at": audit_mod.now_stamp(),
            "manifest": {
                "seed": self.manifest.get("seed"),
                "items": self.manifest.get("items"),
                "element_sample": self.manifest.get("element_sample"),
                "rubric_ref": self.manifest.get("rubric_ref"),
                "corpus": self.manifest.get("corpus"),
                "release": self.manifest.get("release"),
                "population_total": self.manifest.get("population_total"),
                "population_items": self.manifest.get("population_items"),
            },
            "primary_rater": self.primary,
            "raters": self.raters,
            "session": self.session(),
            "mode": self.campaign.mode,
            "swept": {
                "queue_sample": self.swept(),
                "population_sample": self.swept(population),
            },
            "elements": {
                "queue_sample": overall,
                "population_sample": population_summary,
                "by_class": self.element_breakdown(elements, "type",
                                                   lambda row: row.get("element_type") or "?"),
                "by_app": self.element_breakdown(elements, "app",
                                                 lambda row: row.get("element_app") or "?"),
                "by_split": self.element_breakdown(
                    elements, "split", lambda row: self.stratum_of(row.get("index"), "split")),
                "by_occlusion": self.element_breakdown(
                    elements, "occlusion",
                    lambda row: self.stratum_of(row.get("index"), "occlusion")),
                "by_theme": self.element_breakdown(
                    elements, "theme", lambda row: self.stratum_of(row.get("index"), "theme")),
                "by_resolution": self.element_breakdown(
                    elements, "resolution",
                    lambda row: self.stratum_of(row.get("index"), "resolution")),
            },
            "screens": {
                "queue_sample": self.screen_summary(screens),
                "population_sample": self.screen_summary(screens, population),
            },
            "missed": {
                "queue_sample": self.missed_summary(),
                "population_sample": self.missed_summary(population),
            },
            "recall": {"queue_sample": recall, "population_sample": recall_population},
            "f1": {
                "queue_sample": self.f1(self._headline_precision(overall),
                                        recall["recall_upper_bound"]),
                "population_sample": self.f1(
                    self._headline_precision(population_summary),
                    recall_population["recall_upper_bound"]),
                "source": ("swept census" if self.sweep_rows() else "inspected sample"),
                "note": ("harmonic mean of element precision and the recall upper "
                         "bound above; it inherits that bound's caveat"),
            },
            "instruction": ({
                "grounding": {"queue_sample": self.grounding(),
                              "population_sample": self.grounding(population)},
                "quality": {"queue_sample": self.instruction_quality(),
                            "population_sample": self.instruction_quality(population)},
            } if self.campaign.kind == "instruction" else None),
            "kind": self.campaign.kind,
            "exceptions": self.exceptions(),
            "agreement": self.agreement(),
            "automated_proxies": self.proxy_calibration(),
        }


def _median(values: Sequence[int]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def percent(value: Optional[float]) -> str:
    return "—" if value is None else "%.1f%%" % (100 * value)


def band(entry: Dict[str, Any]) -> str:
    if not entry or not entry.get("n"):
        return "—"
    return "%s  [%s–%s]  n=%d" % (
        percent(entry.get("rate")),
        percent(entry["ci95"][0]), percent(entry["ci95"][1]), entry["n"])


def render(stats: Dict[str, Any]) -> str:
    lines: List[str] = []
    add = lines.append
    manifest = stats["manifest"]
    session = stats["session"]
    add("# Human annotation audit — %s" % stats["campaign"])
    add("")
    add("Generated %s. Mode **%s**, rubric `%s`, seed %s, %s items%s."
        % (stats["generated_at"], stats["mode"], manifest["rubric_ref"],
           manifest["seed"], manifest["items"],
           ", %s elements sampled per screen" % manifest["element_sample"]
           if stats["mode"] == "sample" else ""))
    add("")
    if len(stats["raters"]) > 1:
        add("Rates below are **%s**'s ratings; %s also rated part of the queue and "
            "those ratings are used for agreement, not pooled into the rates - a "
            "double-rated element is one element." % (
                stats["primary_rater"],
                ", ".join(name for name in stats["raters"]
                          if name != stats["primary_rater"])))
        add("")
    add("Corpus `%s`%s." % (manifest["corpus"],
                            ", verified against release `%s`" % manifest["release"]
                            if manifest["release"] else ""))
    add("")
    add("## Session")
    add("")
    add("| | |")
    add("| --- | ---: |")
    add("| items done | %d of %d |" % (session["items_done"], session["queue"]))
    add("| items skipped | %d |" % session["items_skipped"])
    add("| answers recorded | %d |" % session["answers"])
    add("| raters | %s |" % ", ".join("%s (%d)" % pair
                                      for pair in sorted(session["raters"].items())))
    add("| window | %s → %s |" % (session["first_answer"], session["last_answer"]))
    add("")

    instruction = stats.get("instruction")
    if instruction:
        ground = instruction["grounding"]["queue_sample"]
        quality = instruction["quality"]["queue_sample"]
        add("## Could a person act on the instruction?")
        add("")
        add("| | rate | 95% CI | n |")
        add("| --- | ---: | :---: | ---: |")
        for name, entry in (("clicked inside the recorded target", ground["hit"]),
                            ("clicked on a visible part of it",
                             ground["hit_visible_fragment"]),
                            ("could not find it at all", ground["gave_up"])):
            add("| %s | %s | %s–%s | %s |"
                % (name, percent(entry["rate"]), percent(entry["ci95"][0]),
                   percent(entry["ci95"][1]), "{:,}".format(entry["n"])))
        add("")
        add("When the click missed, it was a median %s from the recorded point."
            % ("—" if ground["median_miss_distance_px"] is None
               else "%d px" % ground["median_miss_distance_px"]))
        add("")
        add(ground["note"])
        add("")
        for title, table in (("instruction style", ground["by_style"]),
                             ("target size", ground["by_target_size"])):
            rows_ = [(name, entry) for name, entry in table.items() if entry["n"]]
            if not rows_:
                continue
            add("**Grounded by %s** — %s" % (
                title, "  ·  ".join("%s %s (n=%d)" % (name, percent(entry["rate"]),
                                                      entry["n"])
                                    for name, entry in rows_)))
            add("")

        add("## Is the sample sound supervision?")
        add("")
        add("Sound means all of these held for the same sample: %s."
            % "; ".join("%s ∈ {%s}" % (question, ", ".join(values))
                        for question, values in sorted(quality["definition"].items())))
        add("")
        add("| | |")
        add("| --- | ---: |")
        add("| fully answered | %d |" % quality["fully_answered"])
        add("| sound | %s |" % band(quality["sound"]))
        add("")
        if quality["why_not"]:
            add("| why not | count |")
            add("| --- | ---: |")
            for reason, count in quality["why_not"].items():
                add("| %s | %d |" % (reason, count))
            add("")
        for question, summary in sorted(stats["screens"]["queue_sample"].items()):
            if not summary["answered"]:
                continue
            add("**%s** — %d answered: %s" % (
                question, summary["answered"],
                ", ".join("%s %d (%s)" % (value, summary["counts"][value],
                                          percent(summary["shares"][value]))
                          for value in sorted(summary["counts"]))))
        add("")

    swept = stats["swept"]["queue_sample"]
    if swept["screens"]:
        add("## Elements — every one on every swept screen")
        add("")
        add("%s screens, %s elements, each carrying a verdict%s."
            % ("{:,}".format(swept["screens"]), "{:,}".format(swept["judged"]),
               " (%d marked unsure and excluded)" % swept["unsure"]
               if swept["unsure"] else ""))
        add("")
        add("| | rate | 95% CI | n |")
        add("| --- | ---: | :---: | ---: |")
        entry = swept["precision"]
        add("| correct | %s | %s–%s | %s |"
            % (percent(entry["rate"]), percent(entry["ci95"][0]),
               percent(entry["ci95"][1]), "{:,}".format(entry["n"])))
        for flag, flag_entry in sorted(swept["flags"].items()):
            add("| %s | %s | %s–%s | %s |"
                % (flag.replace("_", " "), percent(flag_entry["rate"]),
                   percent(flag_entry["ci95"][0]), percent(flag_entry["ci95"][1]),
                   "{:,}".format(flag_entry["n"])))
        add("")
        add(swept["note"])
        add("")
        exceptions = stats["exceptions"]
        if exceptions["flagged"]:
            add("### What was flagged")
            add("")
            add("The %d elements marked wrong, and as what. Not a sample - these "
                "are the exceptions the rate above is the complement of."
                % exceptions["flagged"])
            add("")
            if exceptions["by_flag"]:
                add("| flag | count |")
                add("| --- | ---: |")
                for flag, count in exceptions["by_flag"].items():
                    add("| %s | %d |" % (flag.replace("_", " "), count))
                add("")
            if exceptions["by_class"]:
                add("Flagged elements by class: %s."
                    % ", ".join("%s %d" % pair for pair in exceptions["by_class"].items()))
                add("")

        classes = [(name, row) for name, row in swept["by_class"].items() if row["judged"]]
        if classes:
            add("### Swept element correctness by class")
            add("")
            add("| class | elements | wrong | correct |")
            add("| --- | ---: | ---: | ---: |")
            for name, row in sorted(classes, key=lambda pair: -pair[1]["judged"]):
                add("| %s | %s | %d | %s |"
                    % (name, "{:,}".format(row["judged"]), row["wrong"],
                       percent(row["precision"]["rate"])))
            add("")

    for label, key in (("Population sample (uniform draw)", "population_sample"),
                       ("Whole queue (uniform draw plus stratum floors)", "queue_sample")):
        summary = stats["elements"][key]
        if not summary["judged"]:
            continue
        add("## Elements individually inspected — %s" % label)
        add("")
        add("%d judged, %d marked unsure and excluded." % (summary["judged"], summary["unsure"]))
        add("")
        add("| | rate | 95% CI | n |")
        add("| --- | ---: | :---: | ---: |")
        for name, entry in (("correct (identified and localized)", summary["precision"]),
                            ("correctly identified", summary["identified"]),
                            ("correctly localized", summary["localized"])):
            add("| %s | %s | %s–%s | %d |"
                % (name, percent(entry["rate"]), percent(entry["ci95"][0]),
                   percent(entry["ci95"][1]), entry["n"]))
        for flag, entry in sorted(summary["flags"].items()):
            add("| %s | %s | %s–%s | %d |"
                % (flag.replace("_", " "), percent(entry["rate"]),
                   percent(entry["ci95"][0]), percent(entry["ci95"][1]), entry["n"]))
        add("")

    for title, key in (("class", "by_class"), ("application", "by_app"),
                       ("split", "by_split"), ("occlusion band", "by_occlusion"),
                       ("theme", "by_theme"), ("resolution", "by_resolution")):
        table = stats["elements"][key]
        if not table:
            continue
        add("### Element correctness by %s" % title)
        add("")
        add("| %s | judged | correct | wrong | phantom | wrong class | box wrong |"
            % title)
        add("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name, summary in sorted(table.items(),
                                    key=lambda pair: -pair[1]["judged"]):
            flags = summary["flags"]
            add("| %s | %d | %s | %d | %d | %d | %d |"
                % (name, summary["judged"], percent(summary["precision"]["rate"]),
                   summary["wrong"], flags["phantom"]["k"], flags["wrong_class"]["k"],
                   flags["bad_geometry"]["k"]))
        add("")

    if not stats.get("instruction"):
        add("## Screens")
        add("")
    for question, summary in (sorted(stats["screens"]["queue_sample"].items())
                              if not stats.get("instruction") else []):
        add("**%s** — %d answered: %s" % (
            question, summary["answered"],
            ", ".join("%s %d (%s)" % (value, summary["counts"][value],
                                      percent(summary["shares"][value]))
                      for value in sorted(summary["counts"]))))
    add("")

    missed = stats["missed"]["queue_sample"]
    recall = stats["recall"]["queue_sample"]
    if stats.get("instruction") or not missed["screens_checked"]:
        # An instruction campaign audits one target per screen; what the dense
        # annotation missed is a different campaign's question.
        add("## Coverage")
        add("")
        _coverage(add, stats)
        return "\n".join(lines) + "\n"
    add("## What the annotation missed")
    add("")
    add("| | |")
    add("| --- | ---: |")
    add("| screens checked blind | %d |" % missed["screens_checked"])
    add("| unannotated elements found | %d |" % missed["missing_elements_found"])
    add("| clicks that landed on a box after all | %d |"
        % missed["clicks_that_hit_an_existing_box"])
    add("| screens with at least one miss | %s |" % band(missed["screens_with_at_least_one_miss"]))
    add("| misses per screen, mean / median / max | %s / %s / %s |" % (
        "—" if missed["misses_per_screen_mean"] is None
        else "%.2f" % missed["misses_per_screen_mean"],
        missed["misses_per_screen_median"], missed["misses_per_screen_max"]))
    add("| recall, upper bound | %s |" % band(recall["recall_upper_bound"]))
    add("| F1 (%s precision with that bound) | %s |"
        % (stats["f1"]["source"],
           "—" if stats["f1"]["queue_sample"] is None
           else percent(stats["f1"]["queue_sample"])))
    add("")
    add("%s" % recall["note"])
    add("")

    agreement = stats["agreement"]
    add("## Agreement")
    add("")
    if agreement.get("note"):
        add(agreement["note"])
    for pair, questions in sorted((agreement.get("pairs") or {}).items()):
        add("")
        add("**%s**" % pair)
        add("")
        add("| unit | n | agreement | kappa |")
        add("| --- | ---: | ---: | ---: |")
        for name, entry in sorted(questions.items()):
            add("| %s | %d | %s | %s |" % (
                name, entry["n"], percent(entry["agreement"]),
                "—" if entry["kappa"] is None else "%.3f" % entry["kappa"]))
    add("")

    add("## Coverage")
    add("")
    _coverage(add, stats)
    return "\n".join(lines) + "\n"


def _coverage(add, stats: Dict[str, Any]) -> None:
    add("Items finished against items drawn, per stratum, so an under-covered")
    add("slice is visible rather than implied.")
    add("")
    for field, tables in sorted(stats["session"]["coverage"].items()):
        parts = []
        for level in sorted(tables["queue"]):
            parts.append("%s %d/%d" % (level, tables["done"].get(level, 0),
                                       tables["queue"][level]))
        add("- **%s** — %s" % (field, "  ".join(parts)))
    add("")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, type=Path,
                        help="campaign directory, e.g. audit/pilot40")
    parser.add_argument("--rater", action="append", default=[],
                        help="restrict to these raters (repeatable)")
    parser.add_argument("--primary-rater", default=None,
                        help="whose ratings the headline rates come from; a "
                             "double-rated element is one element, and the second "
                             "rating is used for agreement (default: the busiest)")
    parser.add_argument("--output", type=Path, default=None,
                        help="where to write stats.json (default: inside the campaign)")
    parser.add_argument("--markdown", type=Path, default=None,
                        help="where to write stats.md (default: inside the campaign)")
    parser.add_argument("--print", action="store_true", help="also print the markdown")
    args = parser.parse_args()

    campaign = audit_mod.Campaign(args.campaign)
    if not campaign.exists():
        raise SystemExit("no campaign at %s" % args.campaign)
    if campaign.torn_bytes:
        print("note: ignored %d unterminated bytes at the end of labels.jsonl "
              "(an interrupted session)" % campaign.torn_bytes)
    report = Report(campaign, args.rater, args.primary_rater)
    if not report.rows:
        raise SystemExit("no labels yet in %s" % campaign.labels_path)
    stats = report.build()
    markdown = render(stats)

    json_path = args.output or (campaign.root / "stats.json")
    md_path = args.markdown or (campaign.root / "stats.md")
    json_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    print("wrote %s and %s" % (json_path, md_path))
    instruction = stats.get("instruction")
    if instruction:
        ground = instruction["grounding"]["queue_sample"]
        print("grounded %d of %d instructions on target %s; %s sound"
              % (ground["hit"]["k"], ground["hit"]["n"], band(ground["hit"]),
                 band(instruction["quality"]["queue_sample"]["sound"])))
    swept = stats["swept"]["queue_sample"]
    summary = stats["elements"]["queue_sample"]
    if swept["screens"]:
        print("swept %d screens, %s elements, correct %s"
              % (swept["screens"], "{:,}".format(swept["judged"]),
                 band(swept["precision"])))
    if summary["judged"]:
        print("individually inspected %d elements, correct %s"
              % (summary["judged"], band(summary["precision"])))
    exceptions = stats["exceptions"]
    if exceptions["flagged"]:
        print("flagged during sweeps: %d elements (%s)"
              % (exceptions["flagged"],
                 ", ".join("%s %d" % pair for pair in exceptions["by_flag"].items())))
    print("%d items done of %d" % (stats["session"]["items_done"],
                                   stats["session"]["queue"]))
    if args.print:
        print()
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
