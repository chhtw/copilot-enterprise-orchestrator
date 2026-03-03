# CCoE Orchestrator Agent

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![MAF](https://img.shields.io/badge/Microsoft%20Agent%20Framework-1.0.0rc2-purple)](https://pypi.org/project/agent-framework-core/)

> **使用 Microsoft Agent Framework (MAF) `WorkflowBuilder` 串接 9 個 Executor 的 Azure 架構編排器。**
> Orchestrator 只做需求澄清與 spec normalization — 所有重工作委派給 Microsoft Foundry 內的 specialist agents。

---

## 目錄

- [架構概覽](#架構概覽)
- [Workflow 流程圖](#workflow-流程圖)
- [Agents 與本地模組](#agents-與本地模組)
- [快速開始](#快速開始)
- [環境變數](#環境變數)
- [Agent 定義同步 (agent_sync)](#agent-定義同步-agent_sync)
- [專案結構](#專案結構)
- [交付物清單](#交付物清單)
- [Workflow 行為規則](#workflow-行為規則)
- [可觀測性 (Observability)](#可觀測性-observability)
- [測試](#測試)
- [Docker 部署](#docker-部署)

---

## 架構概覽

```
使用者需求 (自然語言)
      │
      ▼
┌──────────────────────────────────────────────────────────────┐
│                   Orchestrator Agent (MAF)                    │
│                                                              │
│  [1] NormalizeExecutor                使用者輸入 → spec.json │
│  [2] RequirementClarificationExecutor 多輪對話補齊基本欄位   │
│  [3] ArchitectureClarificationExecutor                       │
│       ↳ Architecture-Clarification-Agent (Foundry)           │
│         → architecture_details.json                          │
│                                                              │
│  [4] DiagramExecutor                                         │
│       ↳ DaC-Dagrams-Mingrammer (Foundry) → diagram.py       │
│  [5] DiagramRenderExecutor    本地渲染 diagram.py → PNG      │
│  [6] DiagramApprovalExecutor  ◆ GATE — 使用者核准架構圖      │
│                                                              │
│  [7] ParallelTerraformCostExecutor ─── 並 行 ───             │
│       ├─ Azure-Terraform-Architect-Agent → main.tf, ...      │
│       └─ Agent-AzureCalculator → cost_structure.json         │
│                                                              │
│  [8] RetailPricesCostExecutor  Azure Retail Prices API       │
│       (或 CostBrowserExecutor, 依 COST_STEP3B_MODE)          │
│       → estimate.xlsx                                        │
│                                                              │
│  [9] SummaryExecutor           → executive_summary.md        │
└──────────────────────────────────────────────────────────────┘
      │
      ▼
  交付物清單 (out/)
```

> **設計決策**：Architecture-Clarification-Agent 在「畫圖前」確認所有架構細節；
> 架構圖核准後才「同時」呼叫 Terraform + Cost Agent，縮短總等待時間並避免 IaC 重做。

---

## Workflow 流程圖

```
      使用者輸入
          │
    ┌─────▼─────┐
    │ Normalize  │  → spec.json
    └─────┬─────┘
    ┌─────▼──────────────────┐
    │ RequirementClarification│  ⟲ multi-turn（補齊欄位）
    └─────┬──────────────────┘
    ┌─────▼──────────────────────┐
    │ ArchitectureClarification   │  ⟲ multi-turn (10 維度)
    │ (Foundry Agent)             │  → architecture_details.json
    └─────┬──────────────────────┘
    ┌─────▼──────────┐
    │ DiagramExecutor │  ⟲ multi-turn
    │ (Foundry Agent) │  → diagram.py
    └─────┬──────────┘
    ┌─────▼───────────────┐
    │ DiagramRenderExecutor│  本地渲染 → diagram.png
    │ (auto-fix imports)   │  → render_log.txt
    └─────┬───────────────┘
    ┌─────▼──────────────────┐
    │ DiagramApprovalExecutor │  ◆ GATE: approve / revise / reject
    └─────┬──────────────────┘
          │ (核准後)
    ┌─────▼───────────────────────────────┐
    │ ParallelTerraformCostExecutor       │
    │  ┌────────────────┬────────────────┐│
    │  │ TF Agent       │ Cost Agent     ││
    │  │→ main.tf ...   │→ cost_struct   ││
    │  └────────────────┴────────────────┘│
    └─────┬───────────────────────────────┘
    ┌─────▼──────────────────────┐
    │ RetailPricesCostExecutor   │  Azure Retail Prices API
    │ (或 CostBrowserExecutor)   │  → estimate.xlsx
    └─────┬──────────────────────┘
    ┌─────▼──────────┐
    │ SummaryExecutor │  → executive_summary.md
    └─────┬──────────┘
          ▼
     WorkflowResult
```

---

## Agents 與本地模組

### Foundry Specialist Agents（遠端呼叫）

| Agent Name | 職責 | 輸入 | 輸出 |
|---|---|---|---|
| **Architecture-Clarification-Agent** | 多輪對話確認架構細節（10 個維度） | `spec.json` | `architecture_details.json` |
| **DaC-Dagrams-Mingrammer** | 生成 Python diagrams 程式碼 | `spec.json` + `architecture_details.json` | `diagram.py` |
| **Azure-Terraform-Architect-Agent** | 生成 Terraform HCL (AVM) | `spec.json` + `approved_resource_manifest` | `main.tf`, `variables.tf`, `outputs.tf`, `locals.tf`, `versions.tf`, `providers.tf` |
| **Agent-AzureCalculator** | 估算 Azure 成本結構 | `spec.json` + `approved_resource_manifest` | `cost_structure.json` |
| **Agent-AzureCalculator-BrowserAuto** | 瀏覽器自動化查詢 Azure 計算機 | `cost_structure.json` | `estimate.xlsx`（browser mode 用） |

### 本地模組（不需 Foundry）

| 模組 | 說明 |
|---|---|
| `diagram_renderer.py` | 本地執行 `diagram.py` → PNG，含 auto-fix import 名稱錯誤（MAF Agent 封裝） |
| `retail_prices.py` | 查詢 Azure Retail Prices REST API，產生逐項定價（免費、無需驗證） |
| `xlsx_builder.py` | 將定價資料組合成 `estimate.xlsx` |
| `agent_sync.py` | 同步 Foundry Agent 定義 ↔ 本地 YAML（pull / push） |

---

## 快速開始

### 先決條件

- Python 3.12+
- [Graphviz](https://graphviz.org/)（diagram 渲染需要）
- Azure CLI（Real Mode 需要 `az login`）

### 1. 建立虛擬環境

```bash
cd ccoe-Orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 設定環境變數

```bash
cp .env.example .env
# 編輯 .env 並填入所需的值（參考下方「環境變數」表格）
```

### 3. Mock Mode（離線測試）

無需 Azure 登入，使用內建 mock agents 模擬所有 Foundry 回應：

```bash
# .env
MOCK_MODE=true

# CLI 互動模式（預設）
python -m orchestrator_app.main

# CLI 帶入需求
python -m orchestrator_app.main "我需要 App Service + VNet in eastasia"

# HTTP Server 模式
RUN_MODE=server python -m orchestrator_app.main
```

### 4. Real Mode（連接 Foundry）

```bash
# .env
MOCK_MODE=false
AZURE_AI_PROJECT_ENDPOINT=https://your-foundry-endpoint.services.ai.azure.com/api/...

# 確保已登入 Azure
az login

# 啟動
python -m orchestrator_app.main
```

### 5. 運行模式

| 模式 | 說明 | 啟動方式 |
|---|---|---|
| **CLI** (預設) | 互動式命令列，agent 追問時從 stdin 讀取回答；支援 `/done`、`/skip` 指令 | `RUN_MODE=cli python -m orchestrator_app.main` |
| **HTTP Server** | ASGI hosting adapter，Multi-turn 由 adapter 自動處理 | `RUN_MODE=server python -m orchestrator_app.main` |

Server 啟動後，預設在 `http://localhost:8088` 接收請求（含 `/health` 健康檢查端點）。

---

## 環境變數

### 核心設定

| 變數 | 預設值 | 說明 |
|---|---|---|
| `MOCK_MODE` | `true` | `true` 離線 mock / `false` 真實呼叫 Foundry |
| `RUN_MODE` | `cli` | `server` HTTP 部署 / `cli` 互動命令列 |
| `OUTPUT_DIR` | `./out` | 產物輸出目錄 |

### Foundry 連線

| 變數 | 預設值 | 說明 |
|---|---|---|
| `AZURE_AI_PROJECT_ENDPOINT` | `https://aif-ch-cht-ccoe-ai-agent.services...` | Foundry Project endpoint |

### Agent 名稱

| 變數 | 預設值 | 說明 |
|---|---|---|
| `CLARIFICATION_AGENT_NAME` | `Architecture-Clarification-Agent` | 架構澄清 agent |
| `TERRAFORM_AGENT_NAME` | `Azure-Terraform-Architect-Agent` | Terraform agent |
| `DIAGRAM_AGENT_NAME` | `DaC-Dagrams-Mingrammer` | Diagram agent |
| `COST_AGENT_NAME` | `Agent-AzureCalculator` | Cost 結構 agent |
| `COST_BROWSER_AGENT_NAME` | `Agent-AzureCalculator-BrowserAuto` | Browser mode cost agent |

### 成本估算

| 變數 | 預設值 | 說明 |
|---|---|---|
| `COST_STEP3B_MODE` | `retail_api` | `retail_api` 本地 Azure Retail Prices API；`browser` Foundry browser agent |

### Diagram 渲染

| 變數 | 預設值 | 說明 |
|---|---|---|
| `RENDER_DIAGRAM` | `true` | 是否啟用本地 `diagram.py` 渲染（需安裝 graphviz） |
| `RENDER_TIMEOUT` | `60` | diagram subprocess 逾時秒數 |
| `MAX_FIX_RETRIES` | `3` | diagram import 自動修正最大重試次數 |

### Agent 呼叫設定

| 變數 | 預設值 | 說明 |
|---|---|---|
| `AGENT_MAX_RETRIES` | `2` | Foundry agent 呼叫最大重試次數 |
| `AGENT_RETRY_DELAY` | `5.0` | 重試間隔秒數 |
| `AGENT_TIMEOUT` | `120` | 預設 agent HTTP timeout（秒） |
| `DIAGRAM_AGENT_TIMEOUT` | `300` | Diagram agent 超時（秒） |
| `TERRAFORM_AGENT_TIMEOUT` | `300` | Terraform agent 超時（秒） |
| `COST_STRUCTURE_AGENT_TIMEOUT` | `300` | Cost Structure agent 超時（秒） |
| `COST_BROWSER_AGENT_TIMEOUT` | `600` | Cost Browser agent 超時（秒） |
| `MAX_AGENT_REGEN_RETRIES` | `2` | Agent 重新生成最大重試次數 |

### Terraform 驗證

| 變數 | 預設值 | 說明 |
|---|---|---|
| `TF_VALIDATE_ENABLED` | `true` | 是否啟用 Terraform validate 檢查 |
| `MAX_TF_VALIDATE_RETRIES` | `2` | Terraform validate 失敗重試次數 |

### 可觀測性

| 變數 | 預設值 | 說明 |
|---|---|---|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | *(空)* | Application Insights 連線字串（未設定則不匯出） |
| `OTEL_SERVICE_NAME` | `ccoe-orchestrator` | OpenTelemetry 服務名稱 |
| `OTEL_SAMPLING_RATIO` | `1.0` | Traces 取樣率 |

---

## Agent 定義同步 (agent_sync)

使用 `agent_sync.py` 在本地 YAML 與 Microsoft Foundry 之間同步 agent 定義：

```bash
# 從 Foundry 拉取所有 agent 定義 → prompts/*.yaml
python -m orchestrator_app.agent_sync pull

# 拉取指定 agent
python -m orchestrator_app.agent_sync pull Architecture-Clarification-Agent

# 推送所有本地 YAML → Foundry（draft → publish）
python -m orchestrator_app.agent_sync push

# 只更新 draft，不 publish
python -m orchestrator_app.agent_sync push Agent-AzureCalculator --no-publish
```

YAML 採用 Microsoft Agent Framework declarative 格式，存放於 `prompts/` 目錄。

---

## 專案結構

```
ccoe-Orchestrator/
├── Dockerfile                    # 容器化部署
├── pyproject.toml                # pytest 設定
├── README.md
├── requirements.txt              # Python 依賴
│
├── prompts/                      # Agent 定義 YAML（Foundry declarative 格式）
│   ├── Agent-AzureCalculator.yaml
│   ├── Agent-AzureCalculator-BrowserAuto.yaml
│   ├── Architecture-Clarification-Agent.yaml
│   ├── Azure-Terraform-Architect-Agent.yaml
│   └── DaC-Dagrams-Mingrammer.yaml
│
├── src/
│   └── orchestrator_app/
│       ├── main.py               # Entrypoint — build_workflow() + HTTP Server + CLI 互動迴圈
│       ├── contracts.py          # Pydantic 資料模型（Spec, ResourceManifest, WorkflowResult 等）
│       ├── executors.py          # MAF Executor 實作（9 個 workflow 步驟 + 內部子 executor）
│       ├── foundry_agents.py     # 真實 Foundry agent 呼叫 + prompt builders
│       ├── mock_agents.py        # Mock mode 替代方案（離線測試用）
│       ├── agent_sync.py         # Foundry Agent 定義 ↔ 本地 YAML 同步（pull / push）
│       ├── diagram_renderer.py   # 本地渲染 diagram.py → PNG + auto-fix import 錯誤
│       ├── retail_prices.py      # Azure Retail Prices REST API 查詢（免費、無需驗證）
│       ├── xlsx_builder.py       # 將定價資料組合成 estimate.xlsx
│       ├── io.py                 # 產物寫入（spec / diagram / tf / cost / summary）
│       └── observability.py      # Azure Monitor + OpenTelemetry 可觀測性設定
│
└── tests/
    ├── test_agent_sync.py        # agent_sync 模組測試
    ├── test_diagram_regen.py     # diagram 重新生成測試
    ├── test_retail_prices.py     # retail_prices 模組單元測試
    └── test_workflow.py          # E2E Workflow 測試（MOCK_MODE=true）
```

---

## 交付物清單

執行成功後，`OUTPUT_DIR/` 產出以下檔案：

```
out/
├── spec.json                     # 需求規格（含 architecture_details）
│
├── diagram.py                    # Diagrams as Code 原始碼
├── diagram.png (或 .svg)         # 渲染後的架構圖
├── render_log.txt                # diagram subprocess 執行日誌
│
├── terraform/                    # Terraform IaC 產物
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── locals.tf
│   ├── versions.tf
│   └── providers.tf
│
├── resource_manifest.json        # 機器可讀資源清單
├── cost_structure.json           # Agent-AzureCalculator 成本結構
├── estimate.xlsx                 # 逐項定價表（Azure Retail Prices API）
├── calculator_share_url.txt      # Azure Calculator 分享連結（browser mode 時產出）
└── executive_summary.md          # 管理層摘要
```

---

## Workflow 行為規則

| # | 規則 | 說明 |
|---|---|---|
| 1 | **Orchestrator 不生成內容** | Terraform / Diagram / Cost 全部委派給 specialist agents |
| 2 | **架構澄清先行** | Architecture-Clarification-Agent 確認 10 個維度後，diagram 才有完整輸入 |
| 3 | **Diagram 先於 Terraform** | 使用者確認架構圖後，才並行呼叫 Terraform + Cost，避免 IaC 重做 |
| 4 | **Diagram Approval Gate** | `DiagramApprovalExecutor` 等待使用者輸入 `approve` / `revise` / `reject` |
| 5 | **Step 7 並行執行** | `ParallelTerraformCostExecutor` 同時呼叫 Terraform 與 Cost Structure Agent |
| 6 | **Diagram auto-fix** | 渲染失敗時自動查找 `diagrams` 套件並修正 import 錯誤，最多重試 `MAX_FIX_RETRIES` 次 |
| 7 | **Assumptions 自動填補** | 未指定欄位用預設值，記錄在 `spec.json` 的 `assumptions` 陣列 |
| 8 | **Cost Step 模式** | `retail_api`（預設）走本地 API；`browser` 走 Foundry `browser_automation_preview` |
| 9 | **Multi-turn 支援** | `ctx.request_info()` 暫停 workflow；CLI 從 stdin 讀取；HTTP 由 hosting adapter 處理 |

---

## 可觀測性 (Observability)

整合 **Azure Monitor + OpenTelemetry**，提供 traces / metrics / logs 匯出：

- 設定 `APPLICATIONINSIGHTS_CONNECTION_STRING` 啟用 Application Insights 匯出
- Agent Framework 內建 OTel instrumentation 自動追蹤 Responses API spans
- 共用 `tracer` / `meter` 供所有 executors 與 foundry_agents 使用
- 未設定連線字串時，仍可用 NoOp tracer 正常運行（不匯出遙測資料）

---

## 測試

```bash
# 執行所有測試
pytest

# 執行指定測試
pytest tests/test_workflow.py -v

# 執行 retail prices 單元測試
pytest tests/test_retail_prices.py -v
```

測試預設使用 `MOCK_MODE=true`，不需要 Foundry 連線。

---

## Docker 部署

### 建置與執行

```bash
# 建置映像
docker build -t ccoe-orchestrator .

# 執行容器
docker run -p 8088:8088 \
  -e MOCK_MODE=false \
  -e AZURE_AI_PROJECT_ENDPOINT=https://your-foundry-endpoint.services.ai.azure.com/api/... \
  ccoe-orchestrator
```

### 容器規格

- 基底映像：`python:3.12-slim`
- 內建 `graphviz`（diagram 渲染）
- 健康檢查：`GET /health`（每 30 秒）
- 預設埠號：`8088`
- 預設模式：`RUN_MODE=server`、`MOCK_MODE=false`

---

## 技術棧

| 元件 | 技術 |
|---|---|
| Agent 框架 | Microsoft Agent Framework (MAF) 1.0.0rc2 |
| AI 平台 | Microsoft Foundry (Azure AI Projects SDK) |
| 資料模型 | Pydantic v2 |
| HTTP Server | MAF Hosting Adapter (ASGI) |
| 可觀測性 | OpenTelemetry + Azure Monitor |
| IaC 產出 | Terraform (AVM) + Terragrunt |
| Diagram 產出 | [diagrams](https://diagrams.mingrammer.com/) (Python) |
| 成本估算 | Azure Retail Prices REST API / Browser Automation |
| 容器化 | Docker |

---

## License

Internal use only — CCoE Team.

## 部署到 Foundry Hosted Agents

1. 在 Azure AI Foundry 中建立 Hosted Agent 資源
2. 設定 Container Image 指向此 Docker image
3. 設定環境變數 (`MOCK_MODE=false`, `AZURE_AI_PROJECT_ENDPOINT`, etc.)
4. 確認下列 **5 個 specialist agents** 在同一個 Foundry Project 中：
   - `Architecture-Clarification-Agent`（`CLARIFICATION_AGENT_NAME`）
   - `DaC-Dagrams-Mingrammer`（`DIAGRAM_AGENT_NAME`）
   - `Azure-Terraform-Architect-Agent`（`TERRAFORM_AGENT_NAME`）
   - `Agent-AzureCalculator`（`COST_AGENT_NAME`）
   - `Agent-AzureCalculator-BrowserAuto`（`COST_BROWSER_AGENT_NAME`，`COST_STEP3B_MODE=browser` 時使用）
5. 部署後通過 Foundry Portal 或 API 進行互動

### Architecture-Clarification-Agent 設定

在 [Azure AI Foundry Portal](https://aif-ch-cht-ccoe-ai-agent.services.ai.azure.com/api/projects/ArchitectAgent) 確認 Hosted Agent 設定：

- **Agent 名稱**：`Architecture-Clarification-Agent`（或自訂後更新 `CLARIFICATION_AGENT_NAME`）
- **System Prompt**：由 `foundry_agents.build_architecture_clarification_prompt()` 產生的內容定義其行為
- **能力**：多輪對話，確認 10 個架構維度，最終輸出 JSON 格式的 `architecture_details`

## 測試

```bash
# 執行所有測試
pytest tests/ -v

# E2E Workflow（mock mode）
MOCK_MODE=true pytest tests/test_workflow.py -v

# Azure Retail Prices 模組單元測試
pytest tests/test_retail_prices.py -v
```

> `test_workflow.py` — 驗證完整 9 步 executor 串接流程（mock agents）  
> `test_retail_prices.py` — 驗證 `retail_prices.py` 的 OData filter 產生與 API 回應解析

## License

Internal — CCoE Team
