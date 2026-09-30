---
pretty_name: DeskForge-1M
license: mit
task_categories:
- image-to-text
- object-detection
language:
- en
tags:
- gui
- gui-grounding
- screen-parsing
- computer-use
- desktop
- accessibility
- webdataset
size_categories:
- 1M<n<10M
configs:
- config_name: default
  default: true
  data_files:
  - split: train
    path: data/train/*.tar
  - split: val
    path: data/val/*.tar
  - split: test_id
    path: data/test_id/*.tar
  - split: test_app
    path: data/test_app/*.tar
  - split: test_theme
    path: data/test_theme/*.tar
  - split: test_resolution
    path: data/test_resolution/*.tar
- config_name: transitions_preview
  data_files:
  - split: train
    path: demo/transitions/train.tar
  - split: val
    path: demo/transitions/val.tar
  - split: test_id
    path: demo/transitions/test_id.tar
  - split: test_app
    path: demo/transitions/test_app.tar
  - split: test_theme
    path: demo/transitions/test_theme.tar
  - split: test_resolution
    path: demo/transitions/test_resolution.tar
dataset_info:
- config_name: default
  features:
  - name: png
    dtype: image
  - name: leaf.json
    list:
    - name: _children_dom_indices
      list: int64
    - name: _depth
      dtype: int64
    - name: _dom_index
      dtype: int64
    - name: _hierarchy_visibility_clipped
      dtype: bool
    - name: _hierarchy_visibility_original_rect
      struct:
      - name: h
        dtype: int64
      - name: w
        dtype: int64
      - name: x
        dtype: int64
      - name: y
        dtype: int64
    - name: _is_occluded_by_overlap
      dtype: bool
    - name: _occlusion_clipped
      dtype: bool
    - name: _occlusion_original_rect
      struct:
      - name: h
        dtype: int64
      - name: w
        dtype: int64
      - name: x
        dtype: int64
      - name: y
        dtype: int64
    - name: _occlusion_partially_covered
      dtype: bool
    - name: _occlusion_preserved_full_rect
      dtype: bool
    - name: _overlay_occlusion_clipped
      dtype: bool
    - name: _parent_dom_index
      dtype: int64
    - name: _row_partition_has_text_sibling
      dtype: bool
    - name: _source_dom_index
      dtype: int64
    - name: _source_parent_dom_index
      dtype: int64
    - name: _visibility_source_rect
      struct:
      - name: h
        dtype: int64
      - name: w
        dtype: int64
      - name: x
        dtype: int64
      - name: y
        dtype: int64
    - name: _window_owner_dom_index
      dtype: int64
    - name: _window_stack_index
      dtype: int64
    - name: app_name
      dtype: string
    - name: attrs
      struct:
      - name: action_names
        list: string
      - name: description
        dtype: string
      - name: interfaces
        struct:
        - name: action
          dtype: bool
        - name: document
          dtype: bool
        - name: editable_text
          dtype: bool
        - name: image
          dtype: bool
        - name: selection
          dtype: bool
        - name: table
          dtype: bool
        - name: text
          dtype: bool
        - name: value
          dtype: bool
      - name: rescued
        dtype: string
      - name: states
        struct:
        - name: checkable
          dtype: bool
        - name: checked
          dtype: bool
        - name: editable
          dtype: bool
        - name: enabled
          dtype: bool
        - name: expandable
          dtype: bool
        - name: expanded
          dtype: bool
        - name: focusable
          dtype: bool
        - name: has_popup
          dtype: bool
        - name: modal
          dtype: bool
        - name: multi_line
          dtype: bool
        - name: pressed
          dtype: bool
        - name: read_only
          dtype: bool
        - name: selectable
          dtype: bool
        - name: selected
          dtype: bool
        - name: sensitive
          dtype: bool
        - name: single_line
          dtype: bool
      - name: synthesized
        dtype: string
      - name: synthetic
        dtype: string
      - name: text_source
        dtype: string
    - name: children_indices
      list: int64
    - name: classes
      dtype: string
    - name: frame_index
      dtype: int64
    - name: id
      dtype: string
    - name: inner_text
      dtype: string
    - name: interaction
      struct:
      - name: actionable
        dtype: bool
      - name: actions
        list: string
      - name: checkable
        dtype: bool
      - name: checked
        dtype: bool
      - name: click_point
        list: int64
      - name: editable
        dtype: bool
      - name: enabled
        dtype: bool
      - name: expandable
        dtype: bool
      - name: expanded
        dtype: bool
      - name: focusable
        dtype: bool
      - name: selected
        dtype: bool
    - name: is_occluded
      dtype: bool
    - name: kind
      dtype: string
    - name: name
      dtype: string
    - name: occlusion_state
      dtype: string
    - name: parent_index
      dtype: int64
    - name: position
      dtype: string
    - name: reading_order_index
      dtype: int64
    - name: rect
      struct:
      - name: h
        dtype: int64
      - name: w
        dtype: int64
      - name: x
        dtype: int64
      - name: y
        dtype: int64
    - name: role
      dtype: string
    - name: source
      dtype: string
    - name: tag
      dtype: string
    - name: text_geometry_confidence
      dtype: float64
    - name: text_geometry_offset_px
      dtype: int64
    - name: text_geometry_unvalidated
      dtype: bool
    - name: text_rehomed_from
      struct:
      - name: h
        dtype: int64
      - name: w
        dtype: int64
      - name: x
        dtype: int64
      - name: y
        dtype: int64
    - name: type
      dtype: string
    - name: uid
      dtype: string
    - name: visible_fragments
      list:
      - name: h
        dtype: int64
      - name: w
        dtype: int64
      - name: x
        dtype: int64
      - name: y
        dtype: int64
    - name: visible_text
      dtype: string
    - name: visible_text_confidence
      dtype: float64
    - name: visible_text_marked
      dtype: string
    - name: visible_text_status
      dtype: string
    - name: vlm_label
      dtype: string
    - name: z
      dtype: int64
    - name: _coords_repaired_from_detached_menu_popup
      dtype: bool
    - name: _detached_menu_popup_repair_source
      dtype: string
  - name: screentag.txt
    dtype: string
  - name: record.json
    struct:
    - name: action_into_this_state
      struct:
      - name: point_norm_1000
        list: int64
      - name: point_px
        list: int64
      - name: point_screentag_500
        list: int64
      - name: target_app
        dtype: string
      - name: target_bbox_norm_1000
        list: int64
      - name: target_bbox_px
        list: int64
      - name: target_kind
        dtype: string
      - name: target_role
        dtype: string
      - name: target_text
        dtype: string
      - name: target_uid
        dtype: string
      - name: train_eligible
        dtype: bool
      - name: transition_id
        dtype: string
      - name: type
        dtype: string
    - name: apps
      list: string
    - name: dataset_version
      dtype: string
    - name: desktop_env
      dtype: string
    - name: effect_of_that_action
      struct:
      - name: appeared
        dtype: int64
      - name: changed
        dtype: bool
      - name: disappeared
        dtype: int64
      - name: magnitude
        dtype: float64
      - name: moved
        dtype: int64
      - name: newly_occluded
        dtype: int64
      - name: persisted
        dtype: int64
      - name: revealed
        dtype: int64
      - name: semantic_changes
        dtype: int64
      - name: state_changed
        dtype: int64
      - name: text_changed
        dtype: int64
    - name: episode_id
      dtype: string
    - name: flags
      struct:
      - name: missing_apps
        list: string
      - name: near_duplicate
        dtype: bool
      - name: no_op_frame
        dtype: bool
      - name: publishable
        dtype: bool
      - name: state_train_eligible
        dtype: bool
    - name: group
      dtype: string
    - name: height
      dtype: int64
    - name: include_desktop_chrome
      dtype: bool
    - name: n_elements
      dtype: int64
    - name: n_elements_filtered
      dtype: int64
    - name: n_windows
      dtype: int64
    - name: observation_key
      dtype: string
    - name: occlusion
      struct:
      - name: num_clipped
        dtype: int64
      - name: num_dropped
        dtype: int64
      - name: num_elements_in
        dtype: int64
    - name: provenance
      struct:
      - name: annotation_source
        dtype: string
      - name: generator
        dtype: string
      - name: generator_commit
        dtype: string
    - name: scene
      struct:
      - name: desktop_content_pack
        dtype: string
      - name: desktop_layout_template
        dtype: string
      - name: desktop_profile
        dtype: string
      - name: display_preset
        dtype: string
      - name: layout
        dtype: string
      - name: panel_variant
        dtype: string
      - name: seed
        dtype: int64
      - name: theme_preset
        dtype: string
    - name: scene_id
      dtype: string
    - name: screentag_grid
      dtype: int64
    - name: split
      dtype: string
    - name: step_index
      dtype: int64
    - name: theme
      struct:
      - name: gtk_theme
        dtype: string
      - name: icon_theme
        dtype: string
      - name: wallpaper
        dtype: string
      - name: wm_theme
        dtype: string
    - name: width
      dtype: int64
    - name: window_stack
      list:
      - name: name
        dtype: string
      - name: rect
        struct:
        - name: h
          dtype: int64
        - name: w
          dtype: int64
        - name: x
          dtype: int64
        - name: y
          dtype: int64
      - name: stack_index
        dtype: int64
  - name: __key__
    dtype: string
  - name: __url__
    dtype: string
  splits:
  - name: train
    num_examples: 999494
  - name: val
    num_examples: 11279
  - name: test_id
    num_examples: 22550
  - name: test_app
    num_examples: 43206
  - name: test_theme
    num_examples: 51627
  - name: test_resolution
    num_examples: 79212
- config_name: transitions_preview
  features:
  - name: instruction.txt
    dtype: string
  - name: before.png
    dtype: image
  - name: target.png
    dtype: image
  - name: after.png
    dtype: image
  - name: action.txt
    dtype: string
  - name: transition.json
    struct:
    - name: transition_id
      dtype: string
    - name: episode_id
      dtype: string
    - name: action_index
      dtype: int64
    - name: before_key
      dtype: string
    - name: after_key
      dtype: string
    - name: instruction
      struct:
      - name: text
        dtype: string
      - name: style
        dtype: string
      - name: variants
        list:
        - name: style
          dtype: string
        - name: text
          dtype: string
        - name: roundtrip
          dtype: string
      - name: referring_expression
        dtype: string
    - name: action
      struct:
      - name: type
        dtype: string
      - name: point_px
        list: int64
      - name: point_norm_1000
        list: int64
      - name: target
        struct:
        - name: uid
          dtype: string
        - name: role
          dtype: string
        - name: kind
          dtype: string
        - name: text
          dtype: string
        - name: app
          dtype: string
        - name: bbox_px
          list: int64
        - name: bbox_norm_1000
          list: int64
    - name: effect
      struct:
      - name: changed
        dtype: bool
      - name: magnitude
        dtype: float64
      - name: appeared
        dtype: int64
      - name: disappeared
        dtype: int64
      - name: moved
        dtype: int64
      - name: text_changed
        dtype: int64
      - name: state_changed
        dtype: int64
      - name: newly_occluded
        dtype: int64
      - name: revealed
        dtype: int64
      - name: semantic_changes
        dtype: int64
      - name: persisted
        dtype: int64
    - name: scene
      struct:
      - name: apps
        list: string
      - name: theme
        dtype: string
      - name: resolution
        dtype: string
      - name: width
        dtype: int64
      - name: height
        dtype: int64
  - name: __key__
    dtype: string
  - name: __url__
    dtype: string
  splits:
  - name: train
    num_examples: 100
  - name: val
    num_examples: 100
  - name: test_id
    num_examples: 100
  - name: test_app
    num_examples: 100
  - name: test_theme
    num_examples: 100
  - name: test_resolution
    num_examples: 100
---

# DeskForge-1M

**DeskForge-1M** is a corpus of **1.21M annotated desktop screenshots** with
**159.7M element instances** and **917K recorded click transitions**, generated
with [DeskForge](https://github.com/Saidgurbuz/deskforge), a controllable
desktop environment that composes and explores real applications.

[**Project page**](https://saidgurbuz.github.io/deskforge/) ·
[**Code**](https://github.com/Saidgurbuz/deskforge) · **Paper** (coming soon)

A. Said Gurbuz · Ahmed Nassar · Sunghwan Hong · Marc Pollefeys · Peter W. J. Staar
<br>ETH Zurich · IBM Research Zurich · Microsoft

![DeskForge overview](https://saidgurbuz.github.io/deskforge/assets/img/overview-2400.webp)

Every screenshot is annotated densely: not one target, but **every visible
element**, with its type, text, geometry, interaction properties, hierarchy and
owning window. Overlapping windows are resolved, so each element records both
its full extent and the fragments that are actually visible. Scenes combine
several real applications with varied content, window layouts, appearance
presets and display resolutions, and short click explorations link each action
to the screen before and after it.

## At a glance

| | |
| --- | --- |
| Observations | 1,207,368 screenshots in 323,731 scenes |
| Element instances | 159.7M (132 per screen on average) |
| Click transitions | 917,211, from 135,731 exploration episodes |
| Instructions | 663,635 clicks with natural-language instructions |
| Applications | 19 real Linux desktop applications, several per screen |
| Appearance | 7 presets, from classic Linux to Windows- and macOS-inspired styles |
| Resolutions | 7, from 1366×768 to 3840×2160 |

## Splits

Splits are made per **scene**, so all frames of a scene stay together. Three test
splits hold an attribute out of training **entirely**, which measures
generalization to desktop configurations never seen in training.

| split | held out | observations | transitions | instructions |
| --- | --- | ---: | ---: | ---: |
| `train` | | 999,494 | 760,850 | 551,651 |
| `val` | | 11,279 | 8,667 | 6,281 |
| `test_id` | new scenes with seen attributes | 22,550 | 17,383 | 12,589 |
| `test_app` | GNOME System Monitor, Pluma, Xarchiver | 43,206 | 32,848 | 22,729 |
| `test_theme` | the Quartz Night Nord preset | 51,627 | 38,671 | 27,175 |
| `test_resolution` | 2880×1800 | 79,212 | 58,792 | 43,210 |

A held-out application is excluded from every scene that contains it, in any
frame. See [`docs/splits.md`](docs/splits.md) for how each axis was chosen.

## What a sample contains

The default configuration streams the observations as WebDataset shards. Each
observation is four members sharing one key:

| member | content |
| --- | --- |
| `png` | the screenshot |
| `leaf.json` | every visible element: type, role, name and visible text, `rect` and `visible_fragments`, interaction state, parent and reading order, owning application and window |
| `screentag.txt` | the screen serialized as [ScreenTag](https://github.com/Saidgurbuz/screenparse) markup, on a 0–500 grid normalized to the screenshot |
| `record.json` | scene metadata (applications, preset, resolution, window stack), eligibility flags, and the action that produced this state |

Alongside the shards, `index/` holds Parquet tables for observations,
transitions, instructions, episodes and scenes:
`index/transitions/<split>.parquet` lists every recorded click with its before
and after observation keys, target element and effect, and
`index/instructions/<split>.parquet` gives the clicks their natural-language
instructions. `demo/` holds browsing samples: stratified observations with
images inline (downscaled to 1024 px) and the `transitions_preview` shards
below. The field reference is in [`docs/schema.md`](docs/schema.md).

### Browsing transitions

The `transitions_preview` subset shows recorded clicks in the Dataset Viewer:
100 per split, each from a different episode, varied over applications, element
types, appearance presets and resolutions. A row reads as one step:

| member | content |
| --- | --- |
| `instruction.txt` | a natural-language instruction for the click |
| `before.png` | the screen the click was taken on |
| `target.png` | the clicked element, cropped from the before screen and outlined |
| `after.png` | the screen the click produced |
| `action.txt` | the click, its target element and application |
| `transition.json` | instruction variants, action and target geometry, effect, scene |

Screenshots are byte-identical copies of the corpus members. Instructions are
synthesized from each recorded click and its before and after screens; the
preview shows instructions that passed the round-trip check described under
[Instructions](#instructions). It is a sample for browsing: all transitions and
instructions are read through `index/` and the shards.

## Loading

```python
from datasets import load_dataset

# Stream observations from any split.
ds = load_dataset("docling-project/DeskForge-1M", split="test_app", streaming=True)
sample = next(iter(ds))
sample["png"], sample["leaf.json"], sample["screentag.txt"], sample["record.json"]

# Browse recorded clicks: instruction, before, clicked element, after.
preview = load_dataset("docling-project/DeskForge-1M", "transitions_preview", split="val")

# Transitions: before/after keys, the clicked target and what changed.
transitions = load_dataset(
    "parquet",
    data_files="hf://datasets/docling-project/DeskForge-1M/index/transitions/val.parquet",
    split="train",
)

# Instructions: one row per click, joined to transitions by transition_id.
instructions = load_dataset(
    "parquet",
    data_files="hf://datasets/docling-project/DeskForge-1M/index/instructions/val.parquet",
    split="train",
)
```

For full passes, read the shards with [`webdataset`](https://github.com/webdataset/webdataset);
the Parquet indexes select subsets before streaming, and give the byte offset of
every member for random access:

```python
import webdataset as wds
ds = (wds.WebDataset("data/train/part-{00000..00903}.tar", shardshuffle=True)
        .decode("pil")
        .to_tuple("png", "leaf.json", "screentag.txt", "record.json"))

import pyarrow.parquet as pq
obs = pq.read_table("index/observations/test_id.parquet")
heavily_occluded = obs.filter(obs["occluded_ratio"] > 0.5)
```

[`examples/`](examples) contains runnable scripts for streaming states, pairing
transitions and fetching a single observation by key.

**Recommended filters.** `state_train_eligible` selects the still-image view
(excluding near-duplicate scenes and frames where a click changed nothing), and
`transition_train_eligible` selects the action view, which keeps no-op clicks as
supervision.

## Instructions

663,635 recorded clicks come with natural-language instructions,
synthesized with Qwen3.6-27B from the click, its target and the screens before
and after it. An instruction states one coordinate-free goal that can be carried
out from the before screen, in a `standard` and a more `detailed_contextual`
style; `primary_instruction` is the standard one where it exists, and
`referring_expression` describes the target on the before screen. The
551,651 training instructions are the pool for grounding training, and
the 105,703 in the four test splits are the grounding evaluation examples.

Each variant also records `roundtrip`: whether an independent grounding model
(UI-TARS-1.5-7B), given only the unmarked before screen and the instruction,
points inside the target. It is a strict geometric check, reported rather than
applied; `primary_roundtrip_pass` selects the subset whose primary instruction
passed. Counts and source hashes are in `index/instructions/manifest.json`.

## Annotation quality

Annotations are generated automatically from the accessibility tree and
reconciled with the screenshot and the window stack. Captures that fail the
automated audits (element coverage against rendered pixels, blank-widget
suppression, window ownership and fragment containment) are not included. A
human audit finds **99.8%** of sampled element annotations correct and
**97.8%** of sampled instructions sound; instructions are model-generated and
are not verified row by row.

## Limitations

All screenshots come from one Linux backend (Xfce): the Windows- and
macOS-inspired presets reproduce the look of those systems rather than run them.
Actions are clicks from undirected exploration, not goal-directed
demonstrations; instructions are written for single clicks after the fact.
Because annotations transcribe what is on screen, text drawn by applications
(for example session paths or live web content in browser scenes) is part of the
data.

## License

The dataset is released under the [MIT License](LICENSE). Application
interfaces, themes, icons, wallpapers and web content visible in the screenshots
remain the property of their respective owners.

## Citation

```bibtex
@article{gurbuz2026deskforge,
  title   = {DeskForge: Dense Supervision from Desktop Environments
             for Computer-Use Agents},
  author  = {Gurbuz, A. Said and Nassar, Ahmed and Hong, Sunghwan and
             Pollefeys, Marc and Staar, Peter W. J.},
  journal = {arXiv preprint},
  year    = {2026}
}
```
