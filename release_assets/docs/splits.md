# Splits

The unit is the **scene**, never a frame: an episode's frames differ by one
click, so splitting them apart tests memorisation.

## What is held out, and what it cost

A held-out split is not a dial. Its size is the footprint of the attribute,
because a valid "unseen X" claim needs *every* scene containing X out of
training — not only the scenes you want to test on.

| axis | held out | observations | asks |
| --- | --- | ---: | --- |
| `test_app` | `gnome-system-monitor` | 11,350 | an unseen *category* |
| | `pluma` | 15,733 | an unseen *instance* beside three seen text editors |
| | `xarchiver` | 15,221 | an unseen *instance* beside file-roller |
| `test_theme` | `quartz_night_nord` | 46,730 | unseen desktop chrome |
| `test_resolution` | `retina_2880x1800` | 69,213 | unseen scale and aspect ratio |

`eog` was a fourth candidate — the unseen category "image viewer". It is not
held out: it appears in 109,454 observations against 11,352 for
`gnome-system-monitor`, and both answer the same question. Dropping it returned
94,326 observations to training and gave up no claim the design still makes.

`retina_2880x1800` is **not** the cheapest resolution — `uhd_3840x2160` is, at
4.83% against 7.06%. UHD is the densest supervision in the corpus, with the
smallest text and the most elements per screen, and training needs it more than
the split does. Of the candidates that leave UHD in training, 2880×1800 is both
the cheapest and the sharper test: it is one of only two 16:10 resolutions, so
holding it out leaves training a single 16:10 scale.

## Zero leakage, and how it is checked

Over the **union of applications across every frame of a scene**. One capture of
a scene can miss an application another capture of it saw; keying on a single
frame is exactly how three held-out applications leaked into train in an earlier
build. The union is taken over *publishable* frames only, since a frame the
release drops cannot leak — one scene turned on that distinction, where the
held-out application launched only in a copy that failed quality checks.

`scripts/verify_hf_release.py` re-checks this against the packed payload and
fails the release if any held-out attribute reaches `train`.

## Scenes the finalized split never placed

`splits_v3.jsonl` covers the 1,067,799 state-train-eligible observations. This
release carries every *publishable* observation, 1,207,368, so 12,515 scenes
needed a split it never assigned. They get one from the same rules in the same
priority (`test_app`, then `test_theme`, then `test_resolution`, else `train`),
and every existing v3 assignment is preserved exactly — verified per split.

`val` and `test_id` are **never** enlarged. They are a finalized random sample;
growing them after the fact would change what a number measured against them
means.

## Occlusion is a reporting axis, not a split

Holding out occluded scenes would remove the capability the corpus exists to
demonstrate. Report it as a degradation curve over `test_id` instead:

| occluded ratio | share of observations |
| --- | ---: |
| none | 2.3% |
| 0–10% | 15.6% |
| 10–25% | 27.5% |
| 25%+ | 54.6% |

Median 0.284, p90 0.743, p99 0.930. Window count is available as `n_windows` for
the same purpose.
