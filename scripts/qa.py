#!/usr/bin/env python
"""One command for "is the annotation any good, and did I just break it".

Every quality question in this project was being answered by hand: run the three
audits, then write a throwaway script to compare the numbers against the last
run, then remember which direction each number should move. That was done four
separate times in one session, with a different ad-hoc comparison each time, and
the numbers only lived in a chat log.

    qa.py audit   --root <run>                 measure one run
    qa.py compare --before <run> --after <run> what changed, and is it better
    qa.py gate    --root <run>                 fail if it regressed vs baseline
    qa.py baseline --root <run> --write        accept a run as the new baseline

`audit` writes `quality.json` next to the run, so every later comparison reads
measurements instead of recomputing them.

## The metrics, and which way is good

Both error directions are measured, because fixing one by breaking the other is
the easy mistake:

- `fp_blank_rate` - annotated widgets with nothing drawn behind them. Down is
  good. Catches boxes over blank pixels (GTK overlay scrollbars).
- `fn_uncovered_ink` - visible ink inside a window that no annotation covers.
  Down is good. Catches missing widgets.
- `phantom_rate` / `drift_rate` - text claimed where there is no ink, and text
  boxes displaced from their glyphs. Down is good.
- `text_unsupported_rate` - elements whose text was withheld because visibility
  could not be determined. Down is good: withheld text is content the screen
  shows and ground truth does not.
- `text_name_only_rate` - elements whose string is an accessible name the box
  cannot be rendering, so no glyphs of it are emitted. Reported beside
  `text_unsupported_rate` on purpose: both withhold text, and moving elements
  from one to the other is not an improvement, so the pair has to be read
  together.
- `elements_per_capture` - a guard rail, not a goal. A big drop alongside
  "improvements" means annotations were deleted rather than fixed.

`gate` fails on any of these moving the wrong way past its tolerance, so a
regression is caught by a command rather than by remembering last week's number.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

DEFAULT_BASELINE = PROJECT_ROOT / "tasks" / "quality_baseline.json"

#: How much each metric may worsen before `gate` fails. Absolute, in the units
#: the metric is reported in (rates are fractions, not percentages).
#:
#: These are not aspirations - they are noise floors. Which windows a seed draws
#: varies between runs even at the same seed, and per-app uncovered ink moved by
#: 1-2 points between two runs that differed by no code at all, so a tighter
#: tolerance would fail on sampling alone.
TOLERANCE: Dict[str, float] = {
    "fp_blank_rate": 0.005,
    "fn_uncovered_ink": 0.02,
    "phantom_rate": 0.005,
    "drift_rate": 0.005,
    "text_unsupported_rate": 0.02,
}

#: Element density may fall this much before it counts as deletion rather than
#: repair.
DENSITY_DROP_TOLERANCE = 0.10


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _captures(root: Path) -> List[Tuple[Path, Path]]:
    out = []
    for leaf in sorted(root.rglob("*.elements.leaf.json")):
        png = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".png"))
        if png.is_file():
            out.append((png, leaf))
    return out


def _scan_text_lazy():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from deskshot.privacy import scan_text as _fn

    return _fn


scan_text = _scan_text_lazy()


def measure(root: Path) -> Dict[str, Any]:
    """Run every audit over `root` and reduce them to one comparable record."""
    coverage = _load("aec", "audit_element_coverage.py")
    structure = _load("ast_", "audit_structure.py")
    blanks = _load("abw", "audit_blank_widgets.py")
    pixels = _load("aap", "audit_annotation_pixels.py")

    captures = _captures(root)
    if not captures:
        raise SystemExit(f"qa: no captures under {root}")

    by_app: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"ink": 0.0, "uncovered": 0.0, "decoration": 0.0, "windows": 0.0}
    )
    blank_checked = blank_hit = 0
    text_total = text_unsupported = text_name_only = 0
    phantom_total = phantom_hit = drift_hit = 0
    elements_total = 0
    status_counts: Counter = Counter()
    blank_by_role: Counter = Counter()
    struct: Counter = Counter()
    windows_with_controls = windows_decorated = 0
    identifying: Counter = Counter()
    captures_identifying = 0

    for png, leaf in captures:
        data = json.loads(leaf.read_text(encoding="utf-8"))
        elements = data if isinstance(data, list) else data.get("elements", [])
        elements_total += len(elements)

        cov = coverage.audit_capture(png, elements)
        for win in cov["windows"]:
            b = by_app[str(win["app_name"])]
            b["ink"] += win["ink"]
            b["uncovered"] += win["uncovered_ink"]
            b["decoration"] += win["decoration_ink"]
            b["windows"] += 1

        blank = blanks.audit_capture(png, elements)
        blank_checked += blank["num_checked"]
        blank_hit += blank["num_blank"]
        for entry in blank["blank"]:
            blank_by_role[str(entry["role"])] += 1

        pix = pixels.audit_capture(png, elements)
        phantom_total += pix["num_text_elements"]
        phantom_hit += pix["num_text_phantoms"]
        drift_hit += pix["num_text_drifted"]

        st = structure.check_capture(elements)
        for key in ("covered_but_visible", "escapes_parent", "duplicate_leaf"):
            struct[key] += len(st[key])
        for row in st["windows_checked"]:
            windows_decorated += 1
            if row["controls"]:
                windows_with_controls += 1

        # A capture that names the account, the host or the checkout cannot be
        # published, and unlike every other metric here it cannot be repaired
        # afterwards: the string is in the pixels. Counted per capture so the
        # number reads as "how much of this run is unpublishable".
        leaks_here = 0
        for elem in elements:
            for field in ("name", "inner_text", "visible_text", "description", "value"):
                text = elem.get(field)
                if isinstance(text, str):
                    for leak in scan_text(text, include_brand=False):
                        identifying[leak.literal] += 1
                        leaks_here += 1
        if leaks_here:
            captures_identifying += 1

        for elem in elements:
            status = elem.get("visible_text_status")
            if status in (None, "no_text"):
                continue
            text_total += 1
            status_counts[str(status)] += 1
            if str(status).startswith("unsupported"):
                text_unsupported += 1
            elif status == "name_only":
                text_name_only += 1

    seeds = []
    provenances = []
    for _png, leaf in captures:
        meta = leaf.with_name(leaf.name.replace(".elements.leaf.json", ".meta.json"))
        if meta.is_file():
            try:
                seed = json.loads(meta.read_text(encoding="utf-8")).get("scene", {}).get("seed")
            except (OSError, ValueError):
                seed = None
            if isinstance(seed, int):
                seeds.append(seed)
            try:
                prov = json.loads(meta.read_text(encoding="utf-8")).get("provenance")
            except (OSError, ValueError):
                prov = None
            if prov:
                provenances.append(prov)

    total_ink = sum(v["ink"] for v in by_app.values())
    total_unc = sum(v["uncovered"] for v in by_app.values())

    return {
        "root": str(root),
        "captures": len(captures),
        "seeds": sorted(set(seeds)),
        # One run is normally one build and one pool; the first stamp stands for
        # the run, and a mixed run is worth knowing about rather than averaging.
        "provenance": provenances[0] if provenances else None,
        "provenance_mixed": len({json.dumps(p, sort_keys=True) for p in provenances}) > 1,
        "elements_per_capture": round(elements_total / len(captures), 2),
        "fp_blank_rate": round(blank_hit / max(1, blank_checked), 5),
        "fp_blank_count": blank_hit,
        "fp_blank_checked": blank_checked,
        "fp_blank_by_role": dict(blank_by_role.most_common()),
        "fn_uncovered_ink": round(total_unc / max(1.0, total_ink), 5),
        "phantom_rate": round(phantom_hit / max(1, phantom_total), 5),
        "drift_rate": round(drift_hit / max(1, phantom_total), 5),
        "text_unsupported_rate": round(text_unsupported / max(1, text_total), 5),
        "text_name_only_rate": round(text_name_only / max(1, text_total), 5),
        "text_status": dict(status_counts.most_common()),
        # Invariants: a violation is a contradiction, not a judgment call, so
        # these are gated at zero rather than against a tolerance.
        "identifying_captures": captures_identifying,
        "identifying_rate": round(captures_identifying / max(1, len(captures)), 5),
        "identifying_literals": dict(identifying.most_common()),
        "covered_but_visible": struct["covered_but_visible"],
        "escapes_parent": struct["escapes_parent"],
        "duplicate_leaf": struct["duplicate_leaf"],
        "window_control_coverage": round(
            windows_with_controls / max(1, windows_decorated), 5),
        "windows_decorated": windows_decorated,
        "by_app": {
            app: {
                "windows": int(v["windows"]),
                "uncovered": round(v["uncovered"] / max(1.0, v["ink"]), 5),
                "decoration": round(v["decoration"] / max(1.0, v["ink"]), 5),
            }
            for app, v in sorted(by_app.items())
        },
    }


def print_report(m: Dict[str, Any]) -> None:
    print(f"captures={m['captures']}  elements/capture={m['elements_per_capture']}")
    print()
    print(f"  false positives  blank widgets   {m['fp_blank_rate'] * 100:6.2f}%"
          f"   ({m['fp_blank_count']}/{m['fp_blank_checked']})")
    print(f"                   text phantoms   {m['phantom_rate'] * 100:6.2f}%")
    print(f"                   text drift      {m['drift_rate'] * 100:6.2f}%")
    print(f"  false negatives  uncovered ink   {m['fn_uncovered_ink'] * 100:6.2f}%")
    print(f"                   window controls {m.get('window_control_coverage', 0) * 100:6.2f}%"
          f"   of {m.get('windows_decorated', 0)} decorated windows")
    print(f"                   text withheld   {m['text_unsupported_rate'] * 100:6.2f}%")
    identifying = m.get("identifying_rate", 0)
    print(f"  publishable      identifying     {identifying * 100:6.2f}%"
          f"   of captures ({m.get('identifying_captures', 0)})")
    for literal, count in list((m.get("identifying_literals") or {}).items())[:4]:
        print(f"                     {count:5d}  {literal!r}")
    print(f"                   name not drawn  {m.get('text_name_only_rate', 0) * 100:6.2f}%")
    print(f"  invariants       covered/visible {m.get('covered_but_visible', 0):6}"
          f"   escapes {m.get('escapes_parent', 0)}   duplicates {m.get('duplicate_leaf', 0)}")
    if m["fp_blank_by_role"]:
        print(f"\n  blank by role: {m['fp_blank_by_role']}")
    print(f"\n{'app':22}{'windows':>8}{'uncovered':>11}{'decoration':>12}")
    print("-" * 55)
    for app, v in sorted(m["by_app"].items(), key=lambda kv: -kv[1]["uncovered"]):
        print(f"{app:22}{v['windows']:>8}{v['uncovered'] * 100:>10.1f}%{v['decoration'] * 100:>11.1f}%")


def _delta_line(name: str, before: float, after: float, *, lower_is_better: bool = True) -> str:
    d = after - before
    good = (d <= 0) if lower_is_better else (d >= 0)
    arrow = "  " if abs(d) < 1e-9 else ("OK" if good else "!!")
    return (f"  {arrow} {name:26}{before * 100:8.2f}% -> {after * 100:7.2f}%"
            f"   ({d * 100:+.2f})")


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    """Print a before/after table. Returns True when nothing regressed."""
    print(f"before: {before['root']}  ({before['captures']} captures)")
    print(f"after:  {after['root']}  ({after['captures']} captures)\n")
    ok = True
    for key in ("fp_blank_rate", "phantom_rate", "drift_rate",
                "fn_uncovered_ink", "text_unsupported_rate"):
        print(_delta_line(key, before[key], after[key]))
        if after[key] - before[key] > TOLERANCE[key]:
            ok = False
    # Not gated: it is a reclassification of withheld text, not an error rate.
    # Printed so that a fall in `text_unsupported_rate` cannot be read as text
    # gained when it was text moved.
    print(_delta_line("text_name_only_rate",
                      before.get("text_name_only_rate", 0.0),
                      after.get("text_name_only_rate", 0.0)))

    b_density, a_density = before["elements_per_capture"], after["elements_per_capture"]
    drop = (b_density - a_density) / max(1.0, b_density)
    flag = "!!" if drop > DENSITY_DROP_TOLERANCE else "OK"
    print(f"  {flag} {'elements/capture':26}{b_density:8.1f}  -> {a_density:7.1f}"
          f"   ({a_density - b_density:+.1f})")
    if drop > DENSITY_DROP_TOLERANCE:
        ok = False

    apps = sorted(set(before["by_app"]) | set(after["by_app"]))
    moved = []
    for app in apps:
        b = before["by_app"].get(app, {}).get("uncovered")
        a = after["by_app"].get(app, {}).get("uncovered")
        if b is None or a is None:
            continue
        if abs(a - b) >= 0.01:
            moved.append((a - b, app, b, a))
    if moved:
        print("\n  per-app uncovered ink, biggest moves:")
        for d, app, b, a in sorted(moved):
            mark = "OK" if d < 0 else "!!"
            print(f"    {mark} {app:20}{b * 100:6.1f}% -> {a * 100:5.1f}%  ({d * 100:+.1f})")
    return ok


#: Below this, one unusual scene moves every rate and the verdict means nothing.
MIN_CAPTURES_FOR_VERDICT = 10

#: Fraction of the baseline's seeds a run must share before the comparison is
#: about the code rather than about which windows the scenes happened to draw.
MIN_SEED_OVERLAP = 0.5


def comparable(m: Dict[str, Any], baseline: Dict[str, Any]) -> Tuple[bool, str]:
    """Is this run measuring the same thing the baseline measured?

    Every metric here is composition-sensitive: a chromium-heavy set of scenes
    has more withheld text and more elements per capture than a mousepad-heavy
    one, with identical code. Judging across different seeds therefore reports
    the scene mix as if it were a regression - a 5-capture run on a different
    seed range failed on density and withheld text purely for that reason. A
    gate that cries wolf gets ignored, which is worse than no gate.
    """
    if m["captures"] < MIN_CAPTURES_FOR_VERDICT:
        return False, (f"only {m['captures']} captures "
                       f"(need {MIN_CAPTURES_FOR_VERDICT} for a verdict)")
    mine, theirs = set(m.get("seeds") or []), set(baseline.get("seeds") or [])
    if not mine or not theirs:
        return False, "no seeds recorded in one of the runs"
    overlap = len(mine & theirs) / max(1, len(theirs))
    if overlap < MIN_SEED_OVERLAP:
        return False, (f"only {overlap * 100:.0f}% of the baseline's seeds are in this run "
                       f"- different scenes, not a like-for-like comparison")

    # Seed overlap was always a proxy for "the same seed still means the same
    # scene". Now that captures carry what they were resolved against, ask
    # directly: same app pool and config, any commit. The commit is excluded on
    # purpose - it is the thing under test.
    mine_prov, base_prov = m.get("provenance"), baseline.get("provenance")
    if mine_prov and base_prov:
        from deskshot.provenance import same_scene_inputs

        if not same_scene_inputs(mine_prov, base_prov):
            return False, ("the app pool or config changed since the baseline, so the "
                           "same seeds no longer describe the same scenes")
        if m.get("provenance_mixed") or baseline.get("provenance_mixed"):
            return False, "a run mixes builds or pools; regenerate it before gating"
        return True, f"{overlap * 100:.0f}% seed overlap, same pool and config"
    return True, f"{overlap * 100:.0f}% seed overlap (no provenance stamp to confirm it)"


def gate(m: Dict[str, Any], baseline: Dict[str, Any], *, require_clean: bool = False) -> bool:
    print(f"gating {m['root']} against baseline recorded from {baseline.get('root')}")
    is_comparable, why = comparable(m, baseline)
    print(f"comparability: {why}\n")
    ok = True
    for key, tol in TOLERANCE.items():
        b, a = baseline.get(key), m.get(key)
        if b is None or a is None:
            continue
        regressed = (a - b) > tol
        ok = ok and not regressed
        print(f"  {'FAIL' if regressed else 'pass'} {key:26}"
              f"baseline {b * 100:6.2f}%  now {a * 100:6.2f}%  (tolerance +{tol * 100:.1f})")
    for key in ("covered_but_visible", "escapes_parent", "duplicate_leaf"):
        now = m.get(key)
        if now is None:
            continue
        was = baseline.get(key, 0)
        # These are contradictions; the bar is "no worse than the baseline, and
        # never growing". Not zero outright, because one duplicate predates the
        # check and gating on it would block every unrelated change.
        regressed = now > was
        ok = ok and not regressed
        print(f"  {'FAIL' if regressed else 'pass'} {key:26}"
              f"baseline {was:6}   now {now:6}  (must not grow)")

    # Reported, not gated by default. These are filterable after the fact - the
    # affected samples get dropped, which needs no re-run - so failing the gate
    # on them would make it fail on every run of a corpus that is known to
    # contain some, and a gate that always fails is a gate nobody reads. Pass
    # --require-clean at publication time, when zero is the actual bar.
    identifying = m.get("identifying_captures")
    if identifying is not None:
        if require_clean:
            ok = ok and not identifying
        label = "FAIL" if (identifying and require_clean) else "pass" if not identifying else "note"
        print(f"  {label} {'identifying_captures':26}"
              f"{identifying:6} of {m['captures']} captures"
              f"  ({'must be 0' if require_clean else 'filterable after the run'})")

    b_density = baseline.get("elements_per_capture")
    if b_density:
        drop = (b_density - m["elements_per_capture"]) / max(1.0, b_density)
        regressed = drop > DENSITY_DROP_TOLERANCE
        ok = ok and not regressed
        print(f"  {'FAIL' if regressed else 'pass'} {'elements_per_capture':26}"
              f"baseline {b_density:6.1f}   now {m['elements_per_capture']:6.1f}"
              f"  (max drop {DENSITY_DROP_TOLERANCE * 100:.0f}%)")
    if not is_comparable:
        print("\nADVISORY - the numbers above are reported, not enforced, because "
              "this run and the baseline\n           are not comparable. Re-run "
              "with the baseline's seeds to get a verdict.")
        return True
    print("\n" + ("PASS - no regression against baseline" if ok else "FAIL - see above"))
    return ok


def _read(path: Path) -> Dict[str, Any]:
    if path.is_dir():
        path = path / "quality.json"
    if not path.is_file():
        raise SystemExit(f"qa: no quality.json at {path} - run `qa.py audit --root <run>` first")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="measure a run and write quality.json")
    a.add_argument("--root", required=True)
    a.add_argument("--out", default=None, help="where to write quality.json (default: --root)")

    c = sub.add_parser("compare", help="before/after with a verdict")
    c.add_argument("--before", required=True)
    c.add_argument("--after", required=True)

    g = sub.add_parser("gate", help="fail if a run regressed against the baseline")
    g.add_argument("--root", required=True)
    g.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    g.add_argument("--require-clean", action="store_true",
                   help="also fail on any capture carrying an identifier "
                        "(the bar at publication time, not during a run)")

    b = sub.add_parser("baseline", help="accept a run as the new baseline")
    b.add_argument("--root", required=True)
    b.add_argument("--write", action="store_true")
    b.add_argument("--baseline", default=str(DEFAULT_BASELINE))

    args = ap.parse_args()

    if args.cmd == "audit":
        m = measure(Path(args.root))
        out = Path(args.out or args.root)
        out.mkdir(parents=True, exist_ok=True)
        (out / "quality.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
        print_report(m)
        print(f"\nwrote {out / 'quality.json'}")
        return 0

    if args.cmd == "compare":
        return 0 if compare(_read(Path(args.before)), _read(Path(args.after))) else 1

    if args.cmd == "gate":
        root = Path(args.root)
        m = _read(root) if (root / "quality.json").is_file() else measure(root)
        baseline_path = Path(args.baseline)
        if not baseline_path.is_file():
            print(f"qa: no baseline at {baseline_path}; record one with `qa.py baseline --write`")
            return 0
        return 0 if gate(
            m,
            json.loads(baseline_path.read_text(encoding="utf-8")),
            require_clean=getattr(args, "require_clean", False),
        ) else 1

    if args.cmd == "baseline":
        root = Path(args.root)
        m = _read(root) if (root / "quality.json").is_file() else measure(root)
        print_report(m)
        if not args.write:
            print("\n(dry run - pass --write to record this as the baseline)")
            return 0
        Path(args.baseline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.baseline).write_text(json.dumps(m, indent=2), encoding="utf-8")
        print(f"\nbaseline written to {args.baseline}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
