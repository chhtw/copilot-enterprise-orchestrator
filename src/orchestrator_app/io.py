"""
io.py — 產物寫入 out/ 目錄、檔案命名、zip 打包。

負責將各 agent 的輸出寫到 OUTPUT_DIR：
  - terraform/main.tf, variables.tf, outputs.tf
  - resource_manifest.json
  - diagram.py, diagram.png/svg, render_log.txt
  - estimate.xlsx, calculator_share_url.txt
  - executive_summary.md
  - spec.json
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .contracts import (
    PricingOutput,
    PricingStructureOutput,
    DiagramOutput,
    Spec,
    StepResult,
    StepStatus,
    TerraformOutput,
    WorkflowResult,
)
from .i18n import tr

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path("./out")
_COPILOT_SESSION_DIRNAME = ".copilot_sessions"


def get_configured_output_dir() -> Path:
    """從環境變數取得目前的輸出目錄，未設定時回退到 ./out。"""
    raw = os.getenv("OUTPUT_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_OUTPUT_DIR


def ensure_output_dir(output_dir: Optional[Path] = None) -> Path:
    """建立並回傳 output 目錄。"""
    d = Path(output_dir) if output_dir is not None else get_configured_output_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_copilot_session_dir(output_dir: Optional[Path] = None) -> Path:
    """回傳本地 Copilot session 持久化目錄。"""
    d = ensure_output_dir(output_dir) / _COPILOT_SESSION_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_copilot_session_state(
    session_id: str,
    payload: dict,
    output_dir: Optional[Path] = None,
) -> Path:
    """將本地 Copilot session 狀態寫入 OUTPUT_DIR。"""
    session_dir = get_copilot_session_dir(output_dir)
    path = session_dir / f"{session_id}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[IO] Wrote %s", path)
    return path


def read_copilot_session_state(
    session_id: str,
    output_dir: Optional[Path] = None,
) -> Optional[dict]:
    """讀取本地 Copilot session 狀態；不存在時回傳 None。"""
    path = get_copilot_session_dir(output_dir) / f"{session_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_spec(spec: Spec, output_dir: Optional[Path] = None) -> Path:
    """將 spec.json 寫入 output dir。"""
    d = ensure_output_dir(output_dir)
    path = d / "spec.json"
    path.write_text(spec.to_json(), encoding="utf-8")
    logger.info("[IO] Wrote %s", path)
    return path


def write_terraform_output(
    tf: TerraformOutput, output_dir: Optional[Path] = None
) -> list[Path]:
    """將 Terraform 產物寫入 output_dir/terraform/。"""
    d = ensure_output_dir(output_dir)
    tf_dir = d / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for filename, content in [
        ("main.tf", tf.main_tf),
        ("variables.tf", tf.variables_tf),
        ("outputs.tf", tf.outputs_tf),
        ("locals.tf", tf.locals_tf),
        ("versions.tf", tf.versions_tf),
        ("providers.tf", tf.providers_tf),
    ]:
        if content:
            p = tf_dir / filename
            p.write_text(content, encoding="utf-8")
            written.append(p)
            logger.info("[IO] Wrote %s", p)

    # Terragrunt files
    for tg_relpath, tg_content in [
        ("terragrunt.hcl", tf.terragrunt_root_hcl),
        ("envs/dev/terragrunt.hcl", tf.terragrunt_dev_hcl),
        ("envs/prod/terragrunt.hcl", tf.terragrunt_prod_hcl),
    ]:
        if tg_content:
            tg_path = tf_dir / tg_relpath
            tg_path.parent.mkdir(parents=True, exist_ok=True)
            tg_path.write_text(tg_content, encoding="utf-8")
            written.append(tg_path)
            logger.info("[IO] Wrote %s", tg_path)

    # Test files → terraform/tests/
    if tf.test_files:
        tests_dir = tf_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        for test_name, test_content in tf.test_files.items():
            if test_content:
                tp = tests_dir / test_name
                tp.write_text(test_content, encoding="utf-8")
                written.append(tp)
                logger.info("[IO] Wrote %s", tp)

    # README.md
    if tf.readme_md:
        readme_path = tf_dir / "README.md"
        readme_path.write_text(tf.readme_md, encoding="utf-8")
        written.append(readme_path)
        logger.info("[IO] Wrote %s", readme_path)

    # resource_manifest.json 放在 output root
    if tf.resource_manifest:
        rm_path = d / "resource_manifest.json"
        rm_path.write_text(tf.resource_manifest.to_json(), encoding="utf-8")
        written.append(rm_path)
        logger.info("[IO] Wrote %s", rm_path)

    return written


def write_diagram_output(
    diag: DiagramOutput, output_dir: Optional[Path] = None
) -> list[Path]:
    """將 diagram 產物寫入 output dir。"""
    d = ensure_output_dir(output_dir)
    written: list[Path] = []

    if diag.diagram_py:
        p = d / "diagram.py"
        p.write_text(diag.diagram_py, encoding="utf-8")
        written.append(p)
        logger.info("[IO] Wrote %s", p)

    if diag.diagram_image:
        ext = diag.diagram_image_ext or "png"
        p = d / f"diagram.{ext}"
        p.write_bytes(diag.diagram_image)
        written.append(p)
        logger.info("[IO] Wrote %s", p)

    # render_log.txt（不論成功或失敗都寫）
    log_path = d / "render_log.txt"
    log_content = diag.render_log or (
        f"[{diag.status.value}] " + (diag.error if diag.error else "No render log available.")
    )
    log_path.write_text(log_content, encoding="utf-8")
    written.append(log_path)
    logger.info("[IO] Wrote %s", log_path)

    return written


def write_pricing_structure_output(
    pricing_structure: PricingStructureOutput, output_dir: Optional[Path] = None
) -> list[Path]:
    """將 pricing_structure.json（Azure-Pricing-Structure-Agent 的中間產物）寫入 output dir。"""
    d = ensure_output_dir(output_dir)
    written: list[Path] = []

    p = d / "pricing_structure.json"
    p.write_text(pricing_structure.to_json(), encoding="utf-8")
    written.append(p)
    logger.info("[IO] Wrote %s", p)

    return written


def write_pricing_output(
    pricing: PricingOutput, output_dir: Optional[Path] = None
) -> list[Path]:
    """將 pricing estimate 產物寫入 output dir。"""
    d = ensure_output_dir(output_dir)
    written: list[Path] = []

    if pricing.estimate_xlsx:
        p = d / "estimate.xlsx"
        p.write_bytes(pricing.estimate_xlsx)
        written.append(p)
        logger.info("[IO] Wrote %s", p)
    else:
        # 寫空 placeholder
        p = d / "estimate.xlsx"
        p.write_text("(placeholder — no estimate generated)", encoding="utf-8")
        written.append(p)
        logger.info("[IO] Wrote placeholder %s", p)

    # calculator_share_url.txt
    url_path = d / "calculator_share_url.txt"
    url_text = pricing.calculator_share_url or "(no URL)"
    url_path.write_text(url_text, encoding="utf-8")
    written.append(url_path)
    logger.info("[IO] Wrote %s", url_path)

    return written


def write_executive_summary(
    spec: Spec,
    steps: list[StepResult],
    output_dir: Optional[Path] = None,
) -> Path:
    """
    產生 executive_summary.md — Orchestrator 只做摘要。
    禁止生成 Terraform / Diagram / Cost 的技術細節。
    """
    d = ensure_output_dir(output_dir)
    language = spec.preferred_language

    lines: list[str] = []
    lines.append(tr(language, f"# 執行摘要 — {spec.project_name}", f"# Executive Summary — {spec.project_name}"))
    lines.append("")
    lines.append(tr(language, f"**產生時間**: {datetime.now(timezone.utc).isoformat()}", f"**Generated at**: {datetime.now(timezone.utc).isoformat()}"))
    lines.append(tr(language, f"**Region**: {spec.region}", f"**Region**: {spec.region}"))
    lines.append(tr(language, f"**環境數**: {spec.environment_count}", f"**Environments**: {spec.environment_count}"))
    lines.append(tr(language, f"**網路模型**: {spec.network_model.value}", f"**Network Model**: {spec.network_model.value}"))
    lines.append(tr(language, f"**承諾類型**: {spec.commitment.value}", f"**Commitment**: {spec.commitment.value}"))
    lines.append(tr(language, f"**幣別**: {spec.currency}", f"**Currency**: {spec.currency}"))
    lines.append("")

    # Section 1: 摘要
    lines.append(tr(language, "## 1. 專案概覽", "## 1. Project Overview"))
    lines.append("")
    lines.append(tr(
        language,
        f"本專案 **{spec.project_name}** 部署於 **{spec.region}**，共 {spec.environment_count} 個環境。",
        f"Project **{spec.project_name}** is planned for **{spec.region}** with {spec.environment_count} environment(s).",
    ))
    if spec.notes:
        lines.append(f"\n> {spec.notes}")
    lines.append("")

    # Section 2: 假設
    lines.append(tr(language, "## 2. 假設", "## 2. Assumptions"))
    lines.append("")
    if spec.assumptions:
        for a in spec.assumptions:
            lines.append(
                tr(
                    language,
                    f"- **{a.field}**: {a.value} _(來源: {a.source})_ — {a.reason}",
                    f"- **{a.field}**: {a.value} _(source: {a.source})_ — {a.reason}",
                )
            )
    else:
        lines.append(tr(language, "- 無假設記錄。", "- No assumptions were recorded."))
    lines.append("")

    # Section 3: 步驟結果
    lines.append(tr(language, "## 3. Workflow 步驟", "## 3. Workflow Steps"))
    lines.append("")
    lines.append(tr(language, "| Step | Status | Artifacts | Notes |", "| Step | Status | Artifacts | Notes |"))
    lines.append("|------|--------|-----------|-------|")
    for s in steps:
        artifacts = ", ".join(s.artifacts) if s.artifacts else "-"
        notes = s.error if s.status == StepStatus.FAILED else "-"
        lines.append(f"| {s.step} | {s.status.value} | {artifacts} | {notes} |")
    lines.append("")

    # Section 4: 注意事項 / 風險
    lines.append(tr(language, "## 4. 風險與注意事項", "## 4. Risks & Notes"))
    lines.append("")
    failed = [s for s in steps if s.status == StepStatus.FAILED]
    if failed:
        lines.append(tr(language, "⚠️ **以下步驟失敗，建議重試或手動處理：**", "⚠️ **The following steps failed and may require a retry or manual action:**"))
        for s in failed:
            lines.append(f"- **{s.step}**: {s.error}")
            if s.retry_suggestion:
                lines.append(tr(language, f"  - 建議: {s.retry_suggestion}", f"  - Suggested action: {s.retry_suggestion}"))
    else:
        lines.append(tr(language, "所有步驟均已成功完成。", "All workflow steps completed successfully."))
    lines.append("")

    # Section 5: 下一步
    lines.append(tr(language, "## 5. 下一步", "## 5. Next Steps"))
    lines.append("")
    lines.append(tr(language, "1. 檢查 `terraform/` 目錄並執行 `terraform plan`", "1. Review the `terraform/` directory and run `terraform plan`"))

    # 檢查 Step 2b (Diagram Render) 是否成功
    diagram_rendered = any(
        s.step == "Step 2b: Diagram Render" and s.status == StepStatus.SUCCESS
        for s in steps
    )
    if diagram_rendered:
        lines.append(tr(language, "2. 檢查 `diagram.png` 架構圖（已由 DiagramRendererAgent 自動渲染）", "2. Review `diagram.png` (rendered automatically by DiagramRendererAgent)"))
    else:
        lines.append(tr(language, "2. 檢查 `diagram.py`，並在本機執行 `python diagram.py` 產生架構圖", "2. Review `diagram.py` and run `python diagram.py` locally to render the diagram"))

    lines.append(tr(language, "3. 檢查 `estimate.xlsx` 確認成本估算", "3. Review `estimate.xlsx` to validate the pricing estimate"))
    lines.append(tr(language, "4. 確認所有假設是否符合實際需求", "4. Confirm that all assumptions match the actual requirements"))
    lines.append("")

    summary_text = "\n".join(lines)
    path = d / "executive_summary.md"
    path.write_text(summary_text, encoding="utf-8")
    logger.info("[IO] Wrote %s", path)
    return path


def zip_output(output_dir: Optional[Path] = None) -> Path:
    """將整個 output 目錄打包成 ZIP。"""
    d = ensure_output_dir(output_dir)
    zip_path = d / "artifacts.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in d.rglob("*"):
            if file_path.is_file() and file_path != zip_path:
                arcname = file_path.relative_to(d)
                zf.write(file_path, arcname)
    logger.info("[IO] Zipped to %s", zip_path)
    return zip_path


def get_artifact_list(output_dir: Optional[Path] = None) -> list[str]:
    """列出 output 目錄中所有檔案。"""
    d = ensure_output_dir(output_dir)
    return sorted(
        str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()
    )
