# Copilot Instructions

## Setup and commands

- This repository uses a `src/` layout. Before running code or tests outside VS Code launch configs, activate the virtualenv and set `PYTHONPATH`:
  ```bash
  source .venv/bin/activate
  export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
  ```
- Install dependencies with:
  ```bash
  pip install -r requirements.txt
  ```
- Run the full test suite with:
  ```bash
  pytest -q
  ```
- Run a single test file with:
  ```bash
  pytest tests/test_workflow.py -v
  ```
- Run a single test case with:
  ```bash
  pytest tests/test_workflow.py::TestNormalizeInput::test_json_input -q
  ```
- Build the container image with:
  ```bash
  docker build -t ccoe-orchestrator .
  ```

## High-level architecture

- The app is a Microsoft Agent Framework workflow orchestrator for Azure solution design. `src/orchestrator_app/main.py` builds a fixed nine-step workflow: normalize input, clarify requirements, clarify architecture, generate diagram code, render the diagram, gate on diagram approval, run Terraform generation and pricing-structure generation in parallel, produce pricing output, then write the executive summary and artifact bundle.
- `src/orchestrator_app/contracts.py` defines the shared Pydantic contracts. `Spec` is the main source of truth for normalized requirements and is persisted to `spec.json`; executor-to-executor handoff happens through shared workflow state keys in `src/orchestrator_app/executors.py`.
- Runtime routing is split by provider. With `MOCK_MODE=true`, executors use deterministic mock agents for offline development and tests. With `MOCK_MODE=false`, `src/orchestrator_app/hybrid_agents.py` routes diagram and Terraform generation to the local GitHub Copilot provider in `src/orchestrator_app/copilot_local_agents.py`, while architecture clarification and pricing agents still use Foundry via `src/orchestrator_app/foundry_agents.py`.
- Artifact writing is centralized in `src/orchestrator_app/io.py`. Executors should write outputs through these helpers so everything lands in `OUTPUT_DIR` consistently, including `spec.json`, `diagram.py`, rendered diagram assets, `pricing_structure.json`, `estimate.xlsx`, Terraform files under `terraform/`, `executive_summary.md`, and the final zip bundle.

## Key repository conventions

- Default user-facing language is Traditional Chinese (`zh-TW`), with English supported through `preferred_language`. Use `src/orchestrator_app/i18n.py` helpers (`normalize_language`, `tr`, and `human_language_instruction`) instead of ad hoc language branching, and do not translate JSON keys, schema fields, filenames, or code identifiers.
- Treat `prompts/*.yaml` as the local source of truth for agent instructions. The local Copilot agents load their instructions and model IDs from those YAML files, and `python -m orchestrator_app.agent_sync pull|push` is the supported way to synchronize them with Foundry definitions.
- Keep provider routing behavior intact: Terraform and diagram work are the Copilot-local specialists in hybrid mode; architecture clarification and pricing remain Foundry-backed unless the repo explicitly changes that routing.
- Tests are written to run in mock mode. Many test modules set `MOCK_MODE=true` themselves, so preserve offline-safe behavior and avoid introducing new test dependencies on Foundry or live Copilot sessions unless the test explicitly covers that integration.
- Respect the repository's environment-driven behavior instead of hardcoding paths or modes. Important toggles include `MOCK_MODE`, `RUN_MODE`, `OUTPUT_DIR`, `PRICING_EXECUTION_MODE`, `GITHUB_COPILOT_TIMEOUT`, `GITHUB_COPILOT_WORKDIR`, and the agent-specific timeout variables used by diagram, Terraform, and pricing flows.
- `observability.setup_observability()` is intentionally called before importing executors in `main.py` so tracing is available early. Preserve that ordering if you change startup code.
