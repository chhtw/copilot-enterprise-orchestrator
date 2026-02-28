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
    CostOutput,
    CostStructureOutput,
    DiagramOutput,
    Spec,
    StepResult,
    StepStatus,
    TerraformOutput,
    WorkflowResult,
)

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./out"))


def ensure_output_dir(output_dir: Optional[Path] = None) -> Path:
    """建立並回傳 output 目錄。"""
    d = output_dir or OUTPUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def write_cost_structure_output(
    cost_structure: CostStructureOutput, output_dir: Optional[Path] = None
) -> list[Path]:
    """將 cost_structure.json（Agent-AzureCalculator 的中間產物）寫入 output dir。"""
    d = ensure_output_dir(output_dir)
    written: list[Path] = []

    p = d / "cost_structure.json"
    p.write_text(cost_structure.to_json(), encoding="utf-8")
    written.append(p)
    logger.info("[IO] Wrote %s", p)

    return written


def write_cost_output(
    cost: CostOutput, output_dir: Optional[Path] = None
) -> list[Path]:
    """將 cost 產物寫入 output dir。"""
    d = ensure_output_dir(output_dir)
    written: list[Path] = []

    if cost.estimate_xlsx:
        p = d / "estimate.xlsx"
        p.write_bytes(cost.estimate_xlsx)
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
    url_text = cost.calculator_share_url or "(no URL)"
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

    lines: list[str] = []
    lines.append(f"# Executive Summary — {spec.project_name}")
    lines.append("")
    lines.append(f"**Generated at**: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"**Region**: {spec.region}")
    lines.append(f"**Environments**: {spec.environment_count}")
    lines.append(f"**Network Model**: {spec.network_model.value}")
    lines.append(f"**Commitment**: {spec.commitment.value}")
    lines.append(f"**Currency**: {spec.currency}")
    lines.append("")

    # Section 1: 摘要
    lines.append("## 1. Project Overview")
    lines.append("")
    lines.append(
        f"本專案 **{spec.project_name}** 部署於 **{spec.region}**，"
        f"共 {spec.environment_count} 個環境。"
    )
    if spec.notes:
        lines.append(f"\n> {spec.notes}")
    lines.append("")

    # Section 2: 假設
    lines.append("## 2. Assumptions")
    lines.append("")
    if spec.assumptions:
        for a in spec.assumptions:
            lines.append(
                f"- **{a.field}**: {a.value} _(source: {a.source})_ — {a.reason}"
            )
    else:
        lines.append("- 無假設記錄。")
    lines.append("")

    # Section 3: 步驟結果
    lines.append("## 3. Workflow Steps")
    lines.append("")
    lines.append("| Step | Status | Artifacts | Notes |")
    lines.append("|------|--------|-----------|-------|")
    for s in steps:
        artifacts = ", ".join(s.artifacts) if s.artifacts else "-"
        notes = s.error if s.status == StepStatus.FAILED else "-"
        lines.append(f"| {s.step} | {s.status.value} | {artifacts} | {notes} |")
    lines.append("")

    # Section 4: 注意事項 / 風險
    lines.append("## 4. Risks & Notes")
    lines.append("")
    failed = [s for s in steps if s.status == StepStatus.FAILED]
    if failed:
        lines.append("⚠️ **以下步驟失敗，建議重試或手動處理：**")
        for s in failed:
            lines.append(f"- **{s.step}**: {s.error}")
            if s.retry_suggestion:
                lines.append(f"  - 建議: {s.retry_suggestion}")
    else:
        lines.append("所有步驟均已成功完成。")
    lines.append("")

    # Section 5: 下一步
    lines.append("## 5. Next Steps")
    lines.append("")
    lines.append("1. Review `terraform/` 目錄並執行 `terraform plan`")

    # 檢查 Step 2b (Diagram Render) 是否成功
    diagram_rendered = any(
        s.step == "Step 2b: Diagram Render" and s.status == StepStatus.SUCCESS
        for s in steps
    )
    if diagram_rendered:
        lines.append("2. Review `diagram.png` 架構圖（已由 DiagramRendererAgent 自動渲染）")
    else:
        lines.append("2. Review `diagram.py` 並在本機執行 `python diagram.py` 產生架構圖")

    lines.append("3. Review `estimate.xlsx` 確認成本估算")
    lines.append("4. 確認所有假設是否符合實際需求")
    lines.append("")

    summary_text = "\n".join(lines)
    path = d / "executive_summary.md"
    path.write_text(summary_text, encoding="utf-8")
    logger.info("[IO] Wrote %s", path)
    return path


def zip_output(output_dir: Optional[Path] = None) -> Path:
    """將整個 output 目錄打包成 ZIP。"""
    d = ensure_output_dir(output_dir)
    zip_path = d.parent / f"{d.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in d.rglob("*"):
            if file_path.is_file():
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
