from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


class TestCopilotLocalAgents:
    def test_resolve_copilot_workdir_defaults_to_current_directory(self, monkeypatch, tmp_path: Path):
        from orchestrator_app import copilot_local_agents as mod

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_COPILOT_WORKDIR", raising=False)

        assert mod._resolve_copilot_workdir() == str(tmp_path.resolve())

    def test_resolve_copilot_workdir_resolves_relative_path_and_creates_directory(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        from orchestrator_app import copilot_local_agents as mod

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_COPILOT_WORKDIR", "out")

        resolved = Path(mod._resolve_copilot_workdir())

        assert resolved == tmp_path.resolve() / "out"
        assert resolved.exists()
        assert resolved.is_dir()

    def test_resolve_copilot_timeout_prefers_global_timeout(self, monkeypatch):
        from orchestrator_app import copilot_local_agents as mod

        monkeypatch.setenv("GITHUB_COPILOT_TIMEOUT", "480")
        monkeypatch.setenv("DIAGRAM_AGENT_TIMEOUT", "360")

        assert mod._resolve_copilot_timeout(mod.DIAGRAM_AGENT) == 480

    def test_resolve_copilot_timeout_falls_back_to_agent_specific_timeout(self, monkeypatch):
        from orchestrator_app import copilot_local_agents as mod

        monkeypatch.delenv("GITHUB_COPILOT_TIMEOUT", raising=False)
        monkeypatch.setenv("DIAGRAM_AGENT_TIMEOUT", "420")
        monkeypatch.delenv("AGENT_TIMEOUT", raising=False)

        assert mod._resolve_copilot_timeout(mod.DIAGRAM_AGENT) == 420

    def test_resolve_copilot_timeout_falls_back_to_agent_timeout_floor(self, monkeypatch):
        from orchestrator_app import copilot_local_agents as mod

        monkeypatch.delenv("GITHUB_COPILOT_TIMEOUT", raising=False)
        monkeypatch.delenv("DIAGRAM_AGENT_TIMEOUT", raising=False)
        monkeypatch.setenv("AGENT_TIMEOUT", "120")

        assert mod._resolve_copilot_timeout(mod.DIAGRAM_AGENT) == 300

    def test_resolve_copilot_progress_timeout_prefers_global_timeout(self, monkeypatch):
        from orchestrator_app import copilot_local_agents as mod

        monkeypatch.setenv("GITHUB_COPILOT_PROGRESS_TIMEOUT", "90")
        monkeypatch.setenv("DIAGRAM_AGENT_PROGRESS_TIMEOUT", "45")

        assert mod._resolve_copilot_progress_timeout(mod.DIAGRAM_AGENT, 480) == 90

    def test_resolve_copilot_progress_timeout_defaults_to_safe_cap(self, monkeypatch):
        from orchestrator_app import copilot_local_agents as mod

        monkeypatch.delenv("GITHUB_COPILOT_PROGRESS_TIMEOUT", raising=False)
        monkeypatch.delenv("DIAGRAM_AGENT_PROGRESS_TIMEOUT", raising=False)

        assert mod._resolve_copilot_progress_timeout(mod.DIAGRAM_AGENT, 1200) == 180
        assert mod._resolve_copilot_progress_timeout(mod.DIAGRAM_AGENT, 60) == 60

    def test_format_copilot_error_includes_actionable_timeout_hint(self):
        from orchestrator_app import copilot_local_agents as mod

        err = RuntimeError("GitHub Copilot request failed: Timeout after 300s waiting for session.idle")
        auth = mod._CopilotAuthStatus(True, login="demo-user", status_message=None)

        message = mod._format_copilot_error(
            mod.DIAGRAM_AGENT,
            "gpt-5.4",
            300,
            err,
            auth,
        )

        assert "session.idle" in message
        assert "demo-user" in message
        assert "GITHUB_COPILOT_TIMEOUT" in message

    def test_retryable_classification_uses_allow_and_deny_lists(self):
        from orchestrator_app import copilot_local_agents as mod

        assert mod._is_retryable_copilot_exception(RuntimeError("503 service unavailable")) is True
        assert mod._is_retryable_copilot_exception(RuntimeError("Sign in required")) is False
        assert mod._is_retryable_copilot_exception(RuntimeError("unsupported model")) is False
        assert mod._is_retryable_copilot_exception(RuntimeError("unexpected internal condition")) is False

    @pytest.mark.asyncio
    async def test_invoke_copilot_fails_fast_when_unauthenticated(self, monkeypatch):
        from orchestrator_app import copilot_local_agents as mod

        async def fake_auth_status():
            return mod._CopilotAuthStatus(
                is_authenticated=False,
                login=None,
                status_message="Sign in required",
            )

        monkeypatch.setattr(mod, "_get_copilot_auth_status", fake_auth_status)
        agent = mod.DiagramCopilotAgent()
        invoke = getattr(agent, "_invoke_copilot")

        with pytest.raises(RuntimeError, match="not authenticated"):
            await invoke("hello")

    @pytest.mark.asyncio
    async def test_get_copilot_auth_status_uses_resolved_workdir(self, monkeypatch, tmp_path: Path):
        from orchestrator_app import copilot_local_agents as mod

        captured: dict[str, object] = {}

        class FakeStatus:
            isAuthenticated = True
            login = "demo-user"
            statusMessage = "ok"

        class FakeCopilotClient:
            def __init__(self, options):
                captured["options"] = options

            async def start(self):
                captured["started"] = True

            async def get_auth_status(self):
                return FakeStatus()

            async def stop(self):
                captured["stopped"] = True

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_COPILOT_WORKDIR", "out")
        monkeypatch.setattr(mod, "CopilotClient", FakeCopilotClient)

        status = await mod._get_copilot_auth_status()

        assert status is not None
        assert status.is_authenticated is True
        assert captured["options"] == {
            "cwd": str((tmp_path / "out").resolve()),
            "log_level": "info",
        }
        assert captured["started"] is True
        assert captured["stopped"] is True

    @pytest.mark.asyncio
    async def test_invoke_copilot_uses_resolved_workdir_for_client(self, monkeypatch, tmp_path: Path):
        from orchestrator_app import copilot_local_agents as mod

        captured: dict[str, object] = {}

        async def fake_auth_status():
            return mod._CopilotAuthStatus(True, login="demo-user", status_message=None)

        class FakeCopilotClient:
            def __init__(self, options):
                captured["options"] = options

            async def stop(self):
                captured["client_stopped"] = True

        class FakeGitHubCopilotAgent:
            def __init__(self, *, instructions=None, client=None, default_options=None, **kwargs):
                captured["instructions"] = instructions
                captured["agent_client"] = client
                captured["default_options"] = default_options

            async def start(self):
                captured["agent_started"] = True

            async def run(self, messages, session=None, options=None, **kwargs):
                captured["messages"] = messages
                captured["run_options"] = options
                return "copilot-output"

            async def stop(self):
                captured["agent_stopped"] = True

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_COPILOT_WORKDIR", "out")
        monkeypatch.setattr(mod, "_get_copilot_auth_status", fake_auth_status)
        monkeypatch.setattr(mod, "CopilotClient", FakeCopilotClient)
        monkeypatch.setattr(mod, "GitHubCopilotAgent", FakeGitHubCopilotAgent)

        agent = mod.DiagramCopilotAgent()
        text = await getattr(agent, "_invoke_copilot")("hello")

        assert text == "copilot-output"
        assert captured["options"] == {
            "cwd": str((tmp_path / "out").resolve()),
            "log_level": "info",
        }
        assert captured["messages"] == "hello"
        assert captured["instructions"] == agent._instructions
        assert captured["default_options"]["model"] == agent._model
        assert captured["agent_started"] is True
        assert captured["agent_stopped"] is True
        assert captured["client_stopped"] is True

    @pytest.mark.asyncio
    async def test_invoke_copilot_restarts_with_fresh_client_after_retryable_failure(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        from orchestrator_app import copilot_local_agents as mod

        mod._SESSIONS.clear()
        captured: dict[str, object] = {"client_options": []}
        run_attempts = {"count": 0}

        async def fake_auth_status():
            return mod._CopilotAuthStatus(True, login="demo-user", status_message=None)

        class FakeCopilotClient:
            def __init__(self, options):
                captured["client_options"].append(options)

            async def stop(self):
                return None

        class FakeGitHubCopilotAgent:
            def __init__(self, *, instructions=None, client=None, default_options=None, **kwargs):
                self._default_options = default_options

            async def start(self):
                return None

            async def run(self, messages, session=None, options=None, **kwargs):
                run_attempts["count"] += 1
                if run_attempts["count"] == 1:
                    raise RuntimeError("Timeout after 300s waiting for session.idle")
                captured["messages"] = messages
                captured["run_options"] = options
                return "recovered-output"

            async def stop(self):
                return None

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_COPILOT_WORKDIR", "out")
        monkeypatch.setenv("GITHUB_COPILOT_MAX_RESTARTS", "1")
        monkeypatch.setenv("GITHUB_COPILOT_RETRY_DELAY", "0")
        monkeypatch.setattr(mod, "_get_copilot_auth_status", fake_auth_status)
        monkeypatch.setattr(mod, "CopilotClient", FakeCopilotClient)
        monkeypatch.setattr(mod, "GitHubCopilotAgent", FakeGitHubCopilotAgent)

        agent = mod.DiagramCopilotAgent()
        text = await getattr(agent, "_invoke_copilot")("hello")

        assert text == "recovered-output"
        assert run_attempts["count"] == 2
        assert len(captured["client_options"]) == 2
        assert captured["messages"] == "hello"
        assert captured["run_options"]["model"] == agent._model

    @pytest.mark.asyncio
    async def test_run_prompt_restores_persisted_session_after_process_restart(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        from orchestrator_app import copilot_local_agents as mod
        from orchestrator_app import io as artifact_io

        mod._SESSIONS.clear()
        recorded_messages: list[str] = []

        async def fake_auth_status():
            return mod._CopilotAuthStatus(True, login="demo-user", status_message=None)

        class FakeCopilotClient:
            def __init__(self, options):
                self.options = options

            async def stop(self):
                return None

        class FakeGitHubCopilotAgent:
            def __init__(self, *, instructions=None, client=None, default_options=None, **kwargs):
                return None

            async def start(self):
                return None

            async def run(self, messages, session=None, options=None, **kwargs):
                recorded_messages.append(messages)
                return f"assistant-{len(recorded_messages)}"

            async def stop(self):
                return None

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("GITHUB_COPILOT_MAX_RESTARTS", "0")
        monkeypatch.setattr(mod, "_get_copilot_auth_status", fake_auth_status)
        monkeypatch.setattr(mod, "CopilotClient", FakeCopilotClient)
        monkeypatch.setattr(mod, "GitHubCopilotAgent", FakeGitHubCopilotAgent)

        agent = mod.DiagramCopilotAgent()
        first_text, session_id = await agent.run_prompt("first")

        session_payload = artifact_io.read_copilot_session_state(session_id)
        assert first_text == "assistant-1"
        assert session_payload is not None
        assert session_payload["agent_name"] == mod.DIAGRAM_AGENT
        assert session_payload["turns"][0]["user"] == "first"
        assert session_payload["events"][0]["event_type"] == "turn.started"
        assert any(event["stage"] == "invoke.completed" for event in session_payload["events"])

        mod._SESSIONS.clear()

        second_text, second_session_id = await agent.run_prompt("second", session_id=session_id)

        assert second_text == "assistant-2"
        assert second_session_id == session_id
        assert len(recorded_messages) == 2
        assert recorded_messages[0] == "first"
        assert "Continue the existing conversation" in recorded_messages[1]
        assert "first" in recorded_messages[1]
        assert "assistant-1" in recorded_messages[1]
        assert "second" in recorded_messages[1]

    @pytest.mark.asyncio
    async def test_run_prompt_persists_events_when_attempt_is_stuck_then_recovers(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        from orchestrator_app import copilot_local_agents as mod
        from orchestrator_app import io as artifact_io

        mod._SESSIONS.clear()
        run_attempts = {"count": 0}

        async def fake_auth_status():
            return mod._CopilotAuthStatus(True, login="demo-user", status_message=None)

        class FakeCopilotClient:
            def __init__(self, options):
                self.options = options

            async def stop(self):
                return None

        class FakeGitHubCopilotAgent:
            def __init__(self, *, instructions=None, client=None, default_options=None, **kwargs):
                return None

            async def start(self):
                return None

            async def run(self, messages, session=None, options=None, **kwargs):
                run_attempts["count"] += 1
                if run_attempts["count"] == 1:
                    await asyncio.sleep(0.05)
                    return "should-not-complete"
                return "recovered-output"

            async def stop(self):
                return None

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("GITHUB_COPILOT_MAX_RESTARTS", "1")
        monkeypatch.setenv("GITHUB_COPILOT_RETRY_DELAY", "0")
        monkeypatch.setenv("GITHUB_COPILOT_PROGRESS_TIMEOUT", "0.01")
        monkeypatch.setattr(mod, "_get_copilot_auth_status", fake_auth_status)
        monkeypatch.setattr(mod, "CopilotClient", FakeCopilotClient)
        monkeypatch.setattr(mod, "GitHubCopilotAgent", FakeGitHubCopilotAgent)

        agent = mod.DiagramCopilotAgent()
        text, session_id = await agent.run_prompt("hello")
        session_payload = artifact_io.read_copilot_session_state(session_id)

        assert text == "recovered-output"
        assert run_attempts["count"] == 2
        assert session_payload is not None
        assert any(event["stage"] == "agent.run.stuck" for event in session_payload["events"])
        assert any(event["event_type"] == "attempt.retry_scheduled" for event in session_payload["events"])
        assert any(event["event_type"] == "turn.completed" for event in session_payload["events"])

    @pytest.mark.asyncio
    async def test_startup_preflight_records_healthy_snapshot(self, monkeypatch):
        from orchestrator_app import copilot_local_agents as mod

        mod._HEALTH_SNAPSHOTS.clear()

        async def fake_preflight_auth(self):
            return mod._CopilotAuthStatus(True, login=f"{self._agent_name}-user", status_message="ok")

        monkeypatch.setattr(mod._CopilotPromptAgent, "_preflight_auth", fake_preflight_auth)

        report = await mod.startup_preflight(strict=True)

        assert report[mod.DIAGRAM_AGENT]["status"] == "healthy"
        assert report[mod.TERRAFORM_AGENT]["status"] == "healthy"
        assert report[mod.DIAGRAM_AGENT]["stage"] == "startup_preflight"
        assert mod.get_provider_health_snapshot(mod.DIAGRAM_AGENT)["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_startup_preflight_raises_when_unauthenticated(self, monkeypatch):
        from orchestrator_app import copilot_local_agents as mod

        mod._HEALTH_SNAPSHOTS.clear()

        async def fake_preflight_auth(self):
            return mod._CopilotAuthStatus(False, login=None, status_message="Sign in required")

        monkeypatch.setattr(mod._CopilotPromptAgent, "_preflight_auth", fake_preflight_auth)

        with pytest.raises(RuntimeError, match="unauthenticated"):
            await mod.startup_preflight(strict=True)

        snapshot = mod.get_provider_health_snapshot(mod.TERRAFORM_AGENT)
        assert snapshot["status"] == "preflight_failed"
        assert snapshot["retryable"] is False

    @pytest.mark.asyncio
    async def test_run_uses_resolved_workdir_for_client(self, monkeypatch, tmp_path: Path):
        from orchestrator_app import copilot_local_agents as mod

        captured: dict[str, object] = {}

        class FakeCopilotClient:
            def __init__(self, options):
                captured["options"] = options

            async def stop(self):
                captured["client_stopped"] = True

        class FakeGitHubCopilotAgent:
            def __init__(self, *, instructions=None, client=None, default_options=None, **kwargs):
                captured["instructions"] = instructions
                captured["default_options"] = default_options
                captured["client"] = client

            async def start(self):
                captured["agent_started"] = True

            async def run(self, messages, session=None, options=None, **kwargs):
                captured["messages"] = messages
                captured["run_options"] = options
                return {"ok": True}

            async def stop(self):
                captured["agent_stopped"] = True

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_COPILOT_WORKDIR", "out")
        monkeypatch.setattr(mod, "CopilotClient", FakeCopilotClient)
        monkeypatch.setattr(mod, "GitHubCopilotAgent", FakeGitHubCopilotAgent)

        agent = mod.DiagramCopilotAgent()
        result = await agent.run("hello")

        assert result == {"ok": True}
        assert captured["options"] == {
            "cwd": str((tmp_path / "out").resolve()),
            "log_level": "info",
        }
        assert captured["instructions"] == agent._instructions
        assert captured["default_options"]["model"] == agent._model
        assert captured["run_options"]["model"] == agent._model
        assert captured["agent_started"] is True
        assert captured["agent_stopped"] is True
        assert captured["client_stopped"] is True
