from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATED_FILES = (
    "desktop_runtime.py",
    "collector.py",
    "geo_updater.py",
    "core_runner.py",
    "alerts.py",
    "manager.py",
    "clash_formatter.py",
    "tester.py",
    "exit_quality_service.py",
    "parser.py",
    "main.py",
    "subscription.py",
)
HIGH_LEVEL_METHODS = {"warning", "error", "critical", "exception"}
HIGH_LEVEL_NAMES = {"WARNING", "WARN", "ERROR", "CRITICAL", "FATAL", "EXCEPTION"}


def _is_logger_expression(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "logger"
    if isinstance(node, ast.Attribute):
        return node.attr == "logger"
    return False


def _literal_level(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
        return str(node.value).strip().upper()
    if isinstance(node, ast.Attribute):
        return node.attr.strip().upper()
    return "DYNAMIC"


def _has_explicit_raw_only(call: ast.Call, source: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg != "extra":
            continue
        extra_source = ast.get_source_segment(source, keyword.value) or ""
        return "RAW_ONLY_LOG_EXTRA" in extra_source or "raw_only" in extra_source
    return False


def _bare_high_level_calls(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    failures: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and _is_logger_expression(node.value)
            and node.attr in HIGH_LEVEL_METHODS
        ):
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                failures.append(f"{path.name}:{node.lineno} logger.{node.attr} reference")
                continue
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute) and _is_logger_expression(function.value):
            if function.attr in HIGH_LEVEL_METHODS and not _has_explicit_raw_only(node, source):
                failures.append(f"{path.name}:{node.lineno} logger.{function.attr}")
                continue
            if function.attr == "log" and node.args:
                level = _literal_level(node.args[0])
                if level in HIGH_LEVEL_NAMES | {"DYNAMIC"} and not _has_explicit_raw_only(node, source):
                    failures.append(f"{path.name}:{node.lineno} logger.log({level})")
                    continue
        if isinstance(function, ast.Name) and function.id == "try_log_nowait":
            if len(node.args) < 2:
                failures.append(f"{path.name}:{node.lineno} try_log_nowait(unknown level)")
                continue
            level = _literal_level(node.args[1])
            if level in HIGH_LEVEL_NAMES | {"DYNAMIC"} and not _has_explicit_raw_only(node, source):
                failures.append(f"{path.name}:{node.lineno} try_log_nowait({level})")
    return sorted(failures)


class StructuredEventCallSiteGateTests(unittest.TestCase):
    def test_gated_first_party_files_have_no_bare_high_level_logs(self) -> None:
        failures: list[str] = []
        for relative_path in GATED_FILES:
            failures.extend(_bare_high_level_calls(PROJECT_ROOT / relative_path))
        self.assertEqual(
            failures,
            [],
            "High-level first-party logs must use log_event(), "
            "log_raw_diagnostic(), or an explicit raw-only extra:\n"
            + "\n".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
