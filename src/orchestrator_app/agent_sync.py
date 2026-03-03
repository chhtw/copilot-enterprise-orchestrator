"""
agent_sync.py — 同步 Foundry Agent 定義 ↔ 本地 YAML 檔案。

用途：
  - pull：從 Microsoft Foundry 下載 agent 定義 → 寫入 prompts/{agent_name}.yaml
  - push：讀取 prompts/{agent_name}.yaml → 建立或更新 Foundry agent（draft → publish）

使用方式（CLI）：
  python -m orchestrator_app.agent_sync pull           # 拉取所有 agent
  python -m orchestrator_app.agent_sync pull AgentName  # 拉取指定 agent
  python -m orchestrator_app.agent_sync push           # 推送所有 agent
  python -m orchestrator_app.agent_sync push AgentName --no-publish  # 只更新 draft

YAML 採用 Microsoft Agent Framework declarative 格式：
  kind: Prompt
  name: <agent-name>
  instructions: |
    ...
  model:
    id: <model-id>
    connection:
      kind: Remote
      endpoint: =Env.AZURE_AI_PROJECT_ENDPOINT
  tools:
    - kind: <tool-kind>
      ...
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 自訂 YAML Dumper：多行字串自動使用 literal block style (|)
# ---------------------------------------------------------------------------
class _LiteralBlockDumper(yaml.SafeDumper):
    """YAML Dumper 在遇到含換行的字串時自動使用 | (literal block) 格式。"""
    pass


def _literal_str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralBlockDumper.add_representer(str, _literal_str_representer)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
load_dotenv(override=False)

PROJECT_ENDPOINT = os.getenv(
    "AZURE_AI_PROJECT_ENDPOINT",
    "https://aif-ch-cht-ccoe-ai-agent.services.ai.azure.com/api/projects/ArchitectAgent",
)

# 所有受管 agent 名稱（與 foundry_agents.py 一致）
MANAGED_AGENTS: list[str] = [
    os.getenv("CLARIFICATION_AGENT_NAME", "Architecture-Clarification-Agent"),
    os.getenv("TERRAFORM_AGENT_NAME", "Azure-Terraform-Architect-Agent"),
    os.getenv("DIAGRAM_AGENT_NAME", "DaC-Dagrams-Mingrammer"),
    os.getenv("COST_AGENT_NAME", "Agent-AzureCalculator"),
    os.getenv("COST_BROWSER_AGENT_NAME", "Agent-AzureCalculator-BrowserAuto"),
]

# prompts 目錄
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# Responses API 支援的 tool type
_SUPPORTED_TOOL_TYPES = {"web_search_preview", "code_interpreter", "browser_automation_preview"}

# Foundry tool type → YAML kind 對應
_TOOL_TYPE_TO_YAML_KIND: dict[str, str] = {
    "web_search_preview": "WebSearch",
    "code_interpreter": "CodeInterpreter",
    "browser_automation_preview": "BrowserAutomation",
}

_YAML_KIND_TO_TOOL_TYPE: dict[str, str] = {v: k for k, v in _TOOL_TYPE_TO_YAML_KIND.items()}


# ---------------------------------------------------------------------------
# Helper: 將 SDK model 物件遞迴轉為 plain dict
# ---------------------------------------------------------------------------
def _to_plain_dict(obj: Any) -> Any:
    """遞迴將 SDK model 物件轉為 plain dict/list/str。"""
    if isinstance(obj, dict):
        return {k: _to_plain_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain_dict(item) for item in obj]
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if hasattr(obj, "items"):
        return {k: _to_plain_dict(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Pull: Foundry → YAML
# ---------------------------------------------------------------------------
def _build_yaml_doc(agent_name: str, model: str, instructions: str, tools: list[dict]) -> dict:
    """組裝符合 declarative agent YAML schema 的 dict。"""
    yaml_tools = []
    for t in tools:
        t_type = t.get("type", "")
        yaml_kind = _TOOL_TYPE_TO_YAML_KIND.get(t_type)
        if yaml_kind:
            tool_entry: dict[str, Any] = {"kind": yaml_kind}
            # 保留額外屬性（如 browser_automation 的 config）
            for k, v in t.items():
                if k not in ("type",):
                    tool_entry[k] = v
            yaml_tools.append(tool_entry)
        else:
            # 未知 tool type → 保留原始結構作為 custom
            yaml_tools.append({"kind": "Custom", **t})

    doc: dict[str, Any] = {
        "kind": "Prompt",
        "name": agent_name,
        "instructions": instructions,
        "model": {
            "id": model,
            "connection": {
                "kind": "Remote",
                "endpoint": "=Env.AZURE_AI_PROJECT_ENDPOINT",
            },
        },
        "tools": yaml_tools,  # 即使空也要明確輸出 tools: []
    }

    return doc


async def pull_agent_to_yaml(agent_name: str, *, overwrite: bool = False) -> Path:
    """
    從 Foundry 拉取 agent 定義並寫入 prompts/{agent_name}.yaml。

    Args:
        agent_name: Foundry 上的 agent 名稱
        overwrite: 若 YAML 已存在是否覆蓋（預設 False）

    Returns:
        寫入的 YAML 檔案路徑
    """
    yaml_path = PROMPTS_DIR / f"{agent_name}.yaml"
    if yaml_path.exists() and not overwrite:
        logger.info("[Sync] YAML already exists, skipping: %s", yaml_path)
        print(f"  ⏭️  YAML 已存在，跳過: {yaml_path.name}")
        return yaml_path

    credential = DefaultAzureCredential()
    try:
        async with AIProjectClient(
            endpoint=PROJECT_ENDPOINT,
            credential=credential,
        ) as client:
            print(f"  📥 Pulling agent: {agent_name} ...", flush=True)
            agent = await client.agents.get(agent_name)

            # 取得 latest version 的 definition
            defn = agent["versions"]["latest"]["definition"]
            model = defn.get("model") or "gpt-5.2"
            instructions = defn.get("instructions") or ""
            raw_tools: list = defn.get("tools") or []

            # 過濾 & 轉換 tools
            tools: list[dict] = []
            for t in raw_tools:
                t_dict = _to_plain_dict(t)
                t_type = t_dict.get("type", "")
                if t_type in _SUPPORTED_TOOL_TYPES:
                    tools.append(t_dict)
                else:
                    logger.warning(
                        "[Sync] Skipping unsupported tool type=%s for agent=%s",
                        t_type, agent_name,
                    )

            yaml_doc = _build_yaml_doc(agent_name, model, instructions, tools)

    finally:
        await credential.close()

    # 寫入 YAML（含 timestamp header）
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        f"# Pulled from Microsoft Foundry at: {ts}\n"
        f"# Agent: {agent_name}\n"
        f"# ⚠️  This file is the local source of truth. Edit here, then push to Foundry.\n"
        f"#     python -m orchestrator_app.agent_sync push {agent_name}\n"
        f"# ---------------------------------------------------------------\n"
    )
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(
            yaml_doc,
            f,
            Dumper=_LiteralBlockDumper,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )

    print(f"  ✅ Saved: {yaml_path.name} (model={model}, tools={len(tools)})")
    logger.info("[Sync] Pulled agent=%s → %s", agent_name, yaml_path)
    return yaml_path


async def pull_all_agents(*, overwrite: bool = False) -> list[Path]:
    """拉取所有受管 agent 定義。"""
    results = []
    for name in MANAGED_AGENTS:
        try:
            path = await pull_agent_to_yaml(name, overwrite=overwrite)
            results.append(path)
        except Exception as exc:
            print(f"  ❌ Failed to pull {name}: {exc}")
            logger.error("[Sync] Failed to pull agent=%s: %s", name, exc)
    return results


# ---------------------------------------------------------------------------
# Push: YAML → Foundry
# ---------------------------------------------------------------------------
def load_yaml(agent_name: str) -> dict:
    """讀取 prompts/{agent_name}.yaml 並回傳 parsed dict。"""
    yaml_path = PROMPTS_DIR / f"{agent_name}.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"YAML not found: {yaml_path}")

    with open(yaml_path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    if not isinstance(doc, dict):
        raise ValueError(f"Invalid YAML structure in {yaml_path}")

    return doc


def _yaml_tools_to_foundry(yaml_tools: list[dict]) -> list[dict]:
    """將 YAML tools 轉換回 Foundry API 格式。"""
    foundry_tools = []
    for t in yaml_tools:
        kind = t.get("kind", "")
        tool_type = _YAML_KIND_TO_TOOL_TYPE.get(kind)
        if tool_type:
            ft: dict[str, Any] = {"type": tool_type}
            for k, v in t.items():
                if k != "kind":
                    ft[k] = v
            foundry_tools.append(ft)
        elif kind == "Custom":
            # Custom tool → pass through without 'kind' key
            ft = {k: v for k, v in t.items() if k != "kind"}
            foundry_tools.append(ft)
        else:
            logger.warning("[Sync] Unknown YAML tool kind=%s, skipping", kind)
    return foundry_tools


def _resolve_env_refs(value: str) -> str:
    """解析 PowerFx 風格的環境變數引用 =Env.VAR_NAME → 實際值。"""
    if isinstance(value, str) and value.startswith("=Env."):
        env_var = value[5:]  # strip "=Env."
        return os.getenv(env_var, value)
    return value


async def push_yaml_to_foundry(
    agent_name: str,
    *,
    publish: bool = True,
) -> None:
    """
    讀取 YAML 並建立或更新 Foundry agent。

    Args:
        agent_name: agent 名稱（須與 YAML 檔名一致）
        publish: 是否發布（True=published, False=只更新 draft）
    """
    doc = load_yaml(agent_name)

    model_cfg = doc.get("model", {})
    model_id = model_cfg.get("id", "gpt-5.2")
    instructions = doc.get("instructions", "")
    yaml_tools = doc.get("tools", [])
    foundry_tools = _yaml_tools_to_foundry(yaml_tools)

    credential = DefaultAzureCredential()
    try:
        async with AIProjectClient(
            endpoint=PROJECT_ENDPOINT,
            credential=credential,
        ) as client:
            # 檢查 agent 是否已存在
            exists = False
            try:
                existing = await client.agents.get(agent_name)
                exists = True
                logger.info("[Sync] Agent exists: %s", agent_name)
            except Exception:
                exists = False
                logger.info("[Sync] Agent not found, will create: %s", agent_name)

            definition = {
                "model": model_id,
                "instructions": instructions,
            }
            if foundry_tools:
                definition["tools"] = foundry_tools

            if exists:
                # 更新現有 agent（draft version）
                print(f"  📤 Updating agent: {agent_name} ...", flush=True)
                await client.agents.update(
                    agent_name,
                    definition=definition,
                )
                print(f"  ✅ Updated draft: {agent_name}")
            else:
                # 建立新 agent
                print(f"  📤 Creating agent: {agent_name} ...", flush=True)
                await client.agents.create(
                    name=agent_name,
                    definition=definition,
                )
                print(f"  ✅ Created: {agent_name}")

            # 發布 (draft → published)
            if publish:
                try:
                    await client.agents.publish(agent_name)
                    print(f"  🚀 Published: {agent_name}")
                except Exception as pub_exc:
                    print(f"  ⚠️  Publish failed (draft saved): {pub_exc}")
                    logger.warning("[Sync] Publish failed for %s: %s", agent_name, pub_exc)

    finally:
        await credential.close()

    logger.info("[Sync] Pushed agent=%s (publish=%s)", agent_name, publish)


async def push_all_agents(*, publish: bool = True) -> None:
    """推送所有 prompts/*.yaml 到 Foundry。"""
    for name in MANAGED_AGENTS:
        yaml_path = PROMPTS_DIR / f"{name}.yaml"
        if not yaml_path.exists():
            print(f"  ⏭️  YAML not found, skipping: {name}")
            continue
        try:
            await push_yaml_to_foundry(name, publish=publish)
        except Exception as exc:
            print(f"  ❌ Failed to push {name}: {exc}")
            logger.error("[Sync] Failed to push agent=%s: %s", name, exc)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Foundry agents ↔ local YAML files",
        prog="python -m orchestrator_app.agent_sync",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # pull
    pull_cmd = sub.add_parser("pull", help="Pull agent definitions from Foundry → YAML")
    pull_cmd.add_argument("agent_name", nargs="?", default=None, help="Agent name (omit for all)")
    pull_cmd.add_argument("--overwrite", action="store_true", help="Overwrite existing YAML files")

    # push
    push_cmd = sub.add_parser("push", help="Push YAML → Foundry agents")
    push_cmd.add_argument("agent_name", nargs="?", default=None, help="Agent name (omit for all)")
    push_cmd.add_argument("--no-publish", action="store_true", help="Only update draft, don't publish")

    return parser.parse_args()


async def _async_main() -> None:
    args = _parse_args()

    if args.command == "pull":
        if args.agent_name:
            await pull_agent_to_yaml(args.agent_name, overwrite=args.overwrite)
        else:
            await pull_all_agents(overwrite=args.overwrite)

    elif args.command == "push":
        publish = not args.no_publish
        if args.agent_name:
            await push_yaml_to_foundry(args.agent_name, publish=publish)
        else:
            await push_all_agents(publish=publish)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_async_main())
