"""What the LOCAL checkout can prove about a pull request's branch. `git` and nothing else.

docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §3.1 put this in
a module of its own, and the boundary is the reason: `ci.py` is AST-tested against a `gh`
allowlist and is the OS's one GitHub WRITE surface, `github.py` is AST-tested read-only,
and a local `git` subprocess belongs in neither. `tests/test_base_heal.py` holds this file
to the other half of the same bargain — every argument list here starts with `git`, and it
imports no module that can reach GitHub.

**THE QUESTION THIS ANSWERS IS "DID THE BRANCH'S OWN CONTRIBUTION CHANGE", and no API can
answer it.** GitHub reports an evil merge — one whose conflict resolution edited the pull
request's own files — with exactly the parentage of a clean one, so the verdict carry needs
a content proof beside the parentage proof (§3.3). A WHITESPACE-EXACT hash of the diff over
`merge-base(base, sha)..sha` is that proof: it is what the branch ADDS on top of its base,
and a resolution that rewrote any of it moves the id.

Every function FAILS SOFT — None, False — and logs, the shape of `landing._git` /
`landing._fetch_ref`. A proof that cannot be computed must refuse the carry rather than
raise into the daemon's tick: the pull request then falls through to the round machine and
behaves exactly as it did before this module existed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("jarvis.branchproof")

#: Same ceiling as `ci.CI_TIMEOUT`, and named apart for its reason: these run on the
#: daemon's tick and one hung `git` must not hold the poll for every other project.
GIT_TIMEOUT = 30

#: A ref BECOMES AN ARGUMENT, and one of the two callers passes `baseRefName` as GitHub
#: answered it about a pull request whose URL a worker wrote. Anchored, no leading `-`, so
#: nothing here can be read by `git` as a flag (`ci.base_runs`' rule).
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: A commit, at any length `git` accepts.
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _env() -> dict[str, str]:
    """`git` with nothing of the user's configuration and NOTHING INTERACTIVE.

    The fetch below talks to a remote, so a checkout whose credentials have lapsed would
    otherwise block the daemon's tick on a username prompt until the timeout.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
            "GIT_CONFIG_NOSYSTEM": "1", "GCM_INTERACTIVE": "never"}


def _git(repo: Path, *args: str, stdin: str | None = None) -> str | None:
    """`git -C repo args`, or None on any failure. Never raises."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, errors="replace", timeout=GIT_TIMEOUT,
                              check=False, env=_env(), input=stdin)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git %s in %s could not run: %s", args[0], repo, exc)
        return None
    if proc.returncode != 0:
        log.debug("git %s in %s exited %d: %s", " ".join(args), repo, proc.returncode,
                  proc.stderr.strip()[:200])
        return None
    return proc.stdout


def _git_bytes(repo: Path, *args: str) -> bytes | None:
    """`_git`'s contract — None on any failure, never raises — with the OUTPUT AS BYTES.

    A diff is not text: a repository's files are whatever bytes they hold, and `git diff`
    copies them through. `errors="replace"` maps every undecodable byte to the same U+FFFD,
    so a fingerprint taken after that decode cannot tell b"caf\\xe9" from b"caf\\xe8" and a
    resolution that swapped them carried the verdict (review round 5). Only `stderr`, which
    is LOGGED and never hashed, is decoded with `errors="replace"` here.
    """
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=GIT_TIMEOUT, check=False, env=_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git %s in %s could not run: %s", args[0], repo, exc)
        return None
    if proc.returncode != 0:
        log.debug("git %s in %s exited %d: %s", " ".join(args), repo, proc.returncode,
                  proc.stderr.decode("utf-8", "replace").strip()[:200])
        return None
    return proc.stdout


def fetch(repo: Path, *refs: str) -> bool:
    """Bring `refs` down from `origin`, once per carry attempt. False when it failed.

    **LOGGED WITH THE REFS IT ASKED FOR**, because of the open question in §8: an `origin`
    that refuses `refs/pull/N/head` makes the whole carry silently inert in that project,
    and "silently" is the part that has to be untrue. A False here is a refusal named
    `fetch` on the record (`ops.CARRY_REFUSED_EVENT`), never a carry.
    """
    wanted = [r.split("/", 1)[1] if r.startswith("origin/") else r for r in refs]
    if not wanted or not all(REF_RE.match(r or "") for r in wanted):
        log.warning("refusing to fetch %r into %s — not a ref this may ask for",
                    list(refs), repo)
        return False
    if _git(repo, "fetch", "--quiet", "origin", *wanted) is None:
        log.warning("git fetch origin %s in %s failed — no local proof can be computed "
                    "about this branch", " ".join(wanted), repo)
        return False
    return True


#: The two pieces of a diff that a BASE MERGE legitimately moves without the branch's
#: contribution changing, and the only two dropped from a TEXT diff (§3.3, review round 1):
#: the hunk header's line ranges — `main` adding lines above the branch's own hunk shifts
#: them and nothing else — and the `index` line's blob ids, which name whole-file contents
#: of the merge base and of the merged head, both of which move for the same reason. The
#: `@@`'s trailing section heading and the mode on the `index` line stay in.
#: BYTES patterns, because the diff is hashed as bytes (review round 5).
_HUNK_RANGE_RE = re.compile(rb"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.M)
_INDEX_IDS_RE = re.compile(rb"^index [0-9a-f]+\.\.[0-9a-f]+", re.M)

#: A BINARY file's diff is exempt from the `index` rule, and has to be (§3.3, review round
#: 3): `git diff` prints `Binary files … differ` and no content, so the new-side blob id is
#: the ONLY fingerprint of what the branch put there. The OLD side is still dropped — it
#: names the merge base's version and moves when the base does.
_INDEX_NEW_ID_RE = re.compile(rb"^index [0-9a-f]+\.\.([0-9a-f]+)", re.M)
_BINARY_RE = re.compile(rb"^(?:Binary files .* differ|GIT binary patch)$", re.M)
_PER_FILE_RE = re.compile(rb"^(?=diff --git )", re.M)


def _normalise(diff: bytes) -> bytes:
    """Drop what a base merge moves, per file, keeping binary content in the hash."""
    out = []
    for section in _PER_FILE_RE.split(diff):
        if _BINARY_RE.search(section):
            out.append(_INDEX_NEW_ID_RE.sub(rb"index \1", section))
        else:
            out.append(_INDEX_IDS_RE.sub(b"index", _HUNK_RANGE_RE.sub(b"@@ @@", section)))
    return b"".join(out)


def diff_fingerprint(repo: Path, base_ref: str, sha: str) -> str | None:
    """What `sha`'s branch ADDS on top of its merge base with `origin/<base_ref>`, as an id.

    PROOF (b) of the carry, §3.3: computed for the judged commit and for the live head, and
    the carry needs the two to be identical.

    **WHITESPACE IS IN THE HASH, and that is why this is not `git patch-id`** (review round
    1 of wo-659be188). `patch-id` strips all whitespace from every line before hashing, so
    it cannot see a conflict resolution that only re-indented Python — dedenting a `return`
    out of its `if` is a different program on the branch's own line, and `--stable` gave it
    the id of the judged commit. The verdict was then carried onto code no seat read, with
    the record saying the diff was byte-identical. So the diff is hashed HERE, verbatim:
    every space, every tab, the `---`/`+++` paths, the mode lines and the `@@`'s section
    heading are all part of the id. `--full-index`, so nothing depends on how short git
    chose to abbreviate; `--no-ext-diff --no-textconv`, so no repository's configuration can
    choose what this hashes.

    **THE HASH IS OVER THE DIFF'S BYTES, never over decoded text** (review round 5). A
    repository holds whatever bytes it holds, and a text file's content is inside its diff,
    so decoding with `errors="replace"` before hashing mapped every undecodable byte to one
    U+FFFD: b"caf\\xe9\\n" and b"caf\\xe8\\n" produced the same id, and a resolution that
    rewrote such a file kept the judged commit's fingerprint. `_git_bytes` captures the diff
    raw and `_normalise` works in bytes; `errors="replace"` survives only where output is
    LOGGED.

    **A BINARY file keeps its new-side blob id** (review round 3). `git diff` prints
    `Binary files … differ` and no content for any path a `-diff`/`binary` attribute marks
    or that holds a NUL byte, so for those the new-side id is the only fingerprint of the
    bytes; dropping it let a resolution swap them and keep the judged commit's id. The old
    side is still dropped, and text hunks are unchanged — see below for why they must be.

    **The `@@` line ranges and the `index` blob ids are the ONLY things dropped** from a
    text diff, because
    they are bookkeeping about the base rather than about the branch's contribution: `main`
    adding lines above the branch's own hunk shifts the ranges and changes both blob ids
    while the branch adds exactly what it added before. Keeping them would refuse the
    commonest catch-up on the fleet, which is the case this whole feature exists for.

    **CONSERVATIVE, and the inverse error is accepted.** A perfectly clean merge can still
    move the id when the base touched lines next to the branch's own, because the merge
    base moved and the diff's CONTEXT lines moved with it. That is a false refusal and it
    costs a round — today's behaviour. The other direction would merge code no seat read.

    None when git could not answer, which the caller reads as "no proof", never as "no
    change". An empty diff is None too: the carry's whole licence is "this differs from
    what was judged by a base merge", and a branch that contributes nothing to compare is
    not a case this may reason about.
    """
    branch = base_ref.split("/", 1)[1] if base_ref.startswith("origin/") else base_ref
    if not REF_RE.match(branch or "") or not SHA_RE.match(sha or ""):
        log.warning("refusing to diff %r against %r in %s", sha, base_ref, repo)
        return None
    merge_base = _git(repo, "merge-base", f"origin/{branch}", sha)
    if not merge_base or not merge_base.strip():
        return None
    diff = _git_bytes(repo, "diff", "--full-index", "--no-ext-diff", "--no-textconv",
                      f"{merge_base.strip()}..{sha}")
    if not diff:
        return None
    return hashlib.sha256(_normalise(diff)).hexdigest()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Is `ancestor` reachable from `descendant`? False when git cannot say.

    §3.2 item 6 asks this of every commit the chain merged in, against `origin/<base>` —
    which is what makes a two-parent merge a BASE merge rather than someone else's branch
    arriving in the same shape. Asked here rather than with a `compare` call per commit:
    the fetch is already paid for by proof (b), and the API cost of the walk would
    otherwise double.
    """
    if not SHA_RE.match(ancestor or "") or not REF_RE.match(descendant or ""):
        log.debug("not asking whether %r is an ancestor of %r", ancestor, descendant)
        return False
    return _git(repo, "merge-base", "--is-ancestor", ancestor, descendant) is not None


def tip(repo: Path, ref: str) -> str:
    """What commit is `ref` at, locally, right now? `""` when git cannot say.

    Spec docs/superpowers/specs/2026-09-28-a-merge-checks-the-base-it-lands-on.md §3.1.
    THE BASE TIP IS THE FACT NOTHING IN THE OS READ FRESH: `pr.base_oid` is GitHub's own
    cached `baseRefOid`, which lagged the real tip of `main` by three commits and 5.3
    hours on the measured incident (issue #837, gate 308). A reading of the ref the caller
    has just fetched cannot lag anything.

    Needed as a function of its own because `is_ancestor` refuses a ref name for its
    `ancestor` argument — `SHA_RE` — so "is the base's tip in this head" has nothing to
    ask with until the ref is resolved to a commit.
    """
    if not REF_RE.match(ref or ""):
        log.debug("not resolving %r in %s", ref, repo)
        return ""
    out = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return (out or "").strip()
