"""What must never reach a published capture, and the identity used instead.

A screenshot cannot be redacted after the fact. If a title bar draws
`/proj/<project>/users/<account>/...`, those pixels are the sample - cropping or
string-replacing the annotation would leave the annotation disagreeing with the
image, which is worse than the leak. So every identifying string has to be kept
off the screen at capture time, and this module is the single place that knows
which strings those are.

## What was actually leaking

Measured over 95 captures in `v235`-`v239`: **83 of them** carried at least one
identifier.

- `<checkout>/assets/audit/...` - the documents
  every editor opens are referenced by absolute path in the app manifests, so
  bluefish, mousepad, pluma and thunar draw the full path in their title bars.
- the GPFS device backing the cluster, drawn as a desktop volume
  icon and again in every GTK places sidebar (thunar, nautilus, homebank,
  xarchiver).
- `<account>'s Home` - Caja's home icon is labelled from the real account.
- `sys`, `proc`, `shm`, `pts` - the host's own mount table, on the desktop.

The mount table is the part that would have been hardest to spot and worst to
publish: `/proc/mounts` on this host also lists `/u/<name>` for every other
person with an account, so anything that enumerates mounts is one settings
change away from putting colleagues' usernames in the corpus.

## The two halves

`session_persona()` supplies the identity a capture is allowed to show. It is
deterministic in the session seed, so a scene re-run from the same seed gets the
same name and the same paths.

`scan_text()` is the negative half: the patterns that must not appear. It is
deliberately built from the *live* environment (this account, this hostname,
this mount table) rather than a hard-coded list, because the whole failure mode
is an identifier nobody remembered to write down.
"""

from __future__ import annotations

import getpass
import os
import pwd
import random
import re
import socket
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Personas a capture may present as. First names only, no surnames, drawn from
#: a spread of scripts so the corpus is not uniformly anglophone. They are
#: fictional and shared by nobody: the point is that the same string appears in
#: `~`, in the Caja home icon and in any file listing, so all three agree.
PERSONAS: Tuple[Tuple[str, str], ...] = (
    ("mira", "Mira Alden"),
    ("tobias", "Tobias Renner"),
    ("yuki", "Yuki Harada"),
    ("noor", "Noor Haddad"),
    ("elena", "Elena Vasquez"),
    ("kwame", "Kwame Boateng"),
    ("iris", "Iris Lindqvist"),
    ("dmitri", "Dmitri Sokolov"),
    ("anika", "Anika Rao"),
    ("felix", "Felix Moreau"),
    ("saoirse", "Saoirse Byrne"),
    ("hana", "Hana Novak"),
)


@dataclass(frozen=True)
class Persona:
    """The identity one session is allowed to show on screen."""

    username: str
    full_name: str

    @property
    def home_icon_label(self) -> str:
        """What Caja should call the home icon, in its own possessive style."""
        return f"{self.full_name.split()[0]}'s Home"


#: Where a home directory sits, by desktop style. The session home is a real
#: directory in a temp tree, but the *shape* of its path is drawn in title bars,
#: so it may as well be the shape that platform actually uses - and varying it
#: is free diversity rather than one path string repeated across the corpus.
HOME_CONTAINERS: Dict[str, Tuple[str, ...]] = {
    "macos": ("Users",),
    "windows": ("Users",),
    "ubuntu": ("home",),
    "linux": ("home", "home", "export/home"),
}


def home_container_for(style: str, seed: int) -> str:
    """The directory a persona's home lives in, for one desktop style."""
    options = HOME_CONTAINERS.get((style or "linux").strip().lower(), HOME_CONTAINERS["linux"])
    return options[random.Random(int(seed) ^ 0x51ED270B).randrange(len(options))]


def session_persona(seed: int) -> Persona:
    """Pick one persona deterministically from a session seed."""
    rng = random.Random(int(seed) ^ 0x9E3779B9)
    username, full_name = PERSONAS[rng.randrange(len(PERSONAS))]
    return Persona(username=username, full_name=full_name)


# ---------------------------------------------------------------------------
# The negative half: what must not appear
# ---------------------------------------------------------------------------

#: Directories whose names are the project's own rather than a person's or an
#: employer's. They are reported separately because they are not confidential -
#: the corpus is published under this name - but they still say "generated", so
#: a run can choose to treat them as failures.
BRAND_TOKENS: Tuple[str, ...] = ("deskshot", "DeskShot")

#: Filesystem types that are the machine's own plumbing. A mount of one of these
#: is never interesting to show and its device name is often the host's.
_SYSTEM_FS = frozenset({
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "securityfs", "pstore", "bpf", "configfs", "debugfs", "tracefs",
    "hugetlbfs", "mqueue", "fusectl", "binfmt_misc", "autofs", "rpc_pipefs",
    "selinuxfs", "efivarfs", "overlay", "squashfs",
})

#: Top-level directories every Linux has. A mount under one of these says
#: nothing about *this* host, and watching them would flag ordinary UI text.
_GENERIC_TOP_LEVEL = frozenset({
    "/run", "/var", "/tmp", "/dev", "/sys", "/proc", "/etc", "/usr", "/opt",
    "/boot", "/media", "/mnt", "/srv", "/lib", "/lib64", "/bin", "/sbin",
    "/home", "/root",
})


def _mount_identifiers() -> List[str]:
    """Device and mount names from this host's mount table.

    Every one of these is a candidate leak: the device name lands on desktop
    volume icons and in GTK places sidebars, and on a shared cluster the mount
    paths are other people's home directories.
    """
    found: List[str] = []
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount_path, fstype = parts[0], parts[1], parts[2]
        if fstype in _SYSTEM_FS:
            continue
        if device.startswith("/dev/") or device in {"none", "rootfs"}:
            continue
        if "/" not in device and len(device) >= 4:
            found.append(device)
        top = "/" + mount_path.strip("/").split("/")[0]
        if len(top) >= 3 and top not in _GENERIC_TOP_LEVEL:
            found.append(top)
    return found


@lru_cache(maxsize=1)
def _environment_identifiers() -> Tuple[Tuple[str, str], ...]:
    """(label, literal) pairs taken from the live environment.

    Built at runtime rather than written down, because the identifiers that
    matter are exactly the ones nobody thought to list.
    """
    out: List[Tuple[str, str]] = []

    try:
        account = getpass.getuser()
    except Exception:
        account = os.environ.get("USER", "")
    if account and len(account) >= 3:
        out.append(("account", account))

    # The GECOS field, which glib serves as `g_get_real_name()`. On managed hosts
    # it is often not a name but `<work email>;<employee serial>;<full name>` - a
    # work email address and an employee serial number. Any app that greets the user
    # by name draws it; MATE's panel does, in "Log Out <name>...". Overriding
    # `$USER` and `$LOGNAME` does not help, because glibc reads passwd directly,
    # so the only defence is to know the strings and refuse to publish them.
    try:
        gecos = pwd.getpwuid(os.getuid()).pw_gecos or ""
    except (KeyError, OSError):
        gecos = ""
    for field in gecos.replace(",", ";").split(";"):
        field = field.strip()
        if len(field) >= 4:
            out.append(("real_name", field))
            for word in field.replace("@", " ").replace(".", " ").split():
                if len(word) >= 4:
                    out.append(("real_name", word))

    host = socket.gethostname() or ""
    for piece in {host, host.split(".")[0]}:
        if piece and len(piece) >= 4:
            out.append(("hostname", piece))

    # The project's own location. Anything under it is a path only this
    # checkout has, so it should never be drawn on a screen.
    project_root = Path(__file__).resolve().parents[2]
    out.append(("project_root", str(project_root)))
    for ancestor in list(project_root.parents)[:-1]:
        text = str(ancestor)
        if text.count("/") >= 1 and text != "/":
            out.append(("project_ancestor", text))

    for device in _mount_identifiers():
        out.append(("mount", device))

    # Deduplicate while keeping the longest match first, so a report attributes
    # a hit to the most specific identifier rather than to `/proj`.
    seen: Dict[str, str] = {}
    for label, literal in out:
        if literal not in seen:
            seen[literal] = label
    ordered = sorted(seen.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    return tuple((label, literal) for literal, label in ordered)


@dataclass(frozen=True)
class Leak:
    """One identifying string found in text that would have been published."""

    label: str
    literal: str
    context: str

    @property
    def is_brand(self) -> bool:
        return self.label == "brand"


#: An account name short enough to collide with ordinary words needs context.
#: `said` is this account *and* an extremely common English verb: a news article
#: reading "Iran said on Friday" was flagged as a leak, which is not merely
#: noise - the gate demands zero affected captures, so a false positive of that
#: frequency makes it unusable. Below this length, the name only counts when it
#: appears somewhere an identity appears.
_SHORT_NAME_MAX = 6

#: Contexts in which a short account name is really an identity: a path
#: component, a possessive (Caja's home icon draws `<account>'s Home`), an email
#: local part or host part, or the whole string.
_IDENTITY_CONTEXT = (
    r"(?:(?<=/)NAME(?![\w-])|(?<![\w-])NAME(?=/)|(?<![\w-])NAME(?='s)"
    r"|(?<![\w-])NAME(?=@)|(?<=@)NAME(?![\w-])|\ANAME\Z)"
)


def _compiled() -> List[Tuple[str, str, "re.Pattern[str]"]]:
    rules: List[Tuple[str, str, "re.Pattern[str]"]] = []
    for label, literal in _environment_identifiers():
        if literal.startswith("/"):
            pattern = re.compile(re.escape(literal) + r"(?![\w-])")
        elif label in {"account", "real_name"} and len(literal) <= _SHORT_NAME_MAX:
            pattern = re.compile(_IDENTITY_CONTEXT.replace("NAME", re.escape(literal)))
        else:
            pattern = re.compile(r"(?<![\w-])" + re.escape(literal) + r"(?![\w-])")
        rules.append((label, literal, pattern))
    for token in BRAND_TOKENS:
        rules.append(("brand", token, re.compile(re.escape(token), re.IGNORECASE)))
    return rules


@lru_cache(maxsize=1)
def _rules() -> Tuple[Tuple[str, str, "re.Pattern[str]"], ...]:
    return tuple(_compiled())


def scan_text(text: str, *, include_brand: bool = True) -> List[Leak]:
    """Every identifier this text would publish, most specific first."""
    if not text:
        return []
    leaks: List[Leak] = []
    claimed: List[Tuple[int, int]] = []
    for label, literal, pattern in _rules():
        if label == "brand" and not include_brand:
            continue
        for match in pattern.finditer(text):
            span = match.span()
            # A longer identifier already covering this span wins: a hit inside
            # `/proj/<project>/users/<account>` is that path, not `<account>`.
            if any(start <= span[0] and span[1] <= end for start, end in claimed):
                continue
            claimed.append(span)
            lo = max(0, span[0] - 24)
            hi = min(len(text), span[1] + 24)
            leaks.append(Leak(label=label, literal=literal, context=text[lo:hi]))
    return leaks


def is_clean(text: str, *, include_brand: bool = True) -> bool:
    """True when nothing identifying appears in the text."""
    return not scan_text(text, include_brand=include_brand)


def describe_watchlist() -> List[Dict[str, str]]:
    """The identifiers currently being watched, for a run's provenance record."""
    return [
        {"label": label, "literal": literal}
        for label, literal in _environment_identifiers()
    ] + [{"label": "brand", "literal": token} for token in BRAND_TOKENS]


def identifying_path_prefixes() -> List[str]:
    """Directories whose mere presence in a path names this account.

    Used to scrub `PATH` and friends before an app inherits them: an app that
    lists its search path publishes whatever is on it, and the launching
    account's home is on it.
    """
    prefixes: List[str] = []
    home = os.environ.get("DESKSHOT_REAL_HOME") or str(Path("~").expanduser())
    if home and home not in ("/", ""):
        prefixes.append(home)
    try:
        account = getpass.getuser()
    except Exception:
        account = os.environ.get("USER", "")
    for label, literal in _environment_identifiers():
        # Mount roots only matter joined to the account name: `/u` is a cluster
        # convention, `/u/<account>` is a person.
        if label == "mount" and literal.startswith("/") and account:
            prefixes.append(f"{literal.rstrip('/')}/{account}")
    if account:
        prefixes.append(f"/u/{account}")
        prefixes.append(f"/home/{account}")

    # The project checkout is deliberately **not** scrubbed. Its path contains
    # the account name, but it is also where every extracted binary lives, so
    # removing it from PATH stops the apps launching at all - which trades a
    # string an app might print for a corpus that does not exist. The staged
    # documents and the persona home already keep that path off the screen in
    # the places apps actually draw.
    # Longest first, so a nested match is attributed to the deepest prefix.
    return sorted({p for p in prefixes if p}, key=len, reverse=True)

