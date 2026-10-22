"""Remote-readiness: async tool dispatch, per-MCP-session chats, streamable HTTP."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from engineer_logbook import server

DSN = os.environ.get("ENGINEER_LOGBOOK_PG_TEST_DSN")
needs_db = pytest.mark.skipif(
    not DSN, reason="ENGINEER_LOGBOOK_PG_TEST_DSN not set; skipping PG tests"
)


# ---------------------------------------------------------------- in-process (no DB)


def test_every_tool_is_async():
    tools = server.mcp._tool_manager.list_tools()
    assert len(tools) >= 35
    sync = [t.name for t in tools if not inspect.iscoroutinefunction(t.fn)]
    assert sync == [], f"sync tools block the event loop for every client: {sync}"


def test_tool_work_runs_in_threads_with_the_callers_chat(monkeypatch):
    """Concurrent calls overlap (event loop not blocked) and the chat id
    resolved on the event loop is what the worker thread sees."""
    resolved: list[str] = []

    def resolve_once_per_call():
        chat = f"chat-{len(resolved)}"
        resolved.append(chat)
        return chat

    def slow_db_work(op, chat):
        time.sleep(0.4)  # stands in for remote Postgres round trips
        return {"ok": True, "arg": chat, "seen": server._chat()}

    monkeypatch.setattr(server, "_resolve_chat", resolve_once_per_call)
    monkeypatch.setattr(server, "_run_sync", slow_db_work)

    async def go():
        t0 = time.perf_counter()
        results = await asyncio.gather(*(server._run(lambda c: {}) for _ in range(4)))
        return results, time.perf_counter() - t0

    results, elapsed = asyncio.run(go())
    assert elapsed < 1.2, f"calls were serialized ({elapsed:.2f}s for 4 x 0.4s)"
    assert len(resolved) == 4  # resolved once per call, never inside a thread
    for r in results:
        assert r["seen"] == r["arg"]  # each thread kept its own caller's chat
    assert sorted(r["seen"] for r in results) == sorted(resolved)


def test_stdio_uses_process_identity(monkeypatch):
    monkeypatch.delenv("ENGINEER_LOGBOOK_LEGACY_GLOBAL", raising=False)
    assert server._resolve_chat() == server.db.chat_id()  # no request context


def test_cli_defaults_to_local_stdio(monkeypatch):
    for v in ("ENGINEER_LOGBOOK_TRANSPORT", "ENGINEER_LOGBOOK_HOST",
              "ENGINEER_LOGBOOK_PORT", "ENGINEER_LOGBOOK_ALLOWED_HOSTS"):
        monkeypatch.delenv(v, raising=False)
    args = server._parse_args([])
    assert (args.transport, args.host, args.port, args.allowed_hosts) == (
        "stdio", "127.0.0.1", 8000, [])
    args = server._parse_args(["--transport", "streamable-http", "--port", "9123",
                               "--allowed-host", "logbook.example.com"])
    assert (args.transport, args.port, args.allowed_hosts) == (
        "streamable-http", 9123, ["logbook.example.com"])


# ---------------------------------------------------------------- real HTTP server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_url():
    if not DSN:
        pytest.skip("ENGINEER_LOGBOOK_PG_TEST_DSN not set")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "engineer_logbook.server", "--transport", "streamable-http",
         "--port", str(port), "--allowed-host", "logbook.example.com"],
        env={**os.environ, "ENGINEER_LOGBOOK_PG_DSN": DSN},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited: {proc.stderr.read()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("HTTP server did not start within 15s")
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def clean_db():
    from engineer_logbook import db_pg
    from conftest import wipe

    c = db_pg.connect(DSN)
    wipe(c)
    yield c
    c.close()


def _payload(res) -> dict:
    assert not res.isError, f"tool error: {res.content}"
    return json.loads(res.content[0].text)


@needs_db
def test_two_http_clients_are_separate_chats(http_url, clean_db):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def flow():
        async with streamablehttp_client(http_url) as (ra, wa, _), \
                streamablehttp_client(http_url) as (rb, wb, _):
            async with ClientSession(ra, wa) as a, ClientSession(rb, wb) as b:
                await a.initialize()
                await b.initialize()

                res = _payload(await a.call_tool("start_session", {"title": "alpha", "project": "h"}))
                assert res["ok"] is True
                # B is a different chat: no session yet, and it may start its own
                assert _payload(await b.call_tool("get_current_session", {}))["session"] is None
                res = _payload(await b.call_tool("start_session", {"title": "beta", "project": "h"}))
                assert res["ok"] is True, res

                # ending B's session leaves A's alone
                assert _payload(await b.call_tool("end_session", {}))["ok"] is True
                cur = _payload(await a.call_tool("get_current_session", {}))
                assert cur["session"]["title"] == "alpha"

    asyncio.run(flow())
    chats = [r["chat_id"] for r in clean_db.execute(
        "SELECT chat_id FROM sessions ORDER BY id").fetchall()]
    assert len(chats) == 2 and len(set(chats)) == 2
    assert all(c.startswith("http-") for c in chats)


@needs_db
def test_http_host_header_protection(http_url):
    import httpx

    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "host-test", "version": "0"}}}
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}

    def status(host: str) -> int:
        return httpx.post(http_url, json=body, headers={**headers, "Host": host},
                          timeout=10).status_code

    assert status("logbook.example.com") == 200  # added via --allowed-host
    assert status("evil.example") == 421  # DNS-rebinding protection rejects others
