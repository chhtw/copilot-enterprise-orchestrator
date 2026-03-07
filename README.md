# Azure Platform Orchestrator

[English version](README.en.md)

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![MAF](https://img.shields.io/badge/Microsoft%20Agent%20Framework-1.0.0rc2-purple)](https://pypi.org/project/agent-framework-core/)

以 Microsoft Agent Framework 建立的 Azure 架構編排器。此專案將需求正規化、多輪澄清、架構圖生成與渲染、Terraform 產生、價格估算與摘要整理串成單一 workflow。

目前採用混合執行模型：

- `Azure-Diagram-Generation-Agent` 與 `Azure-Terraform-Generation-Agent` 走本地 GitHub Copilot provider。
- 架構澄清與 Pricing 相關 agent 仍可走 Microsoft Foundry。
- `MOCK_MODE=true` 時，整條流程可離線測試。

## 功能摘要

- 以 `WorkflowBuilder` 串接 9 個主要步驟
- 支援 CLI 與 HTTP server 兩種執行模式
- 支援繁體中文 / English session 語言切換
- 本地渲染 `diagram.py` 為 PNG/SVG，並支援 import auto-fix / regenerate
- Terraform 與 pricing structure 於架構圖核准後並行執行
- 將所有產物集中寫入 `out/`（或自訂 `OUTPUT_DIR`）
- 提供 `agent_sync.py` 同步 prompt YAML 與 Foundry agent 定義

## Workflow 概覽

| Step | 元件 | 說明 | 主要輸出 |
|---|---|---|---|
| 1 | `NormalizeExecutor` | 將使用者輸入正規化成 `Spec` | `spec.json` |
| 2 | `RequirementsClarificationExecutor` | 補齊基本規格欄位 | 更新後的 `spec.json` |
| 3 | `ArchitectureClarificationExecutor` | 多輪澄清 Azure 架構細節 | `architecture_details` |
| 4 | `DiagramGenerationExecutor` | 生成 diagrams Python 程式碼 | `diagram.py` |
| 5 | `DiagramRenderingExecutor` | 本地渲染架構圖 | `diagram.png` / `diagram.svg`, `render_log.txt` |
| 6 | `DiagramReviewExecutor` | 等待使用者核准、修訂或退回 | 已核准的 diagram 狀態 |
| 7 | `ParallelTerraformPricingExecutor` | 並行產生 Terraform 與 pricing structure | Terraform 檔案、`pricing_structure.json` |
| 8 | `RetailPricingExecutor` / `BrowserPricingExecutor` | 產生成本估算工作簿 | `estimate.xlsx` |
| 9 | `SummaryExecutor` | 輸出管理摘要與 artifact 清單 | `executive_summary.md`, `artifacts.zip` |

## 執行架構

```text
User input
   ↓
Normalize
   ↓
Requirements clarification
   ↓
Architecture clarification (Foundry or mock)
   ↓
Diagram generation (local Copilot agent or mock)
   ↓
Diagram rendering
   ↓
Diagram approval gate
   ↓
Terraform generation + pricing structure (parallel)
   ↓
Retail API or browser pricing
   ↓
Executive summary + zipped artifacts
```

## 快速開始

### 先決條件

- Python 3.12+
- Graphviz（渲染 diagrams 必要）
- 可用的 Python 虛擬環境
- 若使用 Real Mode：
  - 可存取 Microsoft Foundry project
  - 可使用 GitHub Copilot provider
  - 已完成 Azure 身分驗證（例如 `az login`）

### 1. 安裝依賴

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
```

> 專案目前採 `src/` 目錄結構；若不是透過 VS Code 啟動設定執行，請先設定 `PYTHONPATH`。

### 2. 準備環境變數

```bash
cp .env.example .env
```

最小可用設定：

```dotenv
MOCK_MODE=true
RUN_MODE=cli
OUTPUT_DIR=./out
PRICING_EXECUTION_MODE=retail_api
```

### 3. Mock Mode

```bash
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
python -m orchestrator_app.main
python -m orchestrator_app.main "我需要 App Service + VNet in eastasia"
RUN_MODE=server python -m orchestrator_app.main
```

Mock Mode 不需要 Foundry 或 GitHub Copilot 連線，適合開發與測試。

### 4. Real Mode

```bash
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
export MOCK_MODE=false
export AZURE_AI_PROJECT_ENDPOINT="https://<your-project>.services.ai.azure.com/api/projects/<project-name>"
export GITHUB_COPILOT_MODEL="gpt-5.4"
export GITHUB_COPILOT_WORKDIR="out"
python -m orchestrator_app.main
```

Real Mode 說明：

- 架構澄清 / pricing agent 會使用 Foundry project endpoint。
- diagram / terraform agent 會使用本地 GitHub Copilot provider。
- 若需呼叫 Foundry，請確認目前 shell 已具備有效 Azure 認證。

## 執行模式

| 模式 | 說明 |
|---|---|
| `RUN_MODE=cli` | 預設模式。啟動後會先詢問語言，並在多輪對話期間從 stdin 讀取回答。 |
| `RUN_MODE=server` | 啟動 HTTP server，供 hosting adapter / 外部呼叫使用。預設埠為 `8088`。 |

CLI 模式支援 `/done`、`/skip` 等多輪指令。

HTTP 模式首個 request 可帶入：

```json
{
  "preferred_language": "en-US",
  "raw_input": "I need App Service with private networking in eastasia"
}
```

## 主要環境變數

### Core

| 變數 | 預設值 | 說明 |
|---|---|---|
| `MOCK_MODE` | `true` | `true` 使用 mock agents；`false` 使用 hybrid routing |
| `RUN_MODE` | `cli` | `cli` 或 `server` |
| `OUTPUT_DIR` | `./out` | 所有產物輸出目錄 |
| `PRICING_EXECUTION_MODE` | `retail_api` | `retail_api` 或 `browser` |

### Foundry / Agent

| 變數 | 預設值 | 說明 |
|---|---|---|
| `AZURE_AI_PROJECT_ENDPOINT` | 範例 endpoint | Foundry project endpoint |
| `ARCHITECTURE_AGENT_NAME` | `Azure-Architecture-Clarification-Agent` | 架構澄清 agent 名稱 |
| `TERRAFORM_AGENT_NAME` | `Azure-Terraform-Generation-Agent` | Terraform agent 名稱 |
| `DIAGRAM_AGENT_NAME` | `Azure-Diagram-Generation-Agent` | Diagram agent 名稱 |
| `PRICING_STRUCTURE_AGENT_NAME` | `Azure-Pricing-Structure-Agent` | Pricing structure agent 名稱 |
| `PRICING_BROWSER_AGENT_NAME` | `Azure-Pricing-Browser-Agent` | Browser pricing agent 名稱 |

### Timeout / Retry

| 變數 | 預設值 | 說明 |
|---|---|---|
| `AGENT_MAX_RETRIES` | `2` | Foundry agent 呼叫最大重試次數 |
| `AGENT_RETRY_DELAY` | `5.0` | 重試間隔秒數 |
| `AGENT_TIMEOUT` | `120` | Foundry agent 預設 timeout |
| `DIAGRAM_AGENT_TIMEOUT` | `300` | Diagram agent timeout；若未設定 `GITHUB_COPILOT_TIMEOUT`，本地 Copilot diagram agent 也會回退使用此值 |
| `TERRAFORM_AGENT_TIMEOUT` | `300` | Terraform agent timeout；若未設定 `GITHUB_COPILOT_TIMEOUT`，本地 Copilot terraform agent 也會回退使用此值 |
| `COST_STRUCTURE_AGENT_TIMEOUT` | `300` | Pricing structure agent timeout |
| `COST_BROWSER_AGENT_TIMEOUT` | `600` | Browser pricing agent timeout |
| `GITHUB_COPILOT_TIMEOUT` | `1200` | 本地 Copilot provider timeout；優先於 `DIAGRAM_AGENT_TIMEOUT` / `TERRAFORM_AGENT_TIMEOUT`，未設定且沒有 agent-specific timeout 時也會使用此預設值 |
| `GITHUB_COPILOT_PROGRESS_TIMEOUT` | `180` | 本地 Copilot 單次 invoke 的卡住偵測上限秒數；若 provider 在此時間內完全沒有完成，會先主動中止並重建 client，再交給既有 retry 機制 |
| `GITHUB_COPILOT_MAX_RESTARTS` | `1` | 本地 Copilot provider 不健康時，最多重建 client/agent 的次數 |
| `GITHUB_COPILOT_RETRY_DELAY` | `2.0` | 本地 Copilot provider 重建前的初始等待秒數，後續採 exponential backoff |
| `GITHUB_COPILOT_STARTUP_PREFLIGHT` | `true` | 在 CLI / server 啟動時先驗證本地 Copilot provider 是否可用；`MOCK_MODE=true` 時自動跳過 |
| `GITHUB_COPILOT_WORKDIR` | 未設定時使用目前程序工作目錄 | 強制 GitHub Copilot SDK 以指定目錄啟動；相對路徑會相對於啟動程序當下目錄解析，且會自動建立，例如 `out` |

> 本地 Copilot 多輪 session 會持久化到 `OUTPUT_DIR/.copilot_sessions/`。若程序重啟，只要 workflow checkpoint 還保留同一個 `response_id`（本地 Copilot 路徑實際上是 session id），續輪時會自動從磁碟恢復對話歷史。
>
> Session payload 現在除了 `turns` 之外，還會持久化 `events`，記錄 preflight、attempt、stuck timeout、retry、最終完成或失敗等執行軌跡。即使本次 invoke 尚未成功回傳最終文字，也會先把事件寫進 `OUTPUT_DIR/.copilot_sessions/`，方便事後檢查。
>
> 本地 Copilot provider 會輸出 `[CopilotHealth]` 結構化 log，並寫出 OTel metrics：健康事件、client/agent 重建次數、preflight/invoke latency。若有 Application Insights，這些訊號會跟著既有 telemetry 一起匯出。

### Diagram / Terraform 驗證

| 變數 | 預設值 | 說明 |
|---|---|---|
| `RENDER_DIAGRAM` | `true` | 是否渲染 `diagram.py` |
| `RENDER_TIMEOUT` | `60` | diagram subprocess timeout |
| `MAX_FIX_RETRIES` | `3` | import auto-fix 最大重試次數 |
| `MAX_AGENT_REGEN_RETRIES` | `2` | 將 render error 回傳 diagram agent 的重試次數 |
| `TF_VALIDATE_ENABLED` | `true` | 是否執行 Terraform validate |
| `MAX_TF_VALIDATE_RETRIES` | `2` | validate 失敗時回傳 agent 修復的重試次數 |

### Observability

| 變數 | 預設值 | 說明 |
|---|---|---|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | 空值 | 設定後匯出 telemetry 到 Application Insights |
| `OTEL_SERVICE_NAME` | `ccoe-orchestrator` | OTel service name |
| `OTEL_SAMPLING_RATIO` | `1.0` | trace sampling ratio |

## Agent 定義同步

`agent_sync.py` 可在本地 YAML 與 Foundry agent definition/version 間同步。

```bash
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"

# 拉取全部 agent 定義
python -m orchestrator_app.agent_sync pull

# 拉取單一 agent
python -m orchestrator_app.agent_sync pull Azure-Architecture-Clarification-Agent

# 推送全部 agent 定義
python -m orchestrator_app.agent_sync push

# 推送單一 agent
python -m orchestrator_app.agent_sync push Azure-Pricing-Structure-Agent
```

補充：

- runtime 一律以 `prompts/` 底下的 YAML 為本地 source of truth。
- hybrid 模式下，Terraform / Diagram 的 prompt 也直接由本地 YAML 載入。
- 若 Foundry 端仍保留某些 hosted agents，修改 YAML 後可再 push 建立新 version。

## 專案結構

```text
ccoe-Orchestrator/
├── .env.example
├── Dockerfile
├── README.md
├── README.en.md
├── pyproject.toml
├── requirements.txt
├── prompts/
│   ├── Agent-AzureCalculator-BrowserAuto.yaml
│   ├── Agent-AzureCalculator.yaml
│   ├── Architecture-Clarification-Agent.yaml
│   ├── Azure-Architecture-Clarification-Agent.yaml
│   ├── Azure-Diagram-Generation-Agent.yaml
│   ├── Azure-Pricing-Browser-Agent.yaml
│   ├── Azure-Pricing-Structure-Agent.yaml
│   ├── Azure-Terraform-Architect-Agent.yaml
│   ├── Azure-Terraform-Generation-Agent.yaml
│   └── DaC-Dagrams-Mingrammer.yaml
├── skills/
│   └── terraform/
│       ├── azure-verified-modules.md
│       ├── terraform-style-guide.md
│       └── terraform-test.md
├── src/orchestrator_app/
│   ├── agent_sync.py
│   ├── contracts.py
│   ├── copilot_local_agents.py
│   ├── diagram_renderer.py
│   ├── executors.py
│   ├── foundry_agents.py
│   ├── hybrid_agents.py
│   ├── i18n.py
│   ├── io.py
│   ├── main.py
│   ├── mock_agents.py
│   ├── observability.py
│   ├── repair_feedback.py
│   ├── retail_prices.py
│   └── xlsx_builder.py
└── tests/
    ├── test_agent_sync.py
    ├── test_diagram_regen.py
    ├── test_diagram_renderer_agent.py
    ├── test_hybrid_agents.py
    ├── test_retail_prices.py
    └── test_workflow.py
```

### 模組說明

| 檔案 | 用途 |
|---|---|
| `main.py` | workflow 建構、CLI 入口、HTTP server 入口 |
| `executors.py` | 主要 workflow steps 與 shared state 控制 |
| `contracts.py` | Pydantic data contracts |
| `foundry_agents.py` | Foundry Responses API 呼叫與輸出解析 |
| `copilot_local_agents.py` | 本地 GitHub Copilot provider prompt agents |
| `hybrid_agents.py` | Copilot / Foundry 路由邏輯 |
| `diagram_renderer.py` | `diagram.py` 渲染、import 修復與回饋 |
| `retail_prices.py` | Azure Retail Prices API 查詢 |
| `xlsx_builder.py` | 成本估算 Excel 產生 |
| `repair_feedback.py` | Diagram / Terraform 修復 prompt 的回饋上下文 |
| `io.py` | 統一輸出檔案與 zip 打包 |
| `observability.py` | Azure Monitor + OpenTelemetry 設定 |

## 產物輸出

成功執行後，所有產物會寫入 `OUTPUT_DIR`：

```text
out/
├── spec.json
├── diagram.py
├── diagram.png / diagram.svg
├── render_log.txt
├── resource_manifest.json
├── pricing_structure.json
├── estimate.xlsx
├── calculator_share_url.txt
├── executive_summary.md
├── artifacts.zip
└── terraform/
    ├── main.tf
    ├── variables.tf
    ├── outputs.tf
    ├── locals.tf
    ├── versions.tf
    ├── providers.tf
    ├── README.md
    ├── terragrunt.hcl
    ├── envs/dev/terragrunt.hcl
    ├── envs/prod/terragrunt.hcl
    └── tests/
```

## 測試

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
pytest -q
pytest tests/test_workflow.py -v
pytest tests/test_diagram_regen.py tests/test_diagram_renderer_agent.py tests/test_agent_sync.py -q
```

測試主要以 `MOCK_MODE=true` 為前提，不需連到 Foundry。

## Docker

### 建置

```bash
docker build -t ccoe-orchestrator .
```

### 執行

```bash
docker run -p 8088:8088 \
  -e RUN_MODE=server \
  -e MOCK_MODE=false \
  -e AZURE_AI_PROJECT_ENDPOINT="https://<your-project>.services.ai.azure.com/api/projects/<project-name>" \
  -e GITHUB_COPILOT_MODEL="gpt-5.4" \
  ccoe-orchestrator
```

Docker image 特色：

- 基底映像為 `python:3.12-slim`
- 內含 `graphviz`
- 預設 `PYTHONPATH=/app/src`
- 預設 `RUN_MODE=server`
- 預設埠 `8088`

## 技術棧

| 項目 | 技術 |
|---|---|
| Agent framework | Microsoft Agent Framework |
| LLM backend | GitHub Copilot provider + Microsoft Foundry |
| Data contracts | Pydantic v2 |
| Pricing | Azure Retail Prices API / browser automation |
| Diagram | `diagrams` + Graphviz |
| Observability | OpenTelemetry + Azure Monitor |
| Testing | pytest + pytest-asyncio |

## License

Internal — CCoE Team
