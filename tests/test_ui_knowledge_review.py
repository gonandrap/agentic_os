"""The knowledge review page: a graph editor whose every write is one `ops.kb_*` call"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import cli, ops  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.ui.app import VENDORED, create_app  # noqa: E402


@pytest.fixture()
def client(jarvis_home):
    # No OS start: the review reads and writes os.db alone, like `jarvis learn review`
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def base(jarvis_home):
    c = CentralStore()
    try:
        a = c.add_knowledge("gates hold privileged actions", topic="gates", wo_id="wo-1")["id"]
        b = c.add_knowledge(f"a denied gate says why, see {a}", topic="gates",
                            wo_id="wo-1")["id"]
        d = c.add_domain("gates", by="process:kb-regroup")["id"]
        c.assign_domain([a], d, by="process:kb-regroup")
    finally:
        c.close()
    return {"a": a, "b": b, "domain": d}


def assignment(kn_id: str) -> tuple[str, str]:
    c = CentralStore()
    try:
        return c.domain_assignments(with_by=True).get(kn_id, ("", ""))
    finally:
        c.close()


# -- reads

def test_the_page_renders_on_an_empty_base(client):
    r = client.get("/knowledge/review")
    assert r.status_code == 200
    assert 'id="cy"' in r.text and "/vendor/cytoscape.min.js" in r.text
    # the manual rels the page offers are the store's, not a copy
    assert 'data-rels="relates_to,supersedes,depends_on,duplicates"' in r.text
    assert client.get("/knowledge/review.json").json() == {
        "nodes": [], "domains": [], "edges": []}


def test_the_page_and_its_payload_with_data(client, base):
    assert client.get("/knowledge/review").status_code == 200
    p = client.get("/knowledge/review.json").json()
    node = {n["id"]: n for n in p["nodes"]}
    assert node[base["a"]]["domain_id"] == base["domain"]
    assert node[base["a"]]["assigned_by"] == "process:kb-regroup"
    assert {(e["rel"], e["manual"]) for e in p["edges"]} >= {("cites", False),
                                                             ("same_wo", False)}
    assert [d["name"] for d in p["domains"]] == ["gates"]


def test_retired_entries_are_in_the_payload_only_on_request(client, base):
    c = CentralStore()
    try:
        c.retract_knowledge(base["b"], "superseded")
    finally:
        c.close()
    ids = {n["id"] for n in client.get("/knowledge/review.json").json()["nodes"]}
    assert base["b"] not in ids
    nodes = client.get("/knowledge/review.json?retired=1").json()["nodes"]
    assert {n["id"]: n["retired"] for n in nodes}[base["b"]] is True


def test_an_entrys_full_text_is_fetched_on_its_own(client, base):
    r = client.get(f"/knowledge/review/entry/{base['b']}")
    assert r.status_code == 200 and r.json()["content"].startswith("a denied gate")
    assert client.get("/knowledge/review/entry/kn-00000000").status_code == 404


# -- writes: each one persists through ops

def test_creating_editing_and_retiring_a_domain(client, base):
    r = client.post("/knowledge/review/domain",
                    json={"name": "spend", "description": "money", "parent_id": ""})
    assert r.status_code == 200
    spend = r.json()["result"]
    assert spend["by"] == ops.KB_HUMAN_ACTOR

    r = client.post(f"/knowledge/review/domain/{spend['id']}",
                    json={"name": "budgets", "parent_id": base["domain"]})
    assert r.status_code == 200
    domains = {d["id"]: d for d in ops.kb_review_payload()["domains"]}
    assert domains[spend["id"]]["name"] == "budgets"
    assert domains[spend["id"]]["description"] == "money"  # left out, left alone
    assert domains[spend["id"]]["parent_id"] == base["domain"]

    r = client.post(f"/knowledge/review/domain/{base['domain']}/retire",
                    json={"reason": "too broad"})
    assert r.status_code == 200
    domains = {d["id"]: d for d in ops.kb_review_payload()["domains"]}
    assert base["domain"] not in domains and domains[spend["id"]]["parent_id"] == ""
    assert assignment(base["a"])[0] == ""  # its member moved up, to the top


def test_new_domain_from_a_selection_assigns_in_one_step(client, base):
    r = client.post("/knowledge/review/domain",
                    json={"name": "denials", "kn_ids": [base["a"], base["b"]]})
    assert r.status_code == 200
    d = r.json()["result"]["id"]
    assert assignment(base["a"]) == (d, ops.KB_HUMAN_ACTOR)
    assert assignment(base["b"]) == (d, ops.KB_HUMAN_ACTOR)


def test_a_bad_id_in_a_selection_leaves_no_empty_domain(client, base):
    r = client.post("/knowledge/review/domain",
                    json={"name": "denials", "kn_ids": [base["a"], "kn-00000000"]})
    assert r.status_code == 400 and "kn-00000000" in r.json()["error"]
    assert [d["name"] for d in ops.kb_review_payload()["domains"]] == ["gates"]


def test_assigning_accepting_and_unassigning(client, base):
    # re-assigning to the SAME domain is how a proposal becomes human-reviewed
    r = client.post("/knowledge/review/assign",
                    json={"kn_ids": [base["a"]], "domain_id": base["domain"]})
    assert r.status_code == 200 and r.json()["result"] == 1
    assert assignment(base["a"]) == (base["domain"], ops.KB_HUMAN_ACTOR)

    client.post("/knowledge/review/assign", json={"kn_ids": [base["b"]],
                                                  "domain_id": base["domain"]})
    assert assignment(base["b"])[0] == base["domain"]
    client.post("/knowledge/review/assign", json={"kn_ids": [base["b"]], "domain_id": ""})
    assert assignment(base["b"]) == ("", ops.KB_HUMAN_ACTOR)


def test_adding_and_removing_a_link(client, base):
    r = client.post("/knowledge/review/link",
                    json={"src": base["b"], "rel": "depends_on", "dst": base["a"]})
    assert r.status_code == 200
    link = r.json()["result"]["id"]
    manual = [e for e in ops.kb_review_payload()["edges"] if e["manual"]]
    assert [(e["id"], e["rel"], e["by"]) for e in manual] == [
        (link, "depends_on", ops.KB_HUMAN_ACTOR)]

    r = client.post(f"/knowledge/review/link/{link}/retire", json={"reason": "wrong way"})
    assert r.status_code == 200
    assert not [e for e in ops.kb_review_payload()["edges"] if e["manual"]]


# -- refusals reach the page as a 4xx with the printable message

@pytest.mark.parametrize("url, body, words", [
    ("/knowledge/review/domain", {"name": "  "}, "needs a name"),
    ("/knowledge/review/domain", {"name": "gates"}, "already exists"),
    ("/knowledge/review/domain/dom-00000000", {"name": "x"}, "no live domain"),
    ("/knowledge/review/domain/{domain}/retire", {"reason": " "}, "needs a reason"),
    ("/knowledge/review/assign", {"kn_ids": ["kn-00000000"], "domain_id": ""}, "unknown"),
    ("/knowledge/review/assign", {"kn_ids": ["{a}"], "domain_id": "dom-00000000"},
     "no live domain"),
    ("/knowledge/review/link", {"src": "{a}", "rel": "likes", "dst": "{b}"}, "rel must be"),
    ("/knowledge/review/link", {"src": "{a}", "rel": "relates_to", "dst": "{a}"}, "itself"),
    ("/knowledge/review/link/kl-00000000/retire", {"reason": "x"}, "no live link"),
])
def test_an_ops_refusal_is_a_400_with_its_message(client, base, url, body, words):
    fill = {"domain": base["domain"], "a": base["a"], "b": base["b"]}
    body = {k: ([x.format(**fill) for x in v] if isinstance(v, list) else v.format(**fill))
            for k, v in body.items()}
    r = client.post(url.format(**fill), json=body)
    assert r.status_code == 400
    assert words in r.json()["error"] and r.json()["ok"] is False


def test_a_malformed_body_is_a_4xx_not_a_500(client):
    assert client.post("/knowledge/review/assign", json={"domain_id": ""}).status_code == 422


# -- the page's libraries are shipped, not fetched

def test_vendored_libraries_are_served_and_nothing_else_is(client):
    assert {"cytoscape.min.js", "cytoscape-fcose.js", "cose-base.js", "layout-base.js",
            "cytoscape-edgehandles.js"} <= VENDORED
    r = client.get("/vendor/cytoscape.min.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "Cytoscape" in r.text[:2000]
    assert client.get("/vendor/README.md").status_code == 404
    assert client.get("/vendor/..%2Fapp.py").status_code == 404


def test_the_page_loads_no_script_from_another_host(client):
    page = client.get("/knowledge/review").text
    assert "<script src=\"http" not in page and "cdn" not in page.lower()


def test_the_knowledge_page_links_to_the_review(client):
    page = client.get("/knowledge").text
    assert 'href="/knowledge/review"' in page and "Validate knowledge graph" in page
    # main's `/knowledge/{kn_id}` must not swallow the review's fixed routes
    assert "cytoscape" in client.get("/knowledge/review").text
    assert "nodes" in client.get("/knowledge/review.json").json()


# -- `jarvis learn review`

def test_learn_review_serves_the_review_page_without_a_browser(jarvis_home, monkeypatch,
                                                               capsys):
    served = {}
    monkeypatch.setattr(cli, "_serve_dashboard",
                        lambda host, port, open_path=None: served.update(
                            host=host, port=port, open_path=open_path) or 0)
    assert cli.main(["learn", "review", "--port", "8899", "--no-browser"]) == 0
    assert served == {"host": "127.0.0.1", "port": 8899, "open_path": None}
    assert "http://127.0.0.1:8899/knowledge/review" in capsys.readouterr().out

    assert cli.main(["learn", "review"]) == 0
    assert served["open_path"] == "/knowledge/review" and served["port"] > 0
