"""Codex adapter identity injection and persistence coverage."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport, StdioTransport, StreamableHttpTransport
from fastmcp.server import Context

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.codex_adapter import (
    CODEX_THREAD_ID,
    MCP_AGENT_MAIL_BEARER_TOKEN,
    MCP_AGENT_MAIL_URL,
    CodexAdapterError,
    CodexAdapterSettings,
    build_codex_adapter_server,
    canonicalize_upstream_url,
    load_adapter_settings,
    load_or_create_client_identity,
)
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.identity import parse_identity_metadata


class MemoryCredentialStore:
    """Deterministic keyring replacement for adapter tests."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password


def adapter_settings(conversation_uid: str, *, url: str = "https://mail.example.test/api/") -> CodexAdapterSettings:
    return CodexAdapterSettings(
        upstream_url=url,
        canonical_upstream_url=canonicalize_upstream_url(url),
        bearer_token="test-bearer-token",
        conversation_uid=conversation_uid,
        client_label="Codex test adapter",
    )


def structured_result(raw: object) -> dict[str, object]:
    structured = getattr(raw, "structuredContent", None)
    assert isinstance(structured, dict)
    nested = structured.get("result")
    return nested if isinstance(nested, dict) else structured


def test_load_adapter_settings_reads_env_file_and_runtime_thread_id(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "company-agent-mail.env"
    env_file.write_text(
        f"{MCP_AGENT_MAIL_URL}=http://mail.internal:8765/api/\n"
        f"{MCP_AGENT_MAIL_BEARER_TOKEN}=file-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(CODEX_THREAD_ID, "codex-thread-000000000001")

    settings = load_adapter_settings(env_file)

    assert settings.upstream_url == "http://mail.internal:8765/api/"
    assert settings.canonical_upstream_url == "http://mail.internal:8765/api"
    assert settings.bearer_token == "file-token"
    assert settings.conversation_uid == "codex-thread-000000000001"
    assert "file-token" not in repr(settings)
    assert "codex-thread-000000000001" not in repr(settings)


def test_load_adapter_settings_requires_codex_thread_id(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "agent-mail.env"
    env_file.write_text(f"{MCP_AGENT_MAIL_URL}=https://mail.example.test/api/\n", encoding="utf-8")
    monkeypatch.delenv(CODEX_THREAD_ID, raising=False)

    with pytest.raises(CodexAdapterError, match=CODEX_THREAD_ID):
        load_adapter_settings(env_file)


def test_load_adapter_settings_can_use_a_forwarded_bearer_variable(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "agent-mail.env"
    env_file.write_text(f"{MCP_AGENT_MAIL_URL}=https://mail.example.test/api/\n", encoding="utf-8")
    monkeypatch.setenv(CODEX_THREAD_ID, "codex-thread-000000000001")
    monkeypatch.setenv("EXISTING_AGENT_MAIL_TOKEN", "forwarded-token")

    settings = load_adapter_settings(
        env_file,
        bearer_token_env_var="EXISTING_AGENT_MAIL_TOKEN",
    )

    assert settings.bearer_token == "forwarded-token"


def test_client_identity_is_stable_per_canonical_upstream_and_isolated_between_services() -> None:
    store = MemoryCredentialStore()
    company_url = canonicalize_upstream_url("https://MAIL.example.test:443/api/")
    equivalent_company_url = canonicalize_upstream_url("https://mail.example.test/api")
    private_url = canonicalize_upstream_url("https://private.example.test/api/")

    company_first = load_or_create_client_identity(company_url, credential_store=store)
    company_second = load_or_create_client_identity(equivalent_company_url, credential_store=store)
    private = load_or_create_client_identity(private_url, credential_store=store)

    assert company_first == company_second
    assert company_first != private
    assert company_first.client_uid not in repr(company_first)
    assert company_first.client_secret not in repr(company_first)


@pytest.mark.asyncio
async def test_proxy_injects_trusted_metadata_on_every_tool_call() -> None:
    upstream = FastMCP("identity-capture")
    observed: list[tuple[str, str]] = []
    resource_observed: list[str] = []

    @upstream.tool
    async def capture_identity(ctx: Context, value: str) -> dict[str, object]:
        credentials = parse_identity_metadata(ctx.request_context.meta)
        assert credentials is not None
        observed.append((credentials.client_uid, credentials.conversation_uid))
        return {"value": value, "trusted": True}

    @upstream.resource("resource://identity-capture/{value}")
    async def capture_resource_identity(ctx: Context, value: str) -> str:
        credentials = parse_identity_metadata(ctx.request_context.meta)
        assert credentials is not None
        assert value == "current"
        resource_observed.append(credentials.conversation_uid)
        return credentials.conversation_uid

    store = MemoryCredentialStore()
    settings = adapter_settings("codex-thread-000000000001")
    proxy = build_codex_adapter_server(
        settings,
        credential_store=store,
        transport=FastMCPTransport(upstream),
    )

    async with Client(proxy) as client:
        first = await client.call_tool_mcp("capture_identity", {"value": "first"})
        second = await client.call_tool_mcp("capture_identity", {"value": "second"})
        resource_contents = await client.read_resource("resource://identity-capture/current")

    assert structured_result(first) == {"value": "first", "trusted": True}
    assert structured_result(second) == {"value": "second", "trusted": True}
    assert len(observed) == 2
    assert observed[0] == observed[1]
    assert observed[0][1] == "codex-thread-000000000001"
    assert resource_observed
    assert set(resource_observed) == {"codex-thread-000000000001"}
    assert resource_contents[0].text == "codex-thread-000000000001"


@pytest.mark.asyncio
async def test_same_codex_task_reconnects_and_different_task_gets_a_new_agent(isolated_env) -> None:
    upstream = build_mcp_server()
    store = MemoryCredentialStore()
    project_key = "/identity/codex-adapter"

    first_proxy = build_codex_adapter_server(
        adapter_settings("codex-thread-000000000001"),
        credential_store=store,
        transport=FastMCPTransport(upstream),
    )
    async with Client(first_proxy) as client:
        first_raw = await client.call_tool_mcp(
            "macro_start_session",
            {
                "human_key": project_key,
                "program": "codex",
                "model": "test",
                "file_reservation_paths": ["src/**"],
            },
        )
        first = structured_result(first_raw)

    second_proxy = build_codex_adapter_server(
        adapter_settings("codex-thread-000000000001"),
        credential_store=store,
        transport=FastMCPTransport(upstream),
    )
    async with Client(second_proxy) as client:
        second_raw = await client.call_tool_mcp(
            "macro_start_session",
            {"human_key": project_key, "program": "codex", "model": "test"},
        )
        second = structured_result(second_raw)
        prepared_raw = await client.call_tool_mcp(
            "macro_prepare_thread",
            {
                "project_key": project_key,
                "thread_id": "codex-adapter-test",
                "program": "codex",
                "model": "test",
                "register_if_missing": False,
                "llm_mode": False,
            },
        )
        prepared = structured_result(prepared_raw)

    third_proxy = build_codex_adapter_server(
        adapter_settings("codex-thread-000000000002"),
        credential_store=store,
        transport=FastMCPTransport(upstream),
    )
    async with Client(third_proxy) as client:
        third_raw = await client.call_tool_mcp(
            "macro_start_session",
            {"human_key": project_key, "program": "codex", "model": "test"},
        )
        third = structured_result(third_raw)

    first_agent = first["agent"]
    second_agent = second["agent"]
    third_agent = third["agent"]
    prepared_agent = prepared["agent"]
    first_reservations = first["file_reservations"]
    assert isinstance(first_agent, dict)
    assert isinstance(second_agent, dict)
    assert isinstance(third_agent, dict)
    assert isinstance(prepared_agent, dict)
    assert isinstance(first_reservations, dict)
    assert first_agent["identity_action"] == "created"
    assert second_agent["identity_action"] == "reconnected"
    assert first_agent["id"] == second_agent["id"]
    assert prepared_agent["id"] == first_agent["id"]
    assert third_agent["id"] != first_agent["id"]
    assert first_reservations["granted"]
    assert "registration_token" not in first
    assert "registration_token" not in second
    assert "registration_token" not in third
    assert "registration_token" not in prepared


@pytest.mark.asyncio
async def test_concurrent_first_start_creates_only_one_bound_agent(isolated_env) -> None:
    upstream = build_mcp_server()
    store = MemoryCredentialStore()
    settings = adapter_settings("codex-thread-concurrent-0001")
    first_proxy = build_codex_adapter_server(
        settings,
        credential_store=store,
        transport=FastMCPTransport(upstream),
    )
    second_proxy = build_codex_adapter_server(
        settings,
        credential_store=store,
        transport=FastMCPTransport(upstream),
    )

    async def start(proxy: FastMCP) -> dict[str, object]:
        async with Client(proxy) as client:
            raw = await client.call_tool_mcp(
                "macro_start_session",
                {
                    "human_key": "/identity/codex-concurrent",
                    "program": "codex",
                    "model": "test",
                },
            )
            return structured_result(raw)

    first, second = await asyncio.gather(start(first_proxy), start(second_proxy))

    first_agent = first["agent"]
    second_agent = second["agent"]
    project = first["project"]
    assert isinstance(first_agent, dict)
    assert isinstance(second_agent, dict)
    assert isinstance(project, dict)
    assert first_agent["id"] == second_agent["id"]
    assert {first_agent["identity_action"], second_agent["identity_action"]} == {"created", "reconnected"}

    async with Client(upstream) as client:
        contents = await client.read_resource(f"resource://agents/{project['slug']}")
    directory = json.loads(contents[0].text)
    assert len(directory["agents"]) == 1


@pytest.mark.asyncio
async def test_macro_migrates_a_legacy_named_agent_once(isolated_env) -> None:
    upstream = build_mcp_server()
    project_key = "/identity/codex-legacy-migration"
    async with Client(upstream) as legacy_client:
        await legacy_client.call_tool("ensure_project", {"human_key": project_key})
        legacy_raw = await legacy_client.call_tool_mcp(
            "register_agent",
            {
                "project_key": project_key,
                "program": "codex",
                "model": "test",
                "name": "SageBay",
            },
        )
        legacy = structured_result(legacy_raw)

    legacy_token = legacy["registration_token"]
    assert isinstance(legacy_token, str)
    store = MemoryCredentialStore()
    proxy = build_codex_adapter_server(
        adapter_settings("codex-thread-legacy-000001"),
        credential_store=store,
        transport=FastMCPTransport(upstream),
    )
    async with Client(proxy) as client:
        migrated_raw = await client.call_tool_mcp(
            "macro_start_session",
            {
                "human_key": project_key,
                "program": "codex",
                "model": "test",
                "agent_name": "SageBay",
                "registration_token": legacy_token,
            },
        )
        migrated = structured_result(migrated_raw)

    migrated_agent = migrated["agent"]
    assert isinstance(migrated_agent, dict)
    assert migrated_agent["id"] == legacy["id"]
    assert migrated_agent["identity_action"] == "migrated"
    assert migrated_agent["credential_managed_by_mcp"] is True
    assert "registration_token" not in migrated


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_runtime", ["python", "node", "node_metadata"])
async def test_adapter_propagates_identity_through_streamable_http(
    isolated_env,
    monkeypatch,
    adapter_runtime: str,
) -> None:
    node = shutil.which("node")
    repository = Path(__file__).parents[1]
    if adapter_runtime.startswith("node") and (node is None or not (repository / "node_modules/@modelcontextprotocol/sdk").is_dir()):
        pytest.skip("Node adapter dependencies unavailable; run npm install first.")
    bearer_token = "adapter-http-test-bearer"
    monkeypatch.setenv("HTTP_BEARER_TOKEN", bearer_token)
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
    clear_settings_cache()
    http_app = build_http_app(get_settings())
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    http_server = uvicorn.Server(
        uvicorn.Config(http_app, log_level="error", lifespan="on")
    )
    server_task = asyncio.create_task(http_server.serve(sockets=[listener]))
    for _ in range(100):
        if http_server.started:
            break
        await asyncio.sleep(0.01)
    assert http_server.started

    url = f"http://127.0.0.1:{port}/mcp/"
    transport = StreamableHttpTransport(url, auth=bearer_token)
    proxy = build_codex_adapter_server(
        adapter_settings("codex-thread-http-0000001", url=url),
        credential_store=MemoryCredentialStore(),
        transport=transport,
    )
    if adapter_runtime.startswith("node"):
        # Exercise the actual Node STDIO/HTTP bridge with test-only credentials;
        # do not create or touch entries in the developer's OS credential store.
        module_url = (repository / "adapter/cli.mjs").as_uri()
        node_settings = {'url': url, 'token': bearer_token, 'clientLabel': 'Node integration test', 'timeout': 30}
        if adapter_runtime == "node":
            node_settings['thread'] = 'codex-thread-http-0000001'
        script = (
            f"import {{bridge, parseIdentity}} from {json.dumps(module_url)};"
            f"const settings = {json.dumps(node_settings)};"
            f"const identity = parseIdentity({json.dumps(json.dumps({'version': 1, 'client_uid': 'codex-node-http-test-0001', 'client_secret': 'T' * 43}))});"
            "const session = await bridge(settings, identity); await session.done;"
        )
        target = StdioTransport(command=node or "node", args=["--input-type=module", "-e", script], cwd=str(repository), keep_alive=False)
    else:
        target = proxy

    request_meta = {"threadId": "codex-thread-http-0000001"} if adapter_runtime == "node_metadata" else None
    try:
        async with asyncio.timeout(30), Client(target) as client:
            assert await client.list_tools()
            started_raw = await client.session.call_tool(
                "macro_start_session",
                {"human_key": "/identity/codex-http", "program": "codex", "model": "test"},
                meta=request_meta,
            )
            started = structured_result(started_raw)
            status_raw = await client.session.call_tool(
                "identity_status",
                {"project_key": "/identity/codex-http"},
                meta=request_meta,
            )
            status = structured_result(status_raw)
            if adapter_runtime == "node_metadata":
                other = structured_result(await client.session.call_tool(
                    "macro_start_session",
                    {"human_key": "/identity/codex-http", "program": "codex", "model": "test"},
                    meta={"threadId": "codex-thread-http-0000002"},
                ))
                assert isinstance(other["agent"], dict) and isinstance(started["agent"], dict)
                assert other["agent"]["id"] != started["agent"]["id"]
                again = structured_result(await client.session.call_tool(
                    "identity_status", {"project_key": "/identity/codex-http"}, meta=request_meta,
                ))
                assert isinstance(again["agent"], dict)
                assert again["agent"]["id"] == started["agent"]["id"]
        if adapter_runtime.startswith("node"):
            # Same identity and task across a fresh Node process must reconnect.
            async with asyncio.timeout(30), Client(target) as client:
                resumed = structured_result(await client.session.call_tool(
                    "macro_start_session",
                    {"human_key": "/identity/codex-http", "program": "codex", "model": "test"},
                    meta=request_meta,
                ))
                assert isinstance(resumed["agent"], dict)
                assert isinstance(started["agent"], dict)
                assert resumed["agent"]["id"] == started["agent"]["id"]
                assert resumed["agent"]["identity_action"] == "reconnected"
    finally:
        http_server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)

    started_agent = started["agent"]
    status_agent = status["agent"]
    assert isinstance(started_agent, dict)
    assert isinstance(status_agent, dict)
    assert status["bound"] is True
    assert status_agent["id"] == started_agent["id"]
    assert "registration_token" not in started


def test_adapter_repr_and_logs_do_not_expose_credentials(caplog) -> None:
    store = MemoryCredentialStore()
    settings = adapter_settings("codex-thread-sensitive-0001")
    identity = load_or_create_client_identity(settings.canonical_upstream_url, credential_store=store)

    with caplog.at_level("DEBUG"):
        build_codex_adapter_server(
            settings,
            credential_store=store,
            transport=FastMCPTransport(FastMCP("redaction-upstream")),
        )

    rendered = f"{settings!r}\n{identity!r}\n{caplog.text}"
    assert settings.bearer_token is not None
    assert settings.bearer_token not in rendered
    assert settings.conversation_uid not in rendered
    assert identity.client_uid not in rendered
    assert identity.client_secret not in rendered
