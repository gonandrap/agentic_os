"""Every dashboard page survives a blank query param — the shape `filter_bar` submits (wo-522ea53a)"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

import pytest

pytest.importorskip("fastapi")
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.daemon import Daemon  # noqa: E402
from jarvis.ui.app import create_app, query_int  # noqa: E402


def _html_get_routes(app):
    return [r for r in app.routes
            if isinstance(r, APIRoute) and "GET" in r.methods
            and r.response_class is HTMLResponse]


def test_query_int_falls_back_on_blank_and_junk():
    assert query_int("7", 90) == 7
    assert query_int("", 90) == 90
    assert query_int("abc", None) is None


def test_no_html_page_answers_a_blank_param_with_a_422(jarvis_home, fake_claude, catalog_file):
    app = create_app()
    ops.start_os(str(catalog_file), foreground=True)
    client = TestClient(app, follow_redirects=False)
    offenders = []
    for route in _html_get_routes(app):
        path = route.path
        for p in route.dependant.path_params:
            path = path.replace("{" + p.name + "}", "1").replace("{" + p.name + ":path}", "1")
        query = "&".join(f"{q.name}=" for q in route.dependant.query_params)
        if not query:
            continue
        res = client.get(f"{path}?{query}")
        # 400 is the validation failure since the HTML handler (wo-0fbceffa); 422 before it
        if res.status_code in (400, 422):
            offenders.append(route.path)
    assert offenders == []


def test_html_get_query_params_are_str():
    """Blank 'every X' option fails a non-str param: declare str, parse with query_int (kn-34d78a6e)"""
    offenders = []
    for route in _html_get_routes(create_app()):
        for q in route.dependant.query_params:
            if q.field_info.annotation is not str:
                offenders.append(f"{route.path}?{q.name}: {q.field_info.annotation}")
    assert offenders == [], (
        "declare these `str = ''` and parse with query_int in src/jarvis/ui/app.py (kn-34d78a6e)")


def test_a_junk_int_path_segment_renders_the_error_page(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    client = TestClient(create_app(), follow_redirects=False)
    for url in ("/neo/question/abc", "/gates/abc"):
        res = client.get(url)
        assert res.status_code == 404, url
        assert res.headers["content-type"].startswith("text/html"), url
        assert "could not be read" in res.text
    # A form post missing its required field is the request's fault, still a page
    res = client.post("/neo/1/answer")
    assert res.status_code == 400
    assert res.headers["content-type"].startswith("text/html")


def test_api_validation_failure_stays_json_422(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    client = TestClient(create_app(), follow_redirects=False)
    res = client.get("/api/cost/meter?since=abc")
    assert res.status_code == 422
    assert res.headers["content-type"].startswith("application/json")


class _Links(HTMLParser):
    """Same-origin hrefs, and every GET form with its selects' option values"""

    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []
        self.forms: list[dict] = []
        self._form: dict | None = None
        self._select: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        elif tag == "form" and (a.get("method") or "get").lower() == "get":
            self._form = {"action": a.get("action") or "", "fixed": {}, "selects": {}}
            self.forms.append(self._form)
        elif self._form is None or not a.get("name"):
            if tag == "option" and self._form is not None and self._select:
                self._form["selects"][self._select].append(a.get("value", ""))
            return
        elif tag == "select":
            self._select = a["name"]
            self._form["selects"][self._select] = []
        elif tag == "input" and a.get("type", "text") not in ("submit", "button", "checkbox"):
            self._form["fixed"][a["name"]] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None


def _form_urls(page_url: str, form: dict) -> set[str]:
    """Each select at blank and at each option, the others at their first"""
    action = urljoin(page_url, form["action"] or page_url).split("?")[0]
    base = dict(form["fixed"])
    base.update({n: (vals[0] if vals else "") for n, vals in form["selects"].items()})
    urls = {f"{action}?{urlencode(base)}"}
    for name, vals in form["selects"].items():
        for v in {"", *vals}:
            urls.add(f"{action}?{urlencode({**base, name: v})}")
    return urls


def _same_origin(url: str) -> bool:
    parts = urlsplit(url)
    return not parts.scheme and not parts.netloc and url.startswith("/") \
        and not url.startswith("/api/") and not url.startswith("/static/")


def test_every_url_the_templates_produce_answers_with_a_page(
        jarvis_home, fake_claude, catalog_file, live_daemon):
    """Fetch the links and filter forms users click, depth 1 (wo-0fbceffa)"""
    app = create_app()
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    wo = ops.create_work_order("proj_a", "crawl fixture")
    daemon.tick()
    ops.assume(wo["id"], "assumed the API is v2")
    ops.finish(wo["id"], "done-ish")
    client = TestClient(app, follow_redirects=False)

    tops = [r.path for r in _html_get_routes(app) if not r.dependant.path_params]
    found: set[str] = set()
    for top in tops:
        res = client.get(top)
        assert res.status_code < 400, f"{top}: {res.status_code}"
        parser = _Links()
        parser.feed(res.text)
        found.update(urljoin(top, h).split("#")[0] for h in parser.hrefs)
        for form in parser.forms:
            found.update(_form_urls(top, form))

    offenders = []
    for url in sorted(u for u in found if _same_origin(u)):
        res = client.get(url)
        is_json = res.headers.get("content-type", "").startswith("application/json")
        if res.status_code >= 400 or is_json:
            offenders.append(f"{url}: {res.status_code}")
    assert len(found) > len(tops)
    assert offenders == []
