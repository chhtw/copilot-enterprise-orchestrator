"""
hybrid_agents.py — 混合 provider 路由。

Terraform / Diagram：本地 MAF agent + GitHub Copilot provider
其他 agents：沿用 Foundry Responses API
"""

from __future__ import annotations

from typing import Any

from . import copilot_local_agents as copilot_agents
from . import foundry_agents

ARCHITECTURE_AGENT = foundry_agents.ARCHITECTURE_AGENT
TERRAFORM_AGENT = foundry_agents.TERRAFORM_AGENT
DIAGRAM_AGENT = foundry_agents.DIAGRAM_AGENT
PRICING_STRUCTURE_AGENT = foundry_agents.PRICING_STRUCTURE_AGENT
PRICING_BROWSER_AGENT = foundry_agents.PRICING_BROWSER_AGENT


def _is_copilot_local_agent(agent_name: str) -> bool:
    return agent_name in {TERRAFORM_AGENT, DIAGRAM_AGENT}


async def invoke_agent_raw(
    agent_name: str,
    message: str,
    *,
    previous_response_id: str | None = None,
    **kwargs: Any,
) -> tuple[str, str]:
    if _is_copilot_local_agent(agent_name):
        return await copilot_agents.invoke_agent_raw(
            agent_name,
            message,
            previous_response_id=previous_response_id,
            **kwargs,
        )
    return await foundry_agents.invoke_agent_raw(
        agent_name,
        message,
        previous_response_id=previous_response_id,
        **kwargs,
    )


def classify_response(text: str, expected_schema: type | None = None) -> str:
    return foundry_agents.classify_response(text, expected_schema)


def build_terraform_prompt(spec_json: str, approved_resource_manifest_json: str) -> str:
    return copilot_agents.build_terraform_prompt(spec_json, approved_resource_manifest_json)


def build_terraform_fix_prompt(
    spec_json: str,
    approved_resource_manifest_json: str,
    previous_main_tf: str,
    previous_variables_tf: str,
    previous_outputs_tf: str,
    validation_error: str,
    previous_locals_tf: str = "",
    previous_versions_tf: str = "",
    previous_providers_tf: str = "",
    repair_context_json: str = "",
) -> str:
    return copilot_agents.build_terraform_fix_prompt(
        spec_json=spec_json,
        approved_resource_manifest_json=approved_resource_manifest_json,
        previous_main_tf=previous_main_tf,
        previous_variables_tf=previous_variables_tf,
        previous_outputs_tf=previous_outputs_tf,
        validation_error=validation_error,
        previous_locals_tf=previous_locals_tf,
        previous_versions_tf=previous_versions_tf,
        previous_providers_tf=previous_providers_tf,
        repair_context_json=repair_context_json,
    )


def build_diagram_prompt(spec_json: str, architecture_details_json: str = "{}") -> str:
    return copilot_agents.build_diagram_prompt(spec_json, architecture_details_json)


def build_diagram_regen_prompt(
    spec_json: str,
    architecture_details_json: str,
    previous_diagram_py: str,
    render_error: str,
    available_classes_summary: str,
    previous_approved_resource_manifest_json: str = "{}",
    render_log: str = "",
    regen_attempt: int = 1,
    repair_context_json: str = "",
) -> str:
    return copilot_agents.build_diagram_regen_prompt(
        spec_json=spec_json,
        architecture_details_json=architecture_details_json,
        previous_diagram_py=previous_diagram_py,
        render_error=render_error,
        available_classes_summary=available_classes_summary,
        previous_approved_resource_manifest_json=previous_approved_resource_manifest_json,
        render_log=render_log,
        regen_attempt=regen_attempt,
        repair_context_json=repair_context_json,
    )


parse_terraform_output = foundry_agents.parse_terraform_output
parse_diagram_output = foundry_agents.parse_diagram_output
parse_pricing_structure_output = foundry_agents.parse_pricing_structure_output
parse_pricing_output = foundry_agents.parse_pricing_output

build_architecture_clarification_prompt = foundry_agents.build_architecture_clarification_prompt
build_pricing_structure_prompt = foundry_agents.build_pricing_structure_prompt
build_pricing_browser_prompt = foundry_agents.build_pricing_browser_prompt
