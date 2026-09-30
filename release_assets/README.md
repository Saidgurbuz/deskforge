---
pretty_name: DeskForge-1M
license: other
license_name: license-pending-review
task_categories:
  - image-to-text
  - object-detection
language:
  - en
tags:
  - gui
  - screen-understanding
  - computer-use
  - accessibility
  - webdataset
size_categories:
  - 1M<n<10M
configs:
  - config_name: preview
    data_files:
      - split: preview
        path: demo/preview-*.parquet
---

# DeskForge-1M

**1,207,368 annotated Linux desktop screenshots**, every visible element boxed
from the accessibility tree, with occlusion resolved against the real window
stack. 135,731 of the scenes are short click explorations, giving 917,211
state–action–state transitions on top of the still images.

The annotation is not a caption or a bounding box for one target. It is *every*
element a person can see, with its class, its visible text, its modal geometry
(the pixels it actually occupies) and its amodal geometry (where it would be if
nothing covered it).

## What is in it

| split | scenes | episodes | observations | state-eligible | transitions | transition-eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `train` | 266,159 | 111,249 | 999,494 | 881,794 | 760,850 | 716,573 |
| `val` | 2,938 | 1,284 | 11,279 | 10,081 | 8,667 | 8,147 |
| `test_id` | 5,805 | 2,524 | 22,550 | 20,097 | 17,383 | 16,371 |
| `test_app` | 11,512 | 6,553 | 43,206 | 39,884 | 32,848 | 29,934 |
| `test_theme` | 15,390 | 5,586 | 51,627 | 46,730 | 38,671 | 35,636 |
| `test_resolution` | 21,927 | 8,535 | 79,212 | 69,213 | 58,792 | 55,926 |
| **total** | **323,731** | **135,731** | **1,207,368** | **1,067,799** | **917,211** | **862,587** |

1,151 uncompressed WebDataset tars, 1.14 TB. 19 applications, 7 desktop themes,
7 resolutions from 1366×768 to 3840×2160.

**Two eligibility flags, and they are not the same thing.** `state_train_eligible`
is the recommended filter for the still-image view: publishable, not a
near-duplicate scene, not a no-op episode frame. `transition_train_eligible` is
for the action view and **keeps no-op transitions** — a click that changed
nothing is supervision, not a defect. A no-op frame is excluded from the states
view while the transition that produced it is kept.

## The evaluation splits are the contribution, not a convenience

A single random test set answers one question — "does it work on data like the
training data" — so three axes are held out of training **entirely**, not merely
sampled out:

| axis | held out | asks |
| --- | --- | --- |
| `test_app` | `gnome-system-monitor` | an application category never seen |
| | `pluma`, `xarchiver` | an unseen *instance* of a category training knows |
| `test_theme` | `quartz_night_nord` | unseen desktop chrome |
| `test_resolution` | `retina_2880x1800` | unseen scale and aspect ratio |

Holding out an application removes *every* scene containing it, not just the
test scenes, computed over the union of applications across every frame of a
scene. Verified zero leakage. See `docs/splits.md` for what each axis cost and
why these were affordable.

**Occlusion is deliberately not held out.** The corpus argues that this
supervision improves occlusion robustness, and a model cannot learn that from
data with the occlusion removed. Occlusion and window count are reported as
slices of `test_id` instead. They are heavy: median occluded ratio 0.284, p90
0.743, and more than half of all observations have ≥25% of their elements
clipped or dropped.

## Loading

Payload is WebDataset; the indexes are Parquet. No custom loader script.

```python
import webdataset as wds
url = "data/train/part-{00000..00903}.tar"
ds = (wds.WebDataset(url, shardshuffle=True)
        .decode("pil")
        .to_tuple("png", "leaf.json", "screentag.txt", "record.json"))
```

Each observation is four members sharing one key:

```
<key>.png             the screenshot
<key>.leaf.json       every visible element
<key>.screentag.txt   the serialized markup a model is trained to emit
<key>.record.json     sanitized metadata, and the action that produced this state
```

The tars are **uncompressed**, so `index/shard_members.parquet` gives a real
byte offset and length for every member: one seek returns a PNG without reading
the shard. `examples/` has three runnable loaders — streaming states, pairing
transitions, and random access by key. `demo/preview.parquet` is a stratified
sample with images inline for the Dataset Viewer. Its images are **downscaled to
1024 px on the long edge** so the viewer stays responsive - the canonical
full-resolution PNGs are in `data/`. ScreenTag coordinates are on a 0-500 grid
normalized to the viewport, so they still describe the resized image, and each
row keeps the original `width` and `height`.

Filter before you stream, with Parquet:

```python
import pyarrow.parquet as pq
obs = pq.read_table("index/observations/test_id.parquet")
heavy = obs.filter(obs["occluded_ratio"] > 0.5)   # the degradation slice
```

## How it was made

Xvfb + xfwm4 on Linux, real GTK applications driven headlessly. Annotations come
from the AT-SPI2 accessibility tree, then are resolved against the window stack
so a box describes what is *visible*, not what the tree claims. Generation is
deterministic from a seed; the generating commit and the SHA-256 of both source
manifests are in `release_provenance.json`, and `checksums/sha256sums.txt`
covers every published file.

Labels are **automatic**, not human. Quality is enforced by automated audits —
element coverage against rendered pixels, blank-widget suppression, leaf/window
ownership, fragment containment — and 59,103 observations that failed them are
not in this release.

## Actions: what they are and are not

Episodes are **undirected random click explorations**. They are not expert
demonstrations, not goal-conditioned trajectories, and carry no task
instruction, reward or success label. The action space is **click only**.

An action is stored on the step it *produced*:
`observation[k-1] --steps[k].action--> observation[k]`. Pixel coordinates are
authoritative; `_norm_1000` and `_screentag_500` are derived conveniences that
round-trip within one grid cell.

## Limitations

One desktop environment (Xfce), synthetic themes imitating Windows/macOS/Linux
rather than the real thing, click-only actions, no human verification, and
window-control and window-ownership edge cases documented in
`docs/known_issues.md`. Because the annotation transcribes what is on screen,
strings an application printed itself are in it — session paths, synthetic
persona home directories, the generating host name — all listed with counts in
`docs/known_issues.md`. No host path appears in any constructed metadata
record; that is enforced at pack time. Some scenes load live web pages, so a browser window
shows whatever that site served on the capture date; the domain is recorded.
Read `docs/known_issues.md` before using this for anything load-bearing.

## Licence

**Not yet determined — see `LICENSE`.** The screenshots contain third-party
application UI, icon themes, fonts, wallpapers and rendered web pages, and that
licensing has not been reviewed. Do not redistribute pending that review.

## Citation

```bibtex
@misc{deskforge1m,
  title  = {DeskForge-1M: dense desktop UI annotation with occlusion-resolved geometry},
  author = {G\"urb\"uz, Said},
  year   = {2026},
  note   = {Version 1.0.0}
}
```
