# Splits

The unit is the **scene**, never a frame: an episode's frames differ by one
click, so all frames of a scene stay in the same split.

## What is held out

A held-out attribute is removed from training entirely: every scene that
contains it, in any frame, belongs to its test split.

| split | held out | tests |
| --- | --- | --- |
| `test_app` | `gnome-system-monitor` | an unseen application category |
| | `pluma` | an unseen text editor, beside three seen ones |
| | `xarchiver` | an unseen archive manager, beside `file-roller` |
| `test_theme` | `quartz_night_nord` | unseen desktop styling |
| `test_resolution` | `retina_2880x1800` | an unseen scale and aspect ratio |

2880×1800 is one of only two 16:10 resolutions, so holding it out leaves
training a single 16:10 scale, while the densest screens (3840×2160, with the
smallest text and the most elements) stay in training.

## How scenes are assigned

Membership is decided over the **union of applications across every frame of a
scene**, so a held-out application cannot reach training through another frame
of the same scene. A scene that qualifies for more than one held-out split goes
to the first of `test_app`, `test_theme` and `test_resolution`. All other scenes
are divided between `train`, `val` and `test_id`, where `val` and `test_id` are
random samples of scenes whose attributes all appear in training.

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
