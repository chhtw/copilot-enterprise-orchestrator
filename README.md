# CCoE Orchestrator Agent

> 使用 Microsoft Agent Framework (MAF) `SequentialBuilder` 串接 9 個 Executor 的 Azure 架構編排器。
> 只做需求澄清與 spec normalization — 所有重工作委派給 Foundry Project 內的 specialist agents。

## 架構概覽

```
使用者需求 → [Orchestrator Agent]
                  │
                  ├─ [1] NormalizeExecutor            — 使用者輸入 → spec.json
                  ├─ [2] RequirementClarificationExecutor — 多輪對話補齊基本欄位
                  ├─ [3] ArchitectureClarificationExecutor
                  │      (Architecture-Clarification-Agent)
                  │      → architecture_details.json
                  │
                  ├─ [4] DiagramExecutor              — 呼叫 DaC-Dagrams-Mingrammer
                  │      → diagram.py
                  ├─ [5] DiagramRenderExecutor         — 本地執行 diagram.py → diagram.png
                  ├─ [6] DiagramApprovalExecutor       — 使用者核准架構圖（GATE）
                  │
                  ├─ [7] ParallelTerraformCostExecutor — 並行
                  │         ├─ Azure-Terraform-Architect-Agent → main.tf, ...
                  │         └─ Agent-AzureCalculator          → cost_structure.json
                  │
                  ├─ [8] RetailPricesCostExecutor      — Azure Retail Prices API → estimate.xlsx
                  │      (或 CostBrowserExecutor，依 COST_STEP3B_MODE)
                  ├─ [9] SummaryExecutor               — executive_summary.md
                  └─ 回傳交付物清單
```

> **設計決策**：Architecture-Clarification-Agent 在「畫圖前」確認所有架構細節；
> 架構圖核准後才「同時」呼叫 Terraform + Cost Agent，縮短總等待時間並避免 IaC 重做。

## Agents 與本地模組

### Foundry Specialist Agents（遠端呼叫）

| Agent Name                        | 職責                               | 輸入                                    | 輸出                                                          |
|-----------------------------------|------------------------------------|-----------------------------------------|---------------------------------------------------------------|
| Architecture-Clarification-Agent  | 多輪對話確認架構細節（10 個維度）  | spec.json                               | architecture_details.json                                     |
| DaC-Dagrams-Mingrammer            | 生成 Python diagrams 程式碼        | spec.json + architecture_details.json   | diagram.py                                                    |
| Azure-Terraform-Architect-Agent   | 生成 Terraform HCL (AVM + TGrunt)  | spec.json + approved_resource_manifest  | main.tf, variables.tf, outputs.tf, terragrunt.hcl, README.md |
| Agent-AzureCalculator             | 估算 Azure 成本結構                | spec.json + approved_resource_manifest  | cost_structure.json                                           |
| Agent-AzureCalculator-BrowserAuto | 瀏覽器自動化查詢 Azure 計算機      | cost_structure.json                     | estimate.xlsx（browser mode 用）                              |

### 本地模組（不需 Foundry）

| 模組                    | 說明                                                              |
|-------------------------|-------------------------------------------------------------------|
| `diagram_renderer.py`   | 本地執行 diagram.py → PNG，含 auto-fix import 名稱錯誤（MAF Agent 封裝） |
| `retail_prices.py`      | 查詢 Azure Retail Prices REST API，產生逐項定價（免費、無需驗證） |
| `xlsx_builder.py`       | 將定價資料組合成 estimate.xlsx                                    |

## 快速開始

### 1. 建立虛擬環境

```bash
cd ccoe-Orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 設定環境變數

```bash
# 建立 .env 並填入所需的值（參考下方「環境變數」表格）
touch .env
```

### 3. Mock Mode（離線測試）

```bash
# .env 或環境變數
MOCK_MODE=true

# 啟動 HTTP server
python -m orchestrator_app.main

# 或 CLI 模式
RUN_MODE=cli python -m orchestrator_app.main "我需要 App Service + VNet in eastasia"
```

### 4. Real Mode（連接 Foundry）

```bash
# .env
MOCK_MODE=false

# 確保已登入 Azure
az login

# 啟動
python -m orchestrator_app.main
```

Server 啟動後，預設在 `http://localhost:8088` 接收請求。

## 環境變數

| 變數                      | 預設值                                          | 說明                                                     |
|---------------------------|-------------------------------------------------|----------------------------------------------------------|
| PROJECT_ENDPOINT          | `https://aif-ch-cht-ccoe-ai-agent.services...`  | Foundry Project endpoint                                 |
| MODEL_DEPLOYMENT          | `gpt-5.2`                                       | 模型部署名稱                                              |
| CLARIFICATION_AGENT_NAME  | `Architecture-Clarification-Agent`               | 架構澄清 agent 名稱                                       |
| TERRAFORM_AGENT_NAME      | `Azure-Terraform-Architect-Agent`                | Terraform agent 名稱                                      |
| DIAGRAM_AGENT_NAME        | `DaC-Dagrams-Mingrammer`                         | Diagram agent 名稱                                        |
| COST_AGENT_NAME           | `Agent-AzureCalculator`                          | Cost 結構 agent 名稱                                      |
| COST_BROWSER_AGENT_NAME   | `Agent-AzureCalculator-BrowserAuto`              | Browser mode 的 cost agent 名稱                           |
| OUTPUT_DIR                | `./out`                                          | 產物輸出目錄                                              |
| MOCK_MODE                 | `true`                                           | `true`=離線 mock / `false`=真實呼叫 Foundry               |
| RUN_MODE                  | `cli`                                            | `server`=HTTP（Foundry 部署）/ `cli`=互動命令列            |
| COST_STEP3B_MODE          | `retail_api`                                     | `retail_api`=本地 Azure Retail Prices API；`browser`=Foundry browser agent |
| RENDER_DIAGRAM            | `true`                                           | 是否啟用本地 diagram.py 渲染（需安裝 graphviz）            |
| RENDER_TIMEOUT            | `60`                                             | diagram subprocess 逾時秒數                               |
| MAX_FIX_RETRIES           | `3`                                              | diagram import 自動修正最大重試次數                        |

## 專案結構

```
ccoe-Orchestrator/
├── Dockerfile
├── pyproject.toml
├── README.md
├── requirements.txt
├── prompts/
│   ├── Agent-AzureCalculator.system.md
│   └── Agent-AzureCalculator-BrowserAuto.system.md
├── src/
│   ├── __init__.py
│   └── orchestrator_app/
│       ├── __init__.py
│       ├── contracts.py          # Pydantic 資料模型（Spec, TerraformOutput, DiagramOutput 等）
│       ├── diagram_renderer.py   # 本地 diagram.py 渲染 + auto-fix import 名稱錯誤
│       ├── executors.py          # 9 個 MAF Executor 實作（含 multi-turn + ParallelExecutor）
│       ├── foundry_agents.py     # 真實 Foundry agent 呼叫 + prompt builders
│       ├── io.py                 # 產物寫入 (spec / diagram / tf / cost / summary)
│       ├── main.py               # Entrypoint：build_workflow() + HTTP server + CLI 互動迴圈
│       ├── mock_agents.py        # Mock mode 替代方案（離線測試用）
│       ├── retail_prices.py      # Azure Retail Prices REST API 查詢（免費、無需驗證）
│       └── xlsx_builder.py       # 將定價資料組合成 estimate.xlsx
└── tests/
    ├── __init__.py
    ├── test_retail_prices.py     # retail_prices 模組單元測試
    └── test_workflow.py          # E2E Workflow 測試（MOCK_MODE=true）
```

## 交付物清單

執行成功後，`OUTPUT_DIR/` 會包含：

```
out/
├── spec.json                    # 需求規格（含 architecture_details）
├── architecture_details.json    # Architecture-Clarification-Agent 澄清後的架構細節
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── locals.tf
│   ├── versions.tf
│   ├── providers.tf
│   ├── terragrunt.hcl           # root Terragrunt config
│   ├── environments/
│   │   ├── dev/terragrunt.hcl
│   │   └── prod/terragrunt.hcl
│   └── README.md
├── resource_manifest.json
├── diagram.py
├── diagram.png (或 .svg)
├── render_log.txt               # diagram subprocess 執行日誌
├── cost_structure.json          # Agent-AzureCalculator 輸出的成本結構
├── estimate.xlsx                # Azure Retail Prices API 逐項定價表
└── executive_summary.md
```

## Workflow 行為規則

1. **Orchestrator 不生成 Terraform / Diagram / Cost** — 全部委派給 specialist agents
2. **Architecture-Clarification-Agent 先行** — 確認 10 個架構維度後，diagram 才有完整輸入
3. **Diagram 先於 Terraform** — 使用者確認架構圖後，才並行呼叫 Terraform + Cost，避免 IaC 重做
4. **Diagram Approval Gate** — `DiagramApprovalExecutor` 等待使用者輸入 `approve`/`revise`/`reject`
5. **Step 7 並行** — `ParallelTerraformCostExecutor` 同時呼叫 Terraform 與 Cost Structure Agent，輸入皆為 `approved_resource_manifest`
6. **Diagram auto-fix** — `DiagramRenderExecutor` 若遇 import 名稱錯誤，自動查找 diagrams 套件並修正，最多重試 `MAX_FIX_RETRIES` 次
7. **Assumptions 自動填補** — 未指定的欄位用預設值，記錄在 spec.json `assumptions` 陣列中
8. **Cost Step 模式** — `COST_STEP3B_MODE=retail_api`（預設）走 `RetailPricesCostExecutor`（本地 API）；`browser` 走 `CostBrowserExecutor`（Foundry browser_automation_preview）
9. **Multi-turn 支援** — `RequirementClarificationExecutor`、`DiagramApprovalExecutor` 以 `ctx.request_info()` 暫停 workflow，CLI 模式從 stdin 讀取；HTTP 模式由 hosting adapter 處理

## Docker

```bash
docker build -t ccoe-orchestrator .
docker run -p 8088:8088 \
  -e MOCK_MODE=false \
  -e PROJECT_ENDPOINT=... \
  ccoe-orchestrator
```

## 部署到 Foundry Hosted Agents

1. 在 Azure AI Foundry 中建立 Hosted Agent 資源
2. 設定 Container Image 指向此 Docker image
3. 設定環境變數 (`MOCK_MODE=false`, `PROJECT_ENDPOINT`, etc.)
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
