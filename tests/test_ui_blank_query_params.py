"""Every dashboard page survives a blank query param — the shape `filter_bar` submits (wo-522ea53a)"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops  # noqa: E402
from jarvis.ui.app import create_app, query_int  # noqa: E402


def test_query_int_falls_back_on_blank_and_junk():
    assert query_int("7", 90) == 7
    assert query_int("", 90) == 90
    assert query_int("abc", None) is None


def test_no_html_page_answers_a_blank_param_with_a_422(jarvis_home, fake_claude, catalog_file):
    app = create_app()
    ops.start_os(str(catalog_file), foreground=True)
    client = TestClient(app, follow_redirects=False)
    offenders = []
    for route in app.routes:
        if not (isinstance(route, APIRoute) and "GET" in route.methods
                and route.response_class is HTMLResponse):
            continue
        path = route.path
        for p in route.dependant.path_params:
            path = path.replace("{" + p.name + "}", "1").replace("{" + p.name + ":path}", "1")
        query = "&".join(f"{q.name}=" for q in route.dependant.query_params)
        if not query:
            continue
        res = client.get(f"{path}?{query}")
        if res.status_code == 422:
            offenders.append(route.path)
    assert offenders == []
