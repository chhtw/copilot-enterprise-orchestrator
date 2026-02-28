"""
main.py — Orchestrator entrypoint (HTTP Server / CLI)

Uses WorkflowBuilder to chain executors with multi-turn support:
  1. NormalizeExecutor          — 使用者輸入 → Spec
    2. RequirementClarificationExecutor — 與使用者對話補齊基本規格欄位
    3. ArchitectureClarificationExecutor — LLM 架構細節澄清（服務/安全/網路等）
    4. DiagramExecutor            — 呼叫 DaC-Dagrams-Mingrammer（multi-turn）
    5. DiagramRenderExecutor      — 本地渲染 diagram.py → PNG
    6. DiagramApprovalExecutor    — 使用者核准/修訂架構圖（核准後才進行下一步）
    7. ParallelTerraformCostExecutor — 並行執行 Terraform 與 Agent-AzureCalculator
    8. Step 3b executor           — Cost Browser 或 Retail API 產生 estimate.xlsx
    9. SummaryExecutor            — Executive Summary + WorkflowResult

HTTP mode (RUN_MODE=server):
  from_agent_framework() hosting adapter → localhost:8088
  Multi-turn 由 hosting adapter 自動處理 request_info。

CLI mode (RUN_MODE=cli, default):
  Interactive event loop — agent 追問時從 stdin 讀取回答，
  支援 /done 結束、/skip 跳過。

環境變數：
  MOCK_MODE=true   → 使用 mock_agents（離線測試）
  MOCK_MODE=false  → 使用 foundry_agents（真實呼叫 Foundry）
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from agent_framework import AgentResponse, AgentSession, Content, Message, WorkflowBuilder

# Load .env (override=False → Foundry runtime env vars take precedence)
load_dotenv(override=False)

# SSL workaround for Foundry hosted agent runtime
os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
os.environ.setdefault("SSL_CERT_FILE", "")

# ─── Logging ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("orchestrator")

# ─── Executors & contracts ───
from orchestrator_app.contracts import AgentAnswer, AgentQuestion  # noqa: E402
from orchestrator_app.executors import (  # noqa: E402
    COST_STEP3B_MODE,
    ArchitectureClarificationExecutor,
    CostBrowserExecutor,
    DiagramApprovalExecutor,
    DiagramExecutor,
    DiagramRenderExecutor,
    NormalizeExecutor,
    ParallelTerraformCostExecutor,
    RequirementClarificationExecutor,
    RetailPricesCostExecutor,
    SummaryExecutor,
)


# ======================================================================
# Build Workflow (SequentialBuilder)
# ======================================================================
def build_workflow():
    """
    Build WorkflowBuilder workflow with gated + parallelized flow.

    Wiring:
        NormalizeExecutor
              → RequirementClarificationExecutor   (基本規格欄位澄清)
              → ArchitectureClarificationExecutor  (架構服務/安全/網路細節 LLM 澄清)
        → DiagramExecutor         (multi-turn)
        → DiagramRenderExecutor
              → DiagramApprovalExecutor   (使用者核准後才進行下一步)
              → ParallelTerraformCostExecutor  — 並行：Terraform + CostStructure
        → Step 3b executor        — depends on COST_STEP3B_MODE env var:
            "retail_api" → RetailPricesCostExecutor (local Azure Retail Prices API)
            "browser"    → CostBrowserExecutor (Foundry browser_automation_preview)
        → SummaryExecutor
    """
    # Select Step 3b executor based on env var
    if COST_STEP3B_MODE == "browser":
        logger.info("[Workflow] Step 3b: CostBrowserExecutor (Foundry browser_automation_preview)")
        step3b = CostBrowserExecutor()
    else:
        logger.info("[Workflow] Step 3b: RetailPricesCostExecutor (Azure Retail Prices API)")
        step3b = RetailPricesCostExecutor()

    executors = [
        NormalizeExecutor(),
        RequirementClarificationExecutor(),
        ArchitectureClarificationExecutor(),
        DiagramExecutor(),
        DiagramRenderExecutor(),
        DiagramApprovalExecutor(),
        ParallelTerraformCostExecutor(),
        step3b,
        SummaryExecutor(),
    ]
    return (
        WorkflowBuilder(start_executor=executors[0])
        .add_chain(executors)
        .build()
    )


def build_agent():
    """Build *WorkflowAgent* — 統一的 Agent 介面 (HTTP / CLI 共用)。

    內部呼叫 ``build_workflow().as_agent()``，回傳 ``WorkflowAgent``
    instance，使用 ``agent.run()`` / ``agent.create_session()`` 進行
    multi-turn 互動。
    """
    wf = build_workflow()
    return wf.as_agent(name="ccoe-orchestrator")


# ======================================================================
# HTTP Server (Hosting Adapter)
# ======================================================================
def create_app():
    """建立 ASGI app — hosting adapter wraps WorkflowAgent。

    NOTE: ``azure-ai-agentserver-agentframework`` was removed in the
    MAF 1.0.0rc2 migration.  The hosting adapter import is attempted
    dynamically; install the appropriate hosting package if needed.
    """
    try:
        from azure.ai.agentserver.agentframework import from_agent_framework
    except ImportError as exc:
        raise ImportError(
            "Hosting adapter package 'azure-ai-agentserver-agentframework' "
            "is not installed.  Install it or use CLI mode (RUN_MODE=cli)."
        ) from exc

    agent = build_agent()
    app = from_agent_framework(agent)
    return app


def main():
    """HTTP server 入口。"""
    app = create_app()
    app.run()


# ======================================================================
# CLI: Interactive event loop with multi-turn request_info handling
# ======================================================================
async def cli_run():
    """
    CLI 模式：interactive WorkflowAgent with multi-turn support.

    Flow:
      1. agent.run(messages, session) → 啟動 workflow
      2. 檢查 response.user_input_requests → 若有 pending requests
      3. 從 function_call.arguments 解析 AgentQuestion
      4. 從 stdin 取得使用者回答 → AgentAnswer
      5. 組裝 Content.from_function_result → agent.run([reply], session)
      6. 重複 2-5 直到沒有 user_input_requests
    """
    import json as _json

    # ── 取得使用者輸入 ──
    if len(sys.argv) > 1:
        user_input = " ".join(sys.argv[1:])
    else:
        print("\n" + "=" * 60, flush=True)
        print("  CCoE Orchestrator — CLI 模式（Multi-Turn）", flush=True)
        print("=" * 60, flush=True)
        print("請輸入您的 Azure 架構需求（輸入完按 Enter）：", flush=True)
        print("  💡 Agent 追問時可用 /done 結束、/skip 跳過", flush=True)
        try:
            user_input = input("> ").strip()
        except EOFError:
            user_input = ""

    if not user_input:
        user_input = (
            "我需要一個 Azure 環境，包含 App Service + VNet + Subnet，"
            "部署在 eastasia，環境數量 2 (dev + prod)，幣別 TWD。"
        )
        print(f"[INFO] 使用預設輸入: {user_input}", flush=True)

    print(
        f"\n📥 收到需求: {user_input[:100]}{'...' if len(user_input) > 100 else ''}",
        flush=True,
    )
    print("🚀 開始執行 Orchestrator Workflow...\n", flush=True)

    # ── 建立 agent 與 session ──
    agent = build_agent()
    session = agent.create_session()
    messages = [Message(role="user", text=user_input)]

    response: AgentResponse = await agent.run(messages, session=session)
    _display_agent_response(response)

    # ── Multi-turn 互動迴圈 ──
    while response.user_input_requests:
        reply_contents: list[Content] = []

        for req in response.user_input_requests:
            # req 是 Content(type=function_approval_request)
            # req.function_call.arguments 包含 {"request_id": ..., "data": {...}}
            args = req.function_call.parse_arguments()
            request_id: str = args["request_id"]
            data = args["data"]
            question = data if isinstance(data, AgentQuestion) else AgentQuestion.model_validate(data)

            print(f"\n{'─' * 50}", flush=True)
            print(
                f"❓ [{question.agent_name}] (turn {question.turn}):",
                flush=True,
            )
            print(f"   {question.question_text}", flush=True)
            if question.hint:
                print(f"   💡 {question.hint}", flush=True)

            try:
                user_answer = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                user_answer = "/skip"

            if user_answer.lower() == "/skip":
                answer = AgentAnswer(command="skip")
            elif user_answer.lower() == "/done":
                answer = AgentAnswer(command="done")
            else:
                answer = AgentAnswer(answer_text=user_answer)

            # 使用 Content.from_function_result 將 AgentAnswer 送回 WorkflowAgent
            reply_contents.append(
                Content.from_function_result(
                    call_id=request_id,
                    result=answer.model_dump(),
                )
            )

        reply_msg = Message(role="user", contents=reply_contents)
        # NOTE: 不帶 session — 避免 InMemoryHistoryProvider 將先前的
        # text message 注入 _extract_function_responses 導致
        # "Unexpected content type" 錯誤。
        # pending_requests 狀態存在 agent instance 上，不依賴 session。
        response = await agent.run([reply_msg])
        _display_agent_response(response)

    # ── 結束 ──
    print("\n✅ Workflow 結束", flush=True)


def _display_agent_response(response: AgentResponse) -> None:
    """Print agent response messages to console (skip function-related content)."""
    for msg in response.messages:
        if msg.text:
            print(msg.text, end="", flush=True)


if __name__ == "__main__":
    run_mode = os.getenv("RUN_MODE", "cli").lower()
    if run_mode == "server":
        main()
    else:
        asyncio.run(cli_run())
