"""
repair_feedback.py — 統一產生 agent 修復回合的上下游錯誤摘要。

用途：
  - Diagram render regen
  - Terraform validate/fix

讓上游 agent 能以一致格式收到：
  - 目前是第幾次修復
  - 上游 / 下游元件名稱
  - 真實失敗訊息
  - 執行 log
  - 必須維持的契約資料（例如 approved_resource_manifest）
"""

from __future__ import annotations

import json
from typing import Any


def build_repair_context_json(
    *,
    repair_kind: str,
    attempt: int,
    max_attempts: int,
    upstream_agent: str,
    downstream_component: str,
    failure_message: str,
    execution_log: str = "",
    contract_name: str = "",
    contract_payload: str = "",
    preserved_invariants: list[str] | None = None,
    latest_artifact_name: str = "",
    latest_artifact_content: str = "",
    extra_context: dict[str, Any] | None = None,
) -> str:
    """建立 machine-to-machine 修復摘要 JSON。"""
    payload: dict[str, Any] = {
        "repair_kind": repair_kind,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "upstream_agent": upstream_agent,
        "downstream_component": downstream_component,
        "failure_message": failure_message,
        "execution_log": execution_log,
        "preserved_invariants": preserved_invariants or [],
    }

    if latest_artifact_name:
        payload["latest_failed_artifact"] = {
            "name": latest_artifact_name,
            "content": latest_artifact_content,
        }

    if contract_name:
        payload["contract"] = {
            "name": contract_name,
            "payload": _try_parse_json(contract_payload),
        }

    if extra_context:
        payload["extra_context"] = extra_context

    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_repair_context_block(
    repair_context_json: str,
    *,
    heading: str,
    language: str = "json",
) -> str:
    """將 repair context JSON 包成可直接嵌入 prompt 的區塊。"""
    if not repair_context_json:
        return ""
    return (
        f"{heading}\n"
        f"```{language}\n{repair_context_json}\n```\n\n"
    )


def render_prompt_block(
    heading: str,
    content: str,
    *,
    language: str = "",
) -> str:
    """將任意內容包成 prompt 區塊；language 空字串時使用無語言 fenced block。"""
    fence = f"```{language}" if language else "```"
    return (
        f"{heading}\n"
        f"{fence}\n{content}\n```\n\n"
    )


def build_terraform_repair_payload(
    *,
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
    spec_heading: str,
    manifest_heading: str,
    main_heading: str,
    variables_heading: str,
    outputs_heading: str,
    locals_heading: str,
    versions_heading: str,
    providers_heading: str,
    error_heading: str,
    repair_context_heading: str,
) -> str:
    """建立 Terraform 修復 prompt 中共用的 payload 區塊。"""
    extra_files = ""
    if previous_locals_tf:
        extra_files += render_prompt_block(locals_heading, previous_locals_tf, language="hcl")
    if previous_versions_tf:
        extra_files += render_prompt_block(versions_heading, previous_versions_tf, language="hcl")
    if previous_providers_tf:
        extra_files += render_prompt_block(providers_heading, previous_providers_tf, language="hcl")

    repair_context_block = render_repair_context_block(
        repair_context_json,
        heading=repair_context_heading,
    )

    return (
        f"{render_prompt_block(spec_heading, spec_json, language='json')}"
        f"{render_prompt_block(manifest_heading, approved_resource_manifest_json, language='json')}"
        f"{render_prompt_block(main_heading, previous_main_tf, language='hcl')}"
        f"{render_prompt_block(variables_heading, previous_variables_tf, language='hcl')}"
        f"{render_prompt_block(outputs_heading, previous_outputs_tf, language='hcl')}"
        f"{extra_files}"
        f"{render_prompt_block(error_heading, validation_error, language='text')}"
        f"{repair_context_block}"
    )


def build_diagram_repair_payload(
    *,
    spec_json: str,
    architecture_details_json: str,
    previous_diagram_py: str,
    render_error: str,
    render_log: str,
    previous_approved_resource_manifest_json: str,
    available_classes_summary: str,
    repair_context_json: str = "",
    spec_heading: str,
    architecture_heading: str,
    diagram_heading: str,
    error_heading: str,
    render_log_heading: str,
    manifest_heading: str,
    classes_heading: str,
    repair_context_heading: str,
) -> str:
    """建立 Diagram 修復 prompt 中共用的 payload 區塊。"""
    repair_context_block = render_repair_context_block(
        repair_context_json,
        heading=repair_context_heading,
    )

    return (
        f"{render_prompt_block(spec_heading, spec_json, language='json')}"
        f"{render_prompt_block(architecture_heading, architecture_details_json, language='json')}"
        f"{render_prompt_block(diagram_heading, previous_diagram_py, language='python')}"
        f"{render_prompt_block(error_heading, render_error, language='text')}"
        f"{render_prompt_block(render_log_heading, render_log, language='text')}"
        f"{repair_context_block}"
        f"{render_prompt_block(manifest_heading, previous_approved_resource_manifest_json, language='json')}"
        f"{render_prompt_block(classes_heading, available_classes_summary, language='text')}"
    )


def _try_parse_json(value: str) -> Any:
    if not value:
        return ""
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value
