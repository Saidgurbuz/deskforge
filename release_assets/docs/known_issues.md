# Known issues

Read this before using DeskForge-1M for anything load-bearing. Everything here
is measured, not suspected.

## Text visible in the screenshots

The annotation quotes what is on screen, so anything painted into a window title
or a widget label is in `leaf.json`, in the ScreenTag and in the pixels. These
were found by scanning the packed payload and are **not** removed, because
rewriting them would make the annotation disagree with its own image.

| what | how often | what it is |
| --- | --- | --- |
| `/tmp/session-<random>/` | 32.8% of captures | the ephemeral session root of the generating job, in file-chooser paths and window titles |
| synthetic persona homes, e.g. `/Users/anika/`, `/home/kwame/` | 32.5% | **generated** persona names, not real people |
| the generating machine's host name | 9.8% | surfaced as an AT-SPI label in file managers |
| the generating machine's checkout path | 153 captures (0.01%), 71 shards | printed by VS Code itself — a terminal showing its own command line, or a docs entry resolving the `code` binary |
| the dataset author's own account details | **830 captures (0.07%)**, 266 scenes | the account name field of the generating user, captured into a document fixture and therefore painted into those screenshots |

The first three were measured over 600 random captures; the author's account string was counted exactly, across all 1,266,471 source captures. **No host path appears in any of the 1,207,368
constructed `record.json` files** — that is checked for every record at pack
time and refused. **No real home directory and no third party's personal data
appears anywhere.** The checkout path above appears only where an application
printed it on screen and the annotation faithfully transcribed it. The author's own account string
belongs to the dataset's author and is disclosed here rather than removed because it is in the pixels.

Downstream filtering on these strings is straightforward: they are exact and
they are in the text members, not only the images.

## Live web pages

Some scenes drive a real Chromium against a real URL, so a browser window shows
whatever that site served on the capture date. The domain is recorded, the page
content is not curated, and it may include news text, advertising or images
belonging to third parties.

## Annotation

* **Labels are automatic.** They come from the AT-SPI2 accessibility tree and
  automated refinement; a human audit of sampled elements finds 99.8% of them
  correct. An application that reports its own tree badly is annotated badly.
* **Toggle state is invisible on this stack.** AT-SPI 2.40.3 does not expose
  `checkable`/`checked` for menu items here, so a checked menu item is not
  distinguishable from an unchecked one in the annotation.
* **Window controls** (close/minimise/maximise) are synthesised from the title
  bar rather than read from the tree, and are the least reliable class.
* **Window ownership** of popups and menus is resolved through the window stack.
  2.64% of leaf elements legitimately fall outside their owning window's
  rectangle; a containment rule that "fixed" this would delete real menus.
* **59,103 observations failed the automated audits** and are not published.
  The reasons, in order of frequency: leaf/window ownership, visible-fragment
  containment, missing fragments, too few elements, too little type diversity,
  shallow trees, missing elements, low coverage.

## Episodes

* **994 episodes have no `episode.json`** and 6 have a truncated one, spread
  over 183 shards, because the generating job died before writing it. Their
  screenshots are fine and are published; they contribute **no transitions**.
  They carry `episode_status` of `no_episode_json` or `episode_json_truncated`
  and `n_transitions = 0`.
* Actions are **click only**, and episodes are undirected random exploration
  with no goal, instruction or reward.
* 14.3% of eligible transitions changed nothing. That is intentional and they
  are kept; see `transition_train_eligible` against `state_train_eligible`.

## Duplication

* 22,082 observations belong to **near-duplicate scenes** and 124,261 are no-op
  episode frames. Both are published and both are excluded from
  `state_train_eligible`.
* 128 scene ids appear in two shards, because a scene a run failed to finish was
  recaptured by a later run under the same seed. Both copies are published and
  **both are in the same split**; keys and episode ids are shard-qualified so
  they never collide.

## Coverage

One desktop environment (Xfce on Xvfb). The Windows-like and macOS-like themes
are GTK themes imitating those systems, not those systems. 19 applications, all
GTK/Linux desktop software. Nothing here is a mobile or web-only interface.
