"""
contracts.py — Pydantic models for Orchestrator I/O contracts.

定義所有 input / output / intermediate 資料結構：
- Spec: 需求規格（單一真相）
- ResourceManifest: Terraform agent 產出的機器可讀資源清單
- AgentOutput / WorkflowResult: 各 agent 的輸出與最終交付物
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Commitment(str, Enum):
    PAYG = "PAYG"
    RI = "RI"


class NetworkModel(str, Enum):
    PRIVATE = "private"
    PUBLIC = "public"


class StepStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Spec (Step 0 — 單一真相)
# ---------------------------------------------------------------------------
class Assumption(BaseModel):
    """記錄由 Orchestrator 預設填補的欄位。"""
    field: str = Field(..., description="被填補的欄位名稱")
    value: Any = Field(..., description="填入的預設值")
    source: str = Field(default="default", description="來源: 'user' | 'default'")
    reason: str = Field(default="", description="為何使用此預設值")


class Spec(BaseModel):
    """Orchestrator normalize 後的標準規格 — 單一真相 (spec.json)。"""
    preferred_language: str = Field(default="zh-TW", description="使用者偏好語言/locale")
    project_name: str = Field(default="unnamed-project", description="專案名稱")
    region: str = Field(default="eastasia", description="Azure region")
    environment_count: int = Field(default=1, ge=1, description="要佈建幾個環境 (dev/stg/prod)")
    currency: str = Field(default="TWD", description="成本估算幣別")
    commitment: Commitment = Field(default=Commitment.PAYG, description="PAYG or RI")
    assumptions: list[Assumption] = Field(default_factory=list, description="假設清單")
    tags: dict[str, str] = Field(default_factory=dict, description="Azure resource tags")
    network_model: NetworkModel = Field(default=NetworkModel.PUBLIC, description="網路模式")
    completeness_status: Literal["incomplete", "complete"] = Field(
        default="incomplete",
        description="需求完整性狀態",
    )
    missing_fields: list[str] = Field(default_factory=list, description="尚待補齊欄位")
    accepted_assumptions: list[str] = Field(default_factory=list, description="使用者已接受的假設")
    architecture_details: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Azure-Architecture-Clarification-Agent 澄清後的架構細節 "
            "（core_services, security, network, compliance, availability, monitoring 等）"
        ),
    )
    diagram_approved: bool = Field(default=False, description="使用者是否已核准架構圖")
    diagram_feedback: str = Field(default="", description="使用者對架構圖的回饋")
    notes: str = Field(default="", description="使用者備註 / 原始需求摘要")
    raw_input: str = Field(default="", description="使用者原始輸入（保留用於 audit）")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(), description="建立時間")

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    def save(self, path: Path) -> Path:
        path.write_text(self.to_json(), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Clarifying Questions (Step 0 補充)
# ---------------------------------------------------------------------------
class ClarifyingQuestion(BaseModel):
    """Orchestrator 詢問使用者的澄清問題。"""
    field: str
    question: str
    options: list[str] = Field(default_factory=list, description="建議選項（空 = 自由填答）")
    default: Any = Field(default=None, description="若使用者不回覆時的預設值")


# ---------------------------------------------------------------------------
# Resource Manifest (Terraform Agent 產出)
# ---------------------------------------------------------------------------
class ResourceEntry(BaseModel):
    """resource_manifest.json 中的單一資源。"""
    resource_type: str = Field(..., description="e.g. azurerm_resource_group")
    name: str = Field(..., description="Terraform resource name")
    display_name: str = Field(default="", description="人類可讀名稱")
    sku: Optional[str] = Field(default="", description="SKU / pricing tier")
    region: str = Field(default="")
    properties: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def coerce_sku(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('sku') is None:
                data['sku'] = ""
        return data


class ResourceManifest(BaseModel):
    """Terraform agent 輸出的機器可讀資源清單。"""
    project_name: str = ""
    resources: list[ResourceEntry] = Field(default_factory=list)
    terraform_version: str = Field(default=">=1.5")
    provider_version: Any = Field(default=">=3.0", description="Provider version (str or dict)")

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    def save(self, path: Path) -> Path:
        path.write_text(self.to_json(), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Agent Outputs
# ---------------------------------------------------------------------------
class TerraformOutput(BaseModel):
    """Azure-Terraform-Generation-Agent 的輸出。"""
    main_tf: str = Field(default="", description="main.tf 內容")
    variables_tf: str = Field(default="", description="variables.tf 內容")
    outputs_tf: str = Field(default="", description="outputs.tf 內容")
    locals_tf: str = Field(default="", description="locals.tf 內容")
    versions_tf: str = Field(default="", description="versions.tf 內容")
    providers_tf: str = Field(default="", description="providers.tf 內容")
    terragrunt_root_hcl: str = Field(default="", description="根目錄 terragrunt.hcl")
    terragrunt_dev_hcl: str = Field(default="", description="/envs/dev/terragrunt.hcl")
    terragrunt_prod_hcl: str = Field(default="", description="/envs/prod/terragrunt.hcl")
    readme_md: str = Field(default="", description="README.md 完整內容")
    test_files: dict[str, str] = Field(
        default_factory=dict,
        description="測試檔案: key=檔名(如 unit_basic.tftest.hcl), value=內容",
    )
    resource_manifest: ResourceManifest = Field(default_factory=ResourceManifest)
    status: StepStatus = StepStatus.SUCCESS
    error: str = ""


class DiagramOutput(BaseModel):
    """Azure-Diagram-Generation-Agent 的輸出。"""
    diagram_py: str = Field(default="", description="diagram.py 內容")
    diagram_image: bytes = Field(default=b"", description="diagram.png/svg 二進位")
    diagram_image_ext: str = Field(default="png", description="png or svg")
    render_log: str = Field(default="", description="render_log.txt 內容")
    approved_resource_manifest: ResourceManifest = Field(
        default_factory=ResourceManifest,
        description="供下游 TF/Cost 共用的核准架構資源清單",
    )
    status: StepStatus = StepStatus.SUCCESS
    error: str = ""


class PricingLineItem(BaseModel):
    """Azure Calculator 成本結構中的單一 line-item。"""
    resource_type: str = Field(..., description="e.g. azurerm_virtual_machine")
    name: str = Field(default="", description="Terraform resource name")
    display_name: str = Field(default="", description="人類可讀名稱")
    sku: str = Field(default="", description="SKU，e.g. Standard_D2s_v5")
    region: str = Field(default="", description="Azure region")
    product_id: str = Field(default="", description="Azure Calculator product slug / ID")
    meter: str = Field(default="", description="計量方式，e.g. 'per hour', 'per GB/month'")
    unit: str = Field(default="", description="計量單位，e.g. 'hours', 'GB'")
    pricing_tier: str = Field(default="PAYG", description="定價層級，e.g. 'PAYG', '1-year RI', '3-year RI', 'spot'")
    quantity: float = Field(default=1.0, ge=0, description="數量 / 用量預估")
    estimated_monthly_usd: float = Field(default=0.0, ge=0, description="初步估算月費 (USD)")
    notes: str = Field(default="", description="備註")


class PricingStructureOutput(BaseModel):
    """Azure-Pricing-Structure-Agent 的輸出 — 架構轉換為 Azure Calculator 價格結構（Step 7B）。"""
    line_items: list[PricingLineItem] = Field(default_factory=list, description="逐項資源成本結構")
    currency: str = Field(default="USD", description="幣別")
    commitment: str = Field(default="PAYG", description="承諾類型")
    region: str = Field(default="", description="主要 region")
    total_estimated_monthly_usd: float = Field(default=0.0, ge=0, description="總計月費初估 (USD)")
    notes: str = Field(default="", description="備註 / 假設說明")
    status: StepStatus = StepStatus.SUCCESS
    error: str = ""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


class PricingOutput(BaseModel):
    """Azure-Pricing-Browser-Agent 的輸出 — Browser Automation 操作 Azure Calculator 的最終結果（Step 8）。"""
    estimate_xlsx: bytes = Field(default=b"", description="estimate.xlsx 二進位")
    calculator_share_url: str = Field(default="", description="Azure Calculator share link")
    monthly_estimate_usd: float = Field(default=0.0, ge=0, description="總計月費 (USD)")
    cost_breakdown: list[dict] = Field(default_factory=list, description="逐項成本明細")
    status: StepStatus = StepStatus.SUCCESS
    error: str = ""


# ---------------------------------------------------------------------------
# Multi-turn Agent Interaction (request_info / response_handler)
# ---------------------------------------------------------------------------
class AgentQuestion(BaseModel):
    """Executor → 使用者：agent 要求額外資訊時的 request 載體。"""
    agent_name: str = Field(..., description="發問的子 agent 名稱")
    question_text: str = Field(..., description="Agent 提出的問題文字")
    turn: int = Field(default=1, ge=1, description="目前對話輪次")
    hint: str = Field(default="", description="提示文字（例如選項說明）")
    preferred_language: str = Field(default="zh-TW", description="本次 session 使用語言")


class AgentAnswer(BaseModel):
    """使用者 → Executor：回應 agent 問題的載體。"""
    answer_text: str = Field(default="", description="使用者回答文字")
    command: Literal["continue", "done", "skip", "approve", "revise", "reject"] = Field(
        default="continue",
        description="控制指令: continue=繼續對話, done=結束此 agent, skip=跳過此 agent, approve/revise/reject=審核指令",
    )


# ---------------------------------------------------------------------------
# Workflow Result (最終交付)
# ---------------------------------------------------------------------------
class StepResult(BaseModel):
    """單一步驟的結果摘要。"""
    step: str
    status: StepStatus
    error: str = ""
    retry_suggestion: str = ""
    artifacts: list[str] = Field(default_factory=list, description="產出的檔案路徑")


class WorkflowResult(BaseModel):
    """Orchestrator 最終輸出。"""
    spec: Spec
    steps: list[StepResult] = Field(default_factory=list)
    executive_summary: str = ""
    output_dir: str = ""
    all_artifacts: list[str] = Field(default_factory=list)
    success: bool = True

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)
