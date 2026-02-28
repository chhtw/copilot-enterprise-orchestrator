"""
executors.py — MAF-native Executor 實作（含 multi-turn 對話）。

使用 SequentialBuilder 串接多個 Executor：
  1. NormalizeExecutor          — 解析使用者輸入 → Spec
    2. RequirementClarificationExecutor — 與使用者對話補齊需求
    3. DiagramExecutor            — 呼叫 Diagram agent（multi-turn）
    4. DiagramRenderExecutor      — 本地渲染 diagram.py → PNG
    5. DiagramApprovalExecutor    — 等待使用者核准/修訂架構圖
    6. ParallelTerraformCostExecutor — 並行執行 Terraform 與成本結構
    7. CostBrowser/Retail executor — 產生 estimate.xlsx
    8. SummaryExecutor            — 產生 Executive Summary + WorkflowResult

所有 Executor 透過 SharedState 傳遞中間產物。
Multi-turn 使用 ctx.request_info(AgentQuestion, AgentAnswer) +
@response_handler 暫停/恢復 workflow，搭配 on_checkpoint_save/restore
保存 executor 內部狀態（response_id, turn）。
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from agent_framework import (
    Message,
    Executor,
    WorkflowContext,
    handler,
    response_handler,
)

from .contracts import (
    AgentAnswer,
    AgentQuestion,
    Assumption,
    ClarifyingQuestion,
    Commitment,
    CostOutput,
    CostStructureOutput,
    DiagramOutput,
    NetworkModel,
    Spec,
    StepResult,
    StepStatus,
    TerraformOutput,
    WorkflowResult,
)
from .io import (
    ensure_output_dir,
    get_artifact_list,
    write_cost_output,
    write_cost_structure_output,
    write_diagram_output,
    write_executive_summary,
    write_spec,
    write_terraform_output,
)

logger = logging.getLogger("orchestrator.executors")

# ─── Output dir ───
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./out"))

# ─── Diagram render toggle ───
RENDER_DIAGRAM = os.getenv("RENDER_DIAGRAM", "true").lower() in ("true", "1", "yes")

# ─── Agent-regen retry: 渲染失敗時將錯誤回傳 Diagram Agent 重新產生 diagram.py ───
MAX_AGENT_REGEN_RETRIES = int(os.getenv("MAX_AGENT_REGEN_RETRIES", "2"))

# ─── Terraform validation: 產出後執行 terraform init/validate，失敗時回傳 Agent 修正 ───
TF_VALIDATE_ENABLED = os.getenv("TF_VALIDATE_ENABLED", "true").lower() in ("true", "1", "yes")
MAX_TF_VALIDATE_RETRIES = int(os.getenv("MAX_TF_VALIDATE_RETRIES", "2"))

# ─── Step 3b mode: "retail_api" (local API) or "browser" (Foundry browser_automation_preview) ───
COST_STEP3B_MODE = os.getenv("COST_STEP3B_MODE", "retail_api").lower().strip()

# ─── Mock vs Real agents ───
MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() in ("true", "1", "yes")

if MOCK_MODE:
    from orchestrator_app import mock_agents as agents
else:
    from orchestrator_app import foundry_agents as agents  # type: ignore[no-redef]

# Agent names (for classify / multi-turn)
TERRAFORM_AGENT = os.getenv("TERRAFORM_AGENT_NAME", "Azure-Terraform-Architect-Agent")
DIAGRAM_AGENT = os.getenv("DIAGRAM_AGENT_NAME", "DaC-Dagrams-Mingrammer")
COST_AGENT = os.getenv("COST_AGENT_NAME", "Agent-AzureCalculator")
COST_BROWSER_AGENT = os.getenv("COST_BROWSER_AGENT_NAME", "Agent-AzureCalculator-BrowserAuto")
CLARIFICATION_AGENT = os.getenv("CLARIFICATION_AGENT_NAME", "Architecture-Clarification-Agent")


# Type alias — WorkflowContext is invariant; bare usage resolves to Never.
_Ctx = WorkflowContext[list[Message], str]


# ======================================================================
# SharedState keys
# ======================================================================
KEY_SPEC = "spec"
KEY_SPEC_JSON = "spec_json"
KEY_OUTPUT_DIR = "output_dir"
KEY_TF_OUTPUT = "tf_output"
KEY_RESOURCE_MANIFEST_JSON = "resource_manifest_json"
KEY_APPROVED_RESOURCE_MANIFEST_JSON = "approved_resource_manifest_json"
KEY_DIAG_OUTPUT = "diag_output"
KEY_DIAGRAM_APPROVED = "diagram_approved"
KEY_COST_STRUCTURE = "cost_structure"
KEY_COST_STRUCTURE_JSON = "cost_structure_json"
KEY_COST_OUTPUT = "cost_output"
KEY_ARCH_DETAILS = "arch_details"  # ArchitectureClarificationExecutor 產出的架構細節
KEY_STEPS = "steps"  # list[StepResult]


# ======================================================================
# Helper: 把 StepResult 追加到 shared state
# ======================================================================
async def _append_step(ctx: _Ctx, step: StepResult) -> None:
    try:
        steps: list[StepResult] = (ctx.get_state(KEY_STEPS)) or []
    except KeyError:
        steps = []
    steps.append(step)
    ctx.set_state(KEY_STEPS, steps)


# ======================================================================
# Helper: normalize input (deterministic, no LLM)
# ======================================================================
def _normalize_input(user_input: str) -> tuple[Spec, list[ClarifyingQuestion]]:
    """將使用者自然語言輸入解析成 Spec（同 main.py 中的 normalize_input）。"""
    spec_data: dict = {}
    try:
        spec_data = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        spec_data = {"raw_input": user_input, "notes": user_input}

    assumptions: list[Assumption] = []
    questions: list[ClarifyingQuestion] = []

    defaults = {
        "project_name": ("unnamed-project", "未指定專案名稱"),
        "region": ("eastasia", "未指定 region，預設 eastasia"),
        "environment_count": (1, "未指定環境數量，預設 1"),
        "currency": ("TWD", "未指定幣別，預設 TWD"),
        "commitment": ("PAYG", "未指定承諾類型，預設 PAYG"),
        "network_model": ("public", "未指定網路模式，預設 public"),
    }

    for field_name, (default_val, reason) in defaults.items():
        if field_name not in spec_data or not spec_data[field_name]:
            spec_data[field_name] = default_val
            assumptions.append(
                Assumption(field=field_name, value=default_val, source="default", reason=reason)
            )
            questions.append(
                ClarifyingQuestion(
                    field=field_name,
                    question=f"請確認 {field_name} 是否為 {default_val}？",
                    options=[str(default_val)],
                    default=default_val,
                )
            )

    questions = questions[:5]
    spec_data.setdefault("tags", {})
    spec_data.setdefault("missing_fields", [])
    spec_data.setdefault("accepted_assumptions", [])
    spec_data.setdefault("completeness_status", "incomplete")
    spec_data.setdefault("diagram_approved", False)
    spec_data.setdefault("diagram_feedback", "")
    spec_data["assumptions"] = [a.model_dump() for a in assumptions]
    spec_data.setdefault("raw_input", user_input)
    spec_data.setdefault("notes", user_input[:500] if isinstance(user_input, str) else "")

    spec = Spec(**{k: v for k, v in spec_data.items() if k in Spec.model_fields})
    return spec, questions


def _parse_kv_pairs(text: str) -> dict[str, str]:
    """Parse 'k=v' or 'k: v' lines from user free text."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if "=" in candidate:
            k, v = candidate.split("=", 1)
        elif ":" in candidate:
            k, v = candidate.split(":", 1)
        else:
            continue
        key = k.strip().lower().replace("-", "_")
        val = v.strip()
        if key and val:
            parsed[key] = val
    return parsed


def _is_positive_approval(text: str) -> bool:
    normalized = text.strip().lower()
    return any(token in normalized for token in ["approve", "approved", "同意", "確認", "ok", "yes", " y", "y", "yeah"])


def _is_revise_or_reject(text: str) -> bool:
    normalized = text.strip().lower()
    return any(token in normalized for token in ["revise", "reject", "修改", "退回", "重做", "不同意"])


# ======================================================================
# 1. NormalizeExecutor
# ======================================================================
class NormalizeExecutor(Executor):
    """Step 0: 使用者輸入 → Spec + 寫入 spec.json。"""

    def __init__(self) -> None:
        super().__init__(id="normalize")

    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext,
    ) -> None:
        # 取最後一個 user message
        user_input = ""
        for msg in reversed(messages):
            if hasattr(msg, "role") and str(msg.role) == "user":
                user_input = str(msg.text) if hasattr(msg, "text") else str(msg)
                break
        if not user_input:
            user_input = " ".join(str(m.text) if hasattr(m, "text") else str(m) for m in messages)

        logger.info("[NormalizeExecutor] input length=%d", len(user_input))
        await ctx.yield_output("📋 [Step 0] Normalize input → spec.json\n")

        spec, questions = _normalize_input(user_input)
        output_dir = ensure_output_dir(OUTPUT_DIR)
        write_spec(spec, output_dir)
        spec_json = spec.to_json()

        # 存入 SharedState
        ctx.set_state(KEY_SPEC, spec)
        ctx.set_state(KEY_SPEC_JSON, spec_json)
        ctx.set_state(KEY_OUTPUT_DIR, str(output_dir))
        ctx.set_state(KEY_APPROVED_RESOURCE_MANIFEST_JSON, "")
        ctx.set_state(KEY_DIAGRAM_APPROVED, False)
        ctx.set_state(KEY_STEPS, [])

        await _append_step(ctx, StepResult(
            step="Step 0: Normalize",
            status=StepStatus.SUCCESS,
            artifacts=[str(output_dir / "spec.json")],
        ))

        info = (
            f"✅ spec.json — project={spec.project_name}, "
            f"region={spec.region}, env_count={spec.environment_count}\n"
        )
        if questions:
            info += f"❓ {len(questions)} 項使用預設值填補\n"
        await ctx.yield_output(info)

        # 傳遞 messages 給下一個 executor
        await ctx.send_message(messages)


# ======================================================================
# 1b. RequirementClarificationExecutor
# ======================================================================
class RequirementClarificationExecutor(Executor):
    """Step 0b: 與使用者對話補齊需求，完整後才放行後續流程。"""

    _critical_fields = [
        "project_name",
        "region",
        "environment_count",
        "currency",
        "commitment",
        "network_model",
        "tags",
    ]

    def __init__(self) -> None:
        super().__init__(id="requirement-clarification")
        self._pending_messages: list[Message] = []
        self._turn: int = 0

    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext,
    ) -> None:
        self._pending_messages = messages
        spec: Spec = ctx.get_state(KEY_SPEC)
        missing = self._detect_missing(spec)
        if not missing:
            spec.completeness_status = "complete"
            spec.missing_fields = []
            ctx.set_state(KEY_SPEC, spec)
            ctx.set_state(KEY_SPEC_JSON, spec.to_json())
            await _append_step(ctx, StepResult(
                step="Step 0b: Requirement Clarification",
                status=StepStatus.SUCCESS,
            ))
            await ctx.yield_output("✅ [Step 0b] 需求完整，進入架構設計\n")
            await ctx.send_message(messages)
            return

        self._turn = 1
        spec.completeness_status = "incomplete"
        spec.missing_fields = missing
        ctx.set_state(KEY_SPEC, spec)
        ctx.set_state(KEY_SPEC_JSON, spec.to_json())

        question = (
            "目前需求尚未完整，請補齊以下欄位（可用 `key=value` 或 `key: value` 每行一項）：\n"
            + "\n".join(f"- {field}" for field in missing)
            + "\n\n例如：project_name=my-shop\nnetwork_model=private\ntags=team:ccoe,env:prod"
        )
        await ctx.yield_output(f"❓ [Step 0b] 需求釐清 (turn {self._turn})\n")
        await ctx.request_info(
            AgentQuestion(
                agent_name="Requirement-Clarifier-Agent",
                question_text=question,
                turn=self._turn,
                hint="可輸入 /done 使用預設值繼續，或 /skip 跳過（不建議）",
            ),
            AgentAnswer,
        )

    @response_handler
    async def handle_response(
        self,
        original_request: AgentQuestion,
        response: AgentAnswer,
        ctx: WorkflowContext,
    ) -> None:
        messages = self._pending_messages
        spec: Spec = ctx.get_state(KEY_SPEC)
        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))

        if response.command == "skip":
            spec.completeness_status = "incomplete"
            ctx.set_state(KEY_SPEC, spec)
            ctx.set_state(KEY_SPEC_JSON, spec.to_json())
            write_spec(spec, output_dir)
            await _append_step(ctx, StepResult(
                step="Step 0b: Requirement Clarification",
                status=StepStatus.SKIPPED,
                error="User chose to skip requirement clarification",
            ))
            await ctx.yield_output("⏭️  [Step 0b] 使用者跳過需求釐清\n")
            await ctx.send_message(messages)
            return

        if response.command == "done":
            missing = self._detect_missing(spec)
            spec.completeness_status = "complete" if not missing else "incomplete"
            spec.missing_fields = missing
            ctx.set_state(KEY_SPEC, spec)
            ctx.set_state(KEY_SPEC_JSON, spec.to_json())
            write_spec(spec, output_dir)
            step_status = StepStatus.SUCCESS if not missing else StepStatus.SKIPPED
            await _append_step(ctx, StepResult(
                step="Step 0b: Requirement Clarification",
                status=step_status,
                error="" if not missing else f"Still missing fields: {', '.join(missing)}",
            ))
            await ctx.yield_output(
                "✅ [Step 0b] 需求確認完成\n" if not missing else "⚠️  [Step 0b] 仍有未補齊欄位，將帶著假設繼續\n"
            )
            await ctx.send_message(messages)
            return

        self._apply_updates(spec, response.answer_text)
        missing = self._detect_missing(spec)
        spec.missing_fields = missing
        spec.completeness_status = "complete" if not missing else "incomplete"
        ctx.set_state(KEY_SPEC, spec)
        ctx.set_state(KEY_SPEC_JSON, spec.to_json())
        write_spec(spec, output_dir)

        if not missing:
            await _append_step(ctx, StepResult(
                step="Step 0b: Requirement Clarification",
                status=StepStatus.SUCCESS,
            ))
            await ctx.yield_output("✅ [Step 0b] 需求已補齊，進入架構設計\n")
            await ctx.send_message(messages)
            return

        self._turn += 1
        await ctx.request_info(
            AgentQuestion(
                agent_name="Requirement-Clarifier-Agent",
                question_text=(
                    "仍缺少以下欄位，請補充：\n" + "\n".join(f"- {field}" for field in missing)
                ),
                turn=self._turn,
                hint="可輸入 /done 使用目前資訊繼續",
            ),
            AgentAnswer,
        )

    def _detect_missing(self, spec: Spec) -> list[str]:
        missing: list[str] = []
        if not spec.project_name or spec.project_name == "unnamed-project":
            missing.append("project_name")
        if not spec.region:
            missing.append("region")
        if spec.environment_count <= 0:
            missing.append("environment_count")
        if not spec.currency:
            missing.append("currency")
        if not spec.commitment:
            missing.append("commitment")
        if not spec.network_model:
            missing.append("network_model")
        if not spec.tags:
            missing.append("tags")
        return missing

    def _apply_updates(self, spec: Spec, answer_text: str) -> None:
        parsed = _parse_kv_pairs(answer_text)
        if not parsed:
            return

        if "project_name" in parsed:
            spec.project_name = parsed["project_name"]
        if "region" in parsed:
            spec.region = parsed["region"]
        if "environment_count" in parsed:
            try:
                spec.environment_count = int(parsed["environment_count"])
            except ValueError:
                pass
        if "currency" in parsed:
            spec.currency = parsed["currency"]
        if "commitment" in parsed:
            raw_commitment = parsed["commitment"].upper()
            if raw_commitment in ("PAYG", "RI"):
                spec.commitment = Commitment(raw_commitment)
        if "network_model" in parsed:
            raw_network = parsed["network_model"].lower()
            if raw_network in ("public", "private"):
                spec.network_model = NetworkModel(raw_network)
        if "tags" in parsed:
            tags: dict[str, str] = {}
            for kv in parsed["tags"].split(","):
                chunk = kv.strip()
                if not chunk or ":" not in chunk:
                    continue
                k, v = chunk.split(":", 1)
                tags[k.strip()] = v.strip()
            if tags:
                spec.tags = tags


# ======================================================================
# Base class: multi-turn agent executor
# ======================================================================
class _MultiTurnAgentExecutor(Executor):
    """
    Multi-turn 互動基底類別（Terraform / Diagram / Cost 共用）。

    --- Flow ---
    1. @handler: build prompt → invoke_agent_raw → classify
       • "final" → parse → store → send_message (繼續 workflow)
       • "question" → 暫存 state + request_info(AgentQuestion, AgentAnswer) → PAUSE
    2. @response_handler: 使用者回答
       • command="skip"     → 跳過此步驟
       • command="done"     → 以當前資料試著解析後繼續
       • command="continue" → invoke_agent_raw(answer, prev_rid) → classify → loop/finalize

    --- Checkpoint ---
    on_checkpoint_save / on_checkpoint_restore 保存 multi-turn 狀態
    （response_id, turn）以便 workflow 重啟後能銜接對話。

    --- Subclass contract ---
    子類別必須實作以下 property / method：
      _agent_name, _step_label, _build_prompt, _parse_output,
      _store_output, _store_skip   (optional: _should_skip)
    """

    def __init__(self, *, executor_id: str) -> None:
        super().__init__(id=executor_id)
        self._response_id: str | None = None
        self._turn: int = 0
        self._pending_messages: list[Message] = []

    # ── Checkpoint hooks ──────────────────────────────────────────────
    async def on_checkpoint_save(self) -> dict[str, Any]:
        return {
            "response_id": self._response_id,
            "turn": self._turn,
        }

    async def on_checkpoint_restore(self, state: dict[str, Any]) -> None:
        self._response_id = state.get("response_id")
        self._turn = state.get("turn", 0)

    # ── Abstract interface ────────────────────────────────────────────
    @property
    def _agent_name(self) -> str:
        raise NotImplementedError

    @property
    def _step_label(self) -> str:
        raise NotImplementedError

    async def _build_prompt(self, ctx: _Ctx) -> str:
        raise NotImplementedError

    def _parse_output(self, raw_text: str) -> Any:
        raise NotImplementedError

    async def _should_skip(self, ctx: _Ctx) -> str | None:
        """回傳跳過原因字串，或 None 表示不跳過。"""
        return None

    async def _store_output(self, ctx: _Ctx, output: Any) -> list[str]:
        """把 output 存入 SharedState 並寫檔。回傳 artifact 路徑。"""
        raise NotImplementedError

    async def _store_skip(self, ctx: _Ctx, reason: str) -> None:
        """跳過或失敗時寫入預設值到 SharedState。"""
        pass

    # ── Handler: first invocation ─────────────────────────────────────
    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext,
    ) -> None:
        self._pending_messages = messages

        # 前置條件檢查
        skip_reason = await self._should_skip(ctx)
        if skip_reason:
            await ctx.yield_output(f"⏭️  [{self._step_label}] 跳過（{skip_reason}）\n")
            await _append_step(ctx, StepResult(
                step=self._step_label,
                status=StepStatus.SKIPPED,
                error=f"Skipped — {skip_reason}",
            ))
            await self._store_skip(ctx, skip_reason)
            await ctx.send_message(messages)
            return

        await ctx.yield_output(f"🔄 [{self._step_label}] 呼叫 {self._agent_name} ...\n")

        prompt = await self._build_prompt(ctx)
        self._turn = 1

        try:
            raw, rid = await agents.invoke_agent_raw(self._agent_name, prompt)
            self._response_id = rid
        except Exception as exc:
            logger.error("[%s] invoke_agent_raw failed: %s", self.id, exc)
            await self._handle_error(ctx, str(exc), messages)
            return

        logger.info("[%s] invoke_agent_raw returned %d chars, response_id=%s",
                     self.id, len(raw), rid)
        classification = agents.classify_response(raw)
        logger.info("[%s] turn=%d classify=%s", self.id, self._turn, classification)

        if classification == "final":
            await self._handle_final(ctx, raw, messages)
        else:
            # agent 在追問 → 暫停 workflow 等使用者回答
            await ctx.yield_output(
                f"❓ [{self._step_label}] Agent 追問 (turn {self._turn}):\n{raw}\n"
            )
            await ctx.request_info(
                AgentQuestion(
                    agent_name=self._agent_name,
                    question_text=raw,
                    turn=self._turn,
                    hint="輸入回答繼續對話，或輸入 /done 結束、/skip 跳過",
                ),
                AgentAnswer,
            )

    # ── Response handler: multi-turn continuation ─────────────────────
    @response_handler
    async def handle_response(
        self,
        original_request: AgentQuestion,
        response: AgentAnswer,
        ctx: WorkflowContext,
    ) -> None:
        messages = self._pending_messages

        # /skip → 跳過
        if response.command == "skip":
            await ctx.yield_output(f"⏭️  [{self._step_label}] 使用者選擇跳過\n")
            await _append_step(ctx, StepResult(
                step=self._step_label,
                status=StepStatus.SKIPPED,
                error="User chose to skip",
            ))
            await self._store_skip(ctx, "User skipped")
            await ctx.send_message(messages)
            return

        # /done → 結束對話，嘗試用已有資料
        if response.command == "done":
            await ctx.yield_output(f"✅ [{self._step_label}] 使用者結束對話\n")
            await _append_step(ctx, StepResult(
                step=self._step_label,
                status=StepStatus.SKIPPED,
                error="User ended conversation before final answer",
            ))
            await self._store_skip(ctx, "User ended with /done")
            await ctx.send_message(messages)
            return

        # command=="continue" → 把使用者回答送回 agent（multi-turn）
        self._turn += 1
        user_answer = response.answer_text

        try:
            raw, rid = await agents.invoke_agent_raw(
                self._agent_name,
                user_answer,
                previous_response_id=self._response_id,
            )
            self._response_id = rid
        except Exception as exc:
            logger.error("[%s] multi-turn invoke failed: %s", self.id, exc)
            await self._handle_error(ctx, str(exc), messages)
            return

        classification = agents.classify_response(raw)
        logger.info("[%s] turn=%d classify=%s", self.id, self._turn, classification)

        if classification == "final":
            await self._handle_final(ctx, raw, messages)
        else:
            # 再次追問 → 又一輪 request_info
            await ctx.yield_output(
                f"❓ [{self._step_label}] Agent 追問 (turn {self._turn}):\n{raw}\n"
            )
            await ctx.request_info(
                AgentQuestion(
                    agent_name=self._agent_name,
                    question_text=raw,
                    turn=self._turn,
                    hint="輸入回答繼續對話，或輸入 /done 結束、/skip 跳過",
                ),
                AgentAnswer,
            )

    # ── Internal helpers ──────────────────────────────────────────────
    async def _handle_final(
        self, ctx: _Ctx, raw: str, messages: list[Message]
    ) -> None:
        """Agent 回傳最終結果 → parse、store、繼續 workflow。"""
        logger.info("[%s] raw response (%d chars): %.2000s", self.id, len(raw), raw)
        try:
            output = self._parse_output(raw)
            artifacts = await self._store_output(ctx, output)
            status = getattr(output, "status", StepStatus.SUCCESS)
            error = getattr(output, "error", "")
            await _append_step(ctx, StepResult(
                step=self._step_label,
                status=status,
                artifacts=artifacts,
                error=error,
            ))
            icon = "✅" if status == StepStatus.SUCCESS else "❌"
            await ctx.yield_output(
                f"{icon} [{self._step_label}] 完成 ({len(artifacts)} artifacts)\n"
            )
        except Exception as exc:
            logger.error("[%s] parse/store failed: %s", self.id, exc)
            await self._handle_error(ctx, str(exc), messages)
            return

        await ctx.send_message(messages)

    async def _handle_error(
        self, ctx: _Ctx, error: str, messages: list[Message]
    ) -> None:
        """錯誤處理 → 記錄失敗步驟、繼續 workflow。"""
        await _append_step(ctx, StepResult(
            step=self._step_label,
            status=StepStatus.FAILED,
            error=error,
            retry_suggestion="重新執行 workflow",
        ))
        await self._store_skip(ctx, error)
        await ctx.yield_output(f"❌ [{self._step_label}] 失敗: {error}\n")
        await ctx.send_message(messages)


# ======================================================================
# 1c. ArchitectureClarificationExecutor
# ======================================================================
def _try_extract_json(raw_text: str) -> dict | None:
    """嘗試從 agent 回傳的文字中擷取 JSON 物件 (executors 內部輔助)。"""
    import re as _re
    # 找 ```json ... ``` 或純 JSON
    for pattern in (
        r"```json\s*([\s\S]*?)```",
        r"```\s*([\s\S]*?)```",
    ):
        m = _re.search(pattern, raw_text)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
    # 嘗試直接 parse 整段
    try:
        data = json.loads(raw_text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


class ArchitectureClarificationExecutor(_MultiTurnAgentExecutor):
    """
    Step 0c: LLM 驅動的架構細節澄清對話。

    使用者輸入往往簡略（例如「一個具有 HSM 的電商架構」），
    本 Executor 透過呼叫 Architecture-Clarification-Agent，
    以多輪對話補齊以下架構決策點：
      - 核心服務（App Service, AKS, VM, Azure Functions...）
      - 資料層（Azure SQL, Cosmos DB, Redis Cache...）
      - 前端/入口點（Application Gateway + WAF, Front Door...）
      - 安全性（Key Vault + HSM, Private Endpoint, DDoS...）
      - 身分識別（Managed Identity, Entra ID, B2C...）
      - 網路拓樸（standalone, hub-spoke, VPN/ExpressRoute）
      - 合規（PCI DSS, ISO 27001, SOC 2...）
      - 高可用/備援（multi-region, RTO/RPO）
      - 監控（App Insights, Log Analytics, Monitor Alerts）

    確認完畢後將 architecture_details 寫入 spec 與 SharedState，
    後續 DiagramExecutor 會將這份細節傳入 Diagram Agent。
    """

    def __init__(self) -> None:
        super().__init__(executor_id="architecture-clarification")

    @property
    def _agent_name(self) -> str:
        return CLARIFICATION_AGENT

    @property
    def _step_label(self) -> str:
        return "Step 0c: Architecture Clarification"

    async def _build_prompt(self, ctx: _Ctx) -> str:
        spec_json: str = ctx.get_state(KEY_SPEC_JSON)
        return agents.build_architecture_clarification_prompt(spec_json)

    def _parse_output(self, raw_text: str) -> dict:
        """Parse agent JSON → architecture_details dict（不依賴 foundry_agents）。"""
        data = _try_extract_json(raw_text)
        if isinstance(data, dict):
            return data
        return {"raw": raw_text}

    async def _store_output(self, ctx: _Ctx, output: dict) -> list[str]:
        arch_details = output.get("architecture_details", output)
        if not isinstance(arch_details, dict):
            arch_details = {"raw": str(arch_details)}

        spec: Spec = ctx.get_state(KEY_SPEC)
        spec.architecture_details = arch_details
        ctx.set_state(KEY_SPEC, spec)
        ctx.set_state(KEY_SPEC_JSON, spec.to_json())
        ctx.set_state(KEY_ARCH_DETAILS, arch_details)

        # 更新 spec.json
        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
        write_spec(spec, output_dir)

        services = arch_details.get("core_services", [])
        await ctx.yield_output(
            f"  📌 架構細節確認完成: core_services={services}\n"
        )
        return []

    async def _store_skip(self, ctx: _Ctx, reason: str) -> None:
        try:
            existing = ctx.get_state(KEY_ARCH_DETAILS)
        except KeyError:
            existing = None
        if not existing:
            ctx.set_state(KEY_ARCH_DETAILS, {})


# ======================================================================
# Terraform validation helper: terraform init + validate → agent fix loop
# ======================================================================

async def _run_terraform_validate(tf_dir: Path) -> tuple[bool, str]:
    """
    在 tf_dir 執行 terraform init -backend=false && terraform validate。

    Returns:
        (success, error_message) — success=True 表示驗證通過
    """
    import shutil
    import subprocess

    if not tf_dir.exists():
        return False, f"Terraform directory does not exist: {tf_dir}"

    # Step 0: 清理舊的 .terraform cache 和 lock 檔，確保 init 使用最新的版本約束
    dot_tf = tf_dir / ".terraform"
    lock_hcl = tf_dir / ".terraform.lock.hcl"
    if dot_tf.exists():
        shutil.rmtree(dot_tf, ignore_errors=True)
        logger.info("[TF Validate] Cleaned .terraform directory")
    if lock_hcl.exists():
        lock_hcl.unlink(missing_ok=True)
        logger.info("[TF Validate] Cleaned .terraform.lock.hcl")

    # Step 1: terraform init -backend=false
    try:
        init_result = subprocess.run(
            ["terraform", "init", "-backend=false", "-no-color"],
            cwd=str(tf_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if init_result.returncode != 0:
            error = (
                f"terraform init failed (exit {init_result.returncode}):\n"
                f"STDOUT:\n{init_result.stdout}\n"
                f"STDERR:\n{init_result.stderr}"
            )
            logger.warning("[TF Validate] init failed: %s", error[:500])
            return False, error
    except FileNotFoundError:
        return False, "terraform CLI not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "terraform init timed out (120s)"

    # Step 2: terraform validate
    try:
        validate_result = subprocess.run(
            ["terraform", "validate", "-no-color"],
            cwd=str(tf_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if validate_result.returncode != 0:
            error = (
                f"terraform validate failed (exit {validate_result.returncode}):\n"
                f"STDOUT:\n{validate_result.stdout}\n"
                f"STDERR:\n{validate_result.stderr}"
            )
            logger.warning("[TF Validate] validate failed: %s", error[:500])
            return False, error
    except subprocess.TimeoutExpired:
        return False, "terraform validate timed out (60s)"

    logger.info("[TF Validate] ✅ terraform validate passed")
    return True, ""


async def _validate_and_fix_terraform(
    tf_output: TerraformOutput,
    output_dir: Path,
    ctx: _Ctx,
    *,
    spec_json: str = "",
    approved_manifest_json: str = "{}",
    step_label: str = "Step 1: Terraform",
) -> TerraformOutput:
    """
    寫入 Terraform 檔案後執行 terraform init/validate；
    若驗證失敗，將錯誤與原始 TF 碼回傳 Azure-Terraform-Architect-Agent 修正，
    最多重試 MAX_TF_VALIDATE_RETRIES 次。

    回傳最終的 TerraformOutput（可能成功也可能仍失敗）。
    """
    if not TF_VALIDATE_ENABLED:
        logger.info("[%s] TF_VALIDATE_ENABLED=false, skipping validation", step_label)
        return tf_output

    if tf_output.status != StepStatus.SUCCESS:
        return tf_output

    def _progress(msg: str) -> None:
        print(msg, flush=True)
        logger.info(msg)

    current = tf_output
    tf_dir = output_dir / "terraform"

    # 寫入 TF 檔案
    write_terraform_output(current, output_dir)

    # 第一次驗證
    _progress(f"🔍 [{step_label}] 執行 terraform init + validate …")
    success, error = await _run_terraform_validate(tf_dir)

    if success:
        _progress(f"✅ [{step_label}] Terraform 驗證通過！")
        return current

    # --- 驗證失敗 → 進入 agent-fix 重試 ---
    _progress(
        f"⚠️  [{step_label}] Terraform 驗證失敗，進入 agent-fix 重試流程 "
        f"(最多 {MAX_TF_VALIDATE_RETRIES} 次) …"
    )
    agent_fix_error = ""

    for fix_attempt in range(1, MAX_TF_VALIDATE_RETRIES + 1):
        progress_msg = (
            f"🔄 [{step_label}] Terraform 驗證失敗，回傳錯誤給 Terraform Agent 修正 "
            f"(fix {fix_attempt}/{MAX_TF_VALIDATE_RETRIES})..."
        )
        _progress(progress_msg)
        await ctx.yield_output(progress_msg + "\n")
        logger.warning(
            "[%s] TF validate failed (fix %d/%d): %s",
            step_label, fix_attempt, MAX_TF_VALIDATE_RETRIES, error[:500],
        )

        try:
            fix_prompt = agents.build_terraform_fix_prompt(
                spec_json=spec_json,
                approved_resource_manifest_json=approved_manifest_json,
                previous_main_tf=current.main_tf,
                previous_variables_tf=current.variables_tf,
                previous_outputs_tf=current.outputs_tf,
                validation_error=error,
                previous_locals_tf=current.locals_tf,
                previous_versions_tf=current.versions_tf,
                previous_providers_tf=current.providers_tf,
            )
            raw, _ = await agents.invoke_agent_raw(TERRAFORM_AGENT, fix_prompt)
            fixed = agents.parse_terraform_output(raw)
        except Exception as exc:
            logger.error("[%s] Agent fix call failed: %s", step_label, exc)
            await ctx.yield_output(
                f"⚠️  [{step_label}] Agent 修正呼叫失敗: {exc}\n"
            )
            agent_fix_error = f"Agent fix failed: {exc}"
            break

        if fixed.status != StepStatus.SUCCESS:
            agent_fix_error = f"Agent returned non-success status: {fixed.error}"
            break

        # 保留 resource_manifest（除非 agent 回傳了新的）
        if not fixed.resource_manifest or not fixed.resource_manifest.resources:
            fixed.resource_manifest = current.resource_manifest

        current = fixed
        write_terraform_output(current, output_dir)

        # 重新驗證
        _progress(f"🔍 [{step_label}] fix {fix_attempt}: 重新驗證中 …")
        success, error = await _run_terraform_validate(tf_dir)
        if success:
            _progress(
                f"✅ [{step_label}] Agent 修正後驗證通過 "
                f"(fix {fix_attempt}/{MAX_TF_VALIDATE_RETRIES})"
            )
            await ctx.yield_output(
                f"✅ [{step_label}] Agent 修正後 Terraform 驗證通過 "
                f"(fix {fix_attempt}/{MAX_TF_VALIDATE_RETRIES})\n"
            )
            return current

    # 全部重試都失敗
    _progress(
        f"❌ [{step_label}] 全部 {MAX_TF_VALIDATE_RETRIES} 次 agent-fix 重試皆失敗"
    )
    logger.error(
        "[%s] All %d TF validate fix retries exhausted",
        step_label, MAX_TF_VALIDATE_RETRIES,
    )
    final_error = agent_fix_error or error or "All TF validate fix attempts exhausted"
    current.error = final_error
    current.status = StepStatus.FAILED
    await ctx.yield_output(
        f"❌ [{step_label}] Terraform 驗證修正失敗 "
        f"(已嘗試 {MAX_TF_VALIDATE_RETRIES} 次): {final_error[:300]}\n"
    )
    return current


# ======================================================================
# 2. TerraformExecutor (multi-turn)
# ======================================================================
class TerraformExecutor(_MultiTurnAgentExecutor):
    """Step 1: 呼叫 Terraform Agent，支援 multi-turn 追問。"""

    def __init__(self) -> None:
        super().__init__(executor_id="terraform")

    @property
    def _agent_name(self) -> str:
        return TERRAFORM_AGENT

    @property
    def _step_label(self) -> str:
        return "Step 1: Terraform"

    async def _build_prompt(self, ctx: _Ctx) -> str:
        spec_json: str = ctx.get_state(KEY_SPEC_JSON)
        approved_manifest_json: str = (ctx.get_state(KEY_APPROVED_RESOURCE_MANIFEST_JSON)) or "{}"
        return agents.build_terraform_prompt(spec_json, approved_manifest_json)

    async def _should_skip(self, ctx: _Ctx) -> str | None:
        approved: bool = bool(ctx.get_state(KEY_DIAGRAM_APPROVED))
        if not approved:
            return "架構圖尚未核准"
        return None

    def _parse_output(self, raw_text: str) -> TerraformOutput:
        return agents.parse_terraform_output(raw_text)

    async def _store_output(self, ctx: _Ctx, output: TerraformOutput) -> list[str]:
        ctx.set_state(KEY_TF_OUTPUT, output)
        if output.status == StepStatus.SUCCESS:
            output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
            artifacts = [str(p) for p in write_terraform_output(output, output_dir)]
            ctx.set_state(KEY_RESOURCE_MANIFEST_JSON, output.resource_manifest.to_json())
            return artifacts
        ctx.set_state(KEY_RESOURCE_MANIFEST_JSON, "")
        return []

    async def _store_skip(self, ctx: _Ctx, reason: str) -> None:
        try:
            existing = ctx.get_state(KEY_TF_OUTPUT)
        except KeyError:
            existing = None
        if not existing:
            ctx.set_state(
                KEY_TF_OUTPUT,
                TerraformOutput(status=StepStatus.FAILED, error=reason),
            )
            ctx.set_state(KEY_RESOURCE_MANIFEST_JSON, "")


# ======================================================================
# 3. DiagramExecutor (multi-turn)
# ======================================================================
class DiagramExecutor(_MultiTurnAgentExecutor):
    """Step 2: 呼叫 Diagram Agent，支援 multi-turn 追問。"""

    def __init__(self) -> None:
        super().__init__(executor_id="diagram")

    @property
    def _agent_name(self) -> str:
        return DIAGRAM_AGENT

    @property
    def _step_label(self) -> str:
        return "Step 2: Diagram"

    async def _build_prompt(self, ctx: _Ctx) -> str:
        spec_json: str = ctx.get_state(KEY_SPEC_JSON)
        arch_details: dict = (ctx.get_state(KEY_ARCH_DETAILS)) or {}
        architecture_details_json = json.dumps(arch_details, ensure_ascii=False, indent=2)
        return agents.build_diagram_prompt(spec_json, architecture_details_json)

    def _parse_output(self, raw_text: str) -> DiagramOutput:
        return agents.parse_diagram_output(raw_text)

    async def _store_output(self, ctx: _Ctx, output: DiagramOutput) -> list[str]:
        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
        artifacts = [str(p) for p in write_diagram_output(output, output_dir)]
        ctx.set_state(KEY_DIAG_OUTPUT, output)
        ctx.set_state(
            KEY_APPROVED_RESOURCE_MANIFEST_JSON,
            output.approved_resource_manifest.to_json(),
        )
        ctx.set_state(KEY_DIAGRAM_APPROVED, False)
        return artifacts

    async def _store_skip(self, ctx: _Ctx, reason: str) -> None:
        try:
            existing = ctx.get_state(KEY_DIAG_OUTPUT)
        except KeyError:
            existing = None
        if not existing:
            ctx.set_state(
                KEY_DIAG_OUTPUT,
                DiagramOutput(status=StepStatus.SKIPPED, error=reason),
            )


# ======================================================================
# 4. DiagramRenderExecutor (with agent-regen retry)
# ======================================================================

async def _render_with_agent_regen(
    diag_output: DiagramOutput,
    output_dir: Path,
    ctx: _Ctx,
    *,
    spec_json: str = "",
    arch_details_json: str = "{}",
    step_label: str = "Step 2b",
) -> DiagramOutput:
    """
    嘗試本地渲染 diagram.py → PNG；若渲染失敗，將錯誤回傳
    DaC-Dagrams-Mingrammer 重新產生 diagram.py，最多重試
    MAX_AGENT_REGEN_RETRIES 次。

    回傳最終的 DiagramOutput（可能成功也可能仍失敗）。
    """
    from .diagram_renderer import render_diagram_locally, get_available_azure_classes_summary

    def _progress(msg: str) -> None:
        """Print progress directly to terminal for CLI visibility."""
        print(msg, flush=True)
        logger.info(msg)

    current = diag_output
    _progress(f"📐 [{step_label}] 開始本地渲染 diagram.py …")
    logger.info("[%s] _render_with_agent_regen: 開始本地渲染", step_label)
    render_result = await render_diagram_locally(current.diagram_py, output_dir)

    if render_result.status == StepStatus.SUCCESS and render_result.diagram_image:
        # 第一次渲染即成功
        _progress(f"✅ [{step_label}] 第一次渲染即成功！")
        logger.info("[%s] 第一次渲染即成功", step_label)
        current.diagram_image = render_result.diagram_image
        current.diagram_image_ext = render_result.diagram_image_ext
        current.render_log = render_result.render_log
        return current

    # --- 渲染失敗 → 進入 agent-regen 重試 ---
    _progress(f"⚠️  [{step_label}] 第一次渲染失敗，進入 agent-regen 重試流程 (最多 {MAX_AGENT_REGEN_RETRIES} 次) …")
    agent_regen_error = ""
    for regen_attempt in range(1, MAX_AGENT_REGEN_RETRIES + 1):
        error_msg = render_result.error or render_result.render_log or "Unknown render error"
        progress_msg = (
            f"🔄 [{step_label}] 渲染失敗，回傳錯誤給 Diagram Agent 重新產生 "
            f"(regen {regen_attempt}/{MAX_AGENT_REGEN_RETRIES})..."
        )
        _progress(progress_msg)
        await ctx.yield_output(progress_msg + "\n")
        logger.warning(
            "[%s] Render failed (regen %d/%d): %s",
            step_label, regen_attempt, MAX_AGENT_REGEN_RETRIES, error_msg[:300],
        )

        try:
            classes_summary = get_available_azure_classes_summary()
            regen_prompt = agents.build_diagram_regen_prompt(
                spec_json=spec_json,
                architecture_details_json=arch_details_json,
                previous_diagram_py=current.diagram_py,
                render_error=error_msg,
                available_classes_summary=classes_summary,
            )
            raw, _ = await agents.invoke_agent_raw(DIAGRAM_AGENT, regen_prompt)
            regenerated = agents.parse_diagram_output(raw)
        except Exception as exc:
            logger.error("[%s] Agent regen call failed: %s", step_label, exc)
            await ctx.yield_output(
                f"⚠️  [{step_label}] Agent 重新產生失敗: {exc}\n"
            )
            agent_regen_error = f"Agent regen failed: {exc}"
            break

        # 保留 approved_resource_manifest（除非 agent 回傳了新的）
        if (
            not regenerated.approved_resource_manifest
            or not regenerated.approved_resource_manifest.resources
        ):
            regenerated.approved_resource_manifest = current.approved_resource_manifest

        current = regenerated
        write_diagram_output(current, output_dir)

        # 重新渲染
        _progress(f"📐 [{step_label}] regen {regen_attempt}: 重新渲染中 …")
        render_result = await render_diagram_locally(current.diagram_py, output_dir)
        if render_result.status == StepStatus.SUCCESS and render_result.diagram_image:
            _progress(f"✅ [{step_label}] Agent 重新產生後渲染成功 (regen {regen_attempt}/{MAX_AGENT_REGEN_RETRIES})")
            await ctx.yield_output(
                f"✅ [{step_label}] Agent 重新產生後渲染成功 "
                f"(regen {regen_attempt}/{MAX_AGENT_REGEN_RETRIES})\n"
            )
            current.diagram_image = render_result.diagram_image
            current.diagram_image_ext = render_result.diagram_image_ext
            current.render_log = render_result.render_log
            return current

    # 全部重試都失敗
    _progress(f"❌ [{step_label}] 全部 {MAX_AGENT_REGEN_RETRIES} 次 agent-regen 重試皆失敗")
    logger.error("[%s] All %d agent-regen retries exhausted", step_label, MAX_AGENT_REGEN_RETRIES)
    current.render_log = render_result.render_log
    current.error = agent_regen_error or render_result.error or "All agent-regen attempts exhausted"
    current.diagram_image = b""
    return current


class DiagramRenderExecutor(Executor):
    """Step 2b: 本地渲染 diagram.py → PNG。"""

    def __init__(self) -> None:
        super().__init__(id="diagram-render")

    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext,
    ) -> None:
        logger.info("[Step 2b] DiagramRenderExecutor.handle() 開始")
        diag_output: DiagramOutput | None = ctx.get_state(KEY_DIAG_OUTPUT)
        if (
            not diag_output
            or diag_output.status != StepStatus.SUCCESS
            or not diag_output.diagram_py
            or not RENDER_DIAGRAM
        ):
            reason = "RENDER_DIAGRAM=false" if not RENDER_DIAGRAM else "Diagram 步驟未成功"
            await ctx.yield_output(f"⏭️  [Step 2b] 跳過渲染（{reason}）\n")
            await _append_step(ctx, StepResult(
                step="Step 2b: Diagram Render",
                status=StepStatus.SKIPPED,
                error=f"Skipped — {reason}",
            ))
            await ctx.send_message(messages)
            return

        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
        await ctx.yield_output("🖼️  [Step 2b] 本地渲染 Diagram ...\n")

        # 取得 spec / arch_details 給 agent-regen prompt 使用
        try:
            spec_json: str = ctx.get_state(KEY_SPEC_JSON)
        except KeyError:
            spec_json = ""
        try:
            arch_details: dict = (ctx.get_state(KEY_ARCH_DETAILS)) or {}
        except KeyError:
            arch_details = {}
        arch_details_json = json.dumps(arch_details, ensure_ascii=False, indent=2)

        result = await _render_with_agent_regen(
            diag_output,
            output_dir,
            ctx,
            spec_json=spec_json,
            arch_details_json=arch_details_json,
            step_label="Step 2b",
        )

        if result.diagram_image:
            write_diagram_output(result, output_dir)
            ctx.set_state(KEY_DIAG_OUTPUT, result)
            ctx.set_state(
                KEY_APPROVED_RESOURCE_MANIFEST_JSON,
                result.approved_resource_manifest.to_json(),
            )
            await _append_step(ctx, StepResult(
                step="Step 2b: Diagram Render",
                status=StepStatus.SUCCESS,
                artifacts=[str(output_dir / f"diagram.{result.diagram_image_ext}")],
            ))
            await ctx.yield_output(
                f"✅ Diagram 圖片已渲染 ({len(result.diagram_image)} bytes)\n"
            )
        else:
            await _append_step(ctx, StepResult(
                step="Step 2b: Diagram Render",
                status=StepStatus.FAILED,
                error=result.error,
                retry_suggestion="Agent regen 已嘗試但仍失敗，請檢查 render_log.txt",
            ))
            await ctx.yield_output(
                f"⚠️  Diagram 渲染失敗（含 {MAX_AGENT_REGEN_RETRIES} 次 agent 重新產生）: "
                f"{result.error}\n"
            )

        await ctx.send_message(messages)


# ======================================================================
# 2c. DiagramApprovalExecutor
# ======================================================================
class DiagramApprovalExecutor(Executor):
    """Step 2c: 讓使用者確認架構圖；未核准時可要求修訂。"""

    def __init__(self) -> None:
        super().__init__(id="diagram-approval")
        self._pending_messages: list[Message] = []
        self._turn: int = 0

    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext,
    ) -> None:
        self._pending_messages = messages
        diag_output: DiagramOutput | None = ctx.get_state(KEY_DIAG_OUTPUT)
        if not diag_output or diag_output.status != StepStatus.SUCCESS:
            await _append_step(ctx, StepResult(
                step="Step 2c: Diagram Approval",
                status=StepStatus.SKIPPED,
                error="Diagram 尚未成功產生",
            ))
            await ctx.yield_output("⏭️  [Step 2c] 跳過審核（沒有可審核架構圖）\n")
            await ctx.send_message(messages)
            return

        self._turn = 1
        await ctx.yield_output("🧭 [Step 2c] 等待使用者確認架構圖\n")
        await ctx.request_info(
            AgentQuestion(
                agent_name=DIAGRAM_AGENT,
                question_text=(
                    "請確認目前架構圖是否正確。\n"
                    "- 可輸入 `approve` / `revise` / `reject`\n"
                    "- 或直接輸入自由文字（例如：同意、請修改網路為 private）"
                ),
                turn=self._turn,
                hint="核准後才會並行執行 Terraform 與成本計算",
            ),
            AgentAnswer,
        )

    @response_handler
    async def handle_response(
        self,
        original_request: AgentQuestion,
        response: AgentAnswer,
        ctx: WorkflowContext,
    ) -> None:
        messages = self._pending_messages
        spec: Spec = ctx.get_state(KEY_SPEC)
        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
        diag_output: DiagramOutput | None = ctx.get_state(KEY_DIAG_OUTPUT)

        text = (response.answer_text or "").strip()
        approve = response.command == "approve" or _is_positive_approval(text)
        revise = response.command in ("revise", "reject") or _is_revise_or_reject(text)

        if approve:
            spec.diagram_approved = True
            spec.diagram_feedback = text
            ctx.set_state(KEY_SPEC, spec)
            ctx.set_state(KEY_SPEC_JSON, spec.to_json())
            write_spec(spec, output_dir)
            ctx.set_state(KEY_DIAGRAM_APPROVED, True)
            if diag_output:
                ctx.set_state(
                    KEY_APPROVED_RESOURCE_MANIFEST_JSON,
                    diag_output.approved_resource_manifest.to_json(),
                )

            await _append_step(ctx, StepResult(
                step="Step 2c: Diagram Approval",
                status=StepStatus.SUCCESS,
            ))
            await ctx.yield_output("✅ [Step 2c] 架構圖已核准，啟動並行產生 TF 與成本\n")
            await ctx.send_message(messages)
            return

        if response.command == "skip":
            spec.diagram_approved = False
            spec.diagram_feedback = "User skipped approval"
            ctx.set_state(KEY_SPEC, spec)
            ctx.set_state(KEY_SPEC_JSON, spec.to_json())
            write_spec(spec, output_dir)
            ctx.set_state(KEY_DIAGRAM_APPROVED, False)
            await _append_step(ctx, StepResult(
                step="Step 2c: Diagram Approval",
                status=StepStatus.SKIPPED,
                error="User skipped diagram approval",
            ))
            await ctx.yield_output("⏭️  [Step 2c] 使用者跳過架構圖審核\n")
            await ctx.send_message(messages)
            return

        feedback = text or "請依原需求重新檢視架構並調整"
        spec.diagram_feedback = feedback
        spec.diagram_approved = False
        ctx.set_state(KEY_SPEC, spec)
        ctx.set_state(KEY_SPEC_JSON, spec.to_json())
        write_spec(spec, output_dir)
        ctx.set_state(KEY_DIAGRAM_APPROVED, False)
        await ctx.yield_output("🔁 [Step 2c] 收到修改意見，重新產生架構圖...\n")

        try:
            revised_prompt = (
                f"{agents.build_diagram_prompt(spec.to_json())}\n\n"
                f"User feedback for revision:\n{feedback}"
            )
            raw, _ = await agents.invoke_agent_raw(DIAGRAM_AGENT, revised_prompt)
            revised = agents.parse_diagram_output(raw)
            output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
            write_diagram_output(revised, output_dir)

            if RENDER_DIAGRAM and revised.diagram_py:
                try:
                    arch_details: dict = (ctx.get_state(KEY_ARCH_DETAILS)) or {}
                except KeyError:
                    arch_details = {}
                arch_details_json = json.dumps(arch_details, ensure_ascii=False, indent=2)

                revised = await _render_with_agent_regen(
                    revised,
                    output_dir,
                    ctx,
                    spec_json=spec.to_json(),
                    arch_details_json=arch_details_json,
                    step_label="Step 2c",
                )
                if revised.diagram_image:
                    write_diagram_output(revised, output_dir)

            ctx.set_state(KEY_DIAG_OUTPUT, revised)
            ctx.set_state(
                KEY_APPROVED_RESOURCE_MANIFEST_JSON,
                revised.approved_resource_manifest.to_json(),
            )
        except Exception as exc:
            await ctx.yield_output(f"⚠️  [Step 2c] 架構修訂失敗: {exc}\n")

        self._turn += 1
        await ctx.request_info(
            AgentQuestion(
                agent_name=DIAGRAM_AGENT,
                question_text="已更新架構圖，請再次確認（approve/revise/reject）。",
                turn=self._turn,
                hint="可輸入修改建議文字",
            ),
            AgentAnswer,
        )


# ======================================================================
# 3. ParallelTerraformCostExecutor  (with multi-turn cost support)
# ======================================================================
class ParallelTerraformCostExecutor(Executor):
    """
    Step 3: 圖核准後並行執行 Terraform 與成本結構產生。

    Terraform 結果一律立即儲存。
    成本結構若 Agent 追問（classify → "question"），
    會透過 request_info / response_handler 進入 multi-turn 對話，
    讓使用者回答計費參數後再繼續。
    """

    def __init__(self) -> None:
        super().__init__(id="parallel-terraform-cost")
        self._cost_response_id: str | None = None
        self._cost_turn: int = 0
        self._pending_messages: list[Message] = []

    # ── Checkpoint hooks ──────────────────────────────────────────────
    async def on_checkpoint_save(self) -> dict[str, Any]:
        return {
            "cost_response_id": self._cost_response_id,
            "cost_turn": self._cost_turn,
        }

    async def on_checkpoint_restore(self, state: dict[str, Any]) -> None:
        self._cost_response_id = state.get("cost_response_id")
        self._cost_turn = state.get("cost_turn", 0)

    # ── Handler: first invocation ─────────────────────────────────────
    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext,
    ) -> None:
        self._pending_messages = messages

        approved = bool(ctx.get_state(KEY_DIAGRAM_APPROVED))
        if not approved:
            reason = "架構圖未核准，跳過 Terraform 與成本並行步驟"
            await ctx.yield_output(f"⏭️  [Step 3] {reason}\n")
            await _append_step(ctx, StepResult(step="Step 1: Terraform", status=StepStatus.SKIPPED, error=reason))
            await _append_step(ctx, StepResult(step="Step 3a: Cost Structure", status=StepStatus.SKIPPED, error=reason))
            ctx.set_state(KEY_TF_OUTPUT, TerraformOutput(status=StepStatus.SKIPPED, error=reason))
            ctx.set_state(KEY_COST_STRUCTURE, CostStructureOutput(status=StepStatus.SKIPPED, error=reason))
            ctx.set_state(KEY_COST_STRUCTURE_JSON, "")
            await ctx.send_message(messages)
            return

        spec_json: str = ctx.get_state(KEY_SPEC_JSON)
        approved_manifest_json: str = (ctx.get_state(KEY_APPROVED_RESOURCE_MANIFEST_JSON)) or "{}"
        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))

        await ctx.yield_output("⚙️  [Step 3] 並行執行 Terraform 與成本結構產生...\n")

        async def run_tf() -> TerraformOutput:
            prompt = agents.build_terraform_prompt(spec_json, approved_manifest_json)
            raw, _ = await agents.invoke_agent_raw(TERRAFORM_AGENT, prompt)
            return agents.parse_terraform_output(raw)

        async def run_cost_raw() -> tuple[str, str | None]:
            """回傳 (raw_text, response_id)，不在此處 parse — 留給 classify 分流。"""
            prompt = agents.build_cost_structure_prompt(spec_json, approved_manifest_json)
            raw, rid = await agents.invoke_agent_raw(COST_AGENT, prompt)
            return raw, rid

        tf_result, cost_raw_result = await asyncio.gather(
            run_tf(), run_cost_raw(), return_exceptions=True,
        )

        # ── TF result: 驗證 + agent-fix 重試 + 儲存 ──────────────────
        if isinstance(tf_result, Exception):
            tf_output = TerraformOutput(status=StepStatus.FAILED, error=str(tf_result))
            tf_artifacts: list[str] = []
        else:
            # 先寫檔 → terraform init/validate → 失敗則回傳 Agent 修正
            tf_output = await _validate_and_fix_terraform(
                tf_result,
                output_dir,
                ctx,
                spec_json=spec_json,
                approved_manifest_json=approved_manifest_json,
                step_label="Step 1: Terraform",
            )
            tf_artifacts = (
                [str(p) for p in write_terraform_output(tf_output, output_dir)]
                if tf_output.status == StepStatus.SUCCESS else []
            )

        ctx.set_state(KEY_TF_OUTPUT, tf_output)
        ctx.set_state(
            KEY_RESOURCE_MANIFEST_JSON,
            tf_output.resource_manifest.to_json() if tf_output.status == StepStatus.SUCCESS else "",
        )
        await _append_step(ctx, StepResult(
            step="Step 1: Terraform",
            status=tf_output.status,
            artifacts=tf_artifacts,
            error=tf_output.error,
        ))
        await self._check_tf_diff(ctx, tf_output, approved_manifest_json)

        # ── Cost result: classify 後決定走 one-shot 或 multi-turn ───
        if isinstance(cost_raw_result, Exception):
            cs_output = CostStructureOutput(
                status=StepStatus.FAILED, error=str(cost_raw_result),
            )
            await self._finalize_cost(ctx, cs_output, output_dir)
            await ctx.yield_output("✅ [Step 3] Terraform/成本結構並行步驟完成\n")
            await ctx.send_message(messages)
            return

        raw_cost, cost_rid = cost_raw_result
        classification = agents.classify_response(raw_cost)
        logger.info(
            "[parallel-terraform-cost] cost classify=%s (len=%d)",
            classification, len(raw_cost),
        )

        if classification == "final":
            # Agent 直接給出最終 JSON → parse & store
            try:
                cs_output = agents.parse_cost_structure_output(raw_cost)
            except Exception as exc:
                cs_output = CostStructureOutput(
                    status=StepStatus.FAILED, error=str(exc),
                )
            await self._finalize_cost(ctx, cs_output, output_dir)
            await ctx.yield_output("✅ [Step 3] Terraform/成本結構並行步驟完成\n")
            await ctx.send_message(messages)
        else:
            # Agent 追問計費參數 → 暫停 workflow 等使用者回答
            self._cost_response_id = cost_rid
            self._cost_turn = 1
            await ctx.yield_output(
                f"❓ [Step 3a: Cost Structure] Agent 追問 (turn {self._cost_turn}):\n{raw_cost}\n"
            )
            await ctx.request_info(
                AgentQuestion(
                    agent_name=COST_AGENT,
                    question_text=raw_cost,
                    turn=self._cost_turn,
                    hint="輸入回答繼續對話，或輸入 /done 結束、/skip 跳過",
                ),
                AgentAnswer,
            )

    # ── Response handler: multi-turn cost continuation ────────────────
    @response_handler
    async def handle_response(
        self,
        original_request: AgentQuestion,
        response: AgentAnswer,
        ctx: WorkflowContext,
    ) -> None:
        messages = self._pending_messages
        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))

        # /skip → 跳過成本結構
        if response.command == "skip":
            reason = "User chose to skip"
            cs_output = CostStructureOutput(status=StepStatus.SKIPPED, error=reason)
            await ctx.yield_output(f"⏭️  [Step 3a: Cost Structure] 使用者選擇跳過\n")
            await self._finalize_cost(ctx, cs_output, output_dir)
            await ctx.yield_output("✅ [Step 3] Terraform/成本結構並行步驟完成\n")
            await ctx.send_message(messages)
            return

        # /done → 結束對話
        if response.command == "done":
            reason = "User ended conversation before final answer"
            cs_output = CostStructureOutput(status=StepStatus.SKIPPED, error=reason)
            await ctx.yield_output(f"✅ [Step 3a: Cost Structure] 使用者結束對話\n")
            await self._finalize_cost(ctx, cs_output, output_dir)
            await ctx.yield_output("✅ [Step 3] Terraform/成本結構並行步驟完成\n")
            await ctx.send_message(messages)
            return

        # command=="continue" → 把使用者回答送回 cost agent
        self._cost_turn += 1
        user_answer = response.answer_text

        try:
            raw, rid = await agents.invoke_agent_raw(
                COST_AGENT,
                user_answer,
                previous_response_id=self._cost_response_id,
            )
            self._cost_response_id = rid
        except Exception as exc:
            logger.error("[parallel-terraform-cost] cost multi-turn invoke failed: %s", exc)
            cs_output = CostStructureOutput(status=StepStatus.FAILED, error=str(exc))
            await self._finalize_cost(ctx, cs_output, output_dir)
            await ctx.yield_output("✅ [Step 3] Terraform/成本結構並行步驟完成\n")
            await ctx.send_message(messages)
            return

        classification = agents.classify_response(raw)
        logger.info(
            "[parallel-terraform-cost] cost turn=%d classify=%s",
            self._cost_turn, classification,
        )

        if classification == "final":
            try:
                cs_output = agents.parse_cost_structure_output(raw)
            except Exception as exc:
                cs_output = CostStructureOutput(status=StepStatus.FAILED, error=str(exc))
            await self._finalize_cost(ctx, cs_output, output_dir)
            await ctx.yield_output("✅ [Step 3] Terraform/成本結構並行步驟完成\n")
            await ctx.send_message(messages)
        else:
            # 再次追問 → 又一輪 request_info
            await ctx.yield_output(
                f"❓ [Step 3a: Cost Structure] Agent 追問 (turn {self._cost_turn}):\n{raw}\n"
            )
            await ctx.request_info(
                AgentQuestion(
                    agent_name=COST_AGENT,
                    question_text=raw,
                    turn=self._cost_turn,
                    hint="輸入回答繼續對話，或輸入 /done 結束、/skip 跳過",
                ),
                AgentAnswer,
            )

    # ── Internal helpers ──────────────────────────────────────────────
    async def _finalize_cost(
        self, ctx: _Ctx, cs_output: CostStructureOutput, output_dir: Path,
    ) -> None:
        """儲存 cost structure 結果到 SharedState 並記錄步驟。"""
        cs_artifacts: list[str] = []
        if cs_output.status == StepStatus.SUCCESS:
            cs_artifacts = [
                str(p) for p in write_cost_structure_output(cs_output, output_dir)
            ]
        ctx.set_state(KEY_COST_STRUCTURE, cs_output)
        ctx.set_state(
            KEY_COST_STRUCTURE_JSON,
            cs_output.to_json() if cs_output.status == StepStatus.SUCCESS else "",
        )
        await _append_step(ctx, StepResult(
            step="Step 3a: Cost Structure",
            status=cs_output.status,
            artifacts=cs_artifacts,
            error=cs_output.error,
        ))

    async def _check_tf_diff(
        self, ctx: _Ctx, tf_output: TerraformOutput, approved_manifest_json: str,
    ) -> None:
        """比對 Terraform resource manifest 與核准清單，有差異時發出警告。"""
        if tf_output.status != StepStatus.SUCCESS:
            return
        try:
            approved_manifest = json.loads(approved_manifest_json)
        except (json.JSONDecodeError, TypeError):
            approved_manifest = {}
        approved_set = {
            (entry.get("resource_type", ""), entry.get("name", ""))
            for entry in (approved_manifest.get("resources", []) or [])
            if isinstance(entry, dict)
        }
        tf_set = {
            (entry.resource_type, entry.name)
            for entry in tf_output.resource_manifest.resources
        }
        only_in_tf = sorted(tf_set - approved_set)
        only_in_approved = sorted(approved_set - tf_set)
        if only_in_tf or only_in_approved:
            diff_msg = (
                f"架構比對差異：only_in_tf={only_in_tf[:5]}, only_in_approved={only_in_approved[:5]}"
            )
            await ctx.yield_output(f"⚠️  [Step 3] {diff_msg}\n")


# ======================================================================
# 5a. CostStructureExecutor (multi-turn)
# ======================================================================
class CostStructureExecutor(_MultiTurnAgentExecutor):
    """Step 3a: 呼叫 Agent-AzureCalculator 將架構轉換成成本結構 JSON。"""

    def __init__(self) -> None:
        super().__init__(executor_id="cost-structure")

    @property
    def _agent_name(self) -> str:
        return COST_AGENT

    @property
    def _step_label(self) -> str:
        return "Step 3a: Cost Structure"

    async def _should_skip(self, ctx: _Ctx) -> str | None:
        approved: bool = bool(ctx.get_state(KEY_DIAGRAM_APPROVED))
        if not approved:
            return "架構圖尚未核准"
        return None

    async def _build_prompt(self, ctx: _Ctx) -> str:
        spec_json: str = ctx.get_state(KEY_SPEC_JSON)
        resource_manifest_json: str = (ctx.get_state(KEY_APPROVED_RESOURCE_MANIFEST_JSON)) or "{}"
        return agents.build_cost_structure_prompt(spec_json, resource_manifest_json)

    def _parse_output(self, raw_text: str) -> CostStructureOutput:
        return agents.parse_cost_structure_output(raw_text)

    async def _store_output(self, ctx: _Ctx, output: CostStructureOutput) -> list[str]:
        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
        artifacts = [str(p) for p in write_cost_structure_output(output, output_dir)]
        ctx.set_state(KEY_COST_STRUCTURE, output)
        ctx.set_state(KEY_COST_STRUCTURE_JSON, output.to_json())
        return artifacts

    async def _store_skip(self, ctx: _Ctx, reason: str) -> None:
        try:
            existing = ctx.get_state(KEY_COST_STRUCTURE)
        except KeyError:
            existing = None
        if not existing:
            ctx.set_state(
                KEY_COST_STRUCTURE,
                CostStructureOutput(status=StepStatus.SKIPPED, error=reason),
            )
            ctx.set_state(KEY_COST_STRUCTURE_JSON, "")


# ======================================================================
# 5b. RetailPricesCostExecutor (local — Azure Retail Prices API)
# ======================================================================
class RetailPricesCostExecutor(Executor):
    """
    Step 3b (retail_api 模式):
    讀取 Step 3a 的 CostStructureOutput → 查詢 Azure Retail Prices REST API →
    用 openpyxl 產生 estimate.xlsx → 寫入 CostOutput。

    不需要 Foundry agent，全部本地完成。
    """

    def __init__(self) -> None:
        super().__init__(id="cost-browser")  # 保持 id 相同以維持 workflow 相容

    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext,
    ) -> None:
        await ctx.yield_output("💰 [Step 3b] 查詢 Azure Retail Prices API ...\n")

        # Check if Step 3a succeeded
        try:
            cs: CostStructureOutput | None = ctx.get_state(KEY_COST_STRUCTURE)
        except KeyError:
            cs = None

        if not cs or cs.status != StepStatus.SUCCESS or not cs.line_items:
            reason = "Cost Structure 步驟未成功，跳過 Retail Prices 查詢"
            logger.warning("[RetailPricesCostExecutor] skip: %s", reason)
            await ctx.yield_output(f"⏭️ {reason}\n")
            await _append_step(ctx, StepResult(
                step="Step 3b: Cost (Retail Prices API)",
                status=StepStatus.SKIPPED,
                error=reason,
            ))
            ctx.set_state(
                KEY_COST_OUTPUT,
                CostOutput(status=StepStatus.SKIPPED, error=reason),
            )
            await ctx.send_message(messages)
            return

        # Import here to avoid import errors when aiohttp/openpyxl not installed
        from .retail_prices import PricedLineItem, fetch_prices_for_line_items
        from .xlsx_builder import build_estimate_xlsx

        try:
            if MOCK_MODE:
                # ── Mock mode: 直接用 LLM estimate 作為價格，不呼叫 API ──
                await ctx.yield_output(f"  📊 [MOCK] 使用 LLM 估算價格 ({len(cs.line_items)} 項) ...\n")
                priced_items = [
                    PricedLineItem(
                        line_item=item,
                        retail_price=None,
                        unit_price_usd=item.estimated_monthly_usd / item.quantity if item.quantity else 0,
                        monthly_cost_usd=item.estimated_monthly_usd,
                        source="llm_estimate",
                    )
                    for item in cs.line_items
                ]
            else:
                # ── Real mode: 呼叫 Azure Retail Prices API ──
                await ctx.yield_output(f"  📊 查詢 {len(cs.line_items)} 項資源的官方價格 ...\n")
                priced_items = await fetch_prices_for_line_items(cs.line_items)

            # Build xlsx
            spec: Spec | None = None
            try:
                spec = ctx.get_state(KEY_SPEC)
            except KeyError:
                pass

            xlsx_bytes = build_estimate_xlsx(
                priced_items,
                project_name=spec.project_name if spec else "",
                region=cs.region or (spec.region if spec else ""),
                currency=cs.currency or "USD",
                commitment=cs.commitment or "PAYG",
            )

            # Build cost breakdown
            total_monthly = sum(pi.monthly_cost_usd for pi in priced_items)
            cost_breakdown = [
                {
                    "resource": pi.display_name,
                    "resource_type": pi.line_item.resource_type,
                    "sku": pi.line_item.sku or "",
                    "unit_price_usd": pi.unit_price_usd,
                    "quantity": pi.line_item.quantity,
                    "monthly_usd": round(pi.monthly_cost_usd, 2),
                    "source": pi.source,
                }
                for pi in priced_items
            ]

            cost_output = CostOutput(
                estimate_xlsx=xlsx_bytes,
                calculator_share_url="(generated locally via Azure Retail Prices API)",
                monthly_estimate_usd=round(total_monthly, 2),
                cost_breakdown=cost_breakdown,
                status=StepStatus.SUCCESS,
            )

            # Write output
            output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
            artifacts = [str(p) for p in write_cost_output(cost_output, output_dir)]
            ctx.set_state(KEY_COST_OUTPUT, cost_output)

            api_count = sum(1 for pi in priced_items if pi.source == "retail_api")
            await ctx.yield_output(
                f"  ✅ estimate.xlsx 產生完成 — {len(priced_items)} 項資源, "
                f"{api_count} 項來自 API, 月估 ${total_monthly:,.2f}\n"
            )

            await _append_step(ctx, StepResult(
                step="Step 3b: Cost (Retail Prices API)",
                status=StepStatus.SUCCESS,
                artifacts=artifacts,
            ))

        except Exception as e:
            logger.exception("[RetailPricesCostExecutor] Error: %s", e)
            error_msg = f"Retail Prices API 查詢失敗: {e}"
            await ctx.yield_output(f"  ❌ {error_msg}\n")
            await _append_step(ctx, StepResult(
                step="Step 3b: Cost (Retail Prices API)",
                status=StepStatus.FAILED,
                error=error_msg,
                retry_suggestion="檢查網路連線，或切換至 COST_STEP3B_MODE=browser",
            ))
            ctx.set_state(
                KEY_COST_OUTPUT,
                CostOutput(status=StepStatus.FAILED, error=error_msg),
            )

        await ctx.send_message(messages)


# ======================================================================
# 5b-alt. CostBrowserExecutor (multi-turn — Foundry browser_automation_preview)
# ======================================================================
class CostBrowserExecutor(_MultiTurnAgentExecutor):
    """Step 3b (browser 模式): 呼叫 Agent-AzureCalculator-BrowserAuto 使用 browser automation 操作 Azure Calculator。"""

    def __init__(self) -> None:
        super().__init__(executor_id="cost-browser")

    @property
    def _agent_name(self) -> str:
        return COST_BROWSER_AGENT

    @property
    def _step_label(self) -> str:
        return "Step 3b: Cost Browser"

    async def _should_skip(self, ctx: _Ctx) -> str | None:
        cs: CostStructureOutput | None = ctx.get_state(KEY_COST_STRUCTURE)
        if not cs or cs.status != StepStatus.SUCCESS:
            return "Cost Structure 步驟未成功"
        return None

    async def _build_prompt(self, ctx: _Ctx) -> str:
        cost_structure_json: str = ctx.get_state(KEY_COST_STRUCTURE_JSON)
        return agents.build_cost_browser_prompt(cost_structure_json)

    def _parse_output(self, raw_text: str) -> CostOutput:
        return agents.parse_cost_output(raw_text)

    async def _store_output(self, ctx: _Ctx, output: CostOutput) -> list[str]:
        if output.status != StepStatus.SUCCESS:
            await ctx.yield_output("⚠️  [Step 3b] Browser 成本估算失敗，改走 Retail Prices fallback...\n")
            fallback_output, artifacts = await self._fallback_with_retail(ctx)
            ctx.set_state(KEY_COST_OUTPUT, fallback_output)
            return artifacts

        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
        artifacts = [str(p) for p in write_cost_output(output, output_dir)]
        ctx.set_state(KEY_COST_OUTPUT, output)
        return artifacts

    async def _store_skip(self, ctx: _Ctx, reason: str) -> None:
        try:
            existing = ctx.get_state(KEY_COST_OUTPUT)
        except KeyError:
            existing = None
        if not existing:
            ctx.set_state(
                KEY_COST_OUTPUT,
                CostOutput(status=StepStatus.SKIPPED, error=reason),
            )

    async def _fallback_with_retail(self, ctx: _Ctx) -> tuple[CostOutput, list[str]]:
        cs: CostStructureOutput | None = ctx.get_state(KEY_COST_STRUCTURE)
        if not cs or cs.status != StepStatus.SUCCESS or not cs.line_items:
            return CostOutput(status=StepStatus.FAILED, error="Fallback failed: cost structure unavailable"), []

        from .retail_prices import PricedLineItem, fetch_prices_for_line_items
        from .xlsx_builder import build_estimate_xlsx

        if MOCK_MODE:
            priced_items = [
                PricedLineItem(
                    line_item=item,
                    retail_price=None,
                    unit_price_usd=item.estimated_monthly_usd / item.quantity if item.quantity else 0,
                    monthly_cost_usd=item.estimated_monthly_usd,
                    source="llm_estimate",
                )
                for item in cs.line_items
            ]
        else:
            priced_items = await fetch_prices_for_line_items(cs.line_items)

        spec: Spec | None = None
        try:
            spec = ctx.get_state(KEY_SPEC)
        except KeyError:
            pass

        xlsx_bytes = build_estimate_xlsx(
            priced_items,
            project_name=spec.project_name if spec else "",
            region=cs.region or (spec.region if spec else ""),
            currency=cs.currency or "USD",
            commitment=cs.commitment or "PAYG",
        )

        total_monthly = sum(pi.monthly_cost_usd for pi in priced_items)
        cost_breakdown = [
            {
                "resource": pi.display_name,
                "resource_type": pi.line_item.resource_type,
                "sku": pi.line_item.sku or "",
                "unit_price_usd": pi.unit_price_usd,
                "quantity": pi.line_item.quantity,
                "monthly_usd": round(pi.monthly_cost_usd, 2),
                "source": f"fallback:{pi.source}",
            }
            for pi in priced_items
        ]

        fallback_output = CostOutput(
            estimate_xlsx=xlsx_bytes,
            calculator_share_url="(fallback: generated locally via Azure Retail Prices API)",
            monthly_estimate_usd=round(total_monthly, 2),
            cost_breakdown=cost_breakdown,
            status=StepStatus.SUCCESS,
            error="",
        )

        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))
        artifacts = [str(p) for p in write_cost_output(fallback_output, output_dir)]
        return fallback_output, artifacts


# ======================================================================
# 6. SummaryExecutor
# ======================================================================
class SummaryExecutor(Executor):
    """Step 5: 產生 Executive Summary + 最終 WorkflowResult。"""

    def __init__(self) -> None:
        super().__init__(id="summary")

    @handler
    async def handle(
        self,
        messages: list[Message],
        ctx: WorkflowContext,
    ) -> None:
        spec: Spec = ctx.get_state(KEY_SPEC)
        steps: list[StepResult] = (ctx.get_state(KEY_STEPS)) or []
        output_dir = Path(ctx.get_state(KEY_OUTPUT_DIR))

        await ctx.yield_output("📝 [Step 5] 產生 Executive Summary ...\n")

        summary_path = write_executive_summary(spec, steps, output_dir)
        steps.append(StepResult(
            step="Step 5: Executive Summary",
            status=StepStatus.SUCCESS,
            artifacts=[str(summary_path)],
        ))

        all_artifacts = get_artifact_list(output_dir)
        overall_success = not any(s.status == StepStatus.FAILED for s in steps)

        result = WorkflowResult(
            spec=spec,
            steps=steps,
            executive_summary=(
                summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
            ),
            output_dir=str(output_dir),
            all_artifacts=all_artifacts,
            success=overall_success,
        )

        # 存入 SharedState 供外部取用
        ctx.set_state("workflow_result", result)

        icon = "✅" if overall_success else "⚠️"
        await ctx.yield_output(f"{icon} Workflow 完成 — {len(all_artifacts)} 個產物\n")

        # 組裝最終回應
        if result.success:
            response_text = _format_success_response(result)
        else:
            response_text = _format_failure_response(result)

        # yield_output 整個摘要
        await ctx.yield_output(response_text)

        # 傳遞 messages 繼續（workflow 尾端 _EndWithConversation 會接收）
        await ctx.send_message(messages)


# ======================================================================
# 格式化回應
# ======================================================================
def _format_success_response(result: WorkflowResult) -> str:
    lines = [
        "\n## ✅ Orchestrator 交付完成\n",
        "### 交付物清單",
    ]
    for a in result.all_artifacts:
        lines.append(f"- `{a}`")
    lines.append("")
    lines.append("### 步驟結果")
    lines.append("| Step | Status |")
    lines.append("|------|--------|")
    for s in result.steps:
        icon = {"success": "✅", "failed": "❌", "skipped": "⏭️"}.get(s.status.value, "❓")
        lines.append(f"| {s.step} | {icon} {s.status.value} |")
    lines.append("")
    lines.append(f"📁 產物目錄: `{result.output_dir}`")
    return "\n".join(lines)


def _format_failure_response(result: WorkflowResult) -> str:
    lines = [
        "\n## ⚠️ Orchestrator 完成（部分步驟失敗）\n",
    ]
    for s in result.steps:
        if s.status == StepStatus.FAILED:
            lines.append(f"### ❌ {s.step}")
            lines.append(f"- 錯誤: {s.error}")
            if s.retry_suggestion:
                lines.append(f"- 建議: {s.retry_suggestion}")
            lines.append("")
    lines.append("### 已成功的產物")
    for s in result.steps:
        if s.status == StepStatus.SUCCESS and s.artifacts:
            for a in s.artifacts:
                lines.append(f"- `{a}`")
    lines.append("")
    lines.append(f"📁 產物目錄: `{result.output_dir}`")
    return "\n".join(lines)
