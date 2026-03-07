"""
copilot_local_agents.py — 本地 MAF Agent + GitHub Copilot provider。

用途：
  - 讓 Terraform / Diagram 兩個 specialist agent 脫離 Foundry Responses API
  - 以 prompts/*.yaml 作為 system instructions source of truth
  - 保持 executors 既有的 invoke_agent_raw(agent_name, message, previous_response_id)
    介面，讓 workflow 改動最小

設計：
  - 每個 specialist agent 以本地 BaseAgent 類別封裝
  - 真正的 LLM backend 使用 GitHubCopilotAgent
  - multi-turn session 以模組內 session store 維持，並回傳 session_id 給 executors
  - 為了避免綁死 provider session 實作，後續 turn 以歷史 transcript 重建上下文
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .foundry_agents import (
    DIAGRAM_AGENT,
    TERRAFORM_AGENT,
    classify_response,
    parse_diagram_output,
    parse_terraform_output,
)
from .i18n import extract_language_from_spec_json, human_language_instruction
from . import io as artifact_io
from .observability import get_meter, get_tracer
from .repair_feedback import build_diagram_repair_payload, build_terraform_repair_payload

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_COPILOT_WORKDIR_ENV_VAR = "GITHUB_COPILOT_WORKDIR"
_DEFAULT_COPILOT_TIMEOUT = 1200
_DEFAULT_COPILOT_PROGRESS_TIMEOUT = 180.0
_DEFAULT_COPILOT_MAX_RESTARTS = 1
_DEFAULT_COPILOT_RETRY_DELAY = 2.0
_COPILOT_STARTUP_PREFLIGHT_ENV_VAR = "GITHUB_COPILOT_STARTUP_PREFLIGHT"

_tracer = get_tracer()
_meter = get_meter()
_copilot_health_counter = _meter.create_counter(
    name="copilot_provider_health_events",
    unit="1",
    description="Health state transitions and observations for local GitHub Copilot provider",
)
_copilot_restart_counter = _meter.create_counter(
    name="copilot_provider_restarts",
    unit="1",
    description="Number of local GitHub Copilot client/agent rebuild attempts",
)
_copilot_latency_histogram = _meter.create_histogram(
    name="copilot_provider_latency_ms",
    unit="ms",
    description="Latency of local GitHub Copilot provider preflight and invoke operations",
)


def _load_prompt_doc(agent_name: str) -> dict[str, Any]:
    yaml_path = _PROMPTS_DIR / f"{agent_name}.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"Prompt YAML not found: {yaml_path}")

    with open(yaml_path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    if not isinstance(doc, dict):
        raise ValueError(f"Invalid prompt YAML structure: {yaml_path}")
    return doc


def _get_yaml_instructions(agent_name: str) -> str:
    doc = _load_prompt_doc(agent_name)
    return str(doc.get("instructions", "") or "")


def _get_yaml_model(agent_name: str) -> str:
    doc = _load_prompt_doc(agent_name)
    model_cfg = doc.get("model")
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("id", "gpt-5.4") or "gpt-5.4")
    return "gpt-5.4"


def _extract_text_from_result(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if hasattr(result, "output_text"):
        text = getattr(result, "output_text")
        if text:
            return str(text)
    if hasattr(result, "text"):
        text = getattr(result, "text")
        if text:
            return str(text)
    if hasattr(result, "messages"):
        messages = getattr(result, "messages") or []
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                content = last.get("content", "")
                if isinstance(content, list):
                    return "\n".join(str(item) for item in content)
                return str(content)
            if hasattr(last, "content"):
                content = getattr(last, "content")
                if isinstance(content, list):
                    return "\n".join(str(item) for item in content)
                return str(content)
    return str(result)


def _extract_user_text(messages: Any) -> str:
    if messages is None:
        return ""
    if isinstance(messages, str):
        return messages
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return str(msg.get("content", msg.get("text", "")))
            if hasattr(msg, "role") and str(getattr(msg, "role")) == "user":
                if hasattr(msg, "content"):
                    return str(getattr(msg, "content"))
                if hasattr(msg, "text"):
                    return str(getattr(msg, "text"))
        return "\n".join(str(m) for m in messages)
    if hasattr(messages, "content"):
        return str(getattr(messages, "content"))
    if hasattr(messages, "text"):
        return str(getattr(messages, "text"))
    return str(messages)


@dataclass
class _Turn:
    user: str
    assistant: str


@dataclass
class _SessionEvent:
    timestamp: str
    event_type: str
    stage: str
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _SessionState:
    agent_name: str
    turns: list[_Turn] = field(default_factory=list)
    events: list[_SessionEvent] = field(default_factory=list)


@dataclass(frozen=True)
class _CopilotAuthStatus:
    is_authenticated: bool
    login: str | None = None
    status_message: str | None = None


class _CopilotInvocationError(RuntimeError):
    def __init__(
        self,
        stage: str,
        message: str,
        *,
        retryable: bool,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable
        self.cause = cause


@dataclass
class _CopilotHealthSnapshot:
    agent_name: str
    status: str
    stage: str
    retryable: bool | None = None
    attempts: int = 0
    auth_status: str = "auth status unavailable"
    detail: str = ""
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "status": self.status,
            "stage": self.stage,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "auth_status": self.auth_status,
            "detail": self.detail,
            "updated_at": self.updated_at,
        }


_SESSIONS: dict[str, _SessionState] = {}
_HEALTH_SNAPSHOTS: dict[str, _CopilotHealthSnapshot] = {}

_NON_RETRYABLE_EXCEPTION_TYPES = (FileNotFoundError, NotADirectoryError, ValueError, TypeError)
_NON_RETRYABLE_MARKERS = (
    "not authenticated",
    "unauthenticated",
    "sign in required",
    "permission denied",
    "access denied",
    "forbidden",
    "invalid prompt yaml",
    "prompt yaml not found",
    "prompt yaml",
    "unsupported model",
    "model not found",
    "invalid model",
    "invalid_request_error",
    "invalid request",
    "malformed request",
    "bad request",
    "missing required",
    "401",
    "403",
    "404",
    "429 quota exceeded permanently",
)
_RETRYABLE_MARKERS = (
    "session.idle",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "broken pipe",
    "transport",
    "network",
    "empty response",
    "econnreset",
    "rate limit",
    "too many requests",
    "429",
    "500",
    "502",
    "503",
    "504",
    "service unavailable",
    "upstream",
    "gateway timeout",
    "server disconnected",
)


def _get_timeout_env_var(agent_name: str) -> str | None:
    if agent_name == DIAGRAM_AGENT:
        return "DIAGRAM_AGENT_TIMEOUT"
    if agent_name == TERRAFORM_AGENT:
        return "TERRAFORM_AGENT_TIMEOUT"
    return None


def _get_progress_timeout_env_var(agent_name: str) -> str | None:
    if agent_name == DIAGRAM_AGENT:
        return "DIAGRAM_AGENT_PROGRESS_TIMEOUT"
    if agent_name == TERRAFORM_AGENT:
        return "TERRAFORM_AGENT_PROGRESS_TIMEOUT"
    return None


def _resolve_copilot_timeout(agent_name: str) -> int:
    raw_timeout = os.getenv("GITHUB_COPILOT_TIMEOUT")
    if raw_timeout:
        return max(1, int(float(raw_timeout)))

    agent_specific_var = _get_timeout_env_var(agent_name)
    if agent_specific_var:
        agent_specific_timeout = os.getenv(agent_specific_var)
        if agent_specific_timeout:
            return max(1, int(float(agent_specific_timeout)))

    raw_agent_timeout = os.getenv("AGENT_TIMEOUT")
    if raw_agent_timeout:
        return max(300, int(float(raw_agent_timeout)))

    return _DEFAULT_COPILOT_TIMEOUT


def _resolve_copilot_progress_timeout(agent_name: str, total_timeout: int) -> float:
    raw_timeout = os.getenv("GITHUB_COPILOT_PROGRESS_TIMEOUT")
    if raw_timeout:
        return max(0.0, float(raw_timeout))

    agent_specific_var = _get_progress_timeout_env_var(agent_name)
    if agent_specific_var:
        agent_specific_timeout = os.getenv(agent_specific_var)
        if agent_specific_timeout:
            return max(0.0, float(agent_specific_timeout))

    return max(0.0, min(float(total_timeout), _DEFAULT_COPILOT_PROGRESS_TIMEOUT))


def _resolve_copilot_max_restarts() -> int:
    raw_value = os.getenv("GITHUB_COPILOT_MAX_RESTARTS")
    if not raw_value:
        return _DEFAULT_COPILOT_MAX_RESTARTS
    return max(0, int(float(raw_value)))


def _resolve_copilot_retry_delay() -> float:
    raw_value = os.getenv("GITHUB_COPILOT_RETRY_DELAY")
    if not raw_value:
        return _DEFAULT_COPILOT_RETRY_DELAY
    return max(0.0, float(raw_value))


def _resolve_startup_preflight_enabled() -> bool:
    raw_value = (os.getenv(_COPILOT_STARTUP_PREFLIGHT_ENV_VAR, "true") or "").strip().lower()
    return raw_value in {"1", "true", "yes", "on"}


def _resolve_copilot_workdir() -> str:
    raw_workdir = (os.getenv(_COPILOT_WORKDIR_ENV_VAR) or "").strip()
    base_dir = Path.cwd()

    if raw_workdir:
        workdir = Path(raw_workdir).expanduser()
        if not workdir.is_absolute():
            workdir = base_dir / workdir
    else:
        workdir = base_dir

    resolved_workdir = workdir.resolve()
    if resolved_workdir.exists() and not resolved_workdir.is_dir():
        raise NotADirectoryError(
            f"{_COPILOT_WORKDIR_ENV_VAR} must point to a directory: {resolved_workdir}"
        )

    resolved_workdir.mkdir(parents=True, exist_ok=True)
    return str(resolved_workdir)


def _format_auth_status(auth_status: _CopilotAuthStatus | None) -> str:
    if auth_status is None:
        return "auth status unavailable"
    if auth_status.is_authenticated:
        identity = auth_status.login or "logged-in user"
        return f"authenticated as {identity}"
    message = auth_status.status_message or "not authenticated"
    return f"unauthenticated ({message})"


def _update_health_snapshot(
    agent_name: str,
    *,
    status: str,
    stage: str,
    retryable: bool | None = None,
    attempts: int = 0,
    auth_status: _CopilotAuthStatus | None = None,
    detail: str = "",
) -> dict[str, Any]:
    snapshot = _CopilotHealthSnapshot(
        agent_name=agent_name,
        status=status,
        stage=stage,
        retryable=retryable,
        attempts=attempts,
        auth_status=_format_auth_status(auth_status),
        detail=detail,
    )
    _HEALTH_SNAPSHOTS[agent_name] = snapshot
    attributes = {
        "copilot.agent_name": agent_name,
        "copilot.status": status,
        "copilot.stage": stage,
        "copilot.retryable": retryable if retryable is not None else False,
        "copilot.attempts": attempts,
    }
    _copilot_health_counter.add(1, attributes=attributes)
    log_message = (
        "[CopilotHealth] agent=%s status=%s stage=%s retryable=%s attempts=%s auth=%s detail=%s"
    )
    log_args = (
        agent_name,
        status,
        stage,
        retryable,
        attempts,
        snapshot.auth_status,
        detail or "-",
    )
    if status in {"unhealthy", "preflight_failed", "degraded"}:
        logger.warning(log_message, *log_args)
    else:
        logger.info(log_message, *log_args)
    return snapshot.to_dict()


def get_provider_health_snapshot(agent_name: str | None = None) -> dict[str, Any]:
    if agent_name is not None:
        snapshot = _HEALTH_SNAPSHOTS.get(agent_name)
        return snapshot.to_dict() if snapshot is not None else {}
    return {name: snapshot.to_dict() for name, snapshot in _HEALTH_SNAPSHOTS.items()}


def _format_copilot_error(
    agent_name: str,
    model: str | None,
    timeout: int,
    error: Exception,
    auth_status: _CopilotAuthStatus | None,
    *,
    stage: str | None = None,
    attempts: int = 1,
) -> str:
    error_text = str(error)
    stage_text = f", stage={stage}" if stage else ""
    attempts_text = f", attempts={attempts}" if attempts > 1 else ""
    if stage == "agent.run.stuck":
        return (
            f"Local GitHub Copilot provider showed no completion progress within {timeout}s "
            f"(agent={agent_name}, model={model or 'default'}{stage_text}{attempts_text}, {_format_auth_status(auth_status)}). "
            "The local client was proactively restarted before the overall provider timeout elapsed. "
            f"Original error: {error_text}"
        )
    if "session.idle" in error_text:
        return (
            f"Local GitHub Copilot provider timed out after {timeout}s waiting for session.idle "
            f"(agent={agent_name}, model={model or 'default'}{stage_text}{attempts_text}, {_format_auth_status(auth_status)}). "
            "Check that the remote shell can access the same GitHub Copilot sign-in state as VS Code, "
            "or increase GITHUB_COPILOT_TIMEOUT. The local client was recreated before the final failure. "
            f"Original error: {error_text}"
        )
    return (
        f"Local GitHub Copilot provider failed "
        f"(agent={agent_name}, model={model or 'default'}, timeout={timeout}s{stage_text}{attempts_text}, {_format_auth_status(auth_status)}). "
        f"Original error: {error_text}"
    )


def _is_retryable_copilot_exception(exc: Exception) -> bool:
    if isinstance(exc, _CopilotInvocationError):
        return exc.retryable

    if isinstance(exc, _NON_RETRYABLE_EXCEPTION_TYPES):
        return False

    text = str(exc).lower()
    if any(marker in text for marker in _NON_RETRYABLE_MARKERS):
        return False

    if any(marker in text for marker in _RETRYABLE_MARKERS):
        return True

    return False


def _serialize_session_state(session_id: str, session: _SessionState) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "agent_name": session.agent_name,
        "turns": [
            {
                "user": turn.user,
                "assistant": turn.assistant,
            }
            for turn in session.turns
        ],
        "events": [
            {
                "timestamp": event.timestamp,
                "event_type": event.event_type,
                "stage": event.stage,
                "detail": event.detail,
                "payload": event.payload,
            }
            for event in session.events
        ],
    }


def _persist_session_state(session_id: str, session: _SessionState) -> None:
    artifact_io.write_copilot_session_state(
        session_id,
        _serialize_session_state(session_id, session),
    )


def _json_safe(value: Any, *, depth: int = 4) -> Any:
    if depth <= 0:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth=depth - 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth=depth - 1) for v in value]
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value), depth=depth - 1)
    return str(value)


def _summarize_result(result: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "result_type": type(result).__name__,
    }
    text = _extract_text_from_result(result)
    if text:
        payload["text_preview"] = text[:1000]
        payload["text_length"] = len(text)
    messages = getattr(result, "messages", None)
    if messages is not None:
        safe_messages = _json_safe(messages)
        payload["messages"] = safe_messages
        if isinstance(safe_messages, list):
            payload["message_count"] = len(safe_messages)
    return payload


def _append_session_event(
    session_id: str,
    session: _SessionState,
    *,
    event_type: str,
    stage: str,
    detail: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    session.events.append(
        _SessionEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            stage=stage,
            detail=detail,
            payload=payload or {},
        )
    )
    _persist_session_state(session_id, session)


def _restore_session_state(session_id: str) -> _SessionState | None:
    payload = artifact_io.read_copilot_session_state(session_id)
    if payload is None:
        return None

    turns_payload = payload.get("turns")
    if not isinstance(turns_payload, list):
        logger.warning("[CopilotLocalAgent] invalid persisted session payload for %s", session_id)
        return None

    agent_name = str(payload.get("agent_name") or "")
    turns: list[_Turn] = []
    events: list[_SessionEvent] = []
    for item in turns_payload:
        if not isinstance(item, dict):
            continue
        turns.append(
            _Turn(
                user=str(item.get("user", "")),
                assistant=str(item.get("assistant", "")),
            )
        )

    events_payload = payload.get("events")
    if isinstance(events_payload, list):
        for item in events_payload:
            if not isinstance(item, dict):
                continue
            raw_payload = item.get("payload")
            events.append(
                _SessionEvent(
                    timestamp=str(item.get("timestamp") or datetime.now(timezone.utc).isoformat()),
                    event_type=str(item.get("event_type") or "event"),
                    stage=str(item.get("stage") or "unknown"),
                    detail=str(item.get("detail") or ""),
                    payload=raw_payload if isinstance(raw_payload, dict) else {},
                )
            )

    restored = _SessionState(agent_name=agent_name, turns=turns, events=events)
    _SESSIONS[session_id] = restored
    logger.info(
        "[CopilotLocalAgent] restored session=%s agent=%s turns=%d events=%d from disk",
        session_id,
        agent_name or "(unknown)",
        len(turns),
        len(events),
    )
    return restored


def _get_or_restore_session(session_id: str | None, agent_name: str) -> tuple[str, _SessionState]:
    resolved_session_id = session_id or str(uuid4())
    session = _SESSIONS.get(resolved_session_id)

    if session is None and session_id:
        session = _restore_session_state(resolved_session_id)

    if session is None:
        session = _SessionState(agent_name=agent_name)
        _SESSIONS[resolved_session_id] = session

    if session.agent_name != agent_name:
        logger.warning(
            "[CopilotLocalAgent] session=%s belongs to %s, creating a new session for %s",
            resolved_session_id,
            session.agent_name,
            agent_name,
        )
        resolved_session_id = str(uuid4())
        session = _SessionState(agent_name=agent_name)
        _SESSIONS[resolved_session_id] = session

    return resolved_session_id, session


try:
    from agent_framework import AgentResponse, AgentResponseUpdate, AgentSession, BaseAgent
    from agent_framework.github import GitHubCopilotAgent
    from copilot import CopilotClient, PermissionHandler

    def _build_copilot_client() -> CopilotClient:
        return CopilotClient(
            {
                "cwd": _resolve_copilot_workdir(),
                "log_level": os.getenv("GITHUB_COPILOT_LOG_LEVEL", "info"),
            }
        )

    async def _get_copilot_auth_status() -> _CopilotAuthStatus | None:
        client = _build_copilot_client()
        try:
            await client.start()
            status = await client.get_auth_status()
            return _CopilotAuthStatus(
                is_authenticated=bool(status.isAuthenticated),
                login=status.login,
                status_message=status.statusMessage,
            )
        finally:
            try:
                await client.stop()
            except Exception as stop_exc:  # pragma: no cover - cleanup only
                logger.debug("[CopilotLocalAgent] auth preflight stop failed: %s", stop_exc)

    class _CopilotPromptAgent(BaseAgent):
        """以 GitHub Copilot provider 執行 YAML-defined prompt agent。"""

        def __init__(self, *, agent_name: str, **kwargs):
            super().__init__(name=agent_name, **kwargs)
            self._agent_name = agent_name
            self._instructions = _get_yaml_instructions(agent_name)
            self._model = os.getenv("GITHUB_COPILOT_MODEL", _get_yaml_model(agent_name))
            self._timeout = _resolve_copilot_timeout(agent_name)
            self._progress_timeout = _resolve_copilot_progress_timeout(agent_name, self._timeout)
            self._max_restarts = _resolve_copilot_max_restarts()
            self._retry_delay = _resolve_copilot_retry_delay()

        def health_snapshot(self) -> dict[str, Any]:
            return get_provider_health_snapshot(self._agent_name)

        async def _run_with_copilot_agent(
            self,
            *,
            instructions: str | None,
            default_options: dict[str, Any],
            messages: Any,
            session: AgentSession | None = None,
            event_recorder: Any = None,
            **kwargs,
        ) -> Any:
            try:
                if event_recorder is not None:
                    event_recorder(
                        event_type="attempt.stage",
                        stage="client.build",
                        detail="building Copilot client",
                    )
                client = _build_copilot_client()
            except Exception as exc:
                raise _CopilotInvocationError(
                    "client.build",
                    f"Failed to create Copilot client: {exc}",
                    retryable=_is_retryable_copilot_exception(exc),
                    cause=exc,
                ) from exc

            try:
                if event_recorder is not None:
                    event_recorder(
                        event_type="attempt.stage",
                        stage="agent.build",
                        detail="building GitHub Copilot agent",
                    )
                agent = GitHubCopilotAgent(
                    instructions=instructions,
                    client=client,
                    default_options=default_options,
                )
            except Exception as exc:
                raise _CopilotInvocationError(
                    "agent.build",
                    f"Failed to create GitHub Copilot agent: {exc}",
                    retryable=_is_retryable_copilot_exception(exc),
                    cause=exc,
                ) from exc

            try:
                try:
                    if event_recorder is not None:
                        event_recorder(
                            event_type="attempt.stage",
                            stage="agent.start",
                            detail="starting GitHub Copilot agent",
                        )
                    await agent.start()
                    if event_recorder is not None:
                        event_recorder(
                            event_type="attempt.stage",
                            stage="agent.start",
                            detail="GitHub Copilot agent started",
                        )
                except Exception as exc:
                    raise _CopilotInvocationError(
                        "agent.start",
                        f"GitHub Copilot agent failed to start: {exc}",
                        retryable=_is_retryable_copilot_exception(exc),
                        cause=exc,
                    ) from exc

                try:
                    if event_recorder is not None:
                        event_recorder(
                            event_type="attempt.stage",
                            stage="agent.run",
                            detail="awaiting GitHub Copilot response",
                            payload={
                                "timeout": self._timeout,
                                "progress_timeout": self._progress_timeout,
                            },
                        )
                    run_coro = agent.run(messages, session=session, options=default_options, **kwargs)
                    if self._progress_timeout > 0:
                        result = await asyncio.wait_for(run_coro, timeout=self._progress_timeout)
                    else:
                        result = await run_coro
                    if event_recorder is not None:
                        event_recorder(
                            event_type="attempt.stage",
                            stage="agent.run.completed",
                            detail="GitHub Copilot response received",
                            payload=_summarize_result(result),
                        )
                    return result
                except asyncio.TimeoutError as exc:
                    raise _CopilotInvocationError(
                        "agent.run.stuck",
                        (
                            "GitHub Copilot agent showed no completion progress "
                            f"within {self._progress_timeout}s"
                        ),
                        retryable=True,
                        cause=exc,
                    ) from exc
                except Exception as exc:
                    raise _CopilotInvocationError(
                        "agent.run",
                        f"GitHub Copilot agent run failed: {exc}",
                        retryable=_is_retryable_copilot_exception(exc),
                        cause=exc,
                    ) from exc
            finally:
                with contextlib.suppress(Exception):
                    await agent.stop()
                with contextlib.suppress(Exception):
                    await client.stop()

        async def _preflight_auth(self) -> _CopilotAuthStatus | None:
            started = asyncio.get_running_loop().time()
            try:
                status = await _get_copilot_auth_status()
            except Exception as exc:
                logger.warning("[CopilotLocalAgent] auth preflight unavailable for %s: %s", self._agent_name, exc)
                _copilot_latency_histogram.record(
                    (asyncio.get_running_loop().time() - started) * 1000.0,
                    attributes={
                        "copilot.agent_name": self._agent_name,
                        "copilot.operation": "preflight_auth",
                        "copilot.status": "degraded",
                    },
                )
                _update_health_snapshot(
                    self._agent_name,
                    status="degraded",
                    stage="preflight.auth",
                    retryable=_is_retryable_copilot_exception(exc),
                    detail=str(exc),
                )
                return None

            elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000.0
            _copilot_latency_histogram.record(
                elapsed_ms,
                attributes={
                    "copilot.agent_name": self._agent_name,
                    "copilot.operation": "preflight_auth",
                    "copilot.status": "healthy" if (status and status.is_authenticated) else "unhealthy",
                },
            )

            if status is not None:
                logger.info(
                    "[CopilotLocalAgent] auth preflight for %s: %s",
                    self._agent_name,
                    _format_auth_status(status),
                )
                _update_health_snapshot(
                    self._agent_name,
                    status="healthy" if status.is_authenticated else "preflight_failed",
                    stage="preflight.auth",
                    retryable=False,
                    auth_status=status,
                    detail=status.status_message or status.login or "auth preflight completed",
                )
            return status

        async def _invoke_copilot(self, prompt: str, *, event_recorder: Any = None) -> str:
            auth_status = await self._preflight_auth()
            if auth_status is not None and event_recorder is not None:
                event_recorder(
                    event_type="auth.preflight",
                    stage="preflight.auth",
                    detail=auth_status.status_message or auth_status.login or "auth preflight completed",
                    payload={
                        "authenticated": auth_status.is_authenticated,
                        "login": auth_status.login or "",
                    },
                )
            if auth_status is not None and not auth_status.is_authenticated:
                _update_health_snapshot(
                    self._agent_name,
                    status="preflight_failed",
                    stage="preflight.auth",
                    retryable=False,
                    auth_status=auth_status,
                    detail=auth_status.status_message or "not authenticated",
                )
                raise RuntimeError(
                    f"Local GitHub Copilot provider is not authenticated for {self._agent_name}. "
                    f"Status: {auth_status.status_message or 'not authenticated'}"
                )

            options = {
                "timeout": self._timeout,
                "on_permission_request": PermissionHandler.approve_all,
            }
            if self._model:
                options["model"] = self._model

            attempts = self._max_restarts + 1
            last_exc: Exception | None = None
            invoke_started = asyncio.get_running_loop().time()
            for attempt in range(1, attempts + 1):
                try:
                    with _tracer.start_as_current_span(
                        f"invoke_copilot_local:{self._agent_name}",
                        attributes={
                            "copilot.agent_name": self._agent_name,
                            "copilot.attempt": attempt,
                            "copilot.max_attempts": attempts,
                            "copilot.timeout_s": self._timeout,
                            "copilot.progress_timeout_s": self._progress_timeout,
                            "copilot.has_auth_status": auth_status is not None,
                        },
                    ):
                        if event_recorder is not None:
                            event_recorder(
                                event_type="attempt.started",
                                stage="invoke",
                                detail=f"starting attempt {attempt} of {attempts}",
                                payload={"attempt": attempt, "attempts": attempts},
                            )
                        _update_health_snapshot(
                            self._agent_name,
                            status="starting",
                            stage="invoke",
                            attempts=attempt,
                            auth_status=auth_status,
                            detail="Starting local GitHub Copilot invocation",
                        )
                        result = await self._run_with_copilot_agent(
                            instructions=self._instructions,
                            default_options=options,
                            messages=prompt,
                            event_recorder=event_recorder,
                        )
                    response_text = _extract_text_from_result(result)
                    if not response_text.strip():
                        raise _CopilotInvocationError(
                            "result.empty",
                            "Empty response from local GitHub Copilot provider",
                            retryable=True,
                        )
                    _copilot_latency_histogram.record(
                        (asyncio.get_running_loop().time() - invoke_started) * 1000.0,
                        attributes={
                            "copilot.agent_name": self._agent_name,
                            "copilot.operation": "invoke",
                            "copilot.status": "healthy",
                        },
                    )
                    _update_health_snapshot(
                        self._agent_name,
                        status="healthy",
                        stage="invoke.complete",
                        attempts=attempt,
                        auth_status=auth_status,
                        detail=f"Invocation completed successfully (response_length={len(response_text)})",
                    )
                    if event_recorder is not None:
                        event_recorder(
                            event_type="attempt.succeeded",
                            stage="invoke.completed",
                            detail=f"attempt {attempt} completed successfully",
                            payload={"attempt": attempt, "response_length": len(response_text)},
                        )
                    return response_text
                except _CopilotInvocationError as exc:
                    last_exc = exc
                    if event_recorder is not None:
                        event_recorder(
                            event_type="attempt.failed",
                            stage=exc.stage,
                            detail=str(exc),
                            payload={
                                "attempt": attempt,
                                "retryable": exc.retryable,
                            },
                        )
                    if exc.retryable and attempt < attempts:
                        wait = self._retry_delay * (2 ** (attempt - 1))
                        _copilot_restart_counter.add(
                            1,
                            attributes={
                                "copilot.agent_name": self._agent_name,
                                "copilot.stage": exc.stage,
                            },
                        )
                        _update_health_snapshot(
                            self._agent_name,
                            status="degraded",
                            stage=exc.stage,
                            retryable=True,
                            attempts=attempt,
                            auth_status=auth_status,
                            detail=f"{exc}; restarting local client after {wait:.1f}s",
                        )
                        logger.warning(
                            "[CopilotLocalAgent] agent=%s unhealthy at stage=%s attempt=%d/%d; recreating client in %.1fs (%s)",
                            self._agent_name,
                            exc.stage,
                            attempt,
                            attempts,
                            wait,
                            exc,
                        )
                        if event_recorder is not None:
                            event_recorder(
                                event_type="attempt.retry_scheduled",
                                stage=exc.stage,
                                detail=f"recreating client in {wait:.1f}s",
                                payload={
                                    "attempt": attempt,
                                    "next_attempt": attempt + 1,
                                    "delay_s": wait,
                                },
                            )
                        if wait > 0:
                            await asyncio.sleep(wait)
                        continue
                    _copilot_latency_histogram.record(
                        (asyncio.get_running_loop().time() - invoke_started) * 1000.0,
                        attributes={
                            "copilot.agent_name": self._agent_name,
                            "copilot.operation": "invoke",
                            "copilot.status": "unhealthy",
                        },
                    )
                    _update_health_snapshot(
                        self._agent_name,
                        status="unhealthy",
                        stage=exc.stage,
                        retryable=exc.retryable,
                        attempts=attempt,
                        auth_status=auth_status,
                        detail=str(exc),
                    )
                    raise RuntimeError(
                        _format_copilot_error(
                            self._agent_name,
                            self._model,
                            self._progress_timeout if exc.stage == "agent.run.stuck" else self._timeout,
                            exc,
                            auth_status,
                            stage=exc.stage,
                            attempts=attempt,
                        )
                    ) from exc
                except Exception as exc:
                    last_exc = exc
                    _copilot_latency_histogram.record(
                        (asyncio.get_running_loop().time() - invoke_started) * 1000.0,
                        attributes={
                            "copilot.agent_name": self._agent_name,
                            "copilot.operation": "invoke",
                            "copilot.status": "unhealthy",
                        },
                    )
                    _update_health_snapshot(
                        self._agent_name,
                        status="unhealthy",
                        stage="invoke.exception",
                        retryable=_is_retryable_copilot_exception(exc),
                        attempts=attempt,
                        auth_status=auth_status,
                        detail=str(exc),
                    )
                    if event_recorder is not None:
                        event_recorder(
                            event_type="attempt.failed",
                            stage="invoke.exception",
                            detail=str(exc),
                            payload={
                                "attempt": attempt,
                                "retryable": _is_retryable_copilot_exception(exc),
                            },
                        )
                    raise RuntimeError(
                        _format_copilot_error(
                            self._agent_name,
                            self._model,
                            self._timeout,
                            exc,
                            auth_status,
                            stage="invoke.exception",
                            attempts=attempt,
                        )
                    ) from exc

            assert last_exc is not None
            raise RuntimeError(
                _format_copilot_error(
                    self._agent_name,
                    self._model,
                    self._timeout,
                    last_exc,
                    auth_status,
                    attempts=attempts,
                )
            ) from last_exc

        async def run_prompt(self, prompt: str, *, session_id: str | None = None) -> tuple[str, str]:
            resolved_session_id, session = _get_or_restore_session(session_id, self._agent_name)

            def record_event(
                *,
                event_type: str,
                stage: str,
                detail: str = "",
                payload: dict[str, Any] | None = None,
            ) -> None:
                _append_session_event(
                    resolved_session_id,
                    session,
                    event_type=event_type,
                    stage=stage,
                    detail=detail,
                    payload=payload,
                )

            record_event(
                event_type="turn.started",
                stage="run_prompt",
                detail="starting Copilot prompt invocation",
                payload={
                    "prompt_preview": prompt[:500],
                    "prompt_length": len(prompt),
                    "history_turns": len(session.turns),
                },
            )

            compiled_prompt = _compose_prompt_with_history(prompt, session.turns)
            logger.info(
                "[CopilotLocalAgent] invoke agent=%s model=%s session=%s turns=%d",
                self._agent_name,
                self._model,
                resolved_session_id,
                len(session.turns),
            )
            try:
                response_text = await self._invoke_copilot(compiled_prompt, event_recorder=record_event)
            except Exception as exc:
                record_event(
                    event_type="turn.failed",
                    stage="run_prompt.failed",
                    detail=str(exc),
                )
                raise
            session.turns.append(_Turn(user=prompt, assistant=response_text))
            record_event(
                event_type="turn.completed",
                stage="run_prompt.completed",
                detail="persisting completed turn",
                payload={
                    "response_preview": response_text[:1000],
                    "response_length": len(response_text),
                },
            )
            _persist_session_state(resolved_session_id, session)
            return response_text, resolved_session_id

        async def run(
            self,
            messages: Any = None,
            *,
            session: AgentSession | None = None,
            options: dict[str, Any] | None = None,
            **kwargs,
        ) -> AgentResponse:
            prompt = _extract_user_text(messages)
            session_id = None
            if session is not None and hasattr(session, "id"):
                session_id = str(getattr(session, "id"))

            resolved_options = dict(options or {})
            if self._instructions and "instructions" not in resolved_options:
                resolved_options["instructions"] = self._instructions
            if self._model and "model" not in resolved_options:
                resolved_options["model"] = self._model
            if self._timeout and "timeout" not in resolved_options:
                resolved_options["timeout"] = self._timeout

            resolved_instructions = resolved_options.pop("instructions", None)
            return await self._run_with_copilot_agent(
                instructions=resolved_instructions,
                default_options=resolved_options,
                messages=messages,
                session=session,
                **kwargs,
            )

        async def run_stream(
            self,
            messages: Any = None,
            *,
            session: AgentSession | None = None,
            options: dict[str, Any] | None = None,
            **kwargs,
        ):
            prompt = _extract_user_text(messages)
            session_id = None
            if session is not None and hasattr(session, "id"):
                session_id = str(getattr(session, "id"))

            async def _stream():
                yield AgentResponseUpdate(text=f"🚀 {self._agent_name} 啟動...\n")
                text, _ = await self.run_prompt(prompt, session_id=session_id)
                yield AgentResponseUpdate(text=text)

            return _stream()


    class TerraformCopilotAgent(_CopilotPromptAgent):
        def __init__(self, **kwargs):
            super().__init__(agent_name=TERRAFORM_AGENT, **kwargs)


    class DiagramCopilotAgent(_CopilotPromptAgent):
        def __init__(self, **kwargs):
            super().__init__(agent_name=DIAGRAM_AGENT, **kwargs)


except ImportError as exc:  # pragma: no cover - 僅在未安裝 provider 時觸發
    GitHubCopilotAgent = None  # type: ignore[assignment]
    CopilotClient = None  # type: ignore[assignment]
    logger.warning(
        "GitHub Copilot provider unavailable — install 'agent-framework-github-copilot --pre'. (%s)",
        exc,
    )

    class _CopilotPromptAgent:  # type: ignore[no-redef]
        def __init__(self, *, agent_name: str, **kwargs):
            self.name = agent_name

        async def run_prompt(self, prompt: str, *, session_id: str | None = None) -> tuple[str, str]:
            raise ImportError(
                "GitHub Copilot provider unavailable — install 'agent-framework-github-copilot --pre'."
            )


    class TerraformCopilotAgent(_CopilotPromptAgent):
        def __init__(self, **kwargs):
            super().__init__(agent_name=TERRAFORM_AGENT, **kwargs)


    class DiagramCopilotAgent(_CopilotPromptAgent):
        def __init__(self, **kwargs):
            super().__init__(agent_name=DIAGRAM_AGENT, **kwargs)


def _compose_prompt_with_history(current_prompt: str, turns: list[_Turn]) -> str:
    if not turns:
        return current_prompt

    history_lines = [
        "Continue the existing conversation using the same output contract and constraints.",
        "Conversation history:",
    ]
    for idx, turn in enumerate(turns, start=1):
        history_lines.append(f"<turn index=\"{idx}\" role=\"user\">\n{turn.user}\n</turn>")
        history_lines.append(
            f"<turn index=\"{idx}\" role=\"assistant\">\n{turn.assistant}\n</turn>"
        )
    history_lines.append("Current user message:")
    history_lines.append(current_prompt)
    return "\n\n".join(history_lines)


_AGENT_REGISTRY: dict[str, _CopilotPromptAgent] = {
    TERRAFORM_AGENT: TerraformCopilotAgent(),
    DIAGRAM_AGENT: DiagramCopilotAgent(),
}


async def invoke_agent_raw(
    agent_name: str,
    message: str,
    *,
    previous_response_id: str | None = None,
    **_: Any,
) -> tuple[str, str]:
    agent = _AGENT_REGISTRY.get(agent_name)
    if agent is None:
        raise ValueError(f"Unsupported local Copilot agent: {agent_name}")
    return await agent.run_prompt(message, session_id=previous_response_id)


async def startup_preflight(*, strict: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for agent_name, agent in _AGENT_REGISTRY.items():
        try:
            status = await agent._preflight_auth()
        except Exception as exc:
            _update_health_snapshot(
                agent_name,
                status="preflight_failed",
                stage="startup_preflight",
                retryable=False,
                detail=str(exc),
            )
            if strict:
                raise RuntimeError(
                    f"Startup preflight failed for local GitHub Copilot agent {agent_name}: {exc}"
                ) from exc
            report[agent_name] = get_provider_health_snapshot(agent_name)
            continue

        if status is None:
            detail = "Auth status unavailable during startup preflight"
            _update_health_snapshot(
                agent_name,
                status="degraded",
                stage="startup_preflight",
                retryable=True,
                detail=detail,
            )
            if strict:
                raise RuntimeError(
                    f"Startup preflight could not confirm local GitHub Copilot health for {agent_name}"
                )
        elif not status.is_authenticated:
            _update_health_snapshot(
                agent_name,
                status="preflight_failed",
                stage="startup_preflight",
                retryable=False,
                auth_status=status,
                detail=status.status_message or "not authenticated",
            )
            if strict:
                raise RuntimeError(
                    f"Startup preflight found local GitHub Copilot unauthenticated for {agent_name}: {status.status_message or 'not authenticated'}"
                )
        else:
            _update_health_snapshot(
                agent_name,
                status="healthy",
                stage="startup_preflight",
                retryable=False,
                auth_status=status,
                detail=f"Authenticated as {status.login or 'logged-in user'}",
            )
        report[agent_name] = get_provider_health_snapshot(agent_name)
    return report


def build_terraform_prompt(spec_json: str, approved_resource_manifest_json: str) -> str:
    language_instruction = human_language_instruction(extract_language_from_spec_json(spec_json))
    return (
        f"Language rule: {language_instruction}\n\n"
        "Runtime persistence note: Orchestrator will write Terraform outputs under OUTPUT_DIR (default ./out), using ./out/terraform/* and ./out/resource_manifest.json.\n\n"
        "Execution rule: Do not ask clarifying questions. If information is missing or conflicting, make the minimal reasonable assumptions, document them in readme_md, and still return the final Terraform JSON envelope.\n"
        "Output rule: Your entire reply must be exactly one JSON object (or the same single JSON object wrapped in ```json) with no prose, no explanation, and no extra markdown sections outside that JSON object.\n\n"
        "Input payload for Terraform generation.\n\n"
        "spec.json:\n"
        f"```json\n{spec_json}\n```\n\n"
        "approved_resource_manifest.json:\n"
        f"```json\n{approved_resource_manifest_json}\n```"
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
    repair_context_json: str = "",
) -> str:
    language_instruction = human_language_instruction(extract_language_from_spec_json(spec_json))
    payload_block = build_terraform_repair_payload(
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
        spec_heading="spec.json:",
        manifest_heading="approved_resource_manifest.json:",
        main_heading="main.tf:",
        variables_heading="variables.tf:",
        outputs_heading="outputs.tf:",
        locals_heading="locals.tf:",
        versions_heading="versions.tf:",
        providers_heading="providers.tf:",
        error_heading="terraform validation error:",
        repair_context_heading="standardized repair context:",
    )

    return (
        f"Language rule: {language_instruction}\n\n"
        "Runtime persistence note: Orchestrator will write the corrected Terraform outputs under OUTPUT_DIR (default ./out), using ./out/terraform/* and ./out/resource_manifest.json.\n\n"
        "Execution mode: validation_fix\n\n"
        "Execution rule: Do not ask clarifying questions. Return the corrected final Terraform JSON envelope only.\n"
        "Output rule: Your entire reply must be exactly one JSON object (or the same single JSON object wrapped in ```json) with no prose, no explanation, and no extra markdown sections outside that JSON object.\n\n"
        f"{payload_block}"
    )


def build_diagram_prompt(spec_json: str, architecture_details_json: str = "{}") -> str:
    language_instruction = human_language_instruction(extract_language_from_spec_json(spec_json))
    return (
        f"Language rule: {language_instruction}\n\n"
        "Runtime persistence note: Orchestrator will write diagram outputs under OUTPUT_DIR (default ./out), including ./out/diagram.py, ./out/render_log.txt, ./out/diagram.png or ./out/diagram.svg, and ./out/resource_manifest.json.\n\n"
        "Input payload for diagram generation.\n\n"
        "spec.json:\n"
        f"```json\n{spec_json}\n```\n\n"
        "architecture_details.json:\n"
        f"```json\n{architecture_details_json}\n```"
    )


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
    language_instruction = human_language_instruction(extract_language_from_spec_json(spec_json))
    payload_block = build_diagram_repair_payload(
        spec_json=spec_json,
        architecture_details_json=architecture_details_json,
        previous_diagram_py=previous_diagram_py,
        render_error=render_error,
        render_log=render_log,
        previous_approved_resource_manifest_json=previous_approved_resource_manifest_json,
        available_classes_summary=available_classes_summary,
        repair_context_json=repair_context_json,
        spec_heading="spec.json:",
        architecture_heading="architecture_details.json:",
        diagram_heading="latest failed diagram.py:",
        error_heading="render error:",
        render_log_heading="renderer render_log:",
        manifest_heading="current approved_resource_manifest.json:",
        classes_heading="available classes summary:",
        repair_context_heading="standardized repair context:",
    )
    return (
        f"Language rule: {language_instruction}\n\n"
        "Runtime persistence note: The regenerated diagram outputs will still be written under OUTPUT_DIR (default ./out).\n\n"
        "Execution mode: render_regen\n\n"
        f"Repair round: {regen_attempt}\n"
        "The downstream local renderer already executed the script and returned authoritative runtime feedback.\n"
        "Use the latest failed diagram.py below as your repair baseline. Do not ask clarifying questions.\n"
        "Keep approved_resource_manifest stable unless resource names/topology truly changed.\n\n"
        f"{payload_block}"
    )


__all__ = [
    "TERRAFORM_AGENT",
    "DIAGRAM_AGENT",
    "TerraformCopilotAgent",
    "DiagramCopilotAgent",
    "invoke_agent_raw",
    "classify_response",
    "parse_terraform_output",
    "parse_diagram_output",
    "build_terraform_prompt",
    "build_terraform_fix_prompt",
    "build_diagram_prompt",
    "build_diagram_regen_prompt",
]