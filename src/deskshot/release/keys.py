"""Identifiers for the release, and why they are shaped the way they are.

The obvious key for a capture is its stem, `scene-<scene_id>-stepNN`. It is not
unique. Measured over `plan/manifest.jsonl`:

* 152 stems appear in more than one shard, and
* 128 scene ids appear in two different shards,

because a scene that a run failed to finish was recaptured by a later run under
the same seed. `(shard, stem)` is unique across all 1,266,471 rows, so every
identifier here is shard-qualified.

`scene_id` is deliberately **not** qualified: it is the split unit, and the two
copies of a duplicated scene must land in the same split or the split leaks.
An `episode_id` is qualified, because those two copies are two distinct
episodes with their own frames.

Separator is `__`. WebDataset takes the key to be everything before the first
`.` in a member name, so a key must not contain a dot; scene ids are hex and
shard names are `shard-NNNN`, so neither does.
"""

from __future__ import annotations

SEPARATOR = "__"


def observation_key(shard: str, stem: str) -> str:
    return "%s%s%s" % (shard, SEPARATOR, stem)


def episode_id(shard: str, group: str, scene_id: str) -> str:
    return SEPARATOR.join((shard, group, scene_id))


def transition_id(episode: str, action_index: int) -> str:
    return "%s%st%02d" % (episode, SEPARATOR, action_index)


def split_observation_key(key: str) -> tuple:
    shard, _, stem = key.partition(SEPARATOR)
    return shard, stem


#: Suffixes a released observation carries inside a tar shard.
MEMBER_SUFFIXES = {
    "image": ".png",
    "leaf": ".leaf.json",
    "screentag": ".screentag.txt",
    "record": ".record.json",
}

#: Where each member comes from in the corpus.
SOURCE_SUFFIXES = {
    "image": ".png",
    "leaf": ".elements.leaf.json",
    "screentag": ".screentag.txt",
}


def member_name(key: str, kind: str) -> str:
    return key + MEMBER_SUFFIXES[kind]
