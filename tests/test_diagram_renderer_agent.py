from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator_app.contracts import DiagramOutput, StepStatus


@pytest.mark.asyncio
async def test_diagram_renderer_agent_run_returns_agent_response(tmp_path: Path):
    from orchestrator_app.diagram_renderer import DiagramRendererAgent

    fake_result = DiagramOutput(
        diagram_py="print('ok')",
        diagram_image=b"png-bytes",
        diagram_image_ext="png",
        render_log="render ok",
        status=StepStatus.SUCCESS,
    )

    with patch(
        "orchestrator_app.diagram_renderer.render_diagram_locally",
        AsyncMock(return_value=fake_result),
    ):
        agent = DiagramRendererAgent(output_dir=tmp_path)
        response = await agent.run("print('ok')")

    assert response.messages
    assert "Diagram rendered successfully" in str(response.messages[0].text)


@pytest.mark.asyncio
async def test_diagram_renderer_agent_empty_input(tmp_path: Path):
    from orchestrator_app.diagram_renderer import DiagramRendererAgent

    agent = DiagramRendererAgent(output_dir=tmp_path)
    response = await agent.run("")

    assert response.messages
    assert "No diagram.py code provided" in str(response.messages[0].text)
