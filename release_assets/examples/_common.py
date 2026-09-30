"""Shared bits: iterate a WebDataset tar with or without the library.

The canonical shards are ordinary uncompressed tars, so `webdataset` is a
convenience and not a requirement. These examples use it when it is installed
and fall back to eighty lines of `tarfile` when it is not, which is also the
clearest statement of what the format actually is.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any, Dict, Iterator, List

#: The four members every observation carries, keyed by the part of the member
#: name after the first dot.
EXTENSIONS = ("png", "leaf.json", "screentag.txt", "record.json")


def iter_tar(path) -> Iterator[Dict[str, Any]]:
    """Yield one dict per observation from one shard, in stored order.

    Members of a sample are written consecutively, so grouping is a matter of
    watching the key change - no buffering of the whole shard.
    """
    current_key = None
    sample: Dict[str, Any] = {}
    with tarfile.open(str(path), "r:") as tar:
        for member in tar:
            if not member.isfile():
                continue
            key, _, extension = member.name.partition(".")
            if key != current_key:
                if sample:
                    yield sample
                current_key, sample = key, {"__key__": key}
            handle = tar.extractfile(member)
            if handle is None:
                continue
            sample[extension] = handle.read()
    if sample:
        yield sample


def decode(sample: Dict[str, Any], with_image: bool = True) -> Dict[str, Any]:
    """Bytes to usable objects. The PNG is decoded only if asked for."""
    out: Dict[str, Any] = {"key": sample["__key__"]}
    if "record.json" in sample:
        out["record"] = json.loads(sample["record.json"])
    if "leaf.json" in sample:
        out["elements"] = json.loads(sample["leaf.json"])
    if "screentag.txt" in sample:
        out["screentag"] = sample["screentag.txt"].decode("utf-8")
    if with_image and "png" in sample:
        from PIL import Image
        out["image"] = Image.open(io.BytesIO(sample["png"]))
    return out


def shards_for(root, split: str) -> List[Path]:
    return sorted(Path(root).joinpath("data", split).glob("*.tar"))


def shards_for_rank(root, split: str, rank: int = 0, world_size: int = 1,
                    worker: int = 0, num_workers: int = 1) -> List[Path]:
    """Partition shards so no two readers touch the same file.

    Sharding by file, not by sample, is what keeps a distributed run from
    reading everything `world_size` times. Every rank must get the same number
    of shards or the epoch never ends on the short ranks; the tail is dropped.
    """
    shards = shards_for(root, split)
    per_reader = len(shards) // max(1, world_size * num_workers)
    if per_reader == 0:
        raise ValueError(
            "%d shards cannot feed %d readers; lower world_size or num_workers"
            % (len(shards), world_size * num_workers))
    index = rank * num_workers + worker
    return shards[index * per_reader:(index + 1) * per_reader]
