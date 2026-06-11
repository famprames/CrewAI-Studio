"""Regression tests for stable tool identifiers and legacy-name fallback.

Covers issues #115 and #116: tool display names were translated (i18n)
but were also used as internal keys into TOOL_CLASSES and persisted in
the database, so translated (or missing) labels crashed load_data()
with a KeyError and broke every tool in non-English locales.

Run from the repository root:
    pytest tests/test_tool_ids.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

# db_utils reads DB_URL at import time — point it at a throwaway sqlite
# file BEFORE anything imports db_utils.
_TMP_DIR = tempfile.mkdtemp(prefix="crewai_studio_tests_")
os.environ["DB_URL"] = f"sqlite:///{_TMP_DIR}/test_crewai.db"

from my_tools import (  # noqa: E402
    TOOL_CLASSES,
    TOOL_I18N_KEYS,
    resolve_tool_id,
)


def load_translations():
    translations = {}
    for lang_file in (APP_DIR / "i18n").glob("*.json"):
        with open(lang_file, encoding="utf-8") as f:
            translations[lang_file.stem] = json.load(f)
    return translations


class TestStableIdentity:
    def test_every_tool_class_has_an_i18n_key(self):
        assert set(TOOL_CLASSES) == set(TOOL_I18N_KEYS)

    def test_tool_instances_use_stable_names(self):
        """tool.name must equal its TOOL_CLASSES key — never a translation."""
        for stable_id, tool_class in TOOL_CLASSES.items():
            tool = tool_class()
            assert tool.name == stable_id

    def test_display_name_is_resolvable_for_every_tool(self):
        translations = load_translations()
        for stable_id, i18n_key in TOOL_I18N_KEYS.items():
            for lang, data in translations.items():
                label = data.get("tool", {}).get(i18n_key)
                assert isinstance(label, str) and label, (
                    f"Missing '{lang}' translation for tool.{i18n_key} "
                    f"({stable_id})"
                )


class TestResolveToolId:
    def test_stable_ids_resolve_to_themselves(self):
        for stable_id in TOOL_CLASSES:
            assert resolve_tool_id(stable_id) == stable_id

    @pytest.mark.parametrize(
        "legacy_name,expected",
        [
            # Issue #116 — English labels that did not match TOOL_CLASSES keys
            ("Code Interpreter", "CustomCodeInterpreterTool"),
            ("DuckDuckGo Search", "DuckDuckGoSearchTool"),
            ("Call Api", "CustomApiTool"),
            ("Write File", "CustomFileWriteTool"),
            ("Scrape Website Enhanced", "ScrapeWebsiteToolEnhanced"),
            ("CSV Search Enhanced", "CSVSearchToolEnhanced"),
            ("Scrapfly Scrape", "ScrapflyScrapeWebsiteTool"),
            # Issue #115 — Chinese labels persisted while running in zh
            ("DuckDuckGo 搜索", "DuckDuckGoSearchTool"),
            ("代码解释器", "CustomCodeInterpreterTool"),
            ("网页爬取工具", "ScrapeWebsiteTool"),
            # Tools whose translation was missing entirely — the raw i18n
            # key string ended up persisted as the tool name.
            ("tool.docx_search", "DOCXSearchTool"),
            ("tool.yahoo_finance_news", "YahooFinanceNewsTool"),
            ("tool.selenium_scraping", "SeleniumScrapingTool"),
            # my_tools.py referenced a key that never existed in the
            # translation files (they have 'scrapfly_scrape').
            ("tool.scrapfly_scrape_website", "ScrapflyScrapeWebsiteTool"),
        ],
    )
    def test_legacy_names_resolve(self, legacy_name, expected):
        assert resolve_tool_id(legacy_name) == expected

    def test_all_translated_labels_resolve(self):
        """Every label in every language must resolve to its stable id."""
        translations = load_translations()
        for stable_id, i18n_key in TOOL_I18N_KEYS.items():
            for lang, data in translations.items():
                label = data.get("tool", {}).get(i18n_key)
                if label is None or label in TOOL_CLASSES:
                    # Labels equal to a stable id resolve trivially.
                    continue
                assert resolve_tool_id(label) == stable_id, (
                    f"'{lang}' label {label!r} should resolve to {stable_id}"
                )

    def test_unknown_name_returns_none(self):
        assert resolve_tool_id("No Such Tool") is None
        assert resolve_tool_id("") is None


class TestLoadToolsFallback:
    """load_tools() must survive legacy/unknown names instead of crashing."""

    def test_load_tools_with_legacy_and_unknown_names(self):
        import db_utils

        db_utils.initialize_db()
        # Simulate a database written by the broken i18n versions.
        db_utils.save_entity(
            "tool", "T_legacy_en",
            {"name": "Code Interpreter", "description": "x",
             "parameters": {"workspace_dir": "workspace"}},
        )
        db_utils.save_entity(
            "tool", "T_legacy_zh",
            {"name": "DuckDuckGo 搜索", "description": "x", "parameters": {}},
        )
        db_utils.save_entity(
            "tool", "T_raw_key",
            {"name": "tool.docx_search", "description": "x",
             "parameters": {"docx": None}},
        )
        db_utils.save_entity(
            "tool", "T_stable",
            {"name": "ScrapeWebsiteTool", "description": "x",
             "parameters": {"website_url": None}},
        )
        db_utils.save_entity(
            "tool", "T_unknown",
            {"name": "Removed Tool From The Future", "description": "x",
             "parameters": {}},
        )

        tools = db_utils.load_tools()
        by_id = {tool.tool_id: tool for tool in tools}

        # KeyError would have been raised before the fix; now everything
        # known is remapped and the unknown tool is skipped.
        assert by_id["T_legacy_en"].name == "CustomCodeInterpreterTool"
        assert by_id["T_legacy_zh"].name == "DuckDuckGoSearchTool"
        assert by_id["T_raw_key"].name == "DOCXSearchTool"
        assert by_id["T_stable"].name == "ScrapeWebsiteTool"
        assert "T_unknown" not in by_id

        # Legacy rows must be self-healed: stored under the stable name.
        healed = dict(db_utils.load_entities("tool"))
        assert healed["T_legacy_en"]["name"] == "CustomCodeInterpreterTool"
        assert healed["T_legacy_zh"]["name"] == "DuckDuckGoSearchTool"
        assert healed["T_raw_key"]["name"] == "DOCXSearchTool"

        # Parameters survive the migration.
        assert by_id["T_legacy_en"].parameters.get("workspace_dir") == "workspace"
