# Schema

## Payload — one observation, four tar members

The tars are **uncompressed**, members of one observation are **consecutive**,
and the key is everything before the first `.` in a member name. Keys contain no
dots.

```
<key>.png             screenshot, PNG, never re-encoded
<key>.leaf.json       list of visible elements
<key>.screentag.txt   serialized markup
<key>.record.json     sanitized metadata
```

`<key>` is `<shard>__<stem>`, e.g. `shard-0106__scene-002ad3e5b23cbfd9-step00`.
It is shard-qualified because a few scenes were captured in two shards, so a
stem alone is not unique; `(shard, stem)` is.

## `leaf.json` — an element

```jsonc
{
  "type": "Button",                 // release class, 55 in the schema
  "role": "push button",            // AT-SPI role it came from
  "name": "Save",
  "visible_text": "Save",           // text a reader can actually see
  "visible_text_status": "visible",
  "rect":  {"x": 312, "y": 88, "w": 64, "h": 28},   // amodal: the whole element
  "visible_fragments": [                             // modal: pixels it occupies
    {"x": 312, "y": 88, "w": 40, "h": 28}
  ],
  "is_occluded": true,
  "occlusion_state": "partially_occluded",
  "app_name": "homebank",
  "reading_order_index": 41,
  "_window_stack_index": 2,
  "uid": "80b06ebf5db974e2"         // stable within an episode; action targets use it
}
```

**`rect` is amodal and `visible_fragments` is modal.** A box drawn from `rect`
alone claims pixels another window is covering. Train grounding on the
fragments; use `rect` when the question is where the whole element *is*.

## `screentag.txt`

One flat string per screenshot: `<Tag><loc_N>×4 [state] [text]</Tag>`, nested
to mirror the element tree. Coordinates are on a **0–500** grid, normalized to
the viewport (`record["screentag_grid"]`).

## `record.json`

Scene metadata for the observation. Carries `observation_key`, `scene_id`, `episode_id`, `step_index`,
`split`, `group`, `width`, `height`, `apps`, `theme`, `scene`, `n_elements`,
`n_windows`, `window_stack`, `occlusion`, `flags`, `provenance`, and — on any
observation that an action led to — `action_into_this_state` and
`effect_of_that_action`.

`group` is `ep` for a frame of a click episode and `st` for a standalone scene.

## Indexes

| file | one row per | notable columns |
| --- | --- | --- |
| `index/scenes.parquet` | scene | `split`, `split_source`, `apps`, `theme`, `resolution` |
| `index/observations/<split>.parquet` | observation | `tar_path`, `*_member`, `occluded_ratio`, `n_elements`, `n_windows`, the flags |
| `index/transitions/<split>.parquet` | transition | `before_key`, `after_key`, action fields, `effect`, `exclusion_reasons` |
| `index/instructions/<split>.parquet` | transition with an instruction | `primary_instruction`, `instruction_variants`, `referring_expression` |
| `index/episodes/<split>.parquet` | episode | `observation_keys`, `transition_ids`, `episode_status` |
| `index/shard_members.parquet` | tar member | `byte_offset`, `byte_size`, `sha256` |
| `checksums/shard_stats.parquet` | tar | `bytes`, `sha256`, `observations` |

`split_source` is `extended` for the 12,515 scenes that are published but not
state-train-eligible, and `v3` for all others.

## Transitions

An action is stored on the step it **produced**:

```
observation[k-1]  --  steps[k].action  -->  observation[k]
```

Step 0 has no action. Reading it the other way pairs every action with the
screen it was not taken on, and nothing downstream would notice.

Coordinates: `action_point_px` is authoritative. `action_point_norm_1000` and
`action_point_screentag_500` are derived from it and agree to within one grid cell;
`action_target_bbox_px` is `[x0, y0, x1, y1]`.

`effect` counts what changed between the endpoints — `changed`, `magnitude`,
`appeared`, `disappeared`, `moved`, `text_changed`, `state_changed`,
`newly_occluded`, `revealed`, `semantic_changes`, `persisted`.

`exclusion_reasons` is empty when `transition_train_eligible` is true and
otherwise names every rule the transition failed.

## Instructions

`index/instructions/<split>.parquet` has one row per transition with an
instruction, joined to the corpus by `transition_id`, `before_key` and
`after_key`:

| column | content |
| --- | --- |
| `transition_id`, `split`, `before_key`, `after_key`, `target_uid` | the transition and its target element |
| `action_type`, `action_point_px`, `action_target_bbox_px` | the recorded click, as in `index/transitions` |
| `primary_style`, `primary_instruction` | the main instruction: `standard` where it exists, else `detailed_contextual` |
| `instruction_variants` | every phrasing, as `style` and `text` |
| `referring_expression` | how the target is identified on the before screen |

Instructions are coordinate-free: they name a goal, never a position.

## `demo/preview.parquet`

A stratified sample of observations, one row per observation with the image
inline, for browsing without the shards. It is stratified over split, theme,
resolution, element density, occlusion band, static against episode, changed
against no-op, and common against rare application.

**Its images are downscaled to 1024 px on the long edge**; `data/` is
canonical. ScreenTag is on a 0–500 grid normalized to the viewport, so it still
describes the resized image, and `width`/`height` remain the original pixel
dimensions.

## `demo/transitions/<split>.tar` — the `transitions_preview` subset

A browsing sample of recorded clicks for the Dataset Viewer, 100 per split, one
per episode, varied over target application, element role, appearance preset
and resolution. The key is the `transition_id`; each sample has six members:

```
<transition_id>.instruction.txt   primary instruction for the click
<transition_id>.before.png        the before observation's PNG, byte-identical
<transition_id>.target.png        crop of the before screen around the target, outlined
<transition_id>.after.png         the after observation's PNG, byte-identical
<transition_id>.action.txt        e.g. click (1521, 688) on table column header "Compressed" in xarchiver
<transition_id>.transition.json   ids, instruction variants, action, target, effect, scene
```

`before.png` and `after.png` hash to the `sha256` of their members in
`index/shard_members.parquet`. `target.png` is the only derived image: a crop of
the before screen around the target, with the target box and click point drawn
on it.
