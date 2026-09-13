import subprocess

SUMMARY = """Round 2 addressed. The reviewer was right and I had missed it: the commit carrying the spec that said "the working probe strings are deliberately not reproduced here" ALSO committed, to a public repo, all five live bypass regexes and the exact command each one cleared. The claim was false in the same commit that made it, and it widened the exposure it said it had contained.

WHAT I DID. tests/test_gate_rules.py no longer contains a single real rule or a single real dismissed command. Five shape-for-shape stand-ins take their place (DEFECT_SHAPES): the same two defect shapes - a negated class that forgets the newline, and a pattern with no end anchor - written as fresh patterns over commands nobody ever dismissed. The sweep reader list, which still named three real dismissed commands, is generic now too. Nothing is weakened, and I measured that rather than asserting it: reverting Rule.clears to its pre-fix body turns all five stand-ins red, plus three more tests. Retracted rule IDS stay in the repo, deliberately - an id is an opaque hash of a private row, carrying no pattern and no command, and it is the only handle by which someone with access finds the real record. Spec section 1 now says exactly that, so the claim is true.

DO NOT MERGE, and the PR now says so at the top. PR 215 is a DRAFT with a DO NOT MERGE block as its first paragraph, naming the two commits (1cf5aec, 5c874cf) that still carry the corpus on the public remote, and saying plainly that merging copies it onto main permanently and destroys the force-push and branch-deletion remedies I told the user were still open. Something un-drafted it between two edits; I re-drafted it and verified draft=true. The title now leads with DO NOT MERGE as well, so it reads that way in any listing.

THE USER HAS A CORRECTED NOTICE. My first notification understated the exposure - it described the spec and PR body only. The second says so explicitly, lists the four options in cost order (force-push and rewrite, delete and re-open clean, make the repo private, or accept and merge), recommends the first two, and asks which. I have not acted on any of them: three are gated or destructive and all four are the user decision this work order reserved to them.

ONE THING THAT GOT BETTER WHILE I WAS HERE. Checking whether the section 7 heredoc holes were ever exploitable in production: every signature exemption in the live base is at QUOTED position, none at heredoc, so both were holes in the floor rather than open gates. That is now in the spec, and it is why section 7 is safe to describe in public at all.

ALSO, NOT MY CHANGE. Another session merged main into this branch while I worked (PR 211, plus the gate-contest feature). My push was rejected non-fast-forward. I merged rather than rebased - rebasing would rewrite commits already on the public remote, which is precisely what the user has not ruled on - and re-ran the whole suite on the merged tree."""

EVIDENCE = """FULL SUITE on the merged tree, which is what the PR now contains: "JARVIS_ENV= uv run pytest tests/ evals/ -q -p no:randomly" exit 0, 2948 outcomes, 0 F/E, eval scorecard TOTAL 45/45. The count rose from 2853 because main brought in the gate-contest work; my own changes are unaffected by it and gate_rules.py merged without conflict.

GATE TESTS: "uv run pytest tests/test_gate_rules.py -q" 58 passed - 36 before this work order, 50 at round 1, 58 at round 2, unchanged by the corpus substitution. That is the number that matters here: swapping the real patterns for stand-ins changed no test count and no assertion.

MUTATION CHECK ON THE SUBSTITUTION, which is the claim that needed proving. Restoring Rule.clears to its pre-fix body (an unanchored re.search over the raw command) turns EIGHT tests red: all five test_a_stored_bypass_rule_no_longer_clears_what_it_would_have cases, the multi-line sweep, the falsifiability test, and the trailing-newline test. So the stand-ins exercise the floor exactly as the real patterns did. Separately, disabling the unterminated-body branch in shape_of turns three red, as reported in round 1.

REPO SCAN after the substitution: grep across tests/, docs/ and src/ for the five rule ids returns two hits, both the id gr-391ba702 in prose, neither accompanied by its pattern or its command. Grep for the three real dismissed commands returns nothing.

LIVE, READ-ONLY: every exemption in the production base listed with its test and position - gr-88a21f3f, gr-35433985 and gr-97581653 are signatures, all three at QUOTED position, none at heredoc. That is the evidence for the new claim in spec section 7 that both heredoc holes were latent rather than live.

PR STATE VERIFIED rather than assumed: "gh pr view 215 --json isDraft" returns true after the re-draft, and the body first line is the DO NOT MERGE block.

UNCHANGED AND STILL TRUE from earlier rounds: 270-probe live sweep gave 5 NOT GATED before retraction and 0 after; 0 again under this diff with the new canaries; 162 probes 0 NOT GATED after the OS learned gr-35433985 from my own dismissal.

STILL NOT VERIFIED, unchanged and stated again because it has not improved: no A/B eval on the gates.REVIEWER_PERSONA prose. It only narrows what Neo may propose and the OS refuses the disallowed shapes whatever the model does, so the enforcement is what is tested and the prose is advisory on top of it."""

out = subprocess.run(
    ["jarvis", "wo", "finish", "wo-551f5e8c",
     "--pr", "https://github.com/gonandrap/agentic_os/pull/215",
     "--summary", SUMMARY, "--evidence", EVIDENCE],
    capture_output=True, text=True)
print(out.stdout[-700:])
print(out.stderr[-700:])
