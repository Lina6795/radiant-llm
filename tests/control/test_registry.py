"""Registry contract: 4 implemented mocks + 13 metadata-only legacy tools."""

from __future__ import annotations

from app.control.models import Risk, ToolStatus
from app.control.registry import build_default_registry

MOCK_TOOLS = {
    "evidence.search": Risk.READ_ONLY,
    "evidence.inspect": Risk.READ_ONLY,
    "citation.validate": Risk.READ_ONLY,
    "report.export": Risk.BOUNDED_WRITE,
    "context.assemble": Risk.READ_ONLY,
    "answer.draft": Risk.READ_ONLY,
    "answer.verify": Risk.READ_ONLY,
}

LEGACY_TOOLS = {
    "PDFReaderTool": Risk.READ_ONLY,
    "PDFKnowledgeBaseSanitizerTool": Risk.BOUNDED_WRITE,
    "URLValidationTool": Risk.READ_ONLY,
    "WebSearchTool": Risk.EXTERNAL,
    "WebScraperTool": Risk.EXTERNAL,
    "WikipediaSearchTool": Risk.EXTERNAL,
    "PythonREPLTool": Risk.EXTERNAL,
    "ImageAnalysisTool": Risk.READ_ONLY,
    "CSVandExcelFileParserTool": Risk.READ_ONLY,
    "CSVDataFinderTool": Risk.READ_ONLY,
    "TextFileReaderTool": Risk.READ_ONLY,
    "SkillLookupTool": Risk.READ_ONLY,
    "FileDownloaderTool": Risk.EXTERNAL,
}


def test_registry_contains_all_17_tools_with_versions() -> None:
    reg = build_default_registry()
    assert set(reg.names()) == set(MOCK_TOOLS) | set(LEGACY_TOOLS)
    for name in reg.names():
        spec = reg.get(name)
        assert spec is not None and spec.version
        expected_risk = MOCK_TOOLS.get(name) or LEGACY_TOOLS.get(name)
        assert spec.risk == expected_risk


def test_mocks_implemented_legacy_metadata_only() -> None:
    reg = build_default_registry()
    for name in MOCK_TOOLS:
        spec = reg.get(name)
        assert spec.implemented and spec.handler is not None
    for name in LEGACY_TOOLS:
        spec = reg.get(name)
        assert not spec.implemented and spec.handler is None


def test_high_risk_flags() -> None:
    reg = build_default_registry()
    for name in ("PythonREPLTool", "FileDownloaderTool", "WebSearchTool"):
        assert reg.get(name).high_risk


def test_catalog_exposes_no_handlers() -> None:
    reg = build_default_registry()
    for entry in reg.catalog():
        assert "handler" not in entry


def test_metadata_only_tool_cannot_execute() -> None:
    reg = build_default_registry()
    result = reg.invoke("PythonREPLTool", {}, run_id="r", workspace="full")
    assert result.status == ToolStatus.TERMINAL_ERROR
    assert result.error.code == "tool.not_implemented"


def test_duplicate_registration_rejected() -> None:
    import pytest

    from app.control.models import ToolSpec

    reg = build_default_registry()
    with pytest.raises(ValueError):
        reg.register(ToolSpec(name="evidence.search", version="9.9.9", risk=Risk.READ_ONLY))
