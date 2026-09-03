"""The symptom catalogue: health probes as configuration.

§2 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.

WHAT THESE TESTS ARE BUILT TO AVOID. `assert len(cfg.probes) == 5` is green off the
shipped defaults with no parser at all, and `assert len(resolved) == 4` after an override
is green on WHOLESALE REPLACEMENT — the merge rule this section exists to establish. So
every inheritance assertion is a set of four made together: the count, the disabled probe
being PRESENT rather than absent, an untouched probe being byte-identical to the fleet's,
and the fleet's own list being unchanged afterwards.

The section ships no trigger and no model call, which is a property of the diff that no
test can grade. The closest observable is the last test in this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import catalog, config_version, ops, probes, supervisor
from jarvis.catalog import CatalogError, load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

P1 = probes.HealthProbe(id="one", title="First", prompt="the first symptom")
P2 = probes.HealthProbe(id="two", title="Second", prompt="the second symptom",
                        subjects=("work_order",))


#: THE COMMITTED OUTPUT of `build_system_prompt(store, project)` with no probes and
#: no learnings, byte for byte. A literal rather than a re-derivation on purpose:
#: the whole failure this pins is a template that grew, and a test that rebuilt the
#: prompt from the same parts would grow with it.
PROMPT_WITHOUT_PROBES = """\
You are the SUPERVISOR inside the Jarvis agentic OS.

The OS raised a cost alarm on a work order whose turn is STILL RUNNING — it has been going
too long, has been blocked on a subagent too long, or re-sent too much of its conversation
in one call. Left alone that alarm becomes an attention item: the user is interrupted and
has to go and look. Your job is to look instead, and to decide whether it needed them.

You are shown evidence, never the worker's transcript. Judge on what you are given.

ACK when the spend is EXPLICABLE — the shape of the work accounts for it. A long turn on a
design document, a planning session, a large refactor or a test suite that takes an hour is
the work costing what the work costs. A single large cache write at the start of a session
is a cold start and is not a defect. When you ack, the flag comes down and the user gets
your `note` instead of an interruption, so the note must stand alone: say what the order is
doing and why the number is what it is, in plain words, without the reader opening anything.

ESCALATE when you cannot account for it, or when what you can see suggests something is
actually wrong — a turn with no visible progress, a join that has outlived any plausible
subagent, a conversation re-sent repeatedly rather than once. Escalating is not a failure:
it is the honest answer whenever the evidence does not settle the question, and it costs
the user exactly what they were going to pay anyway. PREFER IT WHEN UNSURE. A wrong ack
hides a turn that is still burning money; a wrong escalation costs one glance.

WHAT YOU MAY NOT DO, and the OS enforces it in code rather than trusting this paragraph:
you do not message the worker, cancel its turn, change its status, or act on the work order
in any way. Your entire output is a judgement. Do not offer to intervene and do not phrase
the note as though you had.

Output STRICT JSON, nothing else:
  {"decision": "ack", "reason": "<why, 1-2 sentences, for the record>",
   "note": "<what the user is told, <= 200 chars, plain words>", "question": ""}
  or
  {"decision": "escalate", "reason": "<why, 1-2 sentences, for the record>",
   "note": "", "question": "<the one question to put to Neo>"}

# Learnings (from the user's corrections of your past decisions)
(none yet — escalate when unsure)"""


def _catalog(tmp_path: Path, os_block: dict, project_block: dict | None = None,
             name: str = "p") -> catalog.Catalog:
    project: dict = {"name": name, "path": str(tmp_path)}
    if project_block is not None:
        project["supervisor"] = project_block
    return catalog.parse_catalog({"os": {"supervisor": os_block},
                                  "projects": [project]})


# -- the merge rule --------------------------------------------------------------------


def test_the_merge_is_by_id_and_leaves_the_fleet_list_alone(tmp_path):
    """THE THREE CASES IN ONE TEST, because each alone passes for the wrong reason.

    An OS list of three, a project that disables one of them and adds one of its own.
    Asserting only the length would pass on wholesale replacement; asserting only the
    new probe would pass on a merge that dropped the rest. The fourth assertion — the
    OS-level list is still three — is what catches a merge that edited the shared
    `DEFAULT_PROBES` tuple in place.
    """
    three = [{"id": "a", "title": "A", "prompt": "aaa"},
             {"id": "b", "title": "B", "prompt": "bbb"},
             {"id": "c", "title": "C", "prompt": "ccc"}]
    cat = _catalog(
        tmp_path,
        {"probes": three},
        {"probes": [{"id": "b", "enabled": False},
                    {"id": "d", "title": "D", "prompt": "ddd"}]},
    )
    fleet = cat.os.supervisor.probes
    mine = cat.project("p").supervisor.probes

    # The OS list merged over the shipped five, so it is eight; the project adds one.
    assert [p.id for p in mine] == [p.id for p in fleet] + ["d"]
    assert len(mine) == len(fleet) + 1

    # PRESENT AND DISABLED, never absent: a project cannot delete what the fleet watches
    # for, so the record of the fleet's list stays legible on every project's read.
    (disabled,) = [p for p in mine if p.id == "b"]
    assert disabled.enabled is False
    assert disabled.title == "B" and disabled.prompt == "bbb"

    # An untouched probe is byte-identical to the fleet's, not a rebuilt copy.
    assert next(p for p in mine if p.id == "a") == next(p for p in fleet if p.id == "a")

    # ... and the fleet's own list never moved.
    assert next(p for p in fleet if p.id == "b").enabled is True
    assert len(fleet) == len(probes.DEFAULT_PROBES) + 3


def test_an_override_replaces_only_the_fields_it_names(tmp_path):
    """`{"id": …, "prompt": …}` rewords a probe and keeps everything else — the property
    that makes "watch for this differently here" a one-line catalog edit."""
    cat = _catalog(tmp_path, {}, {"probes": [{"id": "no-progress",
                                              "prompt": "reworded for this project"}]})
    (mine,) = [p for p in cat.project("p").supervisor.probes if p.id == "no-progress"]
    shipped = probes.DEFAULT_PROBES[0]

    assert mine.prompt == "reworded for this project"
    assert mine.title == shipped.title
    assert mine.subjects == shipped.subjects
    assert mine.enabled is shipped.enabled
    assert shipped.prompt != mine.prompt  # the shipped tuple was not edited in place


def test_resolve_does_not_mutate_the_base_it_was_given():
    """Directly, without a catalog: `DEFAULT_PROBES` is module state shared by every
    project's resolved config, so a merge that appended in place would leak across the
    fleet — and across tests."""
    base = (P1, P2)
    out = probes.resolve(base, [{"id": "one", "enabled": False},
                                {"id": "three", "title": "T", "prompt": "ttt"}])

    assert [p.id for p in out] == ["one", "two", "three"]
    assert out[0].enabled is False
    assert base == (P1, P2)
    assert P1.enabled is True


# -- the refusals ----------------------------------------------------------------------


def test_a_probe_id_may_not_shadow_an_inspection_alarm_kind(tmp_path):
    """Two different things would read as one on `/alarms`, in `jarvis alarms` and in
    every listing that groups by kind."""
    with pytest.raises(CatalogError) as e:
        _catalog(tmp_path, {"probes": [{"id": "long-turn", "title": "T",
                                        "prompt": "p"}]})
    assert "long-turn" in str(e.value)


def test_reserved_ids_are_exactly_inspections_alarm_kinds():
    """`probes` cannot import `inspection` (it would be a cycle through `catalog`), so
    the list is duplicated as literals — and this is what keeps the copy honest when a
    fourth alarm kind is added."""
    from jarvis import inspection

    assert set(probes.RESERVED_IDS) == set(inspection.ALARM_KINDS)


def test_a_duplicate_id_in_one_list_is_refused(tmp_path):
    """The merge would silently collapse them and the second entry would win with no
    sign that the first was ever read."""
    with pytest.raises(CatalogError) as e:
        _catalog(tmp_path, {"probes": [{"id": "dup", "title": "A", "prompt": "a"},
                                       {"id": "dup", "title": "B", "prompt": "b"}]})
    assert "dup" in str(e.value)


def test_an_unknown_subject_is_refused(tmp_path):
    """A probe armed for nothing is a setting that silently does nothing."""
    with pytest.raises(CatalogError) as e:
        _catalog(tmp_path, {"probes": [{"id": "x", "title": "X", "prompt": "x",
                                        "subjects": ["gate"]}]})
    assert "gate" in str(e.value) and "x" in str(e.value)


def test_a_prompt_over_the_budget_is_refused(tmp_path):
    """At the shipped budget, so the offender named is the one the catalog added. A
    budget set BELOW the shipped prompts is refused too, naming whichever ships longest
    — which is the honest answer: the cap bounds one prompt and the fleet's own list is
    in it."""
    over = "x" * (catalog.SupervisorConfig().probe_prompt_chars + 1)
    with pytest.raises(CatalogError) as e:
        _catalog(tmp_path, {"probes": [{"id": "wordy", "title": "W", "prompt": over}]})
    assert "wordy" in str(e.value) and str(len(over)) in str(e.value)


def test_more_enabled_probes_than_the_cap_is_refused(tmp_path):
    """They all ride in ONE system prompt, so the cap is on the prompt rather than on
    the catalog's tidiness."""
    with pytest.raises(CatalogError) as e:
        _catalog(tmp_path, {"max_enabled_probes": 5,
                            "probes": [{"id": "extra", "title": "E", "prompt": "e"}]})
    assert "6 probes enabled" in str(e.value) and "extra" in str(e.value)


def test_disabling_a_shipped_probe_makes_room_under_the_cap(tmp_path):
    """The positive partner for the refusal above: the cap counts ENABLED probes, so a
    project at the ceiling can still add one by switching another off."""
    cat = _catalog(tmp_path, {"max_enabled_probes": 5,
                              "probes": [{"id": "no-progress", "enabled": False},
                                         {"id": "extra", "title": "E", "prompt": "e"}]})
    assert len(cat.os.supervisor.probes) == 6
    assert sum(1 for p in cat.os.supervisor.probes if p.enabled) == 5


def test_a_probe_id_must_be_kebab_case(tmp_path):
    with pytest.raises(CatalogError) as e:
        _catalog(tmp_path, {"probes": [{"id": "Not Kebab", "title": "N",
                                        "prompt": "n"}]})
    assert "Not Kebab" in str(e.value)


def test_a_new_probe_must_bring_its_own_title_and_prompt(tmp_path):
    """An override may name one field because the rest are inherited; an ADDITION has
    nothing to inherit from, and a probe with an empty prompt detects nothing."""
    with pytest.raises(CatalogError) as e:
        _catalog(tmp_path, {"probes": [{"id": "bare"}]})
    assert "bare" in str(e.value) and "title" in str(e.value)


# -- the two shared functions that break loudly on the new fields ----------------------


def test_a_catalog_declaring_a_probe_parses_resolves_and_round_trips(tmp_path):
    """`config_version._jsonable` had no dataclass branch, so a `tuple[HealthProbe, …]`
    reached `json.dumps` and took out `jarvis config show`, `jarvis config set` and every
    config-version write. THE DODGE IS `_SKIP`: hiding `probes` from the resolved map
    would pass this and hide every probe from the console and from the version stamp, so
    `tests/test_supervisor.py::test_every_supervisor_setting_reaches_the_config_console`
    must keep passing unmodified beside it.
    """
    cat = _catalog(tmp_path, {"probes": [{"id": "mine", "title": "M", "prompt": "mmm"}]})
    resolved = config_version.resolve(cat)

    assert "os.supervisor.probes" in resolved
    assert "projects.p.supervisor.probes" in resolved
    back = json.loads(config_version.canonicalise(resolved))
    assert back["os.supervisor.probes"][-1] == {
        "id": "mine", "title": "M", "prompt": "mmm",
        "subjects": ["work_order", "feature_order"], "enabled": True}
    assert config_version.version_id(resolved)  # the hash the ledger stores


def test_health_enabled_inherits_field_by_field_and_reaches_the_resolved_map(tmp_path):
    """EXCLUDING A FIELD FROM THE REFLECTIVE `int()` PARSE IS HALF THE FIX. `int(False)`
    is 0, which trips the `>= 1` floor rather than raising a `TypeError`, so a worker who
    widened the exclusion for `probes` alone bricked the OS with a message naming the
    wrong key. And a field excluded but never read back is pinned at its default however
    the catalog sets it — every "ships off" assertion downstream then passes on a switch
    that can never be turned on.
    """
    cat = _catalog(tmp_path, {"health_enabled": True}, {"health_enabled": False})

    assert cat.os.supervisor.health_enabled is True
    assert cat.project("p").supervisor.health_enabled is False
    resolved = config_version.resolve(cat)
    assert resolved["projects.p.supervisor.health_enabled"] is False
    assert resolved["os.supervisor.health_enabled"] is True


def test_the_sweeps_off_switch_is_a_safety_key():
    """Same class of act as `os.supervisor.enabled`: it removes a watcher, and the change
    is invisible on every surface until the thing it was watching goes wrong."""
    assert "os.supervisor.health_enabled" in catalog.SAFETY_KEYS


def test_the_sweep_ships_off_and_its_numbers_are_settings():
    assert catalog.SupervisorConfig().health_enabled is False
    assert catalog.SupervisorConfig().probes == probes.DEFAULT_PROBES


# -- the checklist, and the prompt prefix it may not move ------------------------------


def test_armed_filters_by_enabled_and_by_subject():
    """A feature order is not shown the work-order probes, and neither is shown a
    disabled one."""
    disabled = probes.HealthProbe(id="off", title="Off", prompt="off", enabled=False)
    pool = (P1, P2, disabled)

    assert [p.id for p in probes.armed(pool, "work_order")] == ["one", "two"]
    assert [p.id for p in probes.armed(pool, "feature_order")] == ["one"]


def test_the_checklist_keeps_the_resolved_order():
    out = probes.render_checklist([P1, P2])

    assert out.index(P1.prompt) < out.index(P2.prompt)
    assert P1.id in out and P2.id in out  # a finding names its probe by id
    assert probes.render_checklist([]) == ""


def test_the_system_prompt_is_unchanged_without_probes_and_extends_with_them(
        jarvis_home):
    """THE MOST IMPORTANT PIN IN THIS SECTION, and the existing byte-stability test does
    not grade it: that one compares two calls in one process, so it catches a clock in
    the prompt and NOT a template that grew an empty checklist header for everybody.

    The cost review and the health sweep share this function; the cost prompt's prefix is
    load-bearing, because a changed prefix reprices every review silently. Equality alone
    would forbid the feature, so the second half asserts PREFIX EXTENSION — which is the
    property the cache actually needs.
    """
    from jarvis.neo_store import NeoStore

    store = NeoStore()
    try:
        bare = supervisor.build_system_prompt(store, "proj_a")
        with_probes = supervisor.build_system_prompt(store, "proj_a", probes=[P1, P2])
    finally:
        store.close()

    assert bare == PROMPT_WITHOUT_PROBES
    assert with_probes.startswith(PROMPT_WITHOUT_PROBES)
    assert len(with_probes) > len(PROMPT_WITHOUT_PROBES)
    assert P1.prompt in with_probes


# -- the CLI read ----------------------------------------------------------------------


def test_supervisor_probes_says_where_each_answer_came_from(
        jarvis_home, catalog_file, project):
    """`kn-42c52cec`: a resolved value the user cannot see is a value they cannot trust,
    and merge-by-id is exactly the kind of resolution that goes wrong quietly — a project
    that switched a probe off looks, on every other surface, like one that never had it.
    """
    data = json.loads(catalog_file.read_text())
    data["projects"][0]["supervisor"] = {
        "probes": [{"id": "no-progress", "enabled": False},
                   {"id": "flaky-tests", "title": "Tests flapping",
                    "prompt": "a test passes and fails with nothing changing"}]}
    catalog_file.write_text(json.dumps(data))

    rows = ops.supervisor_probes("proj_a", catalog_path=str(catalog_file))
    by_id = {r["id"]: r for r in rows}

    assert by_id["no-progress"]["source"] == "project override"
    assert by_id["no-progress"]["enabled"] is False   # present AND marked, not absent
    assert by_id["flaky-tests"]["source"] == "project addition"
    assert by_id["brief-mismatch"]["source"] == "fleet"
    assert len(rows) == len(probes.DEFAULT_PROBES) + 1

    # The fleet's own read is unaffected, and everything on it is the fleet's.
    fleet = ops.supervisor_probes(catalog_path=str(catalog_file))
    assert {r["source"] for r in fleet} == {"fleet"}
    assert all(r["enabled"] for r in fleet)


def test_the_cli_prints_the_source_and_marks_a_disabled_probe(
        jarvis_home, catalog_file, project, capsys):
    """The CLI is the OS: whatever the resolution says has to be readable in a terminal,
    not only in `--json`."""
    from jarvis.cli import main

    data = json.loads(catalog_file.read_text())
    data["projects"][0]["supervisor"] = {
        "probes": [{"id": "no-progress", "enabled": False}]}
    catalog_file.write_text(json.dumps(data))

    assert main(["supervisor", "probes", "proj_a",
                 "--catalog", str(catalog_file)]) == 0
    out = capsys.readouterr().out
    assert "no-progress" in out and "disabled" in out
    assert "project override" in out
    assert "brief-mismatch" in out and "fleet" in out


def test_supervisor_probes_json_carries_the_whole_prompt(
        jarvis_home, catalog_file, project, capsys):
    """Human output clips the paragraphs so the inheritance stays readable; `--json` is
    where the text a model will be shown actually lives."""
    from jarvis.cli import main

    assert main(["supervisor", "probes", "--json",
                 "--catalog", str(catalog_file)]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["prompt"] == probes.DEFAULT_PROBES[0].prompt


# -- and nothing looks at any of it yet ------------------------------------------------


def test_configured_probes_raise_nothing_and_call_no_model(
        jarvis_home, fake_claude, catalog_file, project):
    """THE CLOSEST OBSERVABLE FOR "this section adds no trigger". It cannot prove the
    property — that is the reviewer's job and the pull request says so — but a worker who
    wired a sweep in here would fail it.

    The positive partner is the first assertion: the probes really are resolved and armed
    on the project the tick walked, so the zeroes below are not the zeroes of a feature
    that was never configured.
    """
    data = json.loads(catalog_file.read_text())
    data["projects"][0]["supervisor"] = {
        "health_enabled": True,
        "probes": [{"id": "mine", "title": "M", "prompt": "mmm"}]}
    catalog_file.write_text(json.dumps(data))
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))

    spec = daemon.catalog.project("proj_a")
    assert [p.id for p in spec.supervisor.probes][-1] == "mine"
    assert spec.supervisor.health_enabled is True

    daemon.tick()

    assert fake_claude.calls == []
    store = ProjectStore(Path(spec.path))
    try:
        assert store.alarms_across(limit=50) == []
    finally:
        store.close()
