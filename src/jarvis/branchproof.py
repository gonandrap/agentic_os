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
a content proof beside the parentage proof (§3.3). `git patch-id --stable` over
`merge-base(base, sha)..sha` is that proof: it is what the branch ADDS on top of its base,
and a resolution that rewrote any of it moves the id.

Every function FAILS SOFT — None, False — and logs, the shape of `landing._git` /
`landing._fetch_ref`. A proof that cannot be computed must refuse the carry rather than
raise into the daemon's tick: the pull request then falls through to the round machine and
behaves exactly as it did before this module existed.
"""

from __future__ import annotations

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


def patch_id(repo: Path, base_ref: str, sha: str) -> str | None:
    """What `sha`'s branch ADDS on top of its merge base with `origin/<base_ref>`, as an id.

    PROOF (b) of the carry, §3.3: computed for the judged commit and for the live head, and
    the carry needs the two to be identical. `--stable`, never `--unstable`, so the id
    cannot depend on hunk ordering.

    **CONSERVATIVE, and the inverse error is accepted.** A perfectly clean merge can still
    move the id when the base touched lines next to the branch's own, because the merge
    base moved and the diff's context moved with it. That is a false refusal and it costs
    a round — today's behaviour. The other direction would merge code no seat read.

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
    diff = _git(repo, "diff", f"{merge_base.strip()}..{sha}")
    if not diff:
        return None
    out = _git(repo, "patch-id", "--stable", stdin=diff)
    if not out or not out.split():
        return None
    return out.split()[0]


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
