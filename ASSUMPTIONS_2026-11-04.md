# Assumptions — wo-20d859bf

1. **Origin is read with `git ls-remote --tags origin`, not a fetch.** Neo ruled this in Q46:
   it is the only option that states what origin actually has, and it stays clear of
   wo-d4ce8488 (the `/release` fetch) and wo-245af6ec (the shared refresh layer), both still
   running. When ls-remote fails the page says so and labels the local newest tag `(local)`.

2. **The already-released refusal in `ops.release_cut` runs only for the OS project.**
   `release.tags_containing` is hardcoded to `RELEASE_TAG_GLOB` (`jarvis-*`), so on another
   project's repo it would fetch over the network and always find nothing. Rejected:
   calling it unconditionally, and generalising `tags_containing` to take a glob
   (re-implementing the function this order said to reuse).

3. **The refusal checks the OLDEST commit the cut would ship**, not the base. If a release
   tag already carries that commit, the cut re-ships released work — which is the symptom
   the improvement order recorded (28 of 38 pending commits were already in jarvis-0.14.11).

4. **`a jarvis-* tag already contains it` was removed from `RELEASE_CUT_BRIEF`**, per the
   finding's "move the check out of prose and into `ops.release_cut`". The CI-red clause
   stays. `PROJECT_RELEASE_CUT_BRIEF` keeps its prose, since the code guard is OS-only.

5. **The production version is shown only on the OS project's page.** Another project's
   releases are not what the OS production checkout runs.
