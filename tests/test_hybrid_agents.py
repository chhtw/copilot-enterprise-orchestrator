from __future__ import annotations

import json

import pytest


class TestHybridAgents:
    @pytest.mark.asyncio
    async def test_invoke_agent_raw_routes_terraform_to_copilot(self, monkeypatch):
        from orchestrator_app import hybrid_agents

        async def fake_copilot(agent_name: str, message: str, **kwargs):
            return f"copilot:{agent_name}:{message}", "session-1"

        async def fake_foundry(agent_name: str, message: str, **kwargs):
            return f"foundry:{agent_name}:{message}", "response-1"

        monkeypatch.setattr(hybrid_agents.copilot_agents, "invoke_agent_raw", fake_copilot)
        monkeypatch.setattr(hybrid_agents.foundry_agents, "invoke_agent_raw", fake_foundry)

        text, response_id = await hybrid_agents.invoke_agent_raw(
            hybrid_agents.TERRAFORM_AGENT,
            "hello",
        )

        assert text == f"copilot:{hybrid_agents.TERRAFORM_AGENT}:hello"
        assert response_id == "session-1"

    @pytest.mark.asyncio
    async def test_invoke_agent_raw_routes_pricing_to_foundry(self, monkeypatch):
        from orchestrator_app import hybrid_agents

        async def fake_copilot(agent_name: str, message: str, **kwargs):
            return f"copilot:{agent_name}:{message}", "session-1"

        async def fake_foundry(agent_name: str, message: str, **kwargs):
            return f"foundry:{agent_name}:{message}", "response-1"

        monkeypatch.setattr(hybrid_agents.copilot_agents, "invoke_agent_raw", fake_copilot)
        monkeypatch.setattr(hybrid_agents.foundry_agents, "invoke_agent_raw", fake_foundry)

        text, response_id = await hybrid_agents.invoke_agent_raw(
            hybrid_agents.PRICING_STRUCTURE_AGENT,
            "hello",
        )

        assert text == f"foundry:{hybrid_agents.PRICING_STRUCTURE_AGENT}:hello"
        assert response_id == "response-1"

    def test_build_diagram_prompt_contains_architecture_details(self):
        from orchestrator_app import hybrid_agents

        prompt = hybrid_agents.build_diagram_prompt('{"project":"demo"}', '{"core_services":["App Service"]}')

        assert "spec.json" in prompt
        assert "architecture_details.json" in prompt
        assert "App Service" in prompt

    def test_build_terraform_prompt_forbids_follow_up_questions(self):
        from orchestrator_app import hybrid_agents

        prompt = hybrid_agents.build_terraform_prompt('{"preferred_language":"zh-TW","project_name":"demo"}', '{"resources":[]}')

        assert "Do not ask clarifying questions" in prompt
        assert "exactly one JSON object" in prompt

    @pytest.mark.asyncio
    async def test_parallel_terraform_auto_finalize_nonfinal_response(self, monkeypatch):
        from orchestrator_app import executors

        calls: list[tuple[str, str | None]] = []
        monkeypatch.setattr(executors, "TERRAFORM_GENERATION_MODE", "single")

        async def fake_invoke(agent_name: str, message: str, *, previous_response_id: str | None = None, **kwargs):
            calls.append((message, previous_response_id))
            if len(calls) == 1:
                return "請先確認專案名稱與 tags。", "rid-1"
            return json.dumps({
                "main_tf": "resource \"azurerm_resource_group\" \"rg\" {}",
                "variables_tf": "",
                "outputs_tf": "",
                "locals_tf": "",
                "versions_tf": "",
                "providers_tf": "",
                "terragrunt_root_hcl": "",
                "terragrunt_dev_hcl": "",
                "terragrunt_prod_hcl": "",
                "readme_md": "# notes",
                "test_files": {},
                "resource_manifest": {
                    "project_name": "demo",
                    "resources": [],
                    "terraform_version": "1.9.0",
                    "provider_version": "4.0.0"
                }
            }), "rid-1"

        monkeypatch.setattr(executors.agents, "invoke_agent_raw", fake_invoke)
        monkeypatch.setattr(
            executors.agents,
            "classify_response",
            lambda text, expected_schema=None: "final" if text.lstrip().startswith("{") else "question",
        )

        result = await executors._invoke_parallel_terraform_generation(
            '{"preferred_language":"zh-TW","project_name":"demo"}',
            '{"resources":[]}',
        )

        assert result.status.value == "success"
        assert len(calls) == 2
        assert calls[1][1] == "rid-1"
        assert "不要再提問" in calls[1][0]

    @pytest.mark.asyncio
    async def test_parallel_terraform_raises_clear_error_when_still_nonfinal(self, monkeypatch):
        from orchestrator_app import executors
        monkeypatch.setattr(executors, "TERRAFORM_GENERATION_MODE", "single")

        async def fake_invoke(agent_name: str, message: str, *, previous_response_id: str | None = None, **kwargs):
            return "我還需要更多資訊才能產生 Terraform。", "rid-1"

        monkeypatch.setattr(executors.agents, "invoke_agent_raw", fake_invoke)
        monkeypatch.setattr(executors.agents, "classify_response", lambda text, expected_schema=None: "question")

        with pytest.raises(ValueError, match="non-final response"):
            await executors._invoke_parallel_terraform_generation(
                '{"preferred_language":"zh-TW","project_name":"demo"}',
                '{"resources":[]}',
            )

    @pytest.mark.asyncio
    async def test_parallel_terraform_staged_generation_aggregates_bundles(self, monkeypatch):
        from orchestrator_app import executors

        monkeypatch.setattr(executors, "TERRAFORM_GENERATION_MODE", "staged")
        calls: list[str] = []

        async def fake_invoke(agent_name: str, message: str, *, previous_response_id: str | None = None, **kwargs):
            calls.append(message)
            if "Current bundle: foundation" in message:
                return json.dumps({
                    "versions_tf": "terraform {}",
                    "providers_tf": "provider \"azurerm\" {}",
                    "locals_tf": "locals {}",
                    "variables_tf": "variable \"project_name\" {}",
                    "resource_manifest": {
                        "project_name": "demo",
                        "resources": [],
                        "terraform_version": "1.9.0",
                        "provider_version": "4.0.0",
                    },
                }), "rid-foundation"
            if "Current bundle: main" in message:
                return json.dumps({
                    "main_tf": "resource \"azurerm_resource_group\" \"rg\" {}",
                    "outputs_tf": "output \"rg_name\" {}",
                }), "rid-main"
            if "Current bundle: ops" in message:
                return json.dumps({
                    "terragrunt_root_hcl": "remote_state {}",
                    "terragrunt_dev_hcl": "include {}",
                    "terragrunt_prod_hcl": "include {}",
                    "readme_md": "# demo",
                    "test_files": {
                        "unit_basic.tftest.hcl": "run \"plan\" {}",
                    },
                }), "rid-ops"
            raise AssertionError(f"unexpected message: {message[:120]}")

        monkeypatch.setattr(executors.agents, "invoke_agent_raw", fake_invoke)
        monkeypatch.setattr(
            executors.agents,
            "classify_response",
            lambda text, expected_schema=None: "final" if text.lstrip().startswith("{") else "question",
        )

        result = await executors._invoke_parallel_terraform_generation(
            '{"preferred_language":"zh-TW","project_name":"demo"}',
            '{"resources":[]}',
        )

        assert result.status.value == "success"
        assert result.main_tf.startswith("resource")
        assert result.versions_tf.startswith("terraform")
        assert result.readme_md == "# demo"
        assert "unit_basic.tftest.hcl" in result.test_files
        assert len(calls) == 3

    def test_build_terraform_stage_prompt_uses_compact_ops_context(self):
        from orchestrator_app import executors

        prompt = executors._build_terraform_stage_prompt(
            spec_json='{"preferred_language":"zh-TW","project_name":"demo"}',
            approved_manifest_json='{"resources":[]}',
            stage_name='ops',
            description='terragrunt wrappers, README, and terraform tests',
            required_keys=['terragrunt_root_hcl', 'readme_md', 'test_files'],
            completed_payloads={
                'foundation': {
                    'variables_tf': 'variable "project_name" {}\nvariable "environment" {}',
                    'providers_tf': 'provider "azurerm" {}',
                    'resource_manifest': {'resources': [{'resource_type': 'azurerm_resource_group', 'name': 'rg'}]},
                },
                'main': {
                    'main_tf': 'resource "azurerm_resource_group" "rg" {}\nresource "azurerm_storage_account" "st" {}',
                    'outputs_tf': 'output "rg_name" {}',
                },
            },
        )

        assert 'completed_bundles_context.json' in prompt
        assert 'foundation_summary' in prompt
        assert 'main_summary' in prompt
        assert 'resource "azurerm_resource_group" "rg" {}' not in prompt
