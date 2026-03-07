# Azure Platform Orchestrator

[繁體中文版本](README.md)

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![MAF](https://img.shields.io/badge/Microsoft%20Agent%20Framework-1.0.0rc2-purple)](https://pypi.org/project/agent-framework-core/)

An Azure architecture orchestrator built with Microsoft Agent Framework. The project combines requirement normalization, multi-turn clarification, diagram generation and rendering, Terraform generation, pricing estimation, and executive summary generation into a single workflow.

The current runtime model is hybrid:

- `Azure-Diagram-Generation-Agent` and `Azure-Terraform-Generation-Agent` run through the local GitHub Copilot provider.
- Architecture clarification and pricing-related agents can still run through Microsoft Foundry.
- With `MOCK_MODE=true`, the full workflow can run offline for development and tests.

## Highlights

- Chains 9 main workflow steps through `WorkflowBuilder`
- Supports both CLI and HTTP server modes
- Supports session language switching between Traditional Chinese and English
- Renders `diagram.py` locally into PNG/SVG with import auto-fix and regeneration support
- Runs Terraform generation and pricing structure generation in parallel after diagram approval
- Writes all artifacts into `out/` or a custom `OUTPUT_DIR`
- Includes `agent_sync.py` for syncing prompt YAML files and Foundry agent definitions

## Workflow Overview

| Step | Component | Purpose | Primary Output |
|---|---|---|---|
| 1 | `NormalizeExecutor` | Normalize user input into a `Spec` | `spec.json` |
| 2 | `RequirementsClarificationExecutor` | Fill required baseline fields | updated `spec.json` |
| 3 | `ArchitectureClarificationExecutor` | Clarify Azure architecture details in multiple turns | `architecture_details` |
| 4 | `DiagramGenerationExecutor` | Generate Python diagrams code | `diagram.py` |
| 5 | `DiagramRenderingExecutor` | Render the architecture diagram locally | `diagram.png` / `diagram.svg`, `render_log.txt` |
| 6 | `DiagramReviewExecutor` | Wait for approval, revision, or rejection | approved diagram state |
| 7 | `ParallelTerraformPricingExecutor` | Generate Terraform and pricing structure in parallel | Terraform files, `pricing_structure.json` |
| 8 | `RetailPricingExecutor` / `BrowserPricingExecutor` | Produce the pricing workbook | `estimate.xlsx` |
| 9 | `SummaryExecutor` | Generate the executive summary and artifact bundle | `executive_summary.md`, `artifacts.zip` |

## Runtime Flow

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

## Quick Start

### Prerequisites

- Python 3.12+
- Graphviz for diagram rendering
- A usable Python virtual environment
- For Real Mode:
  - access to a Microsoft Foundry project
  - access to the GitHub Copilot provider
  - valid Azure authentication, for example via `az login`

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
```

> The project uses a `src/` layout. If you are not running through VS Code launch settings, set `PYTHONPATH` first.

### 2. Prepare environment variables

```bash
cp .env.example .env
```

Minimal local setup:

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
python -m orchestrator_app.main "I need App Service + VNet in eastasia"
RUN_MODE=server python -m orchestrator_app.main
```

Mock Mode does not require Foundry or GitHub Copilot connectivity.

### 4. Real Mode

```bash
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
export MOCK_MODE=false
export AZURE_AI_PROJECT_ENDPOINT="https://<your-project>.services.ai.azure.com/api/projects/<project-name>"
export GITHUB_COPILOT_MODEL="gpt-5.4"
export GITHUB_COPILOT_WORKDIR="out"
python -m orchestrator_app.main
```

Real Mode notes:

- Architecture clarification and pricing agents use the Foundry project endpoint.
- Diagram and Terraform agents use the local GitHub Copilot provider.
- If Foundry is involved, make sure the current shell has valid Azure credentials.

## Run Modes

| Mode | Description |
|---|---|
| `RUN_MODE=cli` | Default mode. Prompts for session language first and reads follow-up answers from stdin. |
| `RUN_MODE=server` | Starts the HTTP server for hosting-adapter or external callers. Default port is `8088`. |

CLI mode supports multi-turn commands such as `/done` and `/skip`.

The first HTTP request can include:

```json
{
  "preferred_language": "en-US",
  "raw_input": "I need App Service with private networking in eastasia"
}
```

## Main Environment Variables

### Core

| Variable | Default | Description |
|---|---|---|
| `MOCK_MODE` | `true` | `true` uses mock agents; `false` uses hybrid routing |
| `RUN_MODE` | `cli` | `cli` or `server` |
| `OUTPUT_DIR` | `./out` | Output directory for all artifacts |
| `PRICING_EXECUTION_MODE` | `retail_api` | `retail_api` or `browser` |

### Foundry / Agents

| Variable | Default | Description |
|---|---|---|
| `AZURE_AI_PROJECT_ENDPOINT` | sample endpoint | Foundry project endpoint |
| `ARCHITECTURE_AGENT_NAME` | `Azure-Architecture-Clarification-Agent` | architecture clarification agent name |
| `TERRAFORM_AGENT_NAME` | `Azure-Terraform-Generation-Agent` | Terraform agent name |
| `DIAGRAM_AGENT_NAME` | `Azure-Diagram-Generation-Agent` | diagram agent name |
| `PRICING_STRUCTURE_AGENT_NAME` | `Azure-Pricing-Structure-Agent` | pricing structure agent name |
| `PRICING_BROWSER_AGENT_NAME` | `Azure-Pricing-Browser-Agent` | browser pricing agent name |

### Timeout / Retry

| Variable | Default | Description |
|---|---|---|
| `AGENT_MAX_RETRIES` | `2` | maximum retries for Foundry agent calls |
| `AGENT_RETRY_DELAY` | `5.0` | retry delay in seconds |
| `AGENT_TIMEOUT` | `120` | default Foundry agent timeout |
| `DIAGRAM_AGENT_TIMEOUT` | `300` | diagram agent timeout |
| `TERRAFORM_AGENT_TIMEOUT` | `300` | Terraform agent timeout |
| `COST_STRUCTURE_AGENT_TIMEOUT` | `300` | pricing structure agent timeout |
| `COST_BROWSER_AGENT_TIMEOUT` | `600` | browser pricing agent timeout |
| `GITHUB_COPILOT_TIMEOUT` | `1200` | local Copilot provider timeout |
| `GITHUB_COPILOT_WORKDIR` | current process working directory when unset | Forces the GitHub Copilot SDK to start in the specified directory; relative paths are resolved from the process startup directory and created automatically, for example `out` |

### Diagram / Terraform Validation

| Variable | Default | Description |
|---|---|---|
| `RENDER_DIAGRAM` | `true` | whether to render `diagram.py` |
| `RENDER_TIMEOUT` | `60` | diagram subprocess timeout |
| `MAX_FIX_RETRIES` | `3` | maximum import auto-fix retries |
| `MAX_AGENT_REGEN_RETRIES` | `2` | retries when sending render errors back to the diagram agent |
| `TF_VALIDATE_ENABLED` | `true` | whether to run Terraform validate |
| `MAX_TF_VALIDATE_RETRIES` | `2` | retries when sending Terraform validation failures back to the agent |

### Observability

| Variable | Default | Description |
|---|---|---|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | empty | exports telemetry to Application Insights when set |
| `OTEL_SERVICE_NAME` | `ccoe-orchestrator` | OTel service name |
| `OTEL_SAMPLING_RATIO` | `1.0` | trace sampling ratio |

## Agent Definition Sync

Use `agent_sync.py` to sync local YAML files and Foundry agent definitions/versions.

```bash
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"

# Pull all agent definitions
python -m orchestrator_app.agent_sync pull

# Pull a single agent
python -m orchestrator_app.agent_sync pull Azure-Architecture-Clarification-Agent

# Push all agent definitions
python -m orchestrator_app.agent_sync push

# Push a single agent
python -m orchestrator_app.agent_sync push Azure-Pricing-Structure-Agent
```

Notes:

- Runtime always treats the YAML files under `prompts/` as the local source of truth.
- In hybrid mode, Terraform and diagram prompts are also loaded directly from local YAML.
- If hosted agents still exist in Foundry, push after editing YAML to create a new version.

## Project Structure

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

### Module Notes

| File | Purpose |
|---|---|
| `main.py` | workflow construction, CLI entry, HTTP server entry |
| `executors.py` | main workflow steps and shared-state control |
| `contracts.py` | Pydantic data contracts |
| `foundry_agents.py` | Foundry Responses API calls and output parsing |
| `copilot_local_agents.py` | local prompt agents backed by GitHub Copilot provider |
| `hybrid_agents.py` | routing between Copilot and Foundry |
| `diagram_renderer.py` | diagram rendering, import repair, and render feedback |
| `retail_prices.py` | Azure Retail Prices API access |
| `xlsx_builder.py` | pricing workbook generation |
| `repair_feedback.py` | repair context for diagram and Terraform retry prompts |
| `io.py` | output writing and zip packaging |
| `observability.py` | Azure Monitor and OpenTelemetry setup |

## Output Artifacts

On success, all runtime outputs are written into `OUTPUT_DIR`:

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

## Tests

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
pytest -q
pytest tests/test_workflow.py -v
pytest tests/test_diagram_regen.py tests/test_diagram_renderer_agent.py tests/test_agent_sync.py -q
```

Most tests assume `MOCK_MODE=true` and do not require Foundry access.

## Docker

### Build

```bash
docker build -t ccoe-orchestrator .
```

### Run

```bash
docker run -p 8088:8088 \
  -e RUN_MODE=server \
  -e MOCK_MODE=false \
  -e AZURE_AI_PROJECT_ENDPOINT="https://<your-project>.services.ai.azure.com/api/projects/<project-name>" \
  -e GITHUB_COPILOT_MODEL="gpt-5.4" \
  ccoe-orchestrator
```

Image characteristics:

- based on `python:3.12-slim`
- includes `graphviz`
- defaults `PYTHONPATH=/app/src`
- defaults `RUN_MODE=server`
- exposes port `8088`

## Tech Stack

| Area | Technology |
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
