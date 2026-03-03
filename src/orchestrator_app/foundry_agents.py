"""
foundry_agents.py — 呼叫 Microsoft Foundry Project 內的 specialist agents。

策略：
  1. 用 AIProjectClient.agents.get() 取得 agent 定義（model, instructions, tools）
  2. 用 AIProjectClient.get_openai_client() 取得 AsyncOpenAI
  3. 透過 Responses API (openai.responses.create) 以相同的 model + instructions + tools 發送請求

**僅使用 Responses API**：不使用 Assistants / Threads / Runs。
**禁止 A2A 串接**：直接用 Foundry project endpoint + 認證 + agent name 呼叫。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from azure.identity.aio import DefaultAzureCredential
from azure.ai.projects.aio import AIProjectClient

from opentelemetry import trace as _otel_trace

from .contracts import (
    CostLineItem,
    CostOutput,
    CostStructureOutput,
    DiagramOutput,
    ResourceManifest,
    StepStatus,
    TerraformOutput,
)

logger = logging.getLogger(__name__)

# ── OTel tracer ──
_tracer = _otel_trace.get_tracer("ccoe-orchestrator.foundry_agents")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
PROJECT_ENDPOINT = os.getenv(
    "AZURE_AI_PROJECT_ENDPOINT",
    "https://aif-ch-cht-ccoe-ai-agent.services.ai.azure.com/api/projects/ArchitectAgent",
)
CLARIFICATION_AGENT = os.getenv("CLARIFICATION_AGENT_NAME", "Architecture-Clarification-Agent")
TERRAFORM_AGENT = os.getenv("TERRAFORM_AGENT_NAME", "Azure-Terraform-Architect-Agent")
DIAGRAM_AGENT = os.getenv("DIAGRAM_AGENT_NAME", "DaC-Dagrams-Mingrammer")
COST_AGENT = os.getenv("COST_AGENT_NAME", "Agent-AzureCalculator")
COST_BROWSER_AGENT = os.getenv("COST_BROWSER_AGENT_NAME", "Agent-AzureCalculator-BrowserAuto")

MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES", "2"))
RETRY_DELAY = float(os.getenv("AGENT_RETRY_DELAY", "5.0"))

# HTTP timeout（秒）— 預設 120s
AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "120"))
# Diagram agent（生成完整 Python diagrams 程式碼）超時
DIAGRAM_AGENT_TIMEOUT = float(os.getenv("DIAGRAM_AGENT_TIMEOUT", str(max(300, AGENT_TIMEOUT))))
# Terraform agent（生成完整 IaC）超時
TERRAFORM_AGENT_TIMEOUT = float(os.getenv("TERRAFORM_AGENT_TIMEOUT", str(max(300, AGENT_TIMEOUT))))
# Cost Structure agent（架構→成本結構轉換）超時
COST_STRUCTURE_TIMEOUT = float(os.getenv("COST_STRUCTURE_AGENT_TIMEOUT", "300"))
# Cost Browser agent（browser automation）超時
COST_BROWSER_TIMEOUT = float(os.getenv("COST_BROWSER_AGENT_TIMEOUT", "600"))

# Responses API 支援的 tool type（不支援的會被過濾）
_SUPPORTED_TOOL_TYPES = {"web_search_preview", "code_interpreter", "browser_automation_preview"}

# YAML declarative agent 目錄
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# YAML tool kind → Responses API tool type 對應
_YAML_KIND_TO_TOOL_TYPE: dict[str, str] = {
    "WebSearch": "web_search_preview",
    "CodeInterpreter": "code_interpreter",
    "BrowserAutomation": "browser_automation_preview",
}

# 某些 model 不支援特定 tool；遇到時移除該 tool 而非整個失敗
_MODEL_TOOL_EXCLUSIONS: dict[str, set[str]] = {
    "gpt-5.2-codex": {"code_interpreter"},
    "gpt-5.1-codex": {"code_interpreter"},
    "gpt-5.2-codex-max": {"code_interpreter"},
    "gpt-5.1-codex-max": {"code_interpreter"},
    "gpt-5.3-codex": {"code_interpreter"},
}


# ---------------------------------------------------------------------------
# Agent Definition Cache
# ---------------------------------------------------------------------------

def _to_plain_dict(obj: Any) -> Any:
    """
    將 SDK model 物件（_model_base.Model / MutableMapping）遞迴轉為 plain dict/list/str。
    確保可以被 JSON 序列化並傳給 Responses API。
    """
    if isinstance(obj, dict):
        return {k: _to_plain_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain_dict(item) for item in obj]
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if hasattr(obj, "items"):  # MutableMapping-like
        return {k: _to_plain_dict(v) for k, v in obj.items()}
    return obj


@dataclass
class _AgentDef:
    """從 Foundry agent 定義中萃取的 Responses API 所需欄位。"""
    model: str
    instructions: str
    tools: list[dict[str, Any]] = field(default_factory=list)


_agent_def_cache: dict[str, _AgentDef] = {}


def _load_agent_def_from_yaml(agent_name: str) -> _AgentDef | None:
    """
    從 prompts/{agent_name}.yaml 載入 agent 定義。

    YAML 格式（declarative agent）：
      kind: Prompt
      name: <agent-name>
      instructions: |
        ...
      model:
        id: <model-id>
      tools:
        - kind: WebSearch
        - kind: CodeInterpreter
        - kind: BrowserAutomation

    Returns:
        _AgentDef if YAML exists and is valid, None otherwise.
    """
    yaml_path = _PROMPTS_DIR / f"{agent_name}.yaml"
    if not yaml_path.exists():
        return None

    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except Exception as exc:
        logger.warning("[YAML] Failed to parse %s: %s", yaml_path, exc)
        return None

    if not isinstance(doc, dict) or doc.get("kind") != "Prompt":
        logger.warning("[YAML] Invalid kind in %s (expected 'Prompt')", yaml_path)
        return None

    model_cfg = doc.get("model", {})
    model = model_cfg.get("id", "gpt-5.2") if isinstance(model_cfg, dict) else "gpt-5.2"
    instructions = doc.get("instructions", "") or ""

    # 轉換 YAML tools → Responses API tools
    yaml_tools: list = doc.get("tools") or []
    exclusions = _MODEL_TOOL_EXCLUSIONS.get(model, set())
    tools: list[dict[str, Any]] = []
    for t in yaml_tools:
        kind = t.get("kind", "") if isinstance(t, dict) else ""
        tool_type = _YAML_KIND_TO_TOOL_TYPE.get(kind)
        if not tool_type:
            logger.warning("[YAML] Unknown tool kind=%s in %s, skipping", kind, agent_name)
            continue
        if tool_type not in _SUPPORTED_TOOL_TYPES:
            logger.warning("[YAML] Unsupported tool type=%s for agent=%s", tool_type, agent_name)
            continue
        if tool_type in exclusions:
            logger.warning("[YAML] Skipping tool type=%s (incompatible with model=%s)", tool_type, model)
            continue
        tool_dict: dict[str, Any] = {"type": tool_type}
        # 保留 kind 以外的額外屬性
        for k, v in t.items():
            if k != "kind":
                tool_dict[k] = v
        tools.append(tool_dict)

    result = _AgentDef(model=model, instructions=instructions, tools=tools)
    logger.info(
        "[YAML] Loaded agent def from YAML: name=%s model=%s tools=%s instructions_len=%d",
        agent_name, model, [t["type"] for t in tools], len(instructions),
    )
    return result


async def _get_agent_def(client: AIProjectClient, agent_name: str) -> _AgentDef:
    """
    取得 agent 定義（含快取）。

    優先順序：
      1. 記憶體快取
      2. 本地 YAML 檔案（prompts/{agent_name}.yaml）— source of truth
      3. Foundry API fallback（agents.get → versions.latest.definition）
    """
    if agent_name in _agent_def_cache:
        return _agent_def_cache[agent_name]

    # --- 嘗試從 YAML 載入（source of truth）---
    yaml_def = _load_agent_def_from_yaml(agent_name)
    if yaml_def is not None:
        _agent_def_cache[agent_name] = yaml_def
        return yaml_def

    # --- Fallback: Foundry API ---
    logger.info("[Foundry] YAML not found for %s, falling back to Foundry API", agent_name)
    agent = await client.agents.get(agent_name)
    defn = agent["versions"]["latest"]["definition"]

    model = defn.get("model") or "gpt-5.2"
    instructions = defn.get("instructions") or ""

    raw_tools: list = defn.get("tools") or []
    exclusions = _MODEL_TOOL_EXCLUSIONS.get(model, set())
    tools: list[dict[str, Any]] = []
    for t in raw_tools:
        t_dict = _to_plain_dict(t)
        t_type = t_dict.get("type", "")
        if t_type not in _SUPPORTED_TOOL_TYPES:
            logger.warning("[Foundry] Skipping unsupported tool type=%s for agent=%s", t_type, agent_name)
            continue
        if t_type in exclusions:
            logger.warning("[Foundry] Skipping tool type=%s (incompatible with model=%s)", t_type, model)
            continue
        tools.append(t_dict)

    result = _AgentDef(model=model, instructions=instructions, tools=tools)
    _agent_def_cache[agent_name] = result
    logger.info(
        "[Foundry] Cached agent def (API fallback): name=%s model=%s tools=%s instructions_len=%d",
        agent_name, model, [t["type"] for t in tools], len(instructions),
    )
    return result


# ---------------------------------------------------------------------------
# Helper: 呼叫 Foundry Agent (Responses API) — 支援 multi-turn
# ---------------------------------------------------------------------------
async def _invoke_foundry_agent(
    agent_name: str,
    message: str,
    *,
    previous_response_id: str | None = None,
    store: bool = True,
    retries: int = MAX_RETRIES,
    delay: float = RETRY_DELAY,
    timeout: float | None = None,
) -> tuple[str, str]:
    """
    以 Responses API 呼叫 Foundry Project 內的 agent。

    流程：
      1. agents.get(name) → 取得 model / instructions / tools
      2. openai.responses.create(model=..., input=message, store=True,
         previous_response_id=...)  ← multi-turn 透過 Responses API 原生支援
      3. 從 response.output_text 取得回應文字

    Returns:
        (response_text, response_id) — response_id 用於後續 multi-turn 呼叫
    """
    # 依 agent 決定預設 timeout
    if timeout is None:
        if agent_name == COST_BROWSER_AGENT:
            timeout = COST_BROWSER_TIMEOUT
        elif agent_name == COST_AGENT:
            timeout = COST_STRUCTURE_TIMEOUT
        elif agent_name == DIAGRAM_AGENT:
            timeout = DIAGRAM_AGENT_TIMEOUT
        elif agent_name == TERRAFORM_AGENT:
            timeout = TERRAFORM_AGENT_TIMEOUT
        else:
            timeout = AGENT_TIMEOUT

    last_error: Exception | None = None

    for attempt in range(1, retries + 2):
        credential = DefaultAzureCredential()
        try:
            with _tracer.start_as_current_span(
                f"invoke_foundry_agent:{agent_name}",
                attributes={
                    "agent.name": agent_name,
                    "agent.attempt": attempt,
                    "agent.max_retries": retries,
                    "agent.timeout_s": timeout,
                    "agent.has_prev_response": bool(previous_response_id),
                },
            ) as span:
                print(
                    f"  [Foundry] 呼叫 agent={agent_name} (attempt {attempt}/{retries + 1}, timeout={timeout}s) …",
                    flush=True,
                )
                logger.info(
                    "[Foundry] Invoking agent=%s attempt=%d/%d prev_id=%s",
                    agent_name, attempt, retries + 1, previous_response_id or "(none)",
                )
                async with AIProjectClient(
                    endpoint=PROJECT_ENDPOINT,
                    credential=credential,
                ) as client:
                    # 1. 取得 agent 定義
                    agent_def = await _get_agent_def(client, agent_name)
                    span.set_attribute("agent.model", agent_def.model)
                    span.set_attribute("agent.tools_count", len(agent_def.tools))

                    # 2. 取得 OpenAI client（Responses API）
                    openai = client.get_openai_client()

                    # 3. 建構 responses.create 參數
                    create_kwargs: dict[str, Any] = {
                        "model": agent_def.model,
                        "input": message,
                        "store": store,
                    }
                    if previous_response_id:
                        create_kwargs["previous_response_id"] = previous_response_id
                    if agent_def.instructions:
                        create_kwargs["instructions"] = agent_def.instructions
                    if agent_def.tools:
                        create_kwargs["tools"] = agent_def.tools

                    # 4. 呼叫 Responses API（帶 timeout 防止 408）
                    response = await openai.responses.create(
                        **create_kwargs,
                        timeout=timeout,
                    )

                    # 5. 取得回應文字 + response.id (multi-turn 鏈)
                    response_text = response.output_text or ""
                    response_id = response.id or ""
                    span.set_attribute("agent.response_length", len(response_text))
                    span.set_attribute("agent.response_id", response_id)
                    logger.info(
                        "[Foundry] agent=%s model=%s response_length=%d response_id=%s",
                        agent_name, agent_def.model, len(response_text), response_id,
                    )

                    if not response_text.strip():
                        raise RuntimeError("Empty response from Responses API")

                    print(
                        f"  [Foundry] ✅ agent={agent_name} 回覆完成 (response_length={len(response_text)})",
                        flush=True,
                    )
                    return response_text, response_id

        except Exception as exc:
            last_error = exc
            print(
                f"  [Foundry] ⚠️ agent={agent_name} attempt {attempt} 失敗: {exc}",
                flush=True,
            )
            logger.warning(
                "[Foundry] agent=%s attempt=%d failed: %s",
                agent_name, attempt, exc,
            )
            if attempt <= retries:
                wait = delay * (2 ** (attempt - 1))
                logger.info("[Foundry] Retrying in %.1fs...", wait)
                await asyncio.sleep(wait)
        finally:
            await credential.close()

    raise RuntimeError(
        f"Agent '{agent_name}' failed after {retries + 1} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# classify_response: 判斷 agent 回應是「最終結果」還是「追問使用者」
# ---------------------------------------------------------------------------
def classify_response(text: str, expected_schema: type | None = None) -> str:
    """
    分類 agent 回應。

    策略（無需 LLM 呼叫的快速分類）：
      1. 嘗試 _extract_json — 若成功解析且包含 expected_schema 的必要 key → "final"
      2. 否則 → "question" (agent 正在追問)

    Returns:
        "final" | "question"
    """
    try:
        data = _extract_json(text)
        # 若能解析出 JSON dict 且看起來像結構化輸出 → 視為最終結果
        if isinstance(data, dict) and len(data) > 0:
            return "final"
    except (ValueError, json.JSONDecodeError):
        pass
    return "question"


# ---------------------------------------------------------------------------
# Public API: raw invoke + classify (for multi-turn executors)
# ---------------------------------------------------------------------------
# Re-export for uniform interface (executors import from agents module)
invoke_agent_raw = _invoke_foundry_agent


# ---------------------------------------------------------------------------
# Prompt builders (extracted for multi-turn — executor builds first prompt,
# then sends user answers directly in subsequent turns)
# ---------------------------------------------------------------------------
def build_terraform_prompt(spec_json: str, approved_resource_manifest_json: str) -> str:
    return (
        "【角色】你是資深 Azure Terraform/Terragrunt 架構師與 IaC Reviewer。\n\n"
        "【任務】根據 spec.json 與已核准的架構資源清單（approved_resource_manifest.json），"
        "輸出「可直接落地」的 Terraform 專案，嚴格遵循 AVM only、全 Private、"
        "RBAC、Tags、Naming Convention。\n\n"
        "【硬性規則 — 必守】\n"
        "1. Terraform Only + AVM Only：\n"
        "   - 所有 module 必須使用 Azure Verified Modules (AVM):\n"
        "     Resource: https://azure.github.io/Azure-Verified-Modules/indexes/terraform/tf-resource-modules/\n"
        "     Pattern:  https://azure.github.io/Azure-Verified-Modules/indexes/terraform/tf-pattern-modules/\n"
        "   - module source 必須符合 Azure/avm-.../azurerm 格式。\n"
        "   - 若無對應 AVM，才可退回 azurerm_* resource，並在 README notes 段落說明缺口原因。\n"
        "2. 版本固定（必做）：每個 module block 必須包含 version = \"x.y.z\" 或 ~> 約束，不得省略。\n"
        "   **Provider 版本一致性**：versions.tf 中宣告的 azurerm provider 版本約束，\n"
        "   必須與所有 AVM module 內部要求的 provider 版本相容。\n"
        "   例如：若使用 azurerm ~> 4.0，則所有 AVM module 版本也必須相容 azurerm 4.x；\n"
        "   反之若使用 azurerm ~> 3.x，module 版本也須相容 3.x。\n"
        "   請至 https://registry.terraform.io 確認各 AVM module 對 azurerm 的版本要求。\n"
        "3. 全 Private 預設（必做）：\n"
        "   - 關閉所有 public network access（除非 spec 明確允許）。\n"
        "   - PaaS 服務必須配置 Private Endpoint + Private DNS Zone + VNet link。\n"
        "   - 禁止 Public IP / Internet exposure。\n"
        "4. RBAC（必做）：包含 deploy identity 與 workload identity 的最小權限角色指派。\n"
        "5. Tags + Naming（必做）：使用 locals 集中管理；命名格式 {env}-{project}-{service}-{region}。\n"
        "6. Terragrunt multi-env（必做）：結構包含 /envs/dev 與 /envs/prod。\n"
        "   - /src/workload/versions.tf 是唯一的 required_version 與 required_providers 宣告。\n"
        "   - terragrunt generate 僅允許產生 backend.tf（remote state），避免重複宣告。\n\n"
        "【必須輸出的檔案結構】\n"
        "/terragrunt.hcl\n"
        "/envs/dev/terragrunt.hcl, /envs/prod/terragrunt.hcl\n"
        "/src/workload/versions.tf, providers.tf, main.tf, variables.tf, locals.tf, outputs.tf\n"
        "/src/workload/terraform.tfvars.example\n"
        "/README.md（含架構摘要、部署說明、AVM 清單、全 private 設計、RBAC、Tags/Naming）\n\n"
        "【輸出格式 — JSON（MAF 工作流程必要）】\n"
        "你的整個回應必須是一個 JSON 物件（或以 ```json 包住），包含以下 key：\n"
        '- "main_tf": /src/workload/main.tf 完整內容（string）\n'
        '- "variables_tf": /src/workload/variables.tf 完整內容（string）\n'
        '- "outputs_tf": /src/workload/outputs.tf 完整內容（string）\n'
        '- "locals_tf": /src/workload/locals.tf 完整內容（string）\n'
        '- "versions_tf": /src/workload/versions.tf 完整內容（string）\n'
        '- "providers_tf": /src/workload/providers.tf 完整內容（string）\n'
        '- "terragrunt_root_hcl": 根目錄 terragrunt.hcl（string）\n'
        '- "terragrunt_dev_hcl": /envs/dev/terragrunt.hcl（string）\n'
        '- "terragrunt_prod_hcl": /envs/prod/terragrunt.hcl（string）\n'
        '- "readme_md": README.md 完整內容，含 Version Evidence、部署說明（string）\n'
        '- "resource_manifest": JSON object（非 string）\n'
        "  包含 project_name, resources[], terraform_version, provider_version；\n"
        "  每個 resource entry: resource_type, name, display_name, sku, region, properties。\n\n"
        "IMPORTANT:\n"
        "- approved_resource_manifest.json 是資源的唯一真相來源。\n"
        "- 若 spec 與 approved_resource_manifest 衝突，以 approved_resource_manifest 為準，"
        "  並在 README notes 記錄差異。\n"
        "- Generate COMPLETE code — 不得使用 placeholder 或 TODO。\n"
        "- 禁止輸出 Bicep / ARM / Pulumi。\n\n"
        f"spec.json:\n{spec_json}\n\n"
        f"approved_resource_manifest.json:\n{approved_resource_manifest_json}"
    )


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
) -> str:
    """
    建構「Terraform 驗證修正」prompt，讓 Terraform Agent
    根據 terraform init/validate 的錯誤訊息修正 Terraform 程式碼。
    """
    extra_files = ""
    if previous_locals_tf:
        extra_files += (
            "--- locals.tf ---\n"
            f"```hcl\n{previous_locals_tf}\n```\n\n"
        )
    if previous_versions_tf:
        extra_files += (
            "--- versions.tf ---\n"
            f"```hcl\n{previous_versions_tf}\n```\n\n"
        )
    if previous_providers_tf:
        extra_files += (
            "--- providers.tf ---\n"
            f"```hcl\n{previous_providers_tf}\n```\n\n"
        )

    return (
        "【角色】你是資深 Azure Terraform/Terragrunt 架構師與 IaC Reviewer。\n\n"
        "【驗證修正任務】\n"
        "你先前產生的 Terraform 程式碼在本地執行 `terraform init -backend=false && terraform validate` 時失敗了。\n"
        "請根據以下錯誤訊息修正並重新產生完整的 Terraform 程式碼。\n\n"
        "**重要規則**：\n"
        "1. 修正錯誤的同時，必須保持原本的架構設計邏輯與資源不變。\n"
        "2. 所有 AVM module、version 固定、Private Endpoint、RBAC、Tags/Naming 等硬性規則仍需遵守。\n"
        "3. 確保 required_version、required_providers 宣告正確。\n"
        "4. 確保所有 module/resource 的參數名稱與型別與 AVM/Provider 文件一致。\n"
        "5. 修正後的程式碼必須可通過 `terraform init -backend=false && terraform validate`。\n"
        "6. 只修正導致錯誤的部分，不要無故變更其他程式碼。\n"
        "7. **Provider 版本一致性（最重要）**：如果錯誤涉及 provider 版本衝突（例如 'no available releases match the given constraints'），\n"
        "   你必須同時更新 versions.tf 中的 required_providers 版本約束，**以及** main.tf 中所有 AVM module 的 version，\n"
        "   確保整個專案使用同一個 provider 主版本。例如：若使用 azurerm ~> 4.0，則所有 AVM module 版本也必須相容 azurerm 4.x。\n"
        "   反之若使用 azurerm ~> 3.x，則所有 AVM module 版本也必須相容 azurerm 3.x。\n"
        "   請至 https://registry.terraform.io 查找各 AVM module 與 azurerm provider 的相容版本。\n\n"
        "【上次產生的 Terraform 程式碼】\n\n"
        "--- main.tf ---\n"
        f"```hcl\n{previous_main_tf}\n```\n\n"
        "--- variables.tf ---\n"
        f"```hcl\n{previous_variables_tf}\n```\n\n"
        "--- outputs.tf ---\n"
        f"```hcl\n{previous_outputs_tf}\n```\n\n"
        f"{extra_files}"
        "【terraform init/validate 錯誤訊息】\n"
        f"```\n{validation_error}\n```\n\n"
        "【輸出格式 — JSON（MAF 工作流程必要）】\n"
        "你的整個回應必須是一個 JSON 物件（或以 ```json 包住），包含以下 key：\n"
        '- "main_tf": 修正後的 main.tf 完整內容（string）\n'
        '- "variables_tf": 修正後的 variables.tf 完整內容（string）\n'
        '- "outputs_tf": 修正後的 outputs.tf 完整內容（string）\n'
        '- "locals_tf": 修正後的 locals.tf 完整內容（string）\n'
        '- "versions_tf": 修正後的 versions.tf 完整內容（string）\n'
        '- "providers_tf": 修正後的 providers.tf 完整內容（string）\n'
        '- "resource_manifest": JSON object（非 string），結構同原始要求\n'
        '- "fix_summary": 簡述修正了哪些問題（string）\n\n'
        "IMPORTANT:\n"
        "- 回傳 COMPLETE code — 不得使用 placeholder 或 TODO。\n"
        "- 禁止輸出 Bicep / ARM / Pulumi。\n"
        "- approved_resource_manifest.json 仍是資源的唯一真相來源。\n\n"
        f"spec.json:\n{spec_json}\n\n"
        f"approved_resource_manifest.json:\n{approved_resource_manifest_json}"
    )


def build_diagram_prompt(spec_json: str, architecture_details_json: str = "{}") -> str:
    return (
        "【角色】你是使用 Python Diagrams（mingrammer）的雲端架構圖產出專家。\n\n"
        "TASK: Generate a Python architecture diagram script using the 'diagrams' library (mingrammer).\n"
        "This is an ARCHITECTURE DIAGRAM task — NOT a pricing or cost task.\n\n"
        "【MAF 工作流程說明 — 重要】\n"
        "此架構圖將由使用者核准後，才會下游並行執行 Terraform 與成本計算。因此：\n"
        "1. approved_resource_manifest 必須完整精確，是後續 Terraform + 成本的唯一真相來源。\n"
        "2. 必須涵蓋 spec.json 和 architecture_details.json 中描述的所有服務與安全要求。\n"
        "3. 安全控制（HSM/Key Vault、WAF、Private Endpoint、DDoS 等）必須在圖中以獨立節點呈現，\n"
        "   並以帶標籤的連線表示 traffic flow（例如 Edge(label=\"HTTPS 443\")）。\n\n"
        "【Azure Import Allowlist 規則（必守）】\n"
        "僅能匯入 https://diagrams.mingrammer.com/docs/nodes/azure 官方文件中有記載的類別。\n"
        "禁止使用文件未列出的類別（例如 Bastion, PrivateDnsResolver, AMPLS）。\n"
        "必要的替代對應：\n"
        "  - Private DNS Resolver → DNSPrivateZones（在 Assumptions 說明）\n"
        "  - Azure Bastion → VirtualNetworkGateways（在 Assumptions 說明）\n"
        "  - AMPLS → Monitor（在 Assumptions 說明）\n\n"
        "You MUST return a valid JSON object (or wrapped in ```json block) with these keys:\n"
        '- "diagram_py": complete Python script \n'
        '  * Only import Azure nodes from the official allowlist (including documented aliases AKS/VMSS/ACR).\n'
        '  * Use Cluster("Region / VNet / Subnet") for network boundary grouping.\n'
        '  * Use Edge(label="HTTPS 443") etc. for all connections — ALL edges must be labeled.\n'
        '  * Use direction="LR", outformat="png", show=False in Diagram().\n'
        '  * Keep identifiers short and consistent.\n'
        '- "render_log": short build log string (e.g. "diagram rendered successfully")\n'
        '- "assumptions": list of node substitutions made (e.g. "Bastion → VirtualNetworkGateways")\n'
        '- "approved_resource_manifest": machine-readable resource list for downstream TF/cost.\n'
        '  Each entry must include: resource_type, name, display_name, sku, region, properties.\n'
        '- "diagram_image_base64": empty string "" (orchestrator renders locally via graphviz)\n\n'
        "IMPORTANT:\n"
        "- Do NOT ask clarifying questions — use architecture_details.json as the complete requirements.\n"
        "- Do NOT produce pricing or cost output.\n"
        "- Validate ALL imported classes against the official node list before including them.\n"
        "- No dangling nodes; all edges labeled with protocol/port.\n\n"
        f"spec.json:\n{spec_json}\n\n"
        f"architecture_details.json:\n{architecture_details_json}"
    )


def build_diagram_regen_prompt(
    spec_json: str,
    architecture_details_json: str,
    previous_diagram_py: str,
    render_error: str,
    available_classes_summary: str,
) -> str:
    """
    建構「渲染錯誤修正」prompt，讓 Diagram Agent 根據上次錯誤重新產生 diagram.py。

    Args:
        spec_json: 原始 spec JSON
        architecture_details_json: 架構細節 JSON
        previous_diagram_py: 上次生成且渲染失敗的 diagram.py 原始碼
        render_error: 渲染失敗的 stderr / error 訊息
        available_classes_summary: 可用的 diagrams.azure.* class 清單摘要
    """
    return (
        "【角色】你是使用 Python Diagrams（mingrammer）的雲端架構圖產出專家。\n\n"
        "【渲染錯誤修正任務】\n"
        "你先前產生的 diagram.py 在本地渲染時失敗了。\n"
        "請根據以下錯誤訊息與可用類別清單，修正並重新產生完整的 diagram.py。\n\n"
        "**重要規則**：\n"
        "1. 只使用下方「可用類別清單」中列出的類別，不要猜測或編造不存在的類別。\n"
        "2. 如果原本使用的類別不在清單中，請選用最接近的替代類別，並在 assumptions 中說明。\n"
        "3. 確保所有 import 語句正確，模組路徑與類別名稱完全匹配。\n"
        "4. 保持原本的架構邏輯與佈局不變，只修正導致錯誤的部分。\n\n"
        "【上次渲染失敗的 diagram.py】\n"
        f"```python\n{previous_diagram_py}\n```\n\n"
        "【渲染錯誤訊息】\n"
        f"```\n{render_error}\n```\n\n"
        "【可用的 diagrams.azure.* 類別清單】\n"
        f"```\n{available_classes_summary}\n```\n\n"
        "You MUST return a valid JSON object (or wrapped in ```json block) with these keys:\n"
        '- "diagram_py": the COMPLETE corrected Python script (not a diff)\n'
        '- "render_log": short description of what was fixed\n'
        '- "assumptions": list of substitutions or changes made\n'
        '- "approved_resource_manifest": same resource manifest as before '
        "(update only if resource names changed)\n"
        '- "diagram_image_base64": empty string ""\n\n'
        f"spec.json:\n{spec_json}\n\n"
        f"architecture_details.json:\n{architecture_details_json}"
    )


def build_architecture_clarification_prompt(spec_json: str) -> str:
    """Step 0c: 呼叫 Architecture-Clarification-Agent 澄清架構需求細節。"""
    return (
        "【角色】你是雲端架構師助理，負責在架構設計前與使用者確認架構細節，確保需求完整。\n\n"
        "【任務】\n"
        "分析使用者提供的原始需求（spec.json 中的 raw_input 與 notes 欄位），"
        "判斷是否有架構層面的模糊或缺失資訊，並透過有針對性的問答來補齊需求。\n\n"
        "【需要確認的架構面向】\n"
        "1. 核心服務：需要哪些 Azure PaaS/IaaS？"
        "（App Service, AKS, VM, Azure Functions, Container Apps, Event Hub, Service Bus...）\n"
        "2. 資料層：哪種資料庫/快取？（Azure SQL, Cosmos DB, PostgreSQL, MySQL, Redis Cache...）\n"
        "3. 前端/入口點：Application Gateway + WAF? Azure Front Door? CDN? 直接 App Service?\n"
        "4. 安全性：\n"
        "   - Key Vault 是否需要 HSM（Premium 層）？\n"
        "   - 是否需要 Private Endpoint 搭配 Private DNS？\n"
        "   - DDoS Protection Standard？Defender for Cloud？\n"
        "5. 身分識別：Managed Identity（系統/使用者指定）？Entra ID？Azure AD B2C（對外客戶）？\n"
        "6. 網路拓樸：獨立 VNet？Hub-Spoke（是否有 hub 提供）？需要 VPN Gateway / ExpressRoute？\n"
        "7. 合規/法規：PCI DSS? ISO 27001? SOC 2? 需要診斷日誌保留多久？\n"
        "8. 高可用/備援：需要多 region（Active-Active / Active-Passive）？目標 RTO/RPO？\n"
        "9. 監控與可觀測性：Application Insights? Log Analytics Workspace? Azure Monitor Alerts?\n"
        "10. 其他特殊需求：例如電商的 API Management、訂單服務、支付整合等。\n\n"
        "【行為指示】\n"
        "- 若使用者的 raw_input / notes 已清楚涵蓋上述大多數面向，請直接輸出最終 JSON。\n"
        "- 若資訊不足，請以繁體中文提出 3-5 個最關鍵的確認問題（純文字，不含 JSON），"
        "  每題附上推薦預設值，等使用者回答後再輸出 JSON。\n"
        "- 問題要具體，例如：\n"
        "  「您需要 Azure Key Vault 的 HSM 保護嗎？"
        "（建議：是，選 Premium 層；費用約 $5/月 + $0.03/10k 操作）」\n\n"
        "【最終 JSON 輸出格式】\n"
        "當所有細節確認後，輸出 JSON（以 ```json 包住）：\n"
        "{\n"
        '  "architecture_details": {\n'
        '    "core_services": ["App Service", "Azure SQL", ...],\n'
        '    "data_layer": {"database": "Azure SQL", "cache": "Redis Cache", ...},\n'
        '    "ingress": {"type": "Application Gateway", "waf_enabled": true},\n'
        '    "security": {\n'
        '      "key_vault": true, "hsm": false,\n'
        '      "private_endpoints": true, "ddos_protection": false,\n'
        '      "defender_for_cloud": false\n'
        "    },\n"
        '    "identity": {"managed_identity": "system", "b2c": false},\n'
        '    "network": {\n'
        '      "topology": "standalone",\n'
        '      "has_hub": false,\n'
        '      "vpn_gateway": false,\n'
        '      "expressroute": false\n'
        "    },\n"
        '    "compliance": [],\n'
        '    "availability": {"multi_region": false, "rto_minutes": 60, "rpo_minutes": 15},\n'
        '    "monitoring": ["Application Insights", "Log Analytics"],\n'
        '    "special_requirements": "...",\n'
        '    "confirmed_by_user": true\n'
        "  }\n"
        "}\n\n"
        f"spec.json（目前已收集到的規格，請重點分析 raw_input 與 notes 欄位）:\n{spec_json}"
    )


def build_cost_structure_prompt(spec_json: str, resource_manifest_json: str) -> str:
    """Step 3a: 將架構轉換為 Azure Calculator 成本結構（不需要 browser automation）。"""
    return (
        "TASK: Convert the resource manifest into a detailed Azure Pricing Calculator cost structure.\n\n"
        "For EACH resource in the manifest, produce a line-item with ALL of the following fields:\n"
        "- resource_type: Terraform resource type (e.g. azurerm_virtual_machine)\n"
        "- name: Terraform resource name\n"
        "- display_name: human-readable name\n"
        "- sku: the SKU or tier (e.g. Standard_D2s_v5)\n"
        "- region: Azure region\n"
        "- product_id: Azure Pricing Calculator product slug/ID "
        "(e.g. 'virtual-machines-dv5-series', 'azure-firewall-premium'). "
        "Use the exact slugs from https://azure.microsoft.com/en-us/pricing/calculator/\n"
        "- meter: billing meter description (e.g. 'Compute Hours', 'per GB stored/month')\n"
        "- unit: billing unit (e.g. 'hours', 'GB', 'messages')\n"
        "- pricing_tier: one of 'PAYG', '1-year RI', '3-year RI', 'spot' "
        "(match the commitment in spec.json)\n"
        "- quantity: estimated monthly quantity/usage (e.g. 730 hours for 24/7 VM)\n"
        "- estimated_monthly_usd: your best-effort monthly cost estimate in USD\n"
        "- notes: any assumptions about sizing or usage\n\n"
        "You MUST return a valid JSON object (or wrap in ```json``` code block) with these keys:\n"
        '- "line_items": array of line-item objects with ALL fields above\n'
        '- "currency": "USD"\n'
        '- "commitment": commitment type from spec (e.g. "PAYG")\n'
        '- "region": primary region\n'
        '- "total_estimated_monthly_usd": sum of all line-item costs\n'
        '- "notes": overall assumptions or caveats\n\n'
        "IMPORTANT:\n"
        "- Do NOT use browser automation. Use your knowledge of Azure pricing.\n"
        "- If critical pricing parameters (SKU, region, usage) are missing for any resource, "
        "ask the user in PLAIN TEXT (no JSON) listing each missing field with a proposed default value. "
        "If the user confirms or provides values, output the final JSON. "
        "If all parameters are available, output the final JSON directly.\n"
        "- If a resource has no direct cost, include it with estimated_monthly_usd=0.\n"
        "- Ensure product_id values are real Azure Calculator slugs.\n\n"
        f"spec.json:\n{spec_json}\n\n"
        f"resource_manifest.json:\n{resource_manifest_json}"
    )


def build_cost_browser_prompt(cost_structure_json: str) -> str:
    """Step 3b: 使用 Browser Automation 操作 Azure Pricing Calculator。"""
    return (
        "TASK: Use Browser Automation to create an Azure Pricing Calculator estimate.\n\n"
        "You are given a cost_structure.json containing detailed line-items with "
        "product_id, sku, region, quantity, pricing_tier for each Azure resource.\n\n"
        "Steps:\n"
        "1. Navigate to https://azure.microsoft.com/en-us/pricing/calculator/\n"
        "2. For EACH line-item in the cost structure:\n"
        "   a. Search for the product using product_id or display_name\n"
        "   b. Add it to the estimate\n"
        "   c. Configure: region, SKU/tier, pricing_tier (PAYG/RI), quantity\n"
        "3. After adding ALL resources, get the SHARE link (click Share → copy URL)\n"
        "4. Export the estimate as Excel file\n"
        "5. Return the results\n\n"
        "You MUST return a valid JSON object (or wrap in ```json``` code block) with these keys:\n"
        '- "calculator_share_url": the Azure Calculator share link\n'
        '- "estimate_xlsx_base64": base64-encoded exported Excel file (or "" if export failed)\n'
        '- "monthly_estimate_usd": total monthly cost from the calculator (number)\n'
        '- "cost_breakdown": list of {"resource": "...", "sku": "...", "monthly_usd": ...} '
        "from the calculator's output\n\n"
        "IMPORTANT:\n"
        "- Use Browser Automation to interact with the Azure Pricing Calculator website.\n"
        "- Do NOT ask clarifying questions. Work with the data provided.\n"
        "- If browser automation fails for a specific resource, skip it and note in cost_breakdown.\n"
        "- The share URL is critical — make sure to obtain it.\n\n"
        f"cost_structure.json:\n{cost_structure_json}"
    )


# ---------------------------------------------------------------------------
# Parse functions (extracted for multi-turn — executor parses final response)
# ---------------------------------------------------------------------------
def parse_terraform_output(raw_text: str) -> TerraformOutput:
    """Parse raw agent text → TerraformOutput."""
    data = _extract_json(raw_text)
    manifest_data = data.get("resource_manifest", {})
    if isinstance(manifest_data, str):
        try:
            manifest_data = json.loads(manifest_data)
        except (json.JSONDecodeError, TypeError):
            manifest_data = {}
    if not isinstance(manifest_data, dict):
        manifest_data = {}
    manifest = ResourceManifest(**manifest_data)
    return TerraformOutput(
        main_tf=data.get("main_tf", ""),
        variables_tf=data.get("variables_tf", ""),
        outputs_tf=data.get("outputs_tf", ""),
        locals_tf=data.get("locals_tf", ""),
        versions_tf=data.get("versions_tf", ""),
        providers_tf=data.get("providers_tf", ""),
        resource_manifest=manifest,
        status=StepStatus.SUCCESS,
    )


def _unescape_code(code: str) -> str:
    """
    修正 LLM 回傳 JSON 中程式碼被雙重轉義的問題。

    例如 JSON 中的 ``\\\\n`` 經 json.loads 後變成字面 ``\\n``（兩字元），
    而非真正的換行符。此函式將常見的字面轉義序列還原為實際字元。
    """
    if not code:
        return code
    # 只在內容看起來被壓成一行（含字面 \n）時才做替換
    if "\\n" in code:
        code = code.replace("\\n", "\n")
    if "\\t" in code:
        code = code.replace("\\t", "\t")
    if '\\"' in code:
        code = code.replace('\\"', '"')
    return code


def parse_diagram_output(raw_text: str) -> DiagramOutput:
    """Parse raw agent text → DiagramOutput."""
    data = _extract_json(raw_text)
    image_b64 = data.get("diagram_image_base64", "")
    image_bytes = b""
    if image_b64:
        import base64
        image_bytes = base64.b64decode(image_b64)

    manifest_data = data.get("approved_resource_manifest", data.get("resource_manifest", {}))
    if isinstance(manifest_data, str):
        try:
            manifest_data = json.loads(manifest_data)
        except (json.JSONDecodeError, TypeError):
            manifest_data = {}
    if not isinstance(manifest_data, dict):
        manifest_data = {}

    diagram_py = _unescape_code(data.get("diagram_py", ""))

    return DiagramOutput(
        diagram_py=diagram_py,
        diagram_image=image_bytes,
        diagram_image_ext="png",
        render_log=data.get("render_log", ""),
        approved_resource_manifest=ResourceManifest(**manifest_data),
        status=StepStatus.SUCCESS,
    )


def parse_cost_structure_output(raw_text: str) -> CostStructureOutput:
    """Parse raw agent text → CostStructureOutput (Step 3a)."""
    data = _extract_json(raw_text)
    raw_items = data.get("line_items", [])
    line_items = []
    for item in raw_items:
        if isinstance(item, dict):
            line_items.append(CostLineItem(**{
                k: v for k, v in item.items() if k in CostLineItem.model_fields
            }))
    return CostStructureOutput(
        line_items=line_items,
        currency=data.get("currency", "USD"),
        commitment=data.get("commitment", "PAYG"),
        region=data.get("region", ""),
        total_estimated_monthly_usd=float(data.get("total_estimated_monthly_usd", 0)),
        notes=data.get("notes", ""),
        status=StepStatus.SUCCESS,
    )


def parse_cost_output(raw_text: str) -> CostOutput:
    """Parse raw agent text → CostOutput (Step 3b — browser automation)."""
    data = _extract_json(raw_text)

    # Check for error field from browser automation
    error_msg = data.get("error", "")

    xlsx_b64 = data.get("estimate_xlsx_base64", "")
    xlsx_bytes = b""
    if xlsx_b64:
        import base64
        xlsx_bytes = base64.b64decode(xlsx_b64)

    share_url = data.get("calculator_share_url", "")
    monthly = float(data.get("monthly_estimate_usd", 0))
    breakdown = data.get("cost_breakdown", [])

    # Determine status: if error present or no meaningful data, mark as FAILED
    if error_msg:
        status = StepStatus.FAILED
    elif not share_url and not xlsx_bytes and monthly == 0 and not breakdown:
        status = StepStatus.FAILED
        error_msg = "Browser automation returned empty results (no URL, no xlsx, no cost data)"
    else:
        status = StepStatus.SUCCESS

    return CostOutput(
        estimate_xlsx=xlsx_bytes,
        calculator_share_url=share_url,
        monthly_estimate_usd=monthly,
        cost_breakdown=breakdown,
        status=status,
        error=error_msg,
    )


# ---------------------------------------------------------------------------
# Step 1: Terraform Agent (convenience wrapper)
# ---------------------------------------------------------------------------
async def invoke_terraform_agent(spec_json: str) -> TerraformOutput:
    """
    呼叫 Azure-Terraform-Architect-Agent（單次呼叫 — 無 multi-turn）。
    Multi-turn 版本請使用 executor 搭配 invoke_agent_raw + parse_terraform_output。
    """
    try:
        prompt = build_terraform_prompt(spec_json, "{}")
        raw, _ = await _invoke_foundry_agent(TERRAFORM_AGENT, prompt)
        logger.info("[Terraform] Raw response (first 500 chars): %s", raw[:500])
        return parse_terraform_output(raw)
    except Exception as exc:
        logger.error("[Terraform] Failed: %s", exc)
        return TerraformOutput(
            status=StepStatus.FAILED,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Step 2: Diagram Agent (convenience wrapper)
# ---------------------------------------------------------------------------
async def invoke_diagram_agent(
    spec_json: str,
    resource_manifest_json: str,
) -> DiagramOutput:
    """
    呼叫 DaC-Dagrams-Mingrammer（單次呼叫 — 無 multi-turn）。
    """
    try:
        prompt = build_diagram_prompt(spec_json)
        raw, _ = await _invoke_foundry_agent(DIAGRAM_AGENT, prompt)
        logger.info("[Diagram] Raw response (first 500 chars): %s", raw[:500])
        return parse_diagram_output(raw)
    except Exception as exc:
        logger.error("[Diagram] Failed: %s", exc)
        return DiagramOutput(
            status=StepStatus.FAILED,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Step 3a: Cost Structure Agent (convenience wrapper)
# ---------------------------------------------------------------------------
async def invoke_cost_structure_agent(
    spec_json: str,
    resource_manifest_json: str,
) -> CostStructureOutput:
    """
    呼叫 Agent-AzureCalculator 做架構→成本結構轉換（單次呼叫 — 無 multi-turn）。
    """
    try:
        prompt = build_cost_structure_prompt(spec_json, resource_manifest_json)
        raw, _ = await _invoke_foundry_agent(COST_AGENT, prompt, retries=MAX_RETRIES)
        logger.info("[CostStructure] Raw response (first 500 chars): %s", raw[:500])
        return parse_cost_structure_output(raw)
    except Exception as exc:
        logger.error("[CostStructure] Failed: %s", exc)
        return CostStructureOutput(
            status=StepStatus.FAILED,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Step 3b: Cost Browser Agent (convenience wrapper)
# ---------------------------------------------------------------------------
async def invoke_cost_browser_agent(
    cost_structure_json: str,
) -> CostOutput:
    """
    呼叫 Agent-AzureCalculator-BrowserAuto 使用 browser automation 操作 Azure Calculator（單次呼叫）。
    """
    try:
        prompt = build_cost_browser_prompt(cost_structure_json)
        raw, _ = await _invoke_foundry_agent(COST_BROWSER_AGENT, prompt, retries=MAX_RETRIES)
        logger.info("[CostBrowser] Raw response (first 500 chars): %s", raw[:500])
        return parse_cost_output(raw)
    except Exception as exc:
        logger.error("[CostBrowser] Failed: %s", exc)
        return CostOutput(
            status=StepStatus.FAILED,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> dict:
    """
    從 agent 回應文字中擷取 JSON 物件。
    支援 markdown ```json ``` 包裹或直接 JSON。
    """
    import re
    # 嘗試 markdown code block (可能有多個，取最大的)
    matches = re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    for m in sorted(matches, key=len, reverse=True):
        try:
            result = json.loads(m)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            continue

    # 嘗試直接 parse — 找第一個 { 到最後一個 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot extract JSON from agent response: {text[:300]}...")
