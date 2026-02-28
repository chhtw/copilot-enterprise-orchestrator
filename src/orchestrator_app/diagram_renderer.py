"""
diagram_renderer.py — 本地 Diagram Renderer Agent

使用 Microsoft Agent Framework BaseAgent 封裝本地 diagram.py 渲染邏輯：
  1. 接收 diagram.py 原始碼字串
  2. 寫入暫存檔（OUTPUT_DIR/diagram.py）
  3. 透過 subprocess 執行 `python diagram.py`
  4. 掃描產出的 .png / .svg 圖檔並讀取 bytes
  5. 回傳 DiagramOutput（含圖片二進位 + render log）

需求：
  - 系統需安裝 graphviz（`apt-get install graphviz` or `brew install graphviz`）
  - Python 需安裝 diagrams 套件（`pip install diagrams`）

環境變數：
  RENDER_DIAGRAM=true/false  — 是否啟用本地渲染（預設 true）
  RENDER_TIMEOUT=60          — subprocess 執行逾時秒數（預設 60）
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import pkgutil
import re
import shutil
import signal
import sys
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from .contracts import DiagramOutput, StepStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RENDER_TIMEOUT = int(os.getenv("RENDER_TIMEOUT", "60"))
MAX_FIX_RETRIES = int(os.getenv("MAX_FIX_RETRIES", "3"))


# ---------------------------------------------------------------------------
# Helper: check graphviz availability
# ---------------------------------------------------------------------------
def _check_graphviz() -> tuple[bool, str]:
    """檢查系統是否已安裝 graphviz（dot 指令）。"""
    dot_path = shutil.which("dot")
    if dot_path:
        return True, f"graphviz found at {dot_path}"
    return False, (
        "graphviz is NOT installed. "
        "Please install it: `sudo apt-get install graphviz` (Linux) "
        "or `brew install graphviz` (macOS)."
    )


def _check_diagrams_package() -> tuple[bool, str]:
    """檢查 Python diagrams 套件是否可用。"""
    try:
        import diagrams  # noqa: F401
        return True, f"diagrams package version {getattr(diagrams, '__version__', 'unknown')}"
    except ImportError:
        return False, (
            "Python 'diagrams' package is NOT installed. "
            "Please install it: `pip install diagrams`."
        )


# ---------------------------------------------------------------------------
# Auto-fix: 修正 Foundry Agent 產生的 import 名稱錯誤
# ---------------------------------------------------------------------------
_CLASS_REGISTRY: dict[str, list[str]] | None = None  # module_path → [ClassName, ...]
_CLASS_TO_MODULES: dict[str, list[str]] | None = None  # ClassName → [module_path, ...]


def _build_class_registry() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """
    動態掃描 diagrams.azure.* 所有子模組，建立：
      - module_path → [可用 class 列表]
      - class_name → [哪些 module 可匯入]
    """
    global _CLASS_REGISTRY, _CLASS_TO_MODULES
    if _CLASS_REGISTRY is not None and _CLASS_TO_MODULES is not None:
        return _CLASS_REGISTRY, _CLASS_TO_MODULES

    registry: dict[str, list[str]] = {}
    class_to_mods: dict[str, list[str]] = {}

    try:
        import diagrams.azure as da
        for _importer, modname, _ispkg in pkgutil.iter_modules(da.__path__):
            full_path = f"diagrams.azure.{modname}"
            try:
                mod = importlib.import_module(full_path)
                classes = [k for k in dir(mod) if not k.startswith("_") and k[0].isupper()]
                registry[full_path] = classes
                for cls_name in classes:
                    class_to_mods.setdefault(cls_name, []).append(full_path)
            except Exception:
                continue
    except ImportError:
        pass

    _CLASS_REGISTRY = registry
    _CLASS_TO_MODULES = class_to_mods
    return registry, class_to_mods


def get_available_azure_classes_summary() -> str:
    """
    產生可用的 diagrams.azure.* 類別清單摘要，供 Diagram Agent 修正時參考。

    格式範例::

        diagrams.azure.compute: ContainerInstances, FunctionApps, ...
        diagrams.azure.network: ApplicationGateway, Firewall, ...
    """
    registry, _ = _build_class_registry()
    if not registry:
        return "(diagrams package not installed — cannot list available classes)"
    lines: list[str] = []
    for module_path in sorted(registry):
        classes = sorted(registry[module_path])
        if classes:
            lines.append(f"{module_path}: {', '.join(classes)}")
    return "\n".join(lines)


def _parse_import_errors(stderr: str) -> list[tuple[str, str]]:
    """
    從 stderr 解析 ImportError，回傳 [(bad_class_name, module_path), ...] 。

    支援的格式：
      - ImportError: cannot import name 'X' from 'diagrams.azure.y'
      - ImportError: No module named 'diagrams.azure.y'
    """
    results: list[tuple[str, str]] = []

    # Pattern: cannot import name 'ClassName' from 'module.path'
    for m in re.finditer(
        r"cannot import name ['\"](\w+)['\"] from ['\"]([^'\"]+)['\"]", stderr
    ):
        results.append((m.group(1), m.group(2)))

    return results


def _parse_name_errors(stderr: str) -> list[str]:
    """
    從 stderr 解析 NameError，回傳 [undefined_name, ...] 。

    支援格式：
      - NameError: name 'WAF' is not defined
    """
    results: list[str] = []
    for m in re.finditer(r"NameError: name ['\"](\w+)['\"] is not defined", stderr):
        results.append(m.group(1))
    return results


def _find_best_match(bad_name: str, module_path: str) -> tuple[str | None, str]:
    """
    為 bad_name 找到最佳替代的 class name。

    搜尋策略：
      1. 同模組內 case-insensitive 精確比對
      2. 同模組內 difflib fuzzy match（閾值 0.6）
      3. 相關模組（如 network ↔ networking、monitor ↔ managementgovernance）
      4. 全域所有模組 case-insensitive 搜尋
      5. 全域 fuzzy match
    若找不到回傳 (None, module_path)。
    """
    registry, class_to_mods = _build_class_registry()

    # Sibling/related module mapping
    _related_modules: dict[str, list[str]] = {
        "diagrams.azure.network": ["diagrams.azure.networking"],
        "diagrams.azure.networking": ["diagrams.azure.network"],
        "diagrams.azure.monitor": ["diagrams.azure.managementgovernance", "diagrams.azure.analytics"],
        "diagrams.azure.managementgovernance": ["diagrams.azure.monitor"],
        "diagrams.azure.security": ["diagrams.azure.managementgovernance", "diagrams.azure.identity"],
        "diagrams.azure.storage": ["diagrams.azure.database", "diagrams.azure.databases"],
        "diagrams.azure.database": ["diagrams.azure.databases", "diagrams.azure.storage"],
        "diagrams.azure.databases": ["diagrams.azure.database", "diagrams.azure.storage"],
        "diagrams.azure.compute": ["diagrams.azure.containers"],
        "diagrams.azure.containers": ["diagrams.azure.compute"],
    }

    bad_lower = bad_name.lower()

    # --- Strategy 1 & 2: Same module ---
    same_module_classes = registry.get(module_path, [])
    if same_module_classes:
        # Exact case-insensitive
        for cls in same_module_classes:
            if cls.lower() == bad_lower:
                return cls, module_path
        # Fuzzy match in same module
        matches = get_close_matches(bad_name, same_module_classes, n=1, cutoff=0.55)
        if matches:
            return matches[0], module_path

    # --- Strategy 3: Related modules ---
    related = _related_modules.get(module_path, [])
    for rel_mod in related:
        rel_classes = registry.get(rel_mod, [])
        if not rel_classes:
            continue
        for cls in rel_classes:
            if cls.lower() == bad_lower:
                return cls, rel_mod
        matches = get_close_matches(bad_name, rel_classes, n=1, cutoff=0.55)
        if matches:
            return matches[0], rel_mod

    # --- Strategy 4 & 5: Global search ---
    # Exact case-insensitive globally
    for mod_path, classes in registry.items():
        for cls in classes:
            if cls.lower() == bad_lower:
                return cls, mod_path

    # Fuzzy match globally
    all_classes = [(cls, mod) for mod, classes in registry.items() for cls in classes]
    all_names = [c[0] for c in all_classes]
    matches = get_close_matches(bad_name, all_names, n=1, cutoff=0.5)
    if matches:
        matched_name = matches[0]
        for cls, mod in all_classes:
            if cls == matched_name:
                return cls, mod

    return None, module_path


def _find_name_in_registry(name: str) -> tuple[str | None, str | None]:
    """
    在 diagrams.azure.* registry 中搜尋一個未定義的名稱。

    搜尋策略（由精確到模糊）：
      1. 大小寫不敏感精確比對
      2. difflib fuzzy match
      3. 子字串比對（短名稱如 WAF → WebApplicationFirewallPolicieswaf）
    回傳 (best_class_name, module_path) 或 (None, None)。
    """
    registry, class_to_mods = _build_class_registry()
    name_lower = name.lower()

    # Strategy 1: Case-insensitive exact match globally
    for mod_path, classes in registry.items():
        for cls in classes:
            if cls.lower() == name_lower:
                return cls, mod_path

    # Strategy 2: Fuzzy match globally
    all_classes = [(cls, mod) for mod, classes in registry.items() for cls in classes]
    all_names = [c[0] for c in all_classes]
    matches = get_close_matches(name, all_names, n=1, cutoff=0.5)
    if matches:
        for cls, mod in all_classes:
            if cls == matches[0]:
                return cls, mod

    # Strategy 3: Substring match (for short names like WAF)
    # Check if name appears as a substring (case-insensitive) in any class name
    candidates: list[tuple[str, str]] = []
    for mod_path, classes in registry.items():
        for cls in classes:
            if name_lower in cls.lower():
                candidates.append((cls, mod_path))
    if len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        # Pick shortest name (most specific match)
        candidates.sort(key=lambda x: len(x[0]))
        return candidates[0]

    return None, None


def _rename_outside_strings(code: str, old_name: str, new_name: str) -> str:
    """
    對 code 中的 old_name 做 word-boundary 取代 → new_name，
    但跳過 Python 字串常量（單引號、雙引號、三引號、f-string）內的內容。
    """
    # Pattern: match string literals (group 1) OR the target \bname\b
    string_pat = r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
    combined = string_pat + r'|\b' + re.escape(old_name) + r'\b'

    def _replacer(m: re.Match) -> str:
        if m.group(1):  # It's a string literal — keep unchanged
            return m.group(0)
        return new_name  # Outside strings — replace

    return re.sub(combined, _replacer, code)


def _fix_undefined_names(code: str, undefined_names: list[str]) -> tuple[str, list[str]]:
    """
    修正 NameError — 找出未定義的名稱，嘗試在 diagrams registry 中匹配並補上 import。
    找不到則註解掉使用該名稱的程式碼行。
    """
    log: list[str] = []
    removed_names: list[str] = []
    rename_map: dict[str, str] = {}

    # Collect existing diagrams imports to find insertion point
    lines = code.split("\n")
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("from diagrams"):
            insert_idx = i + 1

    for undef_name in undefined_names:
        best_cls, best_mod = _find_name_in_registry(undef_name)
        if best_cls and best_mod:
            # Add import for this name
            import_line = f"from {best_mod} import {best_cls}"
            lines.insert(insert_idx, import_line)
            insert_idx += 1
            log.append(f"[AutoFix] NameError '{undef_name}' → added: {import_line}")
            if undef_name != best_cls:
                rename_map[undef_name] = best_cls
                log.append(f"[AutoFix] Renaming '{undef_name}' → '{best_cls}' in code body")
        else:
            log.append(f"[AutoFix] NameError '{undef_name}' → REMOVED (no match in diagrams registry)")
            removed_names.append(undef_name)

    fixed = "\n".join(lines)

    # Apply renames (only outside string literals to preserve labels)
    for old_name, new_name in rename_map.items():
        fixed = _rename_outside_strings(fixed, old_name, new_name)

    # Comment out removed names
    if removed_names:
        fixed = _comment_out_references(fixed, removed_names)

    return fixed, log


def _auto_fix_diagram_code(code: str, stderr: str) -> tuple[str, list[str]]:
    """
    根據 ImportError 和 NameError 自動修正 diagram.py 原始碼。

    策略：
      1. ImportError → 主動掃描所有 diagrams.azure.* import，一次性修正所有錯誤名稱
      2. NameError → 搜尋未定義名稱，補上 import 或註解掉

    Returns:
        (fixed_code, fix_log_lines)
    """
    log: list[str] = []
    fixed = code

    # Phase 1: Handle ImportError (proactive full scan of all import lines)
    import_errors = _parse_import_errors(stderr)
    if import_errors:
        log.append(f"[AutoFix] Detected {len(import_errors)} import error(s), running proactive full scan…")
        fixed, scan_log = _proactive_fix_all_imports(fixed)
        log.extend(scan_log)

    # Phase 2: Handle NameError (undefined names in code body)
    name_errors = _parse_name_errors(stderr)
    if name_errors:
        log.append(f"[AutoFix] Detected {len(name_errors)} name error(s): {name_errors}")
        fixed, name_log = _fix_undefined_names(fixed, name_errors)
        log.extend(name_log)

    # Phase 3: Proactive body scan — find any remaining undefined diagrams-like names
    fixed, body_log = _proactive_fix_undefined_body_names(fixed)
    log.extend(body_log)

    if not import_errors and not name_errors:
        log.append("[AutoFix] No ImportError or NameError patterns found in stderr.")

    return fixed, log


def _proactive_fix_undefined_body_names(code: str) -> tuple[str, list[str]]:
    """
    主動掃描 code body 中使用如 `var = SomeName(...)` 的 PascalCase 呼叫，
    但沒有被任何 import 涵蓋的名稱。嘗試自動補上 import 或註解掉。

    為避免誤判字串內的文字，先移除所有 Python 字串內容再掃描。
    """
    log: list[str] = []

    # Collect all imported names (from any import line)
    imported_names: set[str] = set()
    for m in re.finditer(r'^(?:from\s+\S+\s+import\s+)(.*)', code, re.MULTILINE):
        for name in m.group(1).split(","):
            name = name.strip()
            if name:
                imported_names.add(name)

    # Also add Python builtins and common diagrams names that are always valid
    always_valid = {"Diagram", "Cluster", "Edge", "Node", "True", "False", "None",
                    "Exception", "TypeError", "ValueError", "KeyError", "Path", "Print"}
    imported_names.update(always_valid)

    # Strip all string contents to avoid false positives from names inside "..."
    # Replace string bodies with placeholder, keeping quotes to preserve structure
    code_stripped = re.sub(r'"""[\s\S]*?"""', '"""x"""', code)
    code_stripped = re.sub(r"'''[\s\S]*?'''", "'''x'''", code_stripped)
    code_stripped = re.sub(r'f"[^"\\]*(?:\\.[^"\\]*)*"', '"x"', code_stripped)
    code_stripped = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '"x"', code_stripped)
    code_stripped = re.sub(r"f'[^'\\]*(?:\\.[^'\\]*)*'", "'x'", code_stripped)
    code_stripped = re.sub(r"'[^'\\]*(?:\\.[^'\\]*)*'", "'x'", code_stripped)

    # Find PascalCase function calls in stripped code body (outside import lines)
    # Pattern: standalone PascalCase name used as constructor, e.g., WAF("...")
    body_names: set[str] = set()
    for m in re.finditer(r'\b([A-Z][A-Za-z0-9]+)\s*\(', code_stripped):
        name = m.group(1)
        if name not in imported_names:
            body_names.add(name)

    if not body_names:
        return code, log

    log.append(f"[AutoFix] Proactive body scan found {len(body_names)} unimported name(s): {sorted(body_names)}")

    undefined_to_fix = list(body_names)
    fixed, fix_log = _fix_undefined_names(code, undefined_to_fix)
    log.extend(fix_log)

    return fixed, log


def _proactive_fix_all_imports(code: str) -> tuple[str, list[str]]:
    """
    主動掃描 code 中所有 `from diagrams.azure.xxx import A, B, C` 行，
    逐一驗證每個 class 名稱是否真實存在於對應 module，若不存在則嘗試修正。

    一次處理所有 import 問題，避免逐次 retry。
    """
    registry, class_to_mods = _build_class_registry()
    log: list[str] = []

    lines = code.split("\n")
    new_lines: list[str] = []
    extra_imports: dict[str, list[str]] = {}  # new_module → [class_name, ...]
    removed_names: list[str] = []
    rename_map: dict[str, str] = {}  # old_name → new_name (for body replacements)

    for line in lines:
        stripped = line.strip()
        match = re.match(r'^(\s*from\s+(diagrams\.azure\.\w+)\s+import\s+)(.*)', line)
        if not match:
            new_lines.append(line)
            continue

        prefix = match.group(1)
        module_path = match.group(2)
        names_str = match.group(3)
        names = [n.strip() for n in names_str.split(",") if n.strip()]
        available = set(registry.get(module_path, []))

        fixed_names: list[str] = []
        for name in names:
            if name in available:
                fixed_names.append(name)
                continue

            # Not available — try to find a replacement
            replacement, new_module = _find_best_match(name, module_path)

            if replacement and new_module == module_path:
                log.append(f"[AutoFix] {module_path}.{name} → {replacement}")
                fixed_names.append(replacement)
                if name != replacement:
                    rename_map[name] = replacement
            elif replacement and new_module != module_path:
                log.append(f"[AutoFix] {module_path}.{name} → {new_module}.{replacement}")
                extra_imports.setdefault(new_module, []).append(replacement)
                if name != replacement:
                    rename_map[name] = replacement
                # Don't add to fixed_names (moved to different module)
            else:
                log.append(f"[AutoFix] {module_path}.{name} → REMOVED (no match)")
                removed_names.append(name)
                # Don't add to fixed_names

        if fixed_names:
            # Deduplicate
            seen: set[str] = set()
            unique: list[str] = []
            for n in fixed_names:
                if n not in seen:
                    seen.add(n)
                    unique.append(n)
            new_lines.append(f"{prefix}{', '.join(unique)}")
        # else: entire import line removed

    # Add extra imports for re-mapped classes
    # Find the insertion point: after the last diagrams import
    insert_idx = 0
    for i, line in enumerate(new_lines):
        if line.strip().startswith("from diagrams"):
            insert_idx = i + 1

    for new_module, class_names in extra_imports.items():
        unique_classes = list(dict.fromkeys(class_names))
        import_line = f"from {new_module} import {', '.join(unique_classes)}"
        new_lines.insert(insert_idx, import_line)
        insert_idx += 1
        log.append(f"[AutoFix] Added import: {import_line}")

    fixed = "\n".join(new_lines)

    # Apply renames in the code body (only outside string literals)
    for old_name, new_name in rename_map.items():
        fixed = _rename_outside_strings(fixed, old_name, new_name)

    # Comment out references to removed names
    if removed_names:
        fixed = _comment_out_references(fixed, removed_names)
        log.append(f"[AutoFix] Commented out references to: {removed_names}")

    return fixed, log


def _remove_name_from_import(code: str, name: str, module_path: str) -> str:
    """
    從 `from module_path import A, B, C` 行中移除指定的 name。
    如果移除後 import 為空，則刪除整行。
    """
    lines = code.split("\n")
    new_lines = []

    for line in lines:
        stripped = line.strip()
        # Match: from <module_path> import ...
        if stripped.startswith(f"from {module_path} import"):
            # Extract the import names part
            match = re.match(rf"^(\s*from\s+{re.escape(module_path)}\s+import\s+)(.*)", line)
            if match:
                prefix = match.group(1)
                names_str = match.group(2)
                names = [n.strip() for n in names_str.split(",")]
                names = [n for n in names if n and n != name]
                if names:
                    new_lines.append(f"{prefix}{', '.join(names)}")
                # else: empty import → skip this line entirely
                continue
        new_lines.append(line)

    return "\n".join(new_lines)


def _comment_out_references(code: str, removed_names: list[str]) -> str:
    """
    將程式碼中使用已移除 class 名稱的行註解掉（保留原始碼可讀性）。
    只處理 import 區塊以外的程式碼行。
    """
    lines = code.split("\n")
    new_lines = []
    in_import_section = True

    for line in lines:
        stripped = line.strip()
        # Detect end of import section
        if in_import_section:
            if stripped and not stripped.startswith(("from ", "import ", "#", "")):
                in_import_section = False

        if not in_import_section and not stripped.startswith("#"):
            # Check if any removed name is used as a function call (constructor)
            # e.g., `fw_policy = FirewallPolicies("...")`
            for name in removed_names:
                if re.search(rf'\b{re.escape(name)}\b', line):
                    # Comment out the line and any chain referencing the variable
                    var_match = re.match(r'^(\s*)(\w+)\s*=\s*' + re.escape(name), line)
                    if var_match:
                        indent = var_match.group(1)
                        var_name = var_match.group(2)
                        new_lines.append(f"{indent}# [AutoFix removed] {stripped}")
                        removed_names.append(var_name)  # Also remove references to this variable
                    else:
                        # Line references a removed variable in an expression
                        indent = re.match(r'^(\s*)', line).group(1)
                        # Try to remove the name from a list expression like [a, B, c]
                        cleaned = _remove_name_from_expression(line, name)
                        if cleaned != line:
                            new_lines.append(cleaned)
                        else:
                            new_lines.append(f"{indent}# [AutoFix removed] {stripped}")
                    break
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    return "\n".join(new_lines)


def _remove_name_from_expression(line: str, name: str) -> str:
    """
    嘗試從 Python 列表/連接表達式中移除某個名稱。
    例如：`rg >> [a, bad_name, c]` → `rg >> [a, c]`
    """
    # Remove from list literals: [a, name, b] → [a, b]
    # Pattern: name preceded/followed by comma in a list context
    cleaned = re.sub(rf',\s*{re.escape(name)}\b', '', line)
    cleaned = re.sub(rf'\b{re.escape(name)}\s*,\s*', '', cleaned)
    return cleaned


def _deduplicate_imports(code: str) -> str:
    """
    移除每個 import 行中重複的 class 名稱。
    例如：`from x import A, B, A` → `from x import A, B`
    """
    lines = code.split("\n")
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("from ") and " import " in stripped:
            match = re.match(r'^(\s*from\s+\S+\s+import\s+)(.*)', line)
            if match:
                prefix = match.group(1)
                names_str = match.group(2)
                names = [n.strip() for n in names_str.split(",") if n.strip()]
                # Deduplicate while preserving order
                seen: set[str] = set()
                unique: list[str] = []
                for n in names:
                    if n not in seen:
                        seen.add(n)
                        unique.append(n)
                if unique:
                    new_lines.append(f"{prefix}{', '.join(unique)}")
                # else: skip empty import line
                continue
        new_lines.append(line)
    return "\n".join(new_lines)


# ---------------------------------------------------------------------------
# Core: 本地渲染 diagram.py → PNG/SVG
# ---------------------------------------------------------------------------
async def render_diagram_locally(
    diagram_py: str,
    output_dir: Path,
    *,
    timeout: int | None = None,
) -> DiagramOutput:
    """
    在本地以 subprocess 執行 diagram.py，產生架構圖。

    Args:
        diagram_py: diagram.py 的 Python 原始碼
        output_dir: 輸出目錄（diagram.py 將寫入此處並在此執行）
        timeout: subprocess 逾時秒數（預設使用 RENDER_TIMEOUT）

    Returns:
        DiagramOutput — 包含圖片 bytes + render log
    """
    if timeout is None:
        timeout = RENDER_TIMEOUT

    log_lines: list[str] = []
    logger.info("[Render] render_diagram_locally() 開始 (timeout=%ss, output_dir=%s)", timeout, output_dir)
    print(f"  [Render] 開始本地渲染 diagram.py (timeout={timeout}s) …", flush=True)

    # ----- Pre-flight checks -----
    gv_ok, gv_msg = _check_graphviz()
    log_lines.append(f"[Pre-flight] {gv_msg}")
    if not gv_ok:
        log_lines.append("[FAILED] graphviz not available — cannot render diagram.")
        return DiagramOutput(
            diagram_py=diagram_py,
            diagram_image=b"",
            render_log="\n".join(log_lines),
            status=StepStatus.FAILED,
            error=gv_msg,
        )

    pkg_ok, pkg_msg = _check_diagrams_package()
    log_lines.append(f"[Pre-flight] {pkg_msg}")
    if not pkg_ok:
        log_lines.append("[FAILED] diagrams package not available — cannot render diagram.")
        return DiagramOutput(
            diagram_py=diagram_py,
            diagram_image=b"",
            render_log="\n".join(log_lines),
            status=StepStatus.FAILED,
            error=pkg_msg,
        )

    # ----- Ensure output dir exists -----
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ----- Write diagram.py -----
    diagram_path = output_dir / "diagram.py"
    current_code = diagram_py
    diagram_path.write_text(current_code, encoding="utf-8")
    log_lines.append(f"[Render] Wrote diagram.py → {diagram_path}")

    # ----- Snapshot existing image files (to detect new ones) -----
    image_exts = {".png", ".svg", ".jpg", ".jpeg", ".pdf"}
    existing_images = {
        p.name for p in output_dir.iterdir()
        if p.is_file() and p.suffix.lower() in image_exts
    }

    # ----- Execute diagram.py via subprocess (with auto-fix retry) -----
    python_exe = sys.executable  # 使用當前 Python 直譯器
    last_stderr_text = ""

    for attempt in range(1, MAX_FIX_RETRIES + 1):
        print(f"  [Render] Attempt {attempt}/{MAX_FIX_RETRIES}: 執行 diagram.py …", flush=True)
        log_lines.append(
            f"[Render] Attempt {attempt}/{MAX_FIX_RETRIES}: "
            f"{python_exe} diagram.py (cwd={output_dir}, timeout={timeout}s)"
        )

        try:
            # ---- Synchronous subprocess in a thread ---------------------
            # asyncio.create_subprocess_exec + proc.communicate() hangs
            # on Python 3.12 / WSL2 when graphviz `dot` (a grand-child)
            # inherits the pipes: even after SIGKILL the asyncio transport
            # never detects EOF.  Using subprocess.Popen in a thread with
            # proc.communicate(timeout=...) gives us reliable, OS-level
            # timeout semantics.
            import subprocess as _sp

            def _run_sync() -> tuple[int, bytes, bytes, bool]:
                """Run diagram.py synchronously; returns (returncode, stdout, stderr, timed_out)."""
                proc = _sp.Popen(
                    [python_exe, "diagram.py"],
                    cwd=str(output_dir),
                    stdout=_sp.PIPE,
                    stderr=_sp.PIPE,
                    start_new_session=True,
                )
                print(f"  [Render] Subprocess PID={proc.pid} started", flush=True)

                # heartbeat thread — prints every 15 s
                import threading
                _stop_heartbeat = threading.Event()

                def _heartbeat() -> None:
                    elapsed = 0
                    while not _stop_heartbeat.wait(1.0):
                        elapsed += 1
                        if elapsed % 15 == 0:
                            print(
                                f"  [Render] ⏳ Still running… ({elapsed}/{timeout}s)",
                                flush=True,
                            )

                hb = threading.Thread(target=_heartbeat, daemon=True)
                hb.start()

                timed_out = False
                try:
                    out, err = proc.communicate(timeout=timeout)
                except _sp.TimeoutExpired:
                    timed_out = True
                    print(
                        f"  [Render] ⏰ Timeout ({timeout}s)! "
                        f"Killing process group PID={proc.pid} …",
                        flush=True,
                    )
                    logger.warning(
                        "[Render] Timeout (%ss) — killing process group PID=%s",
                        timeout, proc.pid,
                    )
                    # Kill entire process group (python + dot + any child)
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                    out, err = b"", b""
                    try:
                        proc.wait(timeout=5)
                    except _sp.TimeoutExpired:
                        pass
                finally:
                    _stop_heartbeat.set()
                    hb.join(timeout=2)

                rc = proc.returncode if proc.returncode is not None else -9
                return rc, out, err, timed_out

            loop = asyncio.get_running_loop()
            returncode, stdout, stderr, timed_out = await loop.run_in_executor(
                None, _run_sync,
            )

            if timed_out:
                log_lines.append(
                    f"[FAILED] Execution timed out after {timeout}s"
                )
                print(
                    f"  [Render] ❌ Timeout after {timeout}s — "
                    f"diagram.py did not complete",
                    flush=True,
                )
                return DiagramOutput(
                    diagram_py=current_code,
                    diagram_image=b"",
                    render_log="\n".join(log_lines),
                    status=StepStatus.FAILED,
                    error=f"diagram.py execution timed out after {timeout}s",
                )
        except Exception as exc:
            log_lines.append(f"[FAILED] Subprocess error: {exc}")
            return DiagramOutput(
                diagram_py=current_code,
                diagram_image=b"",
                render_log="\n".join(log_lines),
                status=StepStatus.FAILED,
                error=f"Failed to execute diagram.py: {exc}",
            )

        # ----- Capture stdout/stderr -----
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if stdout_text:
            log_lines.append(f"[stdout] {stdout_text}")
        if stderr_text:
            log_lines.append(f"[stderr] {stderr_text}")

        log_lines.append(f"[Render] Process exit code: {returncode}")
        last_stderr_text = stderr_text

        if returncode == 0:
            # 成功！跳出 retry 迴圈
            logger.info("[Render] Attempt %d — subprocess exited 0 (success)", attempt)
            print(f"  [Render] Attempt {attempt}: subprocess exit 0 ✓", flush=True)
            break

        # ----- Auto-fix: 嘗試修正 ImportError / NameError / AttributeError -----
        _fixable_errors = ("ImportError", "ModuleNotFoundError", "NameError", "AttributeError")
        if attempt < MAX_FIX_RETRIES and any(e in stderr_text for e in _fixable_errors):
            log_lines.append(f"[AutoFix] Detected fixable errors — attempting auto-fix (attempt {attempt})…")
            logger.info("[AutoFix] Attempt %d — detected fixable errors, running auto-fix…", attempt)
            print(f"  [AutoFix] Attempt {attempt}: 偵測到可修復錯誤，自動修正中 …", flush=True)
            fixed_code, fix_log = _auto_fix_diagram_code(current_code, stderr_text)
            log_lines.extend(fix_log)

            if fixed_code == current_code:
                log_lines.append("[AutoFix] No changes made — cannot fix further.")
                break

            current_code = fixed_code
            diagram_path.write_text(current_code, encoding="utf-8")
            log_lines.append(f"[AutoFix] Wrote fixed diagram.py (attempt {attempt + 1} will retry)")
            continue

        # Non-ImportError failure or exhausted retries
        print(f"  [Render] Attempt {attempt}: 非可修復錯誤或已用盡重試 (exit code {returncode})", flush=True)
        break

    if returncode != 0:
        log_lines.append(f"[FAILED] diagram.py exited with code {returncode} after {attempt} attempt(s)")
        print(f"  [Render] ❌ FAILED: exit code {returncode} — 將觸發 agent-regen", flush=True)
        return DiagramOutput(
            diagram_py=current_code,
            diagram_image=b"",
            render_log="\n".join(log_lines),
            status=StepStatus.FAILED,
            error=f"diagram.py exited with code {returncode}: {last_stderr_text or stdout_text}",
        )

    # ----- Find newly generated image files -----
    new_images: list[Path] = []
    for p in sorted(output_dir.iterdir()):
        if (
            p.is_file()
            and p.suffix.lower() in image_exts
            and p.name not in existing_images
        ):
            new_images.append(p)

    # 也檢查 diagrams 預設命名模式（小寫化的 Diagram title + .png）
    # diagrams library 產出的檔名通常是 Diagram title 轉換而成
    if not new_images:
        # 回退：掃描所有 .png 檔案（可能是舊名被覆寫）
        all_images = sorted(
            (p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() in image_exts),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if all_images:
            new_images = [all_images[0]]  # 取最新的

    if not new_images:
        log_lines.append("[WARNING] diagram.py executed successfully but no image files found.")
        log_lines.append("[FAILED] diagram.py exited 0 but produced no image — treating as failure.")
        logger.warning("[Render] Exit code 0 but no image produced — returning FAILED to trigger agent-regen.")
        print("  [Render] ❌ Exit 0 但無圖片產出 — 視為 FAILED，將觸發 agent-regen", flush=True)
        return DiagramOutput(
            diagram_py=current_code,
            diagram_image=b"",
            render_log="\n".join(log_lines),
            status=StepStatus.FAILED,
            error="No image file produced. Check diagram.py parameters (show=False, outformat, filename).",
        )

    # ----- Read the first (primary) image -----
    primary_image = new_images[0]
    image_bytes = primary_image.read_bytes()
    image_ext = primary_image.suffix.lstrip(".").lower()

    log_lines.append(f"[SUCCESS] Rendered diagram: {primary_image.name} ({len(image_bytes)} bytes)")
    print(f"  [Render] ✅ SUCCESS: {primary_image.name} ({len(image_bytes)} bytes)", flush=True)
    if len(new_images) > 1:
        log_lines.append(f"[INFO] Additional images: {[p.name for p in new_images[1:]]}")

    # ----- 將圖片重新命名/複製為標準名 diagram.png -----
    standard_name = output_dir / f"diagram.{image_ext}"
    if primary_image != standard_name:
        shutil.copy2(primary_image, standard_name)
        log_lines.append(f"[INFO] Copied {primary_image.name} → {standard_name.name}")

    return DiagramOutput(
        diagram_py=current_code,
        diagram_image=image_bytes,
        diagram_image_ext=image_ext,
        render_log="\n".join(log_lines),
        status=StepStatus.SUCCESS,
    )


# ---------------------------------------------------------------------------
# DiagramRendererAgent — Microsoft Agent Framework BaseAgent
# ---------------------------------------------------------------------------
try:
    from agent_framework import (
        BaseAgent,
        AgentRunResponse,
        AgentRunResponseUpdate,
        AgentThread,
        ChatMessage,
    )

    class DiagramRendererAgent(BaseAgent):
        """
        本地 Diagram Renderer Agent — 基於 Microsoft Agent Framework。

        接收 diagram.py 原始碼（作為 user message），在本地執行並回傳渲染結果。
        適用於獨立部署或透過 AgentProtocol 呼叫。
        """

        def __init__(self, name: str = "diagram-renderer", output_dir: Path | None = None, **kwargs):
            super().__init__(name=name, **kwargs)
            self._output_dir = output_dir or Path(os.getenv("OUTPUT_DIR", "./out"))

        async def run(
            self,
            messages: ChatMessage | list[ChatMessage] | None = None,
            *,
            thread: AgentThread | None = None,
            **kwargs,
        ) -> AgentRunResponse:
            """Non-streaming: 接收 diagram.py 原始碼，執行渲染，回傳結果。"""
            diagram_py = self._extract_diagram_code(messages)

            if not diagram_py.strip():
                return AgentRunResponse(
                    messages=[{"role": "assistant", "content": "❌ No diagram.py code provided."}],
                )

            result = await render_diagram_locally(diagram_py, self._output_dir)

            if result.status == StepStatus.SUCCESS and result.diagram_image:
                response_text = (
                    f"✅ Diagram rendered successfully!\n\n"
                    f"**Output**: diagram.{result.diagram_image_ext} "
                    f"({len(result.diagram_image)} bytes)\n\n"
                    f"**Render Log**:\n```\n{result.render_log}\n```"
                )
            elif result.status == StepStatus.SUCCESS:
                response_text = (
                    f"⚠️ Diagram executed but no image produced.\n\n"
                    f"**Render Log**:\n```\n{result.render_log}\n```"
                )
            else:
                response_text = (
                    f"❌ Diagram render failed: {result.error}\n\n"
                    f"**Render Log**:\n```\n{result.render_log}\n```"
                )

            return AgentRunResponse(
                messages=[{"role": "assistant", "content": response_text}],
            )

        def run_stream(
            self,
            messages: ChatMessage | list[ChatMessage] | None = None,
            *,
            thread: AgentThread | None = None,
            **kwargs,
        ):
            """Streaming: yield 渲染進度。"""
            diagram_py = self._extract_diagram_code(messages)

            async def _stream():
                yield AgentRunResponseUpdate(text="🖼️ Diagram Renderer Agent 啟動...\n")

                gv_ok, gv_msg = _check_graphviz()
                yield AgentRunResponseUpdate(text=f"  Pre-flight: {gv_msg}\n")

                if not gv_ok:
                    yield AgentRunResponseUpdate(text=f"❌ {gv_msg}\n")
                    return

                pkg_ok, pkg_msg = _check_diagrams_package()
                yield AgentRunResponseUpdate(text=f"  Pre-flight: {pkg_msg}\n")

                if not pkg_ok:
                    yield AgentRunResponseUpdate(text=f"❌ {pkg_msg}\n")
                    return

                yield AgentRunResponseUpdate(text="🔄 Executing diagram.py...\n")
                result = await render_diagram_locally(diagram_py, self._output_dir)

                if result.status == StepStatus.SUCCESS and result.diagram_image:
                    yield AgentRunResponseUpdate(
                        text=f"✅ Rendered: diagram.{result.diagram_image_ext} "
                             f"({len(result.diagram_image)} bytes)\n"
                    )
                else:
                    yield AgentRunResponseUpdate(
                        text=f"{'⚠️' if result.status == StepStatus.SUCCESS else '❌'} "
                             f"{result.error or 'Completed'}\n"
                    )

                yield AgentRunResponseUpdate(text=f"\nRender Log:\n{result.render_log}\n")

            return _stream()

        def _extract_diagram_code(self, messages: Any) -> str:
            """從 messages 中擷取 diagram.py 原始碼。"""
            if messages is None:
                return ""
            if isinstance(messages, str):
                return messages
            if isinstance(messages, list):
                for msg in reversed(messages):
                    if hasattr(msg, "role") and str(msg.role) == "user":
                        if hasattr(msg, "content"):
                            return str(msg.content)
                    elif isinstance(msg, dict) and msg.get("role") == "user":
                        return str(msg.get("content", ""))
                return "\n".join(
                    str(m.content) if hasattr(m, "content") else str(m)
                    for m in messages
                )
            if hasattr(messages, "content"):
                return str(messages.content)
            return str(messages)

except ImportError:
    # agent_framework 不可用時，仍可使用 render_diagram_locally 函式
    logger.warning(
        "agent_framework not available — DiagramRendererAgent class not defined. "
        "render_diagram_locally() is still usable as a standalone function."
    )
