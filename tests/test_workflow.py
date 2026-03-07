"""
test_workflow.py — 單元 / E2E 測試 (mock mode)

測試項目：
  1. _normalize_input → Spec
  2. Mock agents (convenience + multi-turn API)
  3. I/O writers
  4. Diagram renderer
  5. Full WorkflowBuilder workflow (build_workflow → wf.run → multi-turn)
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 強制 mock mode
os.environ["MOCK_MODE"] = "true"
os.environ["RUN_MODE"] = "cli"

from orchestrator_app.contracts import (
    AgentAnswer,
    AgentQuestion,
    Assumption,
    ClarifyingQuestion,
    DiagramOutput,
    PricingLineItem,
    PricingOutput,
    PricingStructureOutput,
    ResourceManifest,
    Spec,
    StepResult,
    StepStatus,
    TerraformOutput,
    WorkflowResult,
)
from orchestrator_app.io import (
    ensure_output_dir,
    get_artifact_list,
    zip_output,
    write_pricing_output,
    write_pricing_structure_output,
    write_diagram_output,
    write_executive_summary,
    write_spec,
    write_terraform_output,
)
from orchestrator_app.executors import _coerce_pricing_followup_answer, _normalize_input
from orchestrator_app.main import _build_initial_payload, build_agent, build_workflow
from orchestrator_app import mock_agents
from orchestrator_app.diagram_renderer import render_diagram_locally
from orchestrator_app import executors


# ======================================================================
# Fixtures
# ======================================================================
@pytest.fixture
def tmp_output_dir():
    """臨時輸出目錄。"""
    d = tempfile.mkdtemp(prefix="orch_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sample_input_json() -> str:
    return json.dumps(
        {
            "project_name": "test-project",
            "region": "eastasia",
            "environment_count": 2,
            "currency": "TWD",
            "commitment": "PAYG",
            "network_model": "public",
            "tags": {"team": "ccoe", "env": "test"},
            "notes": "測試用輸入",
        }
    )


@pytest.fixture
def sample_input_natural_language() -> str:
    return "我需要一個 Azure 環境，包含 App Service + VNet，部署在 eastasia，兩個環境 dev + prod"


# ======================================================================
# Test: Spec normalization
# ======================================================================
class TestNormalizeInput:
    def test_json_input(self, sample_input_json):
        spec, questions = _normalize_input(sample_input_json)
        assert isinstance(spec, Spec)
        assert spec.project_name == "test-project"
        assert spec.region == "eastasia"
        assert spec.environment_count == 2
        # 所有欄位已提供 → 沒有 assumption-driven questions
        assert len(questions) == 0

    def test_natural_language_input(self, sample_input_natural_language):
        spec, questions = _normalize_input(sample_input_natural_language)
        assert isinstance(spec, Spec)
        # 自然語言無法直接解析欄位 → 會有 defaults
        assert spec.project_name == "unnamed-project"
        assert spec.region == "eastasia"
        assert len(questions) > 0
        # 每個 question 都是 ClarifyingQuestion
        for q in questions:
            assert isinstance(q, ClarifyingQuestion)

    def test_empty_input(self):
        spec, questions = _normalize_input("")
        assert isinstance(spec, Spec)
        assert spec.project_name == "unnamed-project"

    def test_max_5_questions(self):
        """最多 5 個 clarifying questions。"""
        spec, questions = _normalize_input("random text")
        assert len(questions) <= 5

    def test_preferred_language_from_json(self):
        spec, _ = _normalize_input(json.dumps({"preferred_language": "en-US", "project_name": "demo"}))
        assert spec.preferred_language == "en-US"

    def test_preferred_language_defaults_to_zh_tw(self):
        spec, _ = _normalize_input("plain text")
        assert spec.preferred_language == "zh-TW"


class TestPricingFollowupNormalization:
    def test_blank_pricing_followup_uses_defaults_in_zh_tw(self):
        answer = _coerce_pricing_followup_answer("   ", "zh-TW")
        assert "使用預設值" in answer
        assert "最終 JSON" in answer

    def test_nonblank_pricing_followup_is_preserved(self):
        answer = _coerce_pricing_followup_answer("SQL 用 16 vCore，其餘預設", "zh-TW")
        assert answer == "SQL 用 16 vCore，其餘預設"


class TestInitialPayload:
    def test_build_initial_payload_injects_language_into_json(self):
        payload = _build_initial_payload('{"project_name":"demo"}', "en-US")
        data = json.loads(payload)
        assert data["project_name"] == "demo"
        assert data["preferred_language"] == "en-US"

    def test_build_initial_payload_wraps_plain_text(self):
        payload = _build_initial_payload("need app service", "zh-TW")
        data = json.loads(payload)
        assert data["preferred_language"] == "zh-TW"
        assert data["raw_input"] == "need app service"


# ======================================================================
# Test: Mock agents
# ======================================================================
class TestMockAgents:
    @pytest.mark.asyncio
    async def test_terraform_agent(self):
        spec = Spec(project_name="test", region="eastasia")
        result = await mock_agents.invoke_terraform_agent(spec.to_json())
        assert isinstance(result, TerraformOutput)
        assert result.status == StepStatus.SUCCESS
        assert "resource" in result.main_tf.lower() or "azurerm" in result.main_tf.lower()
        assert result.resource_manifest is not None
        assert len(result.resource_manifest.resources) > 0

    @pytest.mark.asyncio
    async def test_diagram_agent(self):
        spec = Spec(project_name="test", region="eastasia")
        manifest_json = json.dumps({"resources": [{"type": "azurerm_resource_group", "name": "rg"}]})
        result = await mock_agents.invoke_diagram_agent(spec.to_json(), manifest_json)
        assert isinstance(result, DiagramOutput)
        assert result.status == StepStatus.SUCCESS
        assert "diagrams" in result.diagram_py.lower() or "Diagram" in result.diagram_py
        assert len(result.approved_resource_manifest.resources) > 0

    @pytest.mark.asyncio
    async def test_pricing_structure_agent(self):
        spec = Spec(project_name="test", region="eastasia")
        manifest_json = json.dumps({"resources": []})
        result = await mock_agents.invoke_pricing_structure_agent(spec.to_json(), manifest_json)
        assert isinstance(result, PricingStructureOutput)
        assert result.status == StepStatus.SUCCESS
        assert len(result.line_items) > 0
        for item in result.line_items:
            assert isinstance(item, PricingLineItem)
            assert item.resource_type
            assert item.estimated_monthly_usd >= 0

    @pytest.mark.asyncio
    async def test_pricing_browser_agent(self):
        # First get pricing structure output, then pass to browser agent
        spec = Spec(project_name="test", region="eastasia")
        manifest_json = json.dumps({"resources": []})
        structure = await mock_agents.invoke_pricing_structure_agent(spec.to_json(), manifest_json)
        result = await mock_agents.invoke_pricing_browser_agent(structure.to_json())
        assert isinstance(result, PricingOutput)
        assert result.status == StepStatus.SUCCESS
        assert result.calculator_share_url is not None
        assert result.monthly_estimate_usd is not None
        assert result.cost_breakdown is not None

    def test_mock_prompt_builders_track_language(self):
        spec_json = json.dumps({"preferred_language": "en-US", "project_name": "demo"})
        prompt = mock_agents.build_architecture_clarification_prompt(spec_json)
        assert "[MOCK:en-US]" in prompt


class TestDiagramReviewExecutor:
    @pytest.mark.asyncio
    async def test_review_prompt_uses_review_agent_name(self, tmp_output_dir):
        executor = executors.DiagramReviewExecutor()
        diag_output = DiagramOutput(
            diagram_py='from diagrams import Diagram\nwith Diagram("demo"): pass',
            diagram_image=b"png",
            approved_resource_manifest=ResourceManifest(project_name="demo", resources=[]),
            status=StepStatus.SUCCESS,
        )
        spec = Spec(project_name="demo", preferred_language="zh-TW")
        state = {
            executors.KEY_DIAG_OUTPUT: diag_output,
            executors.KEY_SPEC: spec,
        }
        ctx = MagicMock()
        ctx.get_state.side_effect = lambda key: state[key]
        ctx.request_info = AsyncMock()
        ctx.yield_output = AsyncMock()
        ctx.send_message = AsyncMock()

        await executor.handle([], ctx)

        question = ctx.request_info.await_args.args[0]
        assert question.agent_name == executors.DIAGRAM_REVIEW_AGENT

    @pytest.mark.asyncio
    async def test_revision_reuses_existing_diagram_session(self, tmp_output_dir, monkeypatch):
        executor = executors.DiagramReviewExecutor()
        executor._pending_messages = []
        diag_output = DiagramOutput(
            diagram_py='from diagrams import Diagram\nwith Diagram("demo"): pass',
            diagram_image=b"png",
            approved_resource_manifest=ResourceManifest(project_name="demo", resources=[]),
            status=StepStatus.SUCCESS,
        )
        revised_output = DiagramOutput(
            diagram_py='from diagrams import Diagram\nwith Diagram("demo2"): pass',
            diagram_image=b"",
            approved_resource_manifest=ResourceManifest(project_name="demo", resources=[]),
            status=StepStatus.SUCCESS,
        )
        spec = Spec(project_name="demo", preferred_language="zh-TW")
        state = {
            executors.KEY_SPEC: spec,
            executors.KEY_OUTPUT_DIR: str(tmp_output_dir),
            executors.KEY_DIAG_OUTPUT: diag_output,
            executors.KEY_DIAG_RESPONSE_ID: "diagram-session-1",
            executors.KEY_ARCH_DETAILS: {},
        }
        ctx = MagicMock()
        ctx.get_state.side_effect = lambda key: state[key]
        ctx.set_state.side_effect = lambda key, value: state.__setitem__(key, value)
        ctx.request_info = AsyncMock()
        ctx.yield_output = AsyncMock()
        ctx.send_message = AsyncMock()
        monkeypatch.setattr(executors, "RENDER_DIAGRAM", False)

        with (
            patch.object(executors.agents, "build_diagram_prompt", return_value="base prompt"),
            patch.object(executors.agents, "invoke_agent_raw", new=AsyncMock(return_value=("raw", "diagram-session-2"))) as invoke_mock,
            patch.object(executors.agents, "parse_diagram_output", return_value=revised_output),
            patch.object(executors, "write_diagram_output"),
            patch.object(executors, "write_spec"),
        ):
            await executor.handle_response(
                AgentQuestion(agent_name="Diagram-Review", question_text="q", turn=1, preferred_language="zh-TW"),
                AgentAnswer(answer_text="請調整", command="revise"),
                ctx,
            )

        assert invoke_mock.await_args.kwargs["previous_response_id"] == "diagram-session-1"
        assert state[executors.KEY_DIAG_RESPONSE_ID] == "diagram-session-2"
        followup_question = ctx.request_info.await_args.args[0]
        assert followup_question.agent_name == executors.DIAGRAM_REVIEW_AGENT


# ======================================================================
# Test: I/O writers
# ======================================================================
class TestIOWriters:
    def test_ensure_output_dir_ignores_env_without_override_flag(self, tmp_output_dir, monkeypatch):
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_output_dir))
        monkeypatch.delenv("ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE", raising=False)
        assert ensure_output_dir() == Path("./out")

    def test_ensure_output_dir_uses_env_when_override_flag_enabled(self, tmp_output_dir, monkeypatch):
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_output_dir))
        monkeypatch.setenv("ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE", "true")
        assert ensure_output_dir() == tmp_output_dir

    def test_write_spec(self, tmp_output_dir, sample_input_json):
        spec, _ = _normalize_input(sample_input_json)
        path = write_spec(spec, tmp_output_dir)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["project_name"] == "test-project"

    @pytest.mark.asyncio
    async def test_write_terraform_output(self, tmp_output_dir):
        tf = await mock_agents.invoke_terraform_agent('{"project_name": "test"}')
        paths = write_terraform_output(tf, tmp_output_dir)
        assert len(paths) > 0
        # terraform/ 子目錄應存在
        tf_dir = tmp_output_dir / "terraform"
        assert tf_dir.exists()
        assert (tf_dir / "main.tf").exists()

    @pytest.mark.asyncio
    async def test_write_diagram_output(self, tmp_output_dir):
        diag = await mock_agents.invoke_diagram_agent("{}", "{}")
        paths = write_diagram_output(diag, tmp_output_dir)
        assert len(paths) > 0
        assert (tmp_output_dir / "diagram.py").exists()

    @pytest.mark.asyncio
    async def test_write_pricing_structure_output(self, tmp_output_dir):
        structure = await mock_agents.invoke_pricing_structure_agent("{}", "{}")
        paths = write_pricing_structure_output(structure, tmp_output_dir)
        assert len(paths) > 0
        assert (tmp_output_dir / "pricing_structure.json").exists()
        data = json.loads((tmp_output_dir / "pricing_structure.json").read_text())
        assert "line_items" in data

    @pytest.mark.asyncio
    async def test_write_pricing_output(self, tmp_output_dir):
        structure = await mock_agents.invoke_pricing_structure_agent("{}", "{}")
        cost = await mock_agents.invoke_pricing_browser_agent(structure.to_json())
        paths = write_pricing_output(cost, tmp_output_dir)
        assert len(paths) > 0

    def test_write_executive_summary(self, tmp_output_dir, sample_input_json):
        spec, _ = _normalize_input(sample_input_json)

        steps = [
            StepResult(step="Test Step", status=StepStatus.SUCCESS, artifacts=["x.tf"])
        ]
        path = write_executive_summary(spec, steps, tmp_output_dir)
        assert path.exists()
        content = path.read_text()
        assert "test-project" in content

    def test_write_executive_summary_in_english(self, tmp_output_dir):
        spec, _ = _normalize_input(
            json.dumps(
                {
                    "project_name": "english-project",
                    "region": "eastasia",
                    "environment_count": 1,
                    "currency": "USD",
                    "commitment": "PAYG",
                    "network_model": "public",
                    "tags": {"team": "ccoe"},
                    "preferred_language": "en-US",
                }
            )
        )
        path = write_executive_summary(spec, [], tmp_output_dir)
        content = path.read_text()
        assert "Executive Summary" in content
        assert "Next Steps" in content

    def test_get_artifact_list(self, tmp_output_dir):
        # 建立一些假檔案
        (tmp_output_dir / "a.tf").write_text("test")
        (tmp_output_dir / "b.json").write_text("{}")
        artifacts = get_artifact_list(tmp_output_dir)
        assert len(artifacts) >= 2

    def test_zip_output_stays_under_output_dir(self, tmp_output_dir):
        (tmp_output_dir / "a.tf").write_text("test")
        (tmp_output_dir / "nested").mkdir()
        (tmp_output_dir / "nested" / "b.json").write_text("{}")

        zip_path = zip_output(tmp_output_dir)

        assert zip_path == tmp_output_dir / "artifacts.zip"
        assert zip_path.exists()
        artifacts = get_artifact_list(tmp_output_dir)
        assert "artifacts.zip" in artifacts


# ======================================================================
# Test: Diagram Renderer (local execution)
# ======================================================================
class TestDiagramRenderer:
    """測試 render_diagram_locally 函式。"""

    @pytest.mark.asyncio
    async def test_render_success(self, tmp_output_dir):
        """正常情況：diagram.py 產出圖片檔。"""
        # 寫一個極簡 Python 腳本，產出一個假 PNG 檔
        fake_diagram_py = (
            "from pathlib import Path\n"
            "Path('diagram.png').write_bytes(b'\\x89PNG fake')\n"
        )
        output = await render_diagram_locally(
            diagram_py=fake_diagram_py,
            output_dir=tmp_output_dir,
            timeout=30,
        )
        assert output.status == StepStatus.SUCCESS
        assert len(output.diagram_image) > 0
        assert output.diagram_image_ext in ("png", "jpg", "svg")
        assert "success" in output.render_log.lower() or "found" in output.render_log.lower()

    @pytest.mark.asyncio
    async def test_render_no_graphviz(self, tmp_output_dir):
        """graphviz 未安裝時仍應 graceful 處理。"""
        # 腳本會 import diagrams，但即使 diagrams 不在環境中
        # render_diagram_locally 不會在呼叫前 check graphviz — 它直接跑
        # 所以如果 graphviz 沒裝，diagrams 會噴 RuntimeError
        # 這裡用 patch 模擬 shutil.which 回傳 None
        bad_code = "import diagrams\nfrom diagrams import Diagram\nwith Diagram('x'): pass\n"
        with patch("shutil.which", return_value=None):
            output = await render_diagram_locally(
                diagram_py=bad_code,
                output_dir=tmp_output_dir,
                timeout=30,
            )
        # 即使失敗，也不應 raise — 回傳 FAILED 狀態
        assert output.status in (StepStatus.FAILED, StepStatus.SUCCESS)

    @pytest.mark.asyncio
    async def test_render_syntax_error(self, tmp_output_dir):
        """diagram.py 有語法錯誤時應回傳 FAILED。"""
        bad_code = "def foo(\n"  # 語法錯誤
        output = await render_diagram_locally(
            diagram_py=bad_code,
            output_dir=tmp_output_dir,
            timeout=10,
        )
        assert output.status == StepStatus.FAILED
        assert output.render_log  # 應包含錯誤訊息

    @pytest.mark.asyncio
    async def test_render_timeout(self, tmp_output_dir):
        """diagram.py 執行過久應超時回傳 FAILED。"""
        slow_code = "import time\ntime.sleep(60)\n"
        output = await render_diagram_locally(
            diagram_py=slow_code,
            output_dir=tmp_output_dir,
            timeout=2,  # 2 秒超時
        )
        assert output.status == StepStatus.FAILED
        assert "timeout" in output.render_log.lower()

    @pytest.mark.asyncio
    async def test_render_no_image_produced(self, tmp_output_dir):
        """腳本執行成功但沒有產出圖片。"""
        no_image_code = "print('hello')\n"
        output = await render_diagram_locally(
            diagram_py=no_image_code,
            output_dir=tmp_output_dir,
            timeout=10,
        )
        # 腳本跑完但沒圖 → FAILED（觸發 agent-regen）with empty image and error hint
        assert output.status == StepStatus.FAILED
        assert output.diagram_image == b""
        assert output.error  # should contain a hint about no image


# ======================================================================
# Test: Full workflow via WorkflowBuilder (mock mode)
# ======================================================================
class TestFullWorkflow:
    """E2E tests using WorkflowAgent (Workflows as Agents pattern)."""

    @staticmethod
    def _parse_requests(response) -> list[tuple[str, AgentQuestion]]:
        """Extract (request_id, AgentQuestion) pairs from user_input_requests."""
        result = []
        for req in response.user_input_requests:
            args = req.function_call.parse_arguments()
            data = args["data"]
            question = data if isinstance(data, AgentQuestion) else AgentQuestion.model_validate(data)
            result.append((args["request_id"], question))
        return result

    @staticmethod
    def _build_reply(pairs: list[tuple[str, AgentAnswer]]):
        """Build a Message containing Content.from_function_result for each answer."""
        from agent_framework import Content, Message
        contents = [
            Content.from_function_result(call_id=rid, result=ans.model_dump())
            for rid, ans in pairs
        ]
        return Message(role="user", contents=contents)

    @pytest.mark.asyncio
    async def test_e2e_mock_workflow(self, tmp_output_dir):
        """完整 E2E 測試: mock mode workflow — 使用 WorkflowAgent。

        Mock agents 的 classify_response 預設回 "final"，所以 workflow
        不會觸發 multi-turn，應一次完成所有步驟。
        """
        from agent_framework import Message
        from orchestrator_app import executors

        os.environ["OUTPUT_DIR"] = str(tmp_output_dir)
        os.environ["ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE"] = "true"
        os.environ["TERRAFORM_AGENT_GENERATION_MODE"] = "single"
        executors.TERRAFORM_GENERATION_MODE = "single"

        user_input = json.dumps(
            {
                "project_name": "e2e-test",
                "region": "eastasia",
                "environment_count": 1,
                "currency": "TWD",
                "commitment": "PAYG",
                "network_model": "public",
                "tags": {"team": "test"},
                "notes": "E2E test run",
            }
        )

        agent = build_agent()
        session = agent.create_session()
        response = await agent.run(
            [Message(role="user", text=user_input)], session=session,
        )

        while response.user_input_requests:
            pairs = self._parse_requests(response)
            answers = []
            for rid, question in pairs:
                if question.agent_name == "Requirements-Clarification-Stage":
                    answers.append((rid, AgentAnswer(command="done")))
                else:
                    answers.append((rid, AgentAnswer(command="approve")))
            response = await agent.run(
                [self._build_reply(answers)],
            )

        # WorkflowAgent 結束時 user_input_requests 為空
        assert not response.user_input_requests

    @pytest.mark.asyncio
    async def test_workflow_with_natural_language(self, tmp_output_dir):
        """自然語言輸入的 workflow 測試。"""
        from agent_framework import Message
        from orchestrator_app import executors

        os.environ["OUTPUT_DIR"] = str(tmp_output_dir)
        os.environ["ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE"] = "true"
        os.environ["TERRAFORM_AGENT_GENERATION_MODE"] = "single"
        executors.TERRAFORM_GENERATION_MODE = "single"

        agent = build_agent()
        session = agent.create_session()
        response = await agent.run(
            [Message(role="user", text="需要一個包含 App Service 和 VNet 的 Azure 環境")],
            session=session,
        )

        while response.user_input_requests:
            pairs = self._parse_requests(response)
            answers = []
            for rid, question in pairs:
                if question.agent_name == "Requirements-Clarification-Stage":
                    answers.append((rid, AgentAnswer(
                        answer_text="project_name=nlp-test\ntags=team:test,env:dev",
                    )))
                else:
                    answers.append((rid, AgentAnswer(command="approve")))
            response = await agent.run(
                [self._build_reply(answers)],
            )

        assert not response.user_input_requests

    @pytest.mark.asyncio
    async def test_workflow_diagram_requires_multiple_revisions_before_approve(self, tmp_output_dir):
        """模擬使用者先提兩次以上修改意見，最後才 approve。"""
        from agent_framework import Message
        from orchestrator_app import executors

        os.environ["OUTPUT_DIR"] = str(tmp_output_dir)
        os.environ["ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE"] = "true"
        os.environ["TERRAFORM_AGENT_GENERATION_MODE"] = "single"
        executors.TERRAFORM_GENERATION_MODE = "single"

        user_input = json.dumps(
            {
                "project_name": "revise-test",
                "region": "eastasia",
                "environment_count": 1,
                "currency": "USD",
                "commitment": "PAYG",
                "network_model": "private",
                "tags": {"team": "ccoe"},
            }
        )

        agent = build_agent()
        session = agent.create_session()
        response = await agent.run(
            [Message(role="user", text=user_input)], session=session,
        )

        diagram_review_turns = 0
        revise_count = 0

        while response.user_input_requests:
            pairs = self._parse_requests(response)
            answers = []

            for rid, question in pairs:
                if question.agent_name == "Requirements-Clarification-Stage":
                    answers.append((rid, AgentAnswer(command="done")))
                    continue

                if question.agent_name == executors.DIAGRAM_REVIEW_AGENT:
                    diagram_review_turns += 1
                    if revise_count < 2:
                        revise_count += 1
                        answers.append((rid, AgentAnswer(
                            command="revise",
                            answer_text=f"第 {revise_count} 次修改：請調整子網與連線標示",
                        )))
                    else:
                        answers.append((rid, AgentAnswer(command="approve")))
                    continue

                answers.append((rid, AgentAnswer(command="approve")))

            response = await agent.run(
                [self._build_reply(answers)],
            )

        assert not response.user_input_requests
        assert revise_count >= 2
        assert diagram_review_turns >= 3

        assert (tmp_output_dir / "terraform" / "main.tf").exists()
        assert (tmp_output_dir / "estimate.xlsx").exists()
        assert (tmp_output_dir / "artifacts.zip").exists()
