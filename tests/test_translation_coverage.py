"""Static guard: every source UI string must have complete locale coverage.

Goal: zero fallback. If a developer adds a new ``self._text("…")`` literal
without inserting it into ``TRANSLATIONS_EN``, this test fails and CI/local
test runs flag the gap before it reaches a user.

Strategy:
- Collect the label tables and Chinese literals of the remaining backend
  modules whose strings reach the UI (log events, DNS fields, Tier1 rows,
  alerts, onboarding, import views).
- Diff against ``TRANSLATIONS_EN`` keys and locale JSON files.
- Allow a small allowlist for strings that are deliberately language-neutral
  (e.g. ``"--"``, ``""``, pure punctuation, brand names).

Every advertised locale must include the same source keys and preserve format
placeholders such as ``{fields}``.
"""
from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from dns_ui import DNS_UI_FIELD_LABELS
from log_events import LOG_SOURCE_LABELS, RUNTIME_NOTICE_PATTERNS, RUNTIME_NOTICE_REPLACEMENTS
from tier1_rows import TIER1_ROW_TEXTS
from ui_i18n import is_translation_neutral, localized_territory_name
from overview_validation_view import OVERVIEW_VALIDATION_TEXTS


PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMPORT_MODE_VIEW_PATH = PROJECT_ROOT / "import_mode_view.py"
IMPORT_PROTOCOL_VIEW_PATH = PROJECT_ROOT / "import_protocol_view.py"
ALERT_CONFIG_PATH = PROJECT_ROOT / "alert_config.py"
NOTIFICATION_ONBOARDING_PATH = PROJECT_ROOT / "notification_onboarding.py"
I18N_PATH = PROJECT_ROOT / "ui_i18n.py"
LOCALE_DIR = PROJECT_ROOT / "locales"
EXPECTED_LANGUAGES = {"en_US", "zh_CN", "ja_JP", "ko_KR", "fr_FR", "de_DE", "es_ES", "pt_BR", "ru_RU"}
LOG_CENTER_REDUCTION_KEYS = frozenset(
    {
        "连接失败",
        "连接超时",
        "单次连接未完成；目标和失败原因保留在原始日志中。",
        "日志源状态",
        "日志流正常",
        "{count} 个日志源异常",
        "冻结",
        "恢复 · {count}",
        "清除筛选",
        "查看当前记录",
        "日志记录",
        "诊断信息",
        "自动换行",
        "复制原文",
        "复制完整 JSON",
        "采集 Mihomo DEBUG",
        "仅影响 Mihomo，从现在开始采集",
        "序号",
        "完整内容",
        "技术信息",
        "事件时间",
        "接收时间",
        "事件键",
        "元数据",
        "复制技术信息",
    }
)
GATEWAY_MICRO_ROUTE_KEYS = frozenset(
    {
        "暂无",
        "未选择",
        "选择",
        "缺失",
    }
)
GATEWAY_SELECTOR_KEYS = frozenset(
    {
        "当前",
        "管理",
    }
)
ROUTING_ORDER_KEYS = frozenset({"上移", "下移"})
EXIT_DYNAMIC_I18N_KEYS = frozenset(
    {
        *OVERVIEW_VALIDATION_TEXTS,
        "检查依据",
        "校验当前出口的身份、网络与信誉证据",
        "仅代表已查来源；未知字段不会按未命中处理",
        "正在校验身份与信誉来源...",
        "骨干网络",
        "商业网络",
        "托管网络",
        "移动网络",
        "住宅网络",
        "运营商网络",
    }
)
SUBSCRIPTION_SNAPSHOT_I18N_KEYS = frozenset(
    {
        "本次",
        "上次",
        "待更新",
        "待复检",
        "未加载",
        "{count} 个节点",
        "已更新",
        "上次可用",
        "上次不可用",
        "上次缓慢",
        "订阅已更新并应用：{healthy}/{total} 可用。",
        "订阅已更新并保存：{healthy}/{total} 可用；将在下次启动时使用。",
        "订阅已更新并保存：{healthy}/{total} 可用；未能应用到当前服务：{error}",
        "本次没有获得可保存的订阅结果，已保留上次订阅缓存。",
        "核心重载未确认",
    }
)
NODE_DIAGNOSTIC_I18N_KEYS = frozenset(
    {
        "连通诊断",
        "连通诊断中...",
        "检测此节点",
        "直连检测",
        "链路检测",
        "代理连通 · {delay} ms",
        "代理检测失败：{reason}",
        "代理连接超时。",
        "代理连接失败。",
        "当前协议不支持连通诊断。",
        "检测组件不可用。",
        "检测已取消。",
        "临时检测组件启动失败。",
        "临时检测组件清理失败。",
        "缺少可用入口，无法进行链路检测。",
        "节点已变化，请重新检测。",
    }
)
IMPORT_EDITOR_KEYS = frozenset(
    {
        "粘贴导入",
        "手动创建",
        "订阅导入",
        "节点信息",
        "节点协议",
        "选择协议…",
        "选择协议",
        "必填",
        "尚未添加协议",
        "尚未添加协议。请先选择并添加一个协议。",
        "尚未添加协议。请先选择并添加协议。",
        "请先选择并添加一个协议。",
        "先添加协议，再在下方维护该协议的端口、密钥与传输细节。",
        "协议回退顺序",
        "协议参数",
        "连接顺序：{order}",
        "连接顺序：尚未添加协议",
        "单协议节点必须选择一个协议。",
        "多协议节点至少需要两个协议。",
        "Argo XHTTP 只能作为单协议节点导入。",
        "移除该协议",
        "请选择一个协议以填写连接参数。",
        "请选择协议",
        "还需添加 {count} 个协议。",
        "身份",
        "传输",
        "安全",
        "Reality 参数",
        "VMess 高级",
        "TLS 高级",
        "端口与认证",
        "加密与认证",
        "连接",
        "连接与认证",
        "身份认证",
        "混淆",
        "QUIC 调优",
        "密钥",
        "隧道地址",
        "路由",
        "高级",
        "本地监听",
        "XHTTP 上游",
        "端点",
        "拖动以调整连接顺序",
        "展开高级设置",
        "收起高级设置",
    }
)

# Language-neutral strings that don't need translation (placeholders, brand
# tokens, format specifiers, examples).
ALLOWLIST = {
    "",
    "--",
    "-",
    " ",
    "DEFAULT",
    "Tesseract",
    "GitHub",
    "Telegram",
    "Mihomo",
    "WireGuard",
    "AmneziaWG",
    "TUIC",
    "VLESS",
    "VMess",
    "Trojan",
    "Hysteria2",
    "AnyTLS",
    "SOCKS5",
    "HTTP",
    "HTTPS",
    "AI-FIXED-IP",
    "PROXY-FALLBACK",
    "FINAL-EXIT",
    "DIRECT",
    "REJECT",
    "GLOBAL",
    "GENERAL",
    "一",
    "鿿",
}

# Argument-format placeholders look like "{x} {y}" — they shouldn't fail the
# guard if the surrounding template is in TRANSLATIONS_EN. So we always keep
# format-only strings in the allowlist regardless of declared content.
PLACEHOLDER_RE = re.compile(r"^\s*\{[^{}]+\}(\s*\{[^{}]+\})*\s*$")
FORMAT_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")
LANGUAGE_PREFIX_RE = re.compile(r"^(?:DE|ES|FR|JA|KO|PT|RU):\s+")


def _collect_chinese_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if any("\u4e00" <= ch <= "\u9fff" for ch in node.value):
            literals.add(node.value)
    return literals


def _log_event_translation_literals() -> set[str]:
    return (
        set(LOG_SOURCE_LABELS.values())
        | {template for _pattern, template in RUNTIME_NOTICE_PATTERNS}
        | {target for _source, target in RUNTIME_NOTICE_REPLACEMENTS}
    )


def _dns_ui_translation_literals() -> set[str]:
    return set(DNS_UI_FIELD_LABELS.values())


def _tier1_row_translation_literals() -> set[str]:
    return set(TIER1_ROW_TEXTS)


def _load_translations_en() -> dict[str, str]:
    source = I18N_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TRANSLATIONS_EN":
                    if isinstance(node.value, ast.Dict):
                        result: dict[str, str] = {}
                        for key, value in zip(node.value.keys, node.value.values):
                            if (
                                isinstance(key, ast.Constant)
                                and isinstance(key.value, str)
                                and isinstance(value, ast.Constant)
                                and isinstance(value.value, str)
                            ):
                                result[key.value] = value.value
                        return result
    return {}


def _load_constant_string(name: str) -> str:
    source = I18N_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return ""


def _load_supported_languages() -> set[str]:
    source = I18N_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    languages: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SUPPORTED_LANGUAGES":
                    if isinstance(node.value, ast.Dict):
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                languages.add(key.value)
    return languages


class TranslationCoverageTests(unittest.TestCase):
    def test_runtime_brand_tokens_are_translation_neutral(self) -> None:
        self.assertTrue(is_translation_neutral("Tor"))

    def test_every_localized_literal_has_english_mapping(self) -> None:
        literals = _log_event_translation_literals()
        literals.update(_dns_ui_translation_literals())
        literals.update(_tier1_row_translation_literals())
        literals.update(_collect_chinese_literals(ALERT_CONFIG_PATH))
        literals.update(_collect_chinese_literals(NOTIFICATION_ONBOARDING_PATH))
        literals.update(_collect_chinese_literals(IMPORT_MODE_VIEW_PATH))
        literals.update(_collect_chinese_literals(IMPORT_PROTOCOL_VIEW_PATH))
        translations = _load_translations_en()
        missing = []
        for literal in sorted(literals):
            if literal in ALLOWLIST:
                continue
            if PLACEHOLDER_RE.match(literal):
                continue
            if is_translation_neutral(literal):
                continue
            if literal not in translations:
                missing.append(literal)
        self.assertFalse(
            missing,
            "These localized UI literals lack an English translation in TRANSLATIONS_EN:\n  - "
            + "\n  - ".join(missing[:40])
            + ("\n  ... and more" if len(missing) > 40 else ""),
        )

    def test_supported_languages_declares_only_complete_locales(self) -> None:
        languages = _load_supported_languages()
        self.assertEqual(
            languages,
            EXPECTED_LANGUAGES,
            "SUPPORTED_LANGUAGES drifted from the maintained full-locale set; "
            "shipping a partially translated locale guarantees user-visible fallback.",
        )
        self.assertEqual(_load_constant_string("SOURCE_LANGUAGE"), "zh_CN")
        self.assertEqual(_load_constant_string("DEFAULT_LANGUAGE"), "en_US")

        translations = _load_translations_en()
        for language in sorted(languages - {"zh_CN", "en_US"}):
            path = LOCALE_DIR / f"{language}.json"
            self.assertTrue(path.exists(), f"Missing locale file: {path}")
            catalog = json.loads(path.read_text(encoding="utf-8"))
            missing = set(translations) - set(catalog)
            self.assertFalse(missing, f"{language} locale is missing translation keys: {sorted(missing)[:20]}")
            for source, english in translations.items():
                localized = str(catalog.get(source, ""))
                self.assertTrue(localized.strip(), f"{language} has an empty translation for {source!r}")
                self.assertEqual(
                    sorted(FORMAT_PLACEHOLDER_RE.findall(english)),
                    sorted(FORMAT_PLACEHOLDER_RE.findall(localized)),
                    f"{language} changed placeholders for {source!r}",
                )
                if not is_translation_neutral(source):
                    self.assertNotEqual(localized, source, f"{language} fell back to source text for {source!r}")
                if any("\u4e00" <= ch <= "\u9fff" for ch in source):
                    self.assertNotEqual(localized, source, f"{language} fell back to source Chinese for {source!r}")
                    self.assertNotEqual(localized, english, f"{language} fell back to English for {source!r}")
                self.assertIsNone(
                    LANGUAGE_PREFIX_RE.match(localized),
                    f"{language} uses a prefixed placeholder translation for {source!r}: {localized!r}",
                )

    def test_log_center_reduction_strings_have_complete_locale_coverage(self) -> None:
        translations = _load_translations_en()
        self.assertFalse(
            LOG_CENTER_REDUCTION_KEYS - set(translations),
            "The reduced log center is missing English UI translations",
        )
        catalogs = {
            language: json.loads((LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))
            for language in EXPECTED_LANGUAGES - {"zh_CN", "en_US"}
        }
        for source in LOG_CENTER_REDUCTION_KEYS:
            expected_placeholders = sorted(FORMAT_PLACEHOLDER_RE.findall(source))
            self.assertEqual(
                expected_placeholders,
                sorted(FORMAT_PLACEHOLDER_RE.findall(translations[source])),
                f"English changed placeholders for {source!r}",
            )
            for language, catalog in catalogs.items():
                self.assertIn(source, catalog, f"{language} is missing {source!r}")
                localized = str(catalog[source])
                self.assertEqual(
                    expected_placeholders,
                    sorted(FORMAT_PLACEHOLDER_RE.findall(localized)),
                    f"{language} changed placeholders for {source!r}",
                )
                self.assertNotEqual(localized, source, f"{language} fell back to source text for {source!r}")
                self.assertNotEqual(localized, translations[source], f"{language} fell back to English for {source!r}")

    def test_gateway_micro_route_strings_have_plain_utf8_locale_coverage(self) -> None:
        translations = _load_translations_en()
        self.assertFalse(
            GATEWAY_MICRO_ROUTE_KEYS - set(translations),
            "The Gateway micro-route is missing English UI translations",
        )
        catalogs = {}
        for language in EXPECTED_LANGUAGES - {"zh_CN", "en_US"}:
            path = LOCALE_DIR / f"{language}.json"
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{language} locale must not use a UTF-8 BOM")
            catalogs[language] = json.loads(raw.decode("utf-8"))

        for source in GATEWAY_MICRO_ROUTE_KEYS:
            expected_placeholders = sorted(FORMAT_PLACEHOLDER_RE.findall(source))
            self.assertEqual(
                expected_placeholders,
                sorted(FORMAT_PLACEHOLDER_RE.findall(translations[source])),
                f"English changed placeholders for {source!r}",
            )
            for language, catalog in catalogs.items():
                self.assertIn(source, catalog, f"{language} is missing {source!r}")
                localized = str(catalog[source])
                self.assertEqual(
                    expected_placeholders,
                    sorted(FORMAT_PLACEHOLDER_RE.findall(localized)),
                    f"{language} changed placeholders for {source!r}",
                )
                self.assertNotEqual(localized, source, f"{language} fell back to source text for {source!r}")
                self.assertNotEqual(localized, translations[source], f"{language} fell back to English for {source!r}")

    def test_gateway_selector_strings_have_complete_locale_coverage(self) -> None:
        translations = _load_translations_en()
        self.assertFalse(
            GATEWAY_SELECTOR_KEYS - set(translations),
            "The Gateway selector is missing English UI translations",
        )
        for source in GATEWAY_SELECTOR_KEYS:
            self.assertTrue(translations[source].strip(), f"English has an empty translation for {source!r}")
        for language in EXPECTED_LANGUAGES - {"zh_CN", "en_US"}:
            path = LOCALE_DIR / f"{language}.json"
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{language} locale must not use a UTF-8 BOM")
            catalog = json.loads(raw.decode("utf-8"))
            self.assertFalse(GATEWAY_SELECTOR_KEYS - set(catalog), language)
            for source in GATEWAY_SELECTOR_KEYS:
                localized = str(catalog[source])
                self.assertTrue(localized.strip(), f"{language} has an empty translation for {source!r}")
                self.assertNotEqual(localized, source, f"{language} fell back to source text for {source!r}")
                self.assertNotEqual(localized, translations[source], f"{language} fell back to English for {source!r}")

    def test_non_english_locale_samples_do_not_fall_back_to_english(self) -> None:
        translations = _load_translations_en()
        english_recent = translations["最近更新"]
        for language in sorted(EXPECTED_LANGUAGES - {"zh_CN", "en_US"}):
            catalog = json.loads((LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))
            self.assertNotEqual(catalog["最近更新"], english_recent, f"{language} fell back to English")

    def test_routing_order_controls_have_complete_locale_coverage(self) -> None:
        translations = _load_translations_en()
        self.assertEqual(
            {key: translations[key] for key in ROUTING_ORDER_KEYS},
            {"上移": "Move Up", "下移": "Move Down"},
        )
        for language in EXPECTED_LANGUAGES - {"zh_CN", "en_US"}:
            catalog = json.loads(
                (LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8")
            )
            self.assertFalse(ROUTING_ORDER_KEYS - set(catalog), language)
            for source in ROUTING_ORDER_KEYS:
                self.assertNotEqual(catalog[source], source, language)
                self.assertNotEqual(catalog[source], translations[source], language)

    def test_exit_dynamic_labels_have_complete_locale_coverage(self) -> None:
        translations = _load_translations_en()
        self.assertFalse(EXIT_DYNAMIC_I18N_KEYS - set(translations))
        for language in EXPECTED_LANGUAGES - {"zh_CN", "en_US"}:
            catalog = json.loads((LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))
            self.assertFalse(EXIT_DYNAMIC_I18N_KEYS - set(catalog), language)

    def test_subscription_snapshot_labels_have_complete_locale_coverage(self) -> None:
        translations = _load_translations_en()
        self.assertFalse(SUBSCRIPTION_SNAPSHOT_I18N_KEYS - set(translations))
        for source in SUBSCRIPTION_SNAPSHOT_I18N_KEYS:
            expected = sorted(FORMAT_PLACEHOLDER_RE.findall(source))
            self.assertEqual(
                expected,
                sorted(FORMAT_PLACEHOLDER_RE.findall(translations[source])),
                f"English changed placeholders for {source!r}",
            )
        for language in EXPECTED_LANGUAGES - {"zh_CN", "en_US"}:
            catalog = json.loads(
                (LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8")
            )
            self.assertFalse(
                SUBSCRIPTION_SNAPSHOT_I18N_KEYS - set(catalog),
                language,
            )
            for source in SUBSCRIPTION_SNAPSHOT_I18N_KEYS:
                localized = str(catalog[source])
                self.assertEqual(
                    sorted(FORMAT_PLACEHOLDER_RE.findall(source)),
                    sorted(FORMAT_PLACEHOLDER_RE.findall(localized)),
                    f"{language} changed placeholders for {source!r}",
                )
                self.assertNotEqual(localized, source, language)
                self.assertNotEqual(localized, translations[source], language)

    def test_node_diagnostic_labels_have_complete_locale_coverage(self) -> None:
        translations = _load_translations_en()
        self.assertFalse(NODE_DIAGNOSTIC_I18N_KEYS - set(translations))
        for source in NODE_DIAGNOSTIC_I18N_KEYS:
            expected = sorted(FORMAT_PLACEHOLDER_RE.findall(source))
            self.assertEqual(
                expected,
                sorted(FORMAT_PLACEHOLDER_RE.findall(translations[source])),
                f"English changed placeholders for {source!r}",
            )
        for language in EXPECTED_LANGUAGES - {"zh_CN", "en_US"}:
            catalog = json.loads((LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))
            self.assertFalse(NODE_DIAGNOSTIC_I18N_KEYS - set(catalog), language)
            for source in NODE_DIAGNOSTIC_I18N_KEYS:
                localized = str(catalog[source])
                self.assertEqual(
                    sorted(FORMAT_PLACEHOLDER_RE.findall(source)),
                    sorted(FORMAT_PLACEHOLDER_RE.findall(localized)),
                    f"{language} changed placeholders for {source!r}",
                )
                self.assertNotEqual(localized, source, language)
                self.assertNotEqual(localized, translations[source], language)

    def test_unified_import_editor_has_complete_locale_coverage(self) -> None:
        translations = _load_translations_en()
        self.assertFalse(IMPORT_EDITOR_KEYS - set(translations))
        for source in IMPORT_EDITOR_KEYS:
            expected_placeholders = sorted(FORMAT_PLACEHOLDER_RE.findall(source))
            self.assertEqual(
                expected_placeholders,
                sorted(FORMAT_PLACEHOLDER_RE.findall(translations[source])),
                f"English changed placeholders for {source!r}",
            )
        for language in EXPECTED_LANGUAGES - {"zh_CN", "en_US"}:
            path = LOCALE_DIR / f"{language}.json"
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{language} locale must not use a UTF-8 BOM")
            catalog = json.loads(raw.decode("utf-8"))
            self.assertFalse(IMPORT_EDITOR_KEYS - set(catalog), language)
            for source in IMPORT_EDITOR_KEYS:
                localized = str(catalog[source])
                self.assertEqual(
                    sorted(FORMAT_PLACEHOLDER_RE.findall(source)),
                    sorted(FORMAT_PLACEHOLDER_RE.findall(localized)),
                    f"{language} changed placeholders for {source!r}",
                )
                self.assertNotEqual(localized, source, f"{language} fell back to source text for {source!r}")
                self.assertNotEqual(localized, translations[source], f"{language} fell back to English for {source!r}")

    def test_country_names_are_localized_from_country_code_for_every_language(self) -> None:
        expected_japan = {
            "en_US": "Japan",
            "zh_CN": "日本",
            "ja_JP": "日本",
            "ko_KR": "일본",
            "fr_FR": "Japon",
            "de_DE": "Japan",
            "es_ES": "Japón",
            "pt_BR": "Japão",
            "ru_RU": "Япония",
        }
        for language, expected in expected_japan.items():
            with self.subTest(language=language):
                self.assertEqual(localized_territory_name("JP", language), expected)
        self.assertEqual(localized_territory_name("ZZ", "zh_CN"), "")
        self.assertEqual(localized_territory_name("ZZZ", "zh_CN"), "")


if __name__ == "__main__":
    unittest.main()
