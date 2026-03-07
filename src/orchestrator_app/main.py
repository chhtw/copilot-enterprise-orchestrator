"""
main.py — Orchestrator entrypoint (HTTP Server / CLI)

Uses WorkflowBuilder to chain executors with multi-turn support:
    1. NormalizeExecutor                 — 使用者輸入 → Spec
    2. RequirementsClarificationExecutor — 與使用者對話補齊基本規格欄位
    3. ArchitectureClarificationExecutor — LLM 架構細節澄清（服務/安全/網路等）
    4. DiagramGenerationExecutor         — 呼叫 Azure-Diagram-Generation-Agent（multi-turn）
    5. DiagramRenderingExecutor          — 本地渲染 diagram.py → PNG
    6. DiagramReviewExecutor             — 使用者核准/修訂架構圖（核准後才進行下一步）
    7. ParallelTerraformPricingExecutor  — 並行執行 Terraform 與 Azure-Pricing-Structure-Agent
    8. Step 8 executor                   — Browser 或 Retail API 產生 estimate.xlsx
    9. SummaryExecutor                   — Executive Summary + WorkflowResult

HTTP mode (RUN_MODE=server):
  from_agent_framework() hosting adapter → localhost:8088
  Multi-turn 由 hosting adapter 自動處理 request_info。

CLI mode (RUN_MODE=cli, default):
  Interactive event loop — agent 追問時從 stdin 讀取回答，
  支援 /done 結束、/skip 跳過。

環境變數：
    MOCK_MODE=true   → 使用 mock provider（離線測試）
    MOCK_MODE=false  → 使用 hybrid router（Terraform/Diagram 走 GitHub Copilot，其餘走 Foundry）
"""

from __future__ import annotations

import asyncio
import json
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

# ─── Observability（在 import executors 之前初始化，確保 spans 能捕捉）───
from orchestrator_app.observability import setup_observability  # noqa: E402

setup_observability()

from orchestrator_app import copilot_local_agents  # noqa: E402

# ─── Executors & contracts ───
from orchestrator_app.contracts import AgentAnswer, AgentQuestion  # noqa: E402
from orchestrator_app.i18n import DEFAULT_LANGUAGE, ENGLISH, TRADITIONAL_CHINESE, tr  # noqa: E402
from orchestrator_app.executors import (  # noqa: E402
    PRICING_EXECUTION_MODE,
    ArchitectureClarificationExecutor,
    BrowserPricingExecutor,
    DiagramGenerationExecutor,
    DiagramRenderingExecutor,
    DiagramReviewExecutor,
    NormalizeExecutor,
    ParallelTerraformPricingExecutor,
    RequirementsClarificationExecutor,
    RetailPricingExecutor,
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
              → RequirementsClarificationExecutor  (基本規格欄位澄清)
              → ArchitectureClarificationExecutor  (架構服務/安全/網路細節 LLM 澄清)
        → DiagramGenerationExecutor   (multi-turn)
        → DiagramRenderingExecutor
              → DiagramReviewExecutor             (使用者核准後才進行下一步)
              → ParallelTerraformPricingExecutor  — 並行：Terraform + Pricing Structure
        → Step 8 executor             — depends on PRICING_EXECUTION_MODE env var:
            "retail_api" → RetailPricingExecutor (local Azure Retail Prices API)
            "browser"    → BrowserPricingExecutor (Foundry browser_automation_preview)
        → SummaryExecutor
    """
    # Select Step 8 executor based on env var
    if PRICING_EXECUTION_MODE == "browser":
        logger.info("[Workflow] Step 8: BrowserPricingExecutor (Foundry browser_automation_preview)")
        pricing_executor = BrowserPricingExecutor()
    else:
        logger.info("[Workflow] Step 8: RetailPricingExecutor (Azure Retail Prices API)")
        pricing_executor = RetailPricingExecutor()

    executors = [
        NormalizeExecutor(),
        RequirementsClarificationExecutor(),
        ArchitectureClarificationExecutor(),
        DiagramGenerationExecutor(),
        DiagramRenderingExecutor(),
        DiagramReviewExecutor(),
        ParallelTerraformPricingExecutor(),
        pricing_executor,
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
    return wf.as_agent(name="azure-platform-orchestrator")


def _is_mock_mode() -> bool:
    return os.getenv("MOCK_MODE", "true").lower() in {"true", "1", "yes", "on"}


async def _run_startup_preflight_async() -> None:
    if _is_mock_mode():
        logger.info("[StartupPreflight] MOCK_MODE=true — skipping local Copilot startup preflight")
        return

    if not copilot_local_agents._resolve_startup_preflight_enabled():
        logger.info("[StartupPreflight] %s disabled — skipping local Copilot startup preflight", "GITHUB_COPILOT_STARTUP_PREFLIGHT")
        return

    report = await copilot_local_agents.startup_preflight(strict=True)
    logger.info("[StartupPreflight] Local Copilot provider ready: %s", report)


def _run_startup_preflight_sync() -> None:
    asyncio.run(_run_startup_preflight_async())


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

    _run_startup_preflight_sync()
    agent = build_agent()
    app = from_agent_framework(agent)
    return app


def main():
    """HTTP server 入口。"""
    app = create_app()
    app.run()


def _prompt_preferred_language() -> str:
    """CLI 啟動時先詢問語言。"""
    print("\n" + "=" * 60, flush=True)
    print("  Select language / 選擇語言", flush=True)
    print("=" * 60, flush=True)
    print("  1. 繁體中文", flush=True)
    print("  2. English", flush=True)
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        choice = ""

    if choice in {"2", "en", "english", "en-us"}:
        return ENGLISH
    return TRADITIONAL_CHINESE


def _build_initial_payload(user_input: str, preferred_language: str) -> str:
    try:
        parsed = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict):
        payload = dict(parsed)
        payload["preferred_language"] = preferred_language
        payload.setdefault("raw_input", user_input)
        payload.setdefault("notes", payload.get("notes") or user_input)
        return json.dumps(payload, ensure_ascii=False)

    return json.dumps(
        {
            "preferred_language": preferred_language,
            "raw_input": user_input,
            "notes": user_input,
        },
        ensure_ascii=False,
    )


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

    preferred_language = _prompt_preferred_language()

    # ── 取得使用者輸入 ──
    if len(sys.argv) > 1:
        user_input = " ".join(sys.argv[1:])
    else:
        print("\n" + "=" * 60, flush=True)
        print(tr(preferred_language, "  CCoE Orchestrator — CLI 模式（Multi-Turn）", "  CCoE Orchestrator — CLI Mode (Multi-Turn)"), flush=True)
        print("=" * 60, flush=True)
        print(tr(preferred_language, "請輸入您的 Azure 架構需求（輸入完按 Enter）：", "Describe your Azure architecture requirements and press Enter:"), flush=True)
        print(tr(preferred_language, "  💡 Agent 追問時可用 /done 結束、/skip 跳過", "  💡 During follow-up questions, use /done to continue or /skip to skip"), flush=True)
        try:
            user_input = input("> ").strip()
        except EOFError:
            user_input = ""

    if not user_input:
        user_input = (
            "我需要一個 Azure 環境，包含 App Service + VNet + Subnet，"
            "部署在 eastasia，環境數量 2 (dev + prod)，幣別 TWD。"
        )
        print(
            tr(
                preferred_language,
                f"[INFO] 使用預設輸入: {user_input}",
                f"[INFO] Using default input: {user_input}",
            ),
            flush=True,
        )

    initial_payload = _build_initial_payload(user_input, preferred_language)

    print(
        tr(
            preferred_language,
            f"\n📥 收到需求: {user_input[:100]}{'...' if len(user_input) > 100 else ''}",
            f"\n📥 Received request: {user_input[:100]}{'...' if len(user_input) > 100 else ''}",
        ),
        flush=True,
    )
    print(tr(preferred_language, "🚀 開始執行 Orchestrator Workflow...\n", "🚀 Starting the Orchestrator workflow...\n"), flush=True)

    # ── 建立 agent 與 session ──
    await _run_startup_preflight_async()
    agent = build_agent()
    session = agent.create_session()
    messages = [Message(role="user", text=initial_payload)]

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
                tr(
                    question.preferred_language,
                    f"❓ [{question.agent_name}] (第 {question.turn} 輪):",
                    f"❓ [{question.agent_name}] (turn {question.turn}):",
                ),
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
    print(tr(preferred_language, "\n✅ Workflow 結束", "\n✅ Workflow finished"), flush=True)


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
