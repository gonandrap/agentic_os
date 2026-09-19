## Summary

<!-- What changed and why, in a few sentences. If the rationale runs longer than that,
     it is a spec: write it under docs/ and link it here. -->

## Implementation notes

<!-- Bullets. What a reviewer would otherwise have to reverse-engineer from the diff:
     what you chose, what you rejected and why, what is risky, what to look at first. -->

-

## Questions asked to Neo

<!-- One bullet per question, each with its link. Write "None." if you asked none. -->

-

## Alarms raised

<!-- One bullet per cost alarm this work order raised, each with its al- id and link.
     `jarvis alarms --wo $JARVIS_WO_ID`. Write "None." if it raised none. -->

-

## Learnings

<!-- One bullet per knowledge-base entry this work wrote, each with its kn- id.
     Write "None." if you wrote none. -->

-

## Test evidence

<!-- What you RAN and what it REPORTED — the command and its actual output, not the
     claim that it passed. Keep every row: a row that does not apply says so and says
     why, because "no UI test" and "no UI change" are different facts to a reviewer.

     TARGETED TESTS ONLY. Do not run the full suite locally — CI runs it on three
     interpreters, it takes ~21 minutes, and a blocking call that long re-sends your
     whole conversation at the cache-write rate (kn-356c724b). Name the tests you ran
     for what you changed and cite the checks on this PR for the rest. -->

| Kind | Command | Result |
| --- | --- | --- |
| Unit / integration | | |
| UI | | |
| Eval | | |
| A/B | | |

## Screenshots

<!-- One image per thing the change claims to do, for any PR that touches a rendered
     surface: a passing UI test is not evidence of what the page looks like. Link by
     raw URL at the commit SHA — a relative path renders broken and is denied:
     https://raw.githubusercontent.com/<owner>/<repo>/<sha>/docs/screenshots/<name>.png
     Write "None — no rendered surface changed." if none applies. -->

-
