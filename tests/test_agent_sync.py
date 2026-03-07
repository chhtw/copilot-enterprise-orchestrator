"""
test_agent_sync.py — 測試 agent_sync YAML 載入 + foundry_agents YAML loading。

測試項目：
  1. load_yaml() — 從 prompts/{name}.yaml 讀取 YAML
  2. _load_agent_def_from_yaml() — YAML → _AgentDef 轉換
  3. _YAML_KIND_TO_TOOL_TYPE mapping
  4. YAML 不存在時的 fallback 行為
  5. observability setup（無 connection string 應 graceful 降級）
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
import yaml

# 強制 mock mode
os.environ["MOCK_MODE"] = "true"
os.environ["RUN_MODE"] = "cli"


# ======================================================================
# 1. agent_sync.load_yaml
# ======================================================================
class TestLoadYaml:
    """Test agent_sync.load_yaml reads YAML correctly."""

    def test_load_existing_yaml(self):
        """Should load any of our 5 YAML files without error."""
        from orchestrator_app.agent_sync import load_yaml

        doc = load_yaml("Azure-Architecture-Clarification-Agent")
        assert doc is not None
        assert doc["kind"] == "Prompt"
        assert doc["name"] == "Azure-Architecture-Clarification-Agent"
        assert "instructions" in doc

    def test_load_nonexistent_raises(self):
        """Should raise FileNotFoundError for a YAML that doesn't exist."""
        from orchestrator_app.agent_sync import load_yaml

        with pytest.raises(FileNotFoundError):
            load_yaml("Nonexistent-Agent-12345")

    def test_all_five_yamls_loadable(self):
        """All 5 managed agent YAMLs should load and have valid structure."""
        from orchestrator_app.agent_sync import MANAGED_AGENTS, load_yaml

        for name in MANAGED_AGENTS:
            doc = load_yaml(name)
            assert doc is not None, f"YAML for {name} not found"
            assert doc["kind"] == "Prompt", f"{name}: expected kind=Prompt"
            assert doc.get("name") == name, f"{name}: name mismatch"
            assert doc.get("instructions"), f"{name}: instructions missing"
            assert doc.get("model", {}).get("id"), f"{name}: model.id missing"


# ======================================================================
# 2. foundry_agents._load_agent_def_from_yaml
# ======================================================================
class TestLoadAgentDefFromYaml:
    """Test the YAML → _AgentDef conversion in foundry_agents."""

    def test_load_clarification_agent(self):
        """Azure-Architecture-Clarification-Agent: model=gpt-5.4, tools=[]."""
        from orchestrator_app.foundry_agents import _load_agent_def_from_yaml

        result = _load_agent_def_from_yaml("Azure-Architecture-Clarification-Agent")
        assert result is not None
        assert result.model == "gpt-5.4"
        assert result.tools == []
        assert len(result.instructions) > 100

    def test_load_terraform_agent_no_tools(self):
        """Azure-Terraform-Generation-Agent: model=gpt-5.4, tools=[] (本地 Copilot 定義)."""
        from orchestrator_app.foundry_agents import _load_agent_def_from_yaml

        result = _load_agent_def_from_yaml("Azure-Terraform-Generation-Agent")
        assert result is not None
        assert result.model == "gpt-5.4"
        assert result.tools == []
        assert len(result.instructions) > 200

    def test_load_diagram_agent_no_tools(self):
        """Azure-Diagram-Generation-Agent: model=gpt-5.4, tools=[] (本地 Copilot 定義)."""
        from orchestrator_app.foundry_agents import _load_agent_def_from_yaml

        result = _load_agent_def_from_yaml("Azure-Diagram-Generation-Agent")
        assert result is not None
        assert result.model == "gpt-5.4"
        assert result.tools == []

    def test_load_cost_browser_agent_local_only(self):
        """Azure-Pricing-Browser-Agent: local-only YAML (尚未部署到 Foundry)."""
        from orchestrator_app.foundry_agents import _load_agent_def_from_yaml

        result = _load_agent_def_from_yaml("Azure-Pricing-Browser-Agent")
        assert result is not None
        tool_types = [t["type"] for t in result.tools]
        assert "browser_automation_preview" in tool_types
        assert "web_search_preview" in tool_types

    def test_load_cost_structure_agent_no_tools(self):
        """Azure-Pricing-Structure-Agent: model=gpt-5.4, tools=[] (Foundry 實際定義)."""
        from orchestrator_app.foundry_agents import _load_agent_def_from_yaml

        result = _load_agent_def_from_yaml("Azure-Pricing-Structure-Agent")
        assert result is not None
        assert result.model == "gpt-5.4"
        assert result.tools == []  # Foundry 上無 tools

    def test_nonexistent_returns_none(self):
        """Should return None for missing YAML."""
        from orchestrator_app.foundry_agents import _load_agent_def_from_yaml

        result = _load_agent_def_from_yaml("Does-Not-Exist-Agent")
        assert result is None

    def test_invalid_yaml_returns_none(self, tmp_path):
        """Should return None when YAML has invalid kind."""
        from orchestrator_app.foundry_agents import _load_agent_def_from_yaml, _PROMPTS_DIR

        # Create a temp YAML with wrong kind
        bad_yaml = _PROMPTS_DIR / "TestBadKind-Agent.yaml"
        try:
            bad_yaml.write_text("kind: NotPrompt\nname: TestBadKind-Agent\n")
            result = _load_agent_def_from_yaml("TestBadKind-Agent")
            assert result is None
        finally:
            bad_yaml.unlink(missing_ok=True)


# ======================================================================
# 3. Tool type mapping
# ======================================================================
class TestToolMapping:
    """Test bidirectional YAML ↔ Foundry tool type mapping."""

    def test_yaml_kind_to_tool_type(self):
        from orchestrator_app.foundry_agents import _YAML_KIND_TO_TOOL_TYPE

        assert _YAML_KIND_TO_TOOL_TYPE["WebSearch"] == "web_search_preview"
        assert _YAML_KIND_TO_TOOL_TYPE["CodeInterpreter"] == "code_interpreter"
        assert _YAML_KIND_TO_TOOL_TYPE["BrowserAutomation"] == "browser_automation_preview"

    def test_agent_sync_tool_type_to_yaml(self):
        from orchestrator_app.agent_sync import _TOOL_TYPE_TO_YAML_KIND

        assert _TOOL_TYPE_TO_YAML_KIND["web_search_preview"] == "WebSearch"
        assert _TOOL_TYPE_TO_YAML_KIND["code_interpreter"] == "CodeInterpreter"
        assert _TOOL_TYPE_TO_YAML_KIND["browser_automation_preview"] == "BrowserAutomation"


# ======================================================================
# 4. YAML ↔ _AgentDef round-trip consistency
# ======================================================================
class TestYamlRoundTrip:
    """Ensure all 5 YAML files produce valid _AgentDef objects."""

    def test_all_agents_load_successfully(self):
        from orchestrator_app.agent_sync import MANAGED_AGENTS
        from orchestrator_app.foundry_agents import _load_agent_def_from_yaml

        for name in MANAGED_AGENTS:
            result = _load_agent_def_from_yaml(name)
            assert result is not None, f"Failed to load _AgentDef for {name}"
            assert result.model, f"{name}: model is empty"
            assert result.instructions, f"{name}: instructions is empty"
            # Verify all tools have 'type' key
            for tool in result.tools:
                assert "type" in tool, f"{name}: tool missing 'type' key: {tool}"


class TestDiagramOutputParsing:
    def test_parse_diagram_output_preserves_escaped_newline_in_string_literal(self):
        from orchestrator_app.foundry_agents import parse_diagram_output

        diagram_py = (
            'ddos = DDOSProtectionPlans("DDoS Protection\\nStandard")\n'
            'frontdoor = FrontDoors("Azure Front Door")'
        )
        raw = json.dumps({
            "diagram_py": diagram_py,
            "render_log": "ok",
            "approved_resource_manifest": {"resources": []},
        }, ensure_ascii=False)

        result = parse_diagram_output(raw)

        assert 'DDOSProtectionPlans("DDoS Protection\\nStandard")' in result.diagram_py
        assert 'FrontDoors("Azure Front Door")' in result.diagram_py

    def test_parse_diagram_output_unescapes_single_line_script_boundaries(self):
        from orchestrator_app.foundry_agents import parse_diagram_output

        raw = json.dumps({
            "diagram_py": 'print("hello")\\nprint("world")',
            "render_log": "ok",
            "approved_resource_manifest": {"resources": []},
        }, ensure_ascii=False)

        result = parse_diagram_output(raw)

        assert result.diagram_py == 'print("hello")\nprint("world")'


# ======================================================================
# 5. SDK compatibility helpers
# ======================================================================
class _AsyncVersionList:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        self._iter = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class TestSdkCompatibility:
    @pytest.mark.asyncio
    async def test_agent_sync_latest_definition_falls_back_to_get_version(self):
        from orchestrator_app.agent_sync import _get_latest_agent_definition

        class FakeAgents:
            async def get_version(self, agent_name, agent_version):
                assert agent_name == "DemoAgent"
                assert agent_version == "7"
                return {"definition": {"model": "gpt-5.4", "instructions": "hello", "tools": []}}

            def list_versions(self, agent_name, limit=1, order="desc"):
                return _AsyncVersionList([])

        class FakeClient:
            agents = FakeAgents()

        agent = {"name": "DemoAgent", "latest_version": "7"}
        definition = await _get_latest_agent_definition(FakeClient(), "DemoAgent", agent)

        assert definition["model"] == "gpt-5.4"
        assert definition["instructions"] == "hello"

    @pytest.mark.asyncio
    async def test_push_yaml_to_foundry_uses_create_version(self, monkeypatch):
        from orchestrator_app import agent_sync

        calls: list[tuple[str, object]] = []

        monkeypatch.setattr(
            agent_sync,
            "load_yaml",
            lambda agent_name: {
                "kind": "Prompt",
                "model": {"id": "gpt-5.4"},
                "instructions": "demo instructions",
                "tools": [],
            },
        )

        class FakeCredential:
            async def close(self):
                calls.append(("credential.close", None))

        class FakeAgents:
            async def get(self, agent_name):
                calls.append(("agents.get", agent_name))
                return {"name": agent_name}

            async def create_version(self, agent_name, *, definition):
                calls.append(("agents.create_version", {"agent_name": agent_name, "definition": definition}))
                return {"version": "3"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.agents = FakeAgents()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(agent_sync, "DefaultAzureCredential", FakeCredential)
        monkeypatch.setattr(agent_sync, "AIProjectClient", FakeClient)

        await agent_sync.push_yaml_to_foundry("DemoAgent", publish=True)

        assert calls[0] == ("agents.get", "DemoAgent")
        create_call = next(item for item in calls if item[0] == "agents.create_version")
        assert create_call[1]["agent_name"] == "DemoAgent"
        assert create_call[1]["definition"]["model"] == "gpt-5.4"
        assert create_call[1]["definition"]["instructions"] == "demo instructions"
        assert all(name not in {"agents.create", "agents.update", "agents.publish"} for name, _ in calls)


# ======================================================================
# 6. Observability graceful degradation
# ======================================================================
class TestObservability:
    """Test observability setup handles missing connection string gracefully."""

    def test_setup_without_connection_string(self):
        """setup_observability() should not raise when conn string is missing."""
        # Reset module state
        import orchestrator_app.observability as obs
        obs._initialized = False
        obs._tracer = None
        obs._meter = None

        with patch.dict(os.environ, {"APPLICATIONINSIGHTS_CONNECTION_STRING": ""}, clear=False):
            obs.setup_observability()

        # Should have NoOp-style tracer/meter
        assert obs._tracer is not None
        assert obs._meter is not None
        assert obs._initialized is True

    def test_get_tracer_returns_tracer(self):
        from orchestrator_app.observability import get_tracer

        tracer = get_tracer()
        assert tracer is not None

    def test_get_meter_returns_meter(self):
        from orchestrator_app.observability import get_meter

        meter = get_meter()
        assert meter is not None
