"""The help terminal's transport: start, websocket, close (docs/superpowers/specs/2026-11-12-onboarding-help-terminal-panel.md §2)"""

from __future__ import annotations

import asyncio
import json
import secrets
import urllib.parse
from collections.abc import Callable

from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import HTTPConnection

from .. import ops

LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
_REPLACED = object()  # pump sentinel: a newer attach took this draft


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    return u.scheme, u.hostname.lower(), port or (443 if u.scheme == "https" else 80)


def _base_origin() -> tuple[str, str, int] | None:
    from .. import notify
    from ..catalog import CatalogError

    try:
        return _origin(notify.ui_base_url(ops.resolve_catalog()))
    except (ops.OpsError, CatalogError, OSError):
        return None


def _origin_ok(conn: HTTPConnection) -> bool:
    """CSRF defence: the page must come from this dashboard; reads the catalog, so call it off the loop"""
    sent = _origin(conn.headers.get("origin", ""))
    if sent is None:
        return False
    scheme = {"ws": "http", "wss": "https"}.get(conn.url.scheme, conn.url.scheme)
    own = _origin(f"{scheme}://{conn.headers.get('host', '')}")
    if own is not None and own[1] in LOOPBACK and sent == own:
        return True
    return sent == _base_origin()


async def _checked(conn: HTTPConnection) -> bool:
    import anyio

    return await anyio.to_thread.run_sync(_origin_ok, conn)


def _put(q: asyncio.Queue, item: object) -> None:
    if q.full():
        q.get_nowait()
    q.put_nowait(item)


def register(app: FastAPI, render: Callable[..., HTMLResponse]) -> None:
    attached: dict[str, asyncio.Queue] = {}

    def _sessions(conn: HTTPConnection):
        return getattr(conn.app.state, "help_sessions", None)

    @app.post("/onboard/{name}/help/pty")
    async def help_pty_start(request: Request, name: str, section: str = Form(...),
                             focus: str = Form("")) -> JSONResponse:
        import anyio

        from .help_pty import HelpPtyError

        if not await _checked(request):
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        sessions = _sessions(request)
        if sessions is None:
            return JSONResponse({"error": "the help terminal did not start with the dashboard; "
                                          "see the dashboard log"}, status_code=502)
        try:
            launch = await anyio.to_thread.run_sync(
                lambda: ops.onboard_help_launch(name, section, focus))
        except ops.OpsError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        try:
            s, created = await sessions.start_or_feed(name, launch)
        except HelpPtyError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        return JSONResponse({"token": s.token, "created": created,
                             "ws_url": f"/onboard/{urllib.parse.quote(name)}/help/pty/ws",
                             "session_name": launch["session_name"]})

    @app.post("/onboard/{name}/help/pty/close")
    async def help_pty_close(request: Request, name: str,
                             token: str = Form("")) -> JSONResponse:
        if not await _checked(request):
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        sessions = _sessions(request)
        s = sessions.get(name) if sessions is not None else None
        # A stale token is the panel closing a session that already ended: not an error
        if s is None or not secrets.compare_digest(s.token, token):
            return JSONResponse({"closed": False})
        return JSONResponse({"closed": await sessions.close(name)})

    @app.websocket("/onboard/{name}/help/pty/ws")
    async def help_pty_ws(ws: WebSocket, name: str) -> None:
        import anyio
        from starlette.websockets import WebSocketDisconnect

        # Accept first: a close before accept reaches the browser as 1006, not our code
        ok = await _checked(ws)
        await ws.accept()
        sessions = _sessions(ws)
        s = sessions.get(name) if sessions is not None else None
        if not ok:
            await ws.close(4403)
            return
        if s is None:
            await ws.close(4404)
            return
        if not secrets.compare_digest(s.token, ws.query_params.get("token", "")):
            await ws.close(4403)
            return
        scrollback, q = s.attach()
        old = attached.get(name)
        attached[name] = q
        if old is not None:
            _put(old, _REPLACED)
        try:
            await ws.send_bytes(scrollback)
            async with anyio.create_task_group() as tg:
                async def pump() -> None:
                    while True:
                        chunk = await q.get()
                        if chunk is None or chunk is _REPLACED:
                            await ws.close(1000 if chunk is None else 4409)
                            tg.cancel_scope.cancel()
                            return
                        await ws.send_bytes(chunk)

                async def read() -> None:
                    try:
                        while True:
                            msg = await ws.receive()
                            if msg["type"] == "websocket.disconnect":
                                break
                            if msg.get("bytes") is not None:
                                s.write(msg["bytes"])
                            elif msg.get("text") is not None:
                                try:
                                    rows, cols = json.loads(msg["text"])["resize"]
                                    s.resize(rows, cols)
                                except (ValueError, KeyError, TypeError):
                                    pass  # malformed control frame: ignore, keep the session
                    except WebSocketDisconnect:
                        pass
                    tg.cancel_scope.cancel()

                tg.start_soon(pump)
                tg.start_soon(read)
        except WebSocketDisconnect:
            pass
        finally:
            # Never close the session here: page navigation is a disconnect
            s.detach(q)
            if attached.get(name) is q:
                del attached[name]
