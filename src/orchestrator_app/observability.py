"""
observability.py — Azure Monitor + OpenTelemetry 可觀測性設定。

功能：
  1. configure_azure_monitor() — 將 traces/metrics/logs 匯出到 Application Insights
  2. Agent Framework 內建 OTel instrumentation（Responses API spans）
  3. 提供 tracer / meter 給 executors 和 foundry_agents 使用

環境變數：
  APPLICATIONINSIGHTS_CONNECTION_STRING — Application Insights 連線字串
  OTEL_SERVICE_NAME — 服務名稱（預設 "ccoe-orchestrator"）
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace, metrics
from opentelemetry.trace import Tracer
from opentelemetry.metrics import Meter

logger = logging.getLogger("orchestrator.observability")

# ── Module-level singletons (populated after setup) ──
_tracer: Tracer | None = None
_meter: Meter | None = None
_initialized: bool = False


def get_tracer() -> Tracer:
    """取得共用 Tracer（若尚未初始化則回傳 NoOp tracer）。"""
    if _tracer is not None:
        return _tracer
    return trace.get_tracer("ccoe-orchestrator")


def get_meter() -> Meter:
    """取得共用 Meter（若尚未初始化則回傳 NoOp meter）。"""
    if _meter is not None:
        return _meter
    return metrics.get_meter("ccoe-orchestrator")


def setup_observability() -> None:
    """
    初始化 Azure Monitor 可觀測性。

    呼叫順序：
      1. configure_azure_monitor()  — 設定 exporter
      2. Agent Framework OTel      — enable_instrumentation / configure_otel_providers

    若 APPLICATIONINSIGHTS_CONNECTION_STRING 未設定，則跳過（僅記錄 warning）。
    """
    global _tracer, _meter, _initialized

    if _initialized:
        logger.debug("[Observability] Already initialized, skipping")
        return

    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    service_name = os.getenv("OTEL_SERVICE_NAME", "ccoe-orchestrator")

    if not conn_str:
        logger.warning(
            "[Observability] APPLICATIONINSIGHTS_CONNECTION_STRING not set — "
            "telemetry will NOT be exported to Azure Monitor. "
            "Set the env var to enable Application Insights integration."
        )
        # 即使沒有 connection string，仍然設定 tracer/meter 作為 NoOp
        _tracer = trace.get_tracer(service_name)
        _meter = metrics.get_meter(service_name)
        _initialized = True
        return

    # ── Azure Monitor Distro ──
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=conn_str,
            service_name=service_name,
            # 降低 sampling rate 避免開發階段大量 traces
            sampling_ratio=float(os.getenv("OTEL_SAMPLING_RATIO", "1.0")),
        )
        logger.info("[Observability] Azure Monitor configured (service=%s)", service_name)
    except ImportError:
        logger.warning(
            "[Observability] azure-monitor-opentelemetry not installed — "
            "run: pip install azure-monitor-opentelemetry"
        )
    except Exception as exc:
        logger.error("[Observability] Failed to configure Azure Monitor: %s", exc)

    # ── Agent Framework OTel instrumentation ──
    try:
        from agent_framework.observability import (
            configure_otel_providers,
            enable_instrumentation,
        )

        configure_otel_providers()
        enable_instrumentation()
        logger.info("[Observability] Agent Framework OTel instrumentation enabled")
    except ImportError:
        logger.debug(
            "[Observability] agent_framework.observability not available — "
            "Agent Framework tracing disabled"
        )
    except Exception as exc:
        logger.warning("[Observability] Agent Framework OTel setup failed: %s", exc)

    # ── Create shared tracer / meter ──
    _tracer = trace.get_tracer(service_name)
    _meter = metrics.get_meter(service_name)
    _initialized = True

    logger.info(
        "[Observability] Setup complete — tracer=%s meter=%s",
        service_name,
        service_name,
    )
