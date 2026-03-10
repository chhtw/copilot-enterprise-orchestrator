# Azure Platform Orchestrator

[繁體中文版本](README.md)

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![MAF Core](https://img.shields.io/badge/Microsoft%20Agent%20Framework%20Core-1.0.0rc3-purple)](https://pypi.org/project/agent-framework-core/)

An Azure architecture orchestrator built with Microsoft Agent Framework. The project chains requirement normalization, multi-turn clarification, diagram generation and rendering, Terraform generation, pricing estimation, and executive summary generation into a single workflow, with prompts/ YAML files as the local prompt source of truth.

The current runtime model is hybrid:

- Azure-Diagram-Generation-Agent and Azure-Terraform-Generation-Agent run through the local GitHub Copilot provider.
- Architecture clarification and pricing-related agents can still run through Microsoft Foundry.
- With MOCK_MODE=true, the full workflow can run offline for development and tests.

## Current Status

- The workflow is fixed at 9 steps, from Spec normalization through diagram, Terraform, pricing, and executive summary output.
- The local Copilot provider now supports startup preflight, retry/restart, idle progress timeout detection, session persistence, and process-restart conversation recovery.
- Terraform generation defaults to staged mode, splitting output into foundation, main, and ops bundles before assembling the final TerraformOutput.
- Diagram review and revision reuse the same diagram session so local Copilot multi-turn context stays intact.
- Output defaults to out/; a custom OUTPUT_DIR is honored only when ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE=true is explicitly enabled.

## Highlights

- Chains 9 main workflow steps through WorkflowBuilder
- Supports both CLI and HTTP server modes
- Supports session language switching between Traditional Chinese and English
- Renders diagram.py locally into PNG/SVG with import auto-fix and agent regeneration support
- Runs Terraform generation and pricing structure generation in parallel after diagram approval
- Treats approved_resource_manifest as the downstream contract for Terraform and pricing
- Persists local Copilot sessions under out/.copilot_sessions/ with both turns and execution events
- Includes agent_sync.py for syncing local YAML files and Foundry agent definitions

## Workflow Overview

| Step | Component | Purpose | Primary Output |
|---|---|---|---|
| 1 | NormalizeExecutor | Normalize user input into a Spec | spec.json |
| 2 | RequirementsClarificationExecutor | Fill required baseline fields | updated spec.json |
| 3 | ArchitectureClarificationExecutor | Clarify Azure architecture details in multiple turns | architecture_details |
| 4 | DiagramGenerationExecutor | Generate diagrams Python code and approved_resource_manifest | diagram.py |
| 5 | DiagramRenderingExecutor | Render diagram.py locally | diagram.png / diagram.svg, render_log.txt |
| 6 | DiagramReviewExecutor | Wait for approval, revision, or rejection | approved diagram state |
| 7 | ParallelTerraformPricingExecutor | Generate Terraform and pricing structure in parallel | terraform/*, pricing_structure.json, resource_manifest.json |
| 8 | RetailPricingExecutor / BrowserPricingExecutor | Produce the pricing workbook | estimate.xlsx, calculator_share_url.txt |
| 9 | SummaryExecutor | Generate the executive summary and artifact bundle | executive_summary.md, artifacts.zip |

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
  - valid Azure authentication, for example az login

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
```

The project uses a src/ layout. If you are not running through VS Code launch settings, set PYTHONPATH first.

### 2. Prepare environment variables

```bash
cp .env.example .env
```

`.env.example` is a minimal local sample. The environment variable tables in this README are the authoritative list of runtime knobs.

Minimal local setup:

```dotenv
MOCK_MODE=true
RUN_MODE=cli
OUTPUT_DIR=./out
PRICING_EXECUTION_MODE=retail_api

# Only enable this for tests or isolated runs
# ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE=true
```

Notes:

- On success, runtime outputs are written to out/ by default.
- OUTPUT_DIR is ignored unless ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE=true is explicitly set.
- Local Copilot sessions persist under out/.copilot_sessions/ by default; with override mode enabled they are written under OUTPUT_DIR/.copilot_sessions/.
- Session payloads store both turns and events, including preflight, attempts, streaming progress, stuck timeout, overall timeout, retries, completion, and failure trails.

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

- Startup runs a local Copilot preflight check first; this is skipped automatically when MOCK_MODE=true.
- Architecture clarification and pricing agents use the Foundry project endpoint.
- Diagram and Terraform agents use the local GitHub Copilot provider.
- If Foundry is involved, make sure the current shell has valid Azure credentials.

## Run Modes

| Mode | Description |
|---|---|
| RUN_MODE=cli | Default mode. Prompts for session language first and reads follow-up answers from stdin. |
| RUN_MODE=server | Starts the HTTP server for hosting-adapter or external callers. Default port is 8088. |

CLI mode supports multi-turn commands such as /done and /skip.

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
| MOCK_MODE | true | true uses mock agents; false uses hybrid routing |
| RUN_MODE | cli | cli or server |
| OUTPUT_DIR | ./out | Output directory for all artifacts; only honored when the override flag is enabled |
| ORCHESTRATOR_ALLOW_OUTPUT_DIR_OVERRIDE | unset | Explicitly allows OUTPUT_DIR to override the default out/ |
| PRICING_EXECUTION_MODE | retail_api | retail_api or browser |

### Foundry / Agents

| Variable | Default | Description |
|---|---|---|
| AZURE_AI_PROJECT_ENDPOINT | sample endpoint | Foundry project endpoint |
| AGENT_TIMEOUT | 120 | Base timeout for Foundry agents; diagram/terraform fall back to this when their specific timeout is unset |
| AGENT_MAX_RETRIES | 2 | Maximum retry attempts for Foundry Responses API calls |
| AGENT_RETRY_DELAY | 5.0 | Base delay in seconds before Foundry retries |
| ARCHITECTURE_AGENT_NAME | Azure-Architecture-Clarification-Agent | architecture clarification agent name |
| TERRAFORM_AGENT_NAME | Azure-Terraform-Generation-Agent | Terraform agent name |
| DIAGRAM_AGENT_NAME | Azure-Diagram-Generation-Agent | diagram agent name |
| DIAGRAM_REVIEW_AGENT_NAME | Diagram-Review | display name used for Step 6 review prompts |
| PRICING_STRUCTURE_AGENT_NAME | Azure-Pricing-Structure-Agent | pricing structure agent name |
| PRICING_BROWSER_AGENT_NAME | Azure-Pricing-Browser-Agent | browser pricing agent name |
| COST_STRUCTURE_AGENT_TIMEOUT | 300 | Pricing structure agent timeout |
| COST_BROWSER_AGENT_TIMEOUT | 600 | Browser pricing agent timeout |

### Local Copilot Provider

| Variable | Default | Description |
|---|---|---|
| GITHUB_COPILOT_MODEL | resolved from prompt YAML | Model ID used by the local Copilot provider |
| GITHUB_COPILOT_WORKDIR | current process working directory when unset | Forces the GitHub Copilot SDK to start in the specified directory |
| GITHUB_COPILOT_TIMEOUT | 1200 | Overall hard timeout for local Copilot; takes precedence over agent-specific timeouts |
| GITHUB_COPILOT_PROGRESS_TIMEOUT | 180 | Idle timeout for one invoke; if no streaming progress arrives, the attempt is treated as stuck |
| GITHUB_COPILOT_MAX_RESTARTS | 1 | Maximum number of client/agent rebuilds after unhealthy provider behavior |
| GITHUB_COPILOT_RETRY_DELAY | 2.0 | Initial delay before rebuild; subsequent retries use exponential backoff |
| GITHUB_COPILOT_STARTUP_PREFLIGHT | true | Validates local Copilot availability during CLI/server startup |
| DIAGRAM_AGENT_TIMEOUT | 300 | Diagram agent timeout fallback when GITHUB_COPILOT_TIMEOUT is unset |
| TERRAFORM_AGENT_TIMEOUT | 300 | Terraform agent timeout fallback when GITHUB_COPILOT_TIMEOUT is unset |

### Diagram / Terraform Validation

| Variable | Default | Description |
|---|---|---|
| RENDER_DIAGRAM | true | whether to render diagram.py |
| RENDER_TIMEOUT | 60 | diagram subprocess timeout |
| MAX_FIX_RETRIES | 3 | maximum import auto-fix retries |
| MAX_AGENT_REGEN_RETRIES | 2 | retries when sending render errors back to the diagram agent |
| TF_VALIDATE_ENABLED | true | whether to run terraform validate |
| MAX_TF_VALIDATE_RETRIES | 2 | retries when sending Terraform validation failures back to the agent |
| TERRAFORM_AGENT_GENERATION_MODE | staged | staged or single; staged splits generation into foundation/main/ops bundles |
| MAX_TERRAFORM_NONFINAL_RETRIES | 1 | maximum auto-finalize retries when the Terraform agent returns a non-final response |

### Observability

| Variable | Default | Description |
|---|---|---|
| APPLICATIONINSIGHTS_CONNECTION_STRING | empty | exports telemetry to Application Insights when set |
| OTEL_SERVICE_NAME | ccoe-orchestrator | OTel service name |
| OTEL_SAMPLING_RATIO | 1.0 | trace sampling ratio |

Additional notes:

- The local Copilot provider emits structured [CopilotHealth] logs.
- OpenTelemetry metrics track health events, restart counts, and preflight/invoke latency.

## Agent Definition Sync

Use agent_sync.py to sync local YAML files and Foundry agent definitions/versions.

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

The repository currently manages 5 prompt YAML files under prompts/:

- Azure-Architecture-Clarification-Agent.yaml
- Azure-Diagram-Generation-Agent.yaml
- Azure-Pricing-Browser-Agent.yaml
- Azure-Pricing-Structure-Agent.yaml
- Azure-Terraform-Generation-Agent.yaml

Runtime always treats the YAML files under prompts/ as the local source of truth. In hybrid mode, diagram and Terraform prompts are also loaded directly from local YAML.

The files under `skills/terraform/` are loaded by the Terraform prompt builders to inject style-guide, Azure Verified Modules, and test guidance into generation and repair flows.

## Project Structure

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
   ├── __init__.py
   ├── conftest.py
    ├── test_agent_sync.py
    ├── test_copilot_local_agents.py
    ├── test_diagram_regen.py
    ├── test_diagram_renderer_agent.py
    ├── test_hybrid_agents.py
    ├── test_retail_prices.py
    └── test_workflow.py
```

### Module Notes

| File | Purpose |
|---|---|
| main.py | workflow construction, CLI entry, HTTP server entry |
| executors.py | workflow steps, shared state, and parallel Terraform/Pricing control |
| contracts.py | Pydantic data contracts |
| foundry_agents.py | Foundry Responses API calls and output parsing |
| copilot_local_agents.py | local GitHub Copilot provider, session persistence, and health/retry logic |
| hybrid_agents.py | routing between Copilot and Foundry |
| diagram_renderer.py | diagram rendering, import repair, and render feedback |
| repair_feedback.py | standardized repair context for diagram and Terraform fixes |
| retail_prices.py | Azure Retail Prices API access |
| xlsx_builder.py | pricing workbook generation |
| io.py | output writing, session state persistence, and zip packaging |
| observability.py | Azure Monitor and OpenTelemetry setup |

## Output Artifacts

On success, runtime outputs are written to out/ by default:

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

Notes:

- resource_manifest.json carries the approved resource contract into downstream pricing and Terraform steps.
- .copilot_sessions/<session-id>.json persists both turns and events for resume and debugging scenarios.

## Tests

```bash
source .venv/bin/activate
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
pytest -q
pytest tests/test_workflow.py -v
pytest tests/test_retail_prices.py -q
pytest tests/test_hybrid_agents.py tests/test_copilot_local_agents.py -q
pytest tests/test_diagram_regen.py tests/test_diagram_renderer_agent.py tests/test_agent_sync.py -q
```

Note: `tests/conftest.py` automatically adds `src/` to `sys.path`, so plain `pytest` usually works without extra setup. The `PYTHONPATH` export remains documented because it keeps shell usage, CLI runs, and direct module execution consistent.

Most tests assume MOCK_MODE=true and do not require Foundry access. Current coverage focuses on:

- workflow normalization, multi-turn flow, and output writers
- hybrid routing and staged Terraform generation
- local Copilot timeout, restart, session recovery, and progress-event persistence
- diagram regeneration repair context and render feedback
- prompt YAML loading and agent_sync compatibility

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
  -e GITHUB_COPILOT_WORKDIR="out" \
  ccoe-orchestrator
```

Image characteristics:

- based on python:3.12-slim
- includes graphviz
- defaults PYTHONPATH=/app/src
- defaults RUN_MODE=server
- exposes port 8088

## Tech Stack

| Area | Technology |
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
