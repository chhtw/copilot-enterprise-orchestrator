# Azure Platform Orchestrator

[English version](README.en.md)

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![MAF Core](https://img.shields.io/badge/Microsoft%20Agent%20Framework%20Core-1.0.0rc3-purple)](https://pypi.org/project/agent-framework-core/)

以 Microsoft Agent Framework 建立的 Azure 架構編排器。此專案將需求正規化、多輪澄清、架構圖生成與渲染、Terraform 產出、成本估算與管理摘要串成單一 workflow，並以 prompts/ 內的 YAML 作為本地 prompt source of truth。

目前採用混合執行模型：

- Azure-Diagram-Generation-Agent 與 Azure-Terraform-Generation-Agent 走本地 GitHub Copilot provider。
- 架構澄清與 Pricing 相關 agent 仍可走 Microsoft Foundry。
- MOCK_MODE=true 時，整條流程可離線測試。

## 目前狀態

- Workflow 固定為 9 個步驟，從 Spec 正規化一路產生 diagram、Terraform、pricing 與 executive summary。
- 本地 Copilot provider 已支援 startup preflight、retry/restart、idle progress timeout、session 持久化與程序重啟後對話恢復。
- Terraform 預設使用 staged generation，將輸出拆成 foundation、main、ops 三個 bundle，再彙整成完整 TerraformOutput。
- Diagram review/revise 會沿用同一個 diagram session，避免本地 Copilot 多輪上下文斷裂。
- 輸出目錄預設固定為 out/；只有明確設定 ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE=true 時才會套用自訂 OUTPUT_DIR。

## 功能摘要

- 以 WorkflowBuilder 串接 9 個主要步驟
- 支援 CLI 與 HTTP server 兩種執行模式
- 支援繁體中文與 English session 語言切換
- 本地渲染 diagram.py 為 PNG/SVG，並支援 import auto-fix 與 agent regen
- Diagram 核准後，並行執行 Terraform 與 pricing structure generation
- approved_resource_manifest 會作為下游 Terraform 與 pricing 的契約來源
- 本地 Copilot session 會寫入 out/.copilot_sessions/，包含 turns 與 events 軌跡
- 提供 agent_sync.py 同步本地 YAML 與 Foundry agent 定義

## Workflow 概覽

| Step | 元件 | 說明 | 主要輸出 |
|---|---|---|---|
| 1 | NormalizeExecutor | 將使用者輸入正規化成 Spec | spec.json |
| 2 | RequirementsClarificationExecutor | 補齊基本規格欄位 | 更新後的 spec.json |
| 3 | ArchitectureClarificationExecutor | 多輪澄清 Azure 架構細節 | architecture_details |
| 4 | DiagramGenerationExecutor | 生成 diagrams Python 程式碼與 approved_resource_manifest | diagram.py |
| 5 | DiagramRenderingExecutor | 本地渲染 diagram.py | diagram.png / diagram.svg, render_log.txt |
| 6 | DiagramReviewExecutor | 等待使用者核准、修訂或退回 | 已核准的 diagram 狀態 |
| 7 | ParallelTerraformPricingExecutor | 並行產生 Terraform 與 pricing structure | terraform/*, pricing_structure.json, resource_manifest.json |
| 8 | RetailPricingExecutor / BrowserPricingExecutor | 產生成本估算工作簿 | estimate.xlsx, calculator_share_url.txt |
| 9 | SummaryExecutor | 輸出管理摘要與 artifact bundle | executive_summary.md, artifacts.zip |

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
  - 已完成 Azure 身分驗證，例如 az login

### 1. 安裝依賴

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
```

專案採 src/ 目錄結構；若不是透過 VS Code launch settings 執行，請先設定 PYTHONPATH。

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

# 只有在測試或隔離執行時，才明確允許覆寫輸出目錄
# ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE=true
```

補充：

- 成功執行後，所有產物預設寫入 out/。
- 若未設定 ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE=true，OUTPUT_DIR 會被忽略。
- 本地 Copilot session 預設持久化到 out/.copilot_sessions/；若明確啟用 override，則改寫到 OUTPUT_DIR/.copilot_sessions/。
- Session payload 除了 turns，也會記錄 events，包含 preflight、attempt、串流 progress、stuck timeout、overall timeout、retry、完成或失敗等軌跡。

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

- 啟動時會先執行本地 Copilot startup preflight；MOCK_MODE=true 時自動跳過。
- 架構澄清與 pricing agent 使用 Foundry project endpoint。
- diagram 與 terraform agent 使用本地 GitHub Copilot provider。
- 若需呼叫 Foundry，請確認目前 shell 已具備有效 Azure 認證。

## 執行模式

| 模式 | 說明 |
|---|---|
| RUN_MODE=cli | 預設模式。啟動後先詢問語言，並在多輪對話期間從 stdin 讀取回答。 |
| RUN_MODE=server | 啟動 HTTP server，供 hosting adapter 或外部呼叫使用。預設埠為 8088。 |

CLI 模式支援 /done、/skip 等多輪指令。

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
| MOCK_MODE | true | true 使用 mock agents；false 使用 hybrid routing |
| RUN_MODE | cli | cli 或 server |
| OUTPUT_DIR | ./out | 所有產物輸出目錄；僅在 override flag 開啟時會生效 |
| ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE | 未設定 | 顯式允許使用 OUTPUT_DIR 覆寫預設 out/ |
| PRICING_EXECUTION_MODE | retail_api | retail_api 或 browser |

### Foundry / Agent

| 變數 | 預設值 | 說明 |
|---|---|---|
| AZURE_AI_PROJECT_ENDPOINT | 範例 endpoint | Foundry project endpoint |
| ARCHITECTURE_AGENT_NAME | Azure-Architecture-Clarification-Agent | 架構澄清 agent 名稱 |
| TERRAFORM_AGENT_NAME | Azure-Terraform-Generation-Agent | Terraform agent 名稱 |
| DIAGRAM_AGENT_NAME | Azure-Diagram-Generation-Agent | Diagram agent 名稱 |
| DIAGRAM_REVIEW_AGENT_NAME | Diagram-Review | Step 6 review 對話顯示名稱 |
| PRICING_STRUCTURE_AGENT_NAME | Azure-Pricing-Structure-Agent | Pricing structure agent 名稱 |
| PRICING_BROWSER_AGENT_NAME | Azure-Pricing-Browser-Agent | Browser pricing agent 名稱 |

### 本地 Copilot Provider

| 變數 | 預設值 | 說明 |
|---|---|---|
| GITHUB_COPILOT_MODEL | 由 prompt YAML 決定 | 本地 Copilot provider 使用的模型 ID |
| GITHUB_COPILOT_WORKDIR | 未設定時使用目前程序工作目錄 | 強制 GitHub Copilot SDK 以指定目錄啟動 |
| GITHUB_COPILOT_TIMEOUT | 1200 | 本地 Copilot 整體 hard timeout；優先於 agent-specific timeout |
| GITHUB_COPILOT_PROGRESS_TIMEOUT | 180 | 本地 Copilot 單次 invoke 的 idle timeout；完全無串流更新時判定 stuck |
| GITHUB_COPILOT_MAX_RESTARTS | 1 | provider 不健康時，最多重建 client/agent 的次數 |
| GITHUB_COPILOT_RETRY_DELAY | 2.0 | 重建前初始等待秒數，後續採 exponential backoff |
| GITHUB_COPILOT_STARTUP_PREFLIGHT | true | CLI 或 server 啟動時先驗證本地 Copilot provider 是否可用 |
| DIAGRAM_AGENT_TIMEOUT | 300 | diagram agent timeout；若未設定 GITHUB_COPILOT_TIMEOUT，會作為 fallback |
| TERRAFORM_AGENT_TIMEOUT | 300 | terraform agent timeout；若未設定 GITHUB_COPILOT_TIMEOUT，會作為 fallback |

### Diagram / Terraform 驗證

| 變數 | 預設值 | 說明 |
|---|---|---|
| RENDER_DIAGRAM | true | 是否渲染 diagram.py |
| RENDER_TIMEOUT | 60 | diagram subprocess timeout |
| MAX_FIX_RETRIES | 3 | import auto-fix 最大重試次數 |
| MAX_AGENT_REGEN_RETRIES | 2 | 將 render error 回傳 diagram agent 的最大重試次數 |
| TF_VALIDATE_ENABLED | true | 是否執行 terraform validate |
| MAX_TF_VALIDATE_RETRIES | 2 | validate 失敗時回傳 agent 修復的重試次數 |
| TERRAFORM_AGENT_GENERATION_MODE | staged | staged 或 single；staged 會拆成 foundation/main/ops bundles |
| MAX_TERRAFORM_NONFINAL_RETRIES | 1 | Terraform agent 回傳 non-final 回應時，自動 finalize 的最大重試次數 |

### Observability

| 變數 | 預設值 | 說明 |
|---|---|---|
| APPLICATIONINSIGHTS_CONNECTION_STRING | 空值 | 設定後匯出 telemetry 到 Application Insights |
| OTEL_SERVICE_NAME | ccoe-orchestrator | OTel service name |
| OTEL_SAMPLING_RATIO | 1.0 | trace sampling ratio |

補充：

- 本地 Copilot provider 會輸出 [CopilotHealth] 結構化 log。
- 會透過 OpenTelemetry meter 發出健康事件、重建次數與 preflight/invoke latency 指標。

## Agent 定義同步

agent_sync.py 可在本地 YAML 與 Foundry agent definition/version 間同步。

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

目前由 prompts/ 管理的 agent YAML 共 5 份：

- Azure-Architecture-Clarification-Agent.yaml
- Azure-Diagram-Generation-Agent.yaml
- Azure-Pricing-Browser-Agent.yaml
- Azure-Pricing-Structure-Agent.yaml
- Azure-Terraform-Generation-Agent.yaml

Runtime 一律以 prompts/ 內的 YAML 為本地 source of truth；hybrid 模式下 diagram 與 terraform prompt 也直接由本地 YAML 載入。

## 專案結構

```text
ccoe-Orchestrator/
├── Dockerfile
├── README.md
├── README.en.md
├── pyproject.toml
├── requirements.txt
├── prompts/
│   ├── Azure-Architecture-Clarification-Agent.yaml
│   ├── Azure-Diagram-Generation-Agent.yaml
│   ├── Azure-Pricing-Browser-Agent.yaml
│   ├── Azure-Pricing-Structure-Agent.yaml
│   └── Azure-Terraform-Generation-Agent.yaml
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
    ├── test_copilot_local_agents.py
    ├── test_diagram_regen.py
    ├── test_diagram_renderer_agent.py
    ├── test_hybrid_agents.py
    ├── test_retail_prices.py
    └── test_workflow.py
```

### 模組說明

| 檔案 | 用途 |
|---|---|
| main.py | workflow 建構、CLI 入口、HTTP server 入口 |
| executors.py | 主要 workflow steps、shared state、並行 Terraform/Pricing 控制 |
| contracts.py | Pydantic data contracts |
| foundry_agents.py | Foundry Responses API 呼叫與輸出解析 |
| copilot_local_agents.py | 本地 GitHub Copilot provider、session 持久化、health/retry 邏輯 |
| hybrid_agents.py | Copilot / Foundry 路由邏輯 |
| diagram_renderer.py | diagram.py 渲染、import 修復與 render feedback |
| repair_feedback.py | Diagram / Terraform 修復 prompt 的標準化 repair context |
| retail_prices.py | Azure Retail Prices API 查詢 |
| xlsx_builder.py | 成本估算 Excel 產生 |
| io.py | 統一輸出檔案、session state 與 zip 打包 |
| observability.py | Azure Monitor + OpenTelemetry 設定 |

## 產物輸出

成功執行後，所有產物預設會寫入 out/：

```text
out/
├── .copilot_sessions/
│   └── <session-id>.json
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

註記：

- resource_manifest.json 由核准後的資源清單往下游傳遞。
- .copilot_sessions/<session-id>.json 會持久化 turns 與 events，供續輪與除錯使用。

## 測試

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
pytest -q
pytest tests/test_workflow.py -v
pytest tests/test_hybrid_agents.py tests/test_copilot_local_agents.py -q
pytest tests/test_diagram_regen.py tests/test_diagram_renderer_agent.py tests/test_agent_sync.py -q
```

測試主要以 MOCK_MODE=true 為前提，不需連到 Foundry。現有測試重點覆蓋：

- workflow 正規化、多輪對話與輸出寫入
- hybrid routing 與 Terraform staged generation
- 本地 Copilot provider 的 timeout、restart、session 恢復與 progress event 持久化
- diagram regen repair context 與 render feedback
- prompt YAML 載入與 agent_sync 相容性

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
  -e GITHUB_COPILOT_WORKDIR="out" \
  ccoe-orchestrator
```

Docker image 特色：

- 基底映像為 python:3.12-slim
- 內含 graphviz
- 預設 PYTHONPATH=/app/src
- 預設 RUN_MODE=server
- 預設埠 8088

## 技術棧

| 項目 | 技術 |
|---|---|
| Agent framework | Microsoft Agent Framework Core 1.0.0rc3 |
| LLM backend | GitHub Copilot provider + Microsoft Foundry |
| Data contracts | Pydantic v2 |
| Pricing | Azure Retail Prices API / browser automation |
| Diagram | diagrams + Graphviz |
| Observability | OpenTelemetry + Azure Monitor |
| Testing | pytest + pytest-asyncio |

## License

Internal — CCoE Team
