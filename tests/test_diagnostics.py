"""Report retrieval preserves raw evidence and reports unavailable/truncated logs."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = (
    Path(__file__).resolve().parents[1] / "addon/FreeCADMCP/rpc_server/diagnostics.py"
)
spec = importlib.util.spec_from_file_location("diagnostics", PATH)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class TextWidget:
    def __init__(self, name, text):
        self.name, self.text = name, text

    def objectName(self):
        return self.name

    def toPlainText(self):
        return self.text


class PlainWidget(TextWidget):
    pass


@pytest.mark.parametrize("widget_type", [TextWidget, PlainWidget])
def test_report_preserves_traceback_and_unicode_without_clearing(widget_type):
    content = "earlier output\nTraceback:\nValueError: 단위 오류\n"
    widget = widget_type("Report view", content)
    window = SimpleNamespace(
        findChildren=lambda cls: [widget] if cls is widget_type else []
    )
    qt = SimpleNamespace(QTextEdit=TextWidget, QPlainTextEdit=PlainWidget)
    result = diagnostics.read_report_view(window, qt, 30)
    assert result["text"] == content[-30:]
    assert result["omitted_chars"] == len(content) - 30
    assert result["total_chars"] == len(content)
    assert widget.text == content
    assert diagnostics.read_report_view(window, qt)["text"] == content
    widget.text = ""
    assert diagnostics.read_report_view(window, qt)["success"]


def test_missing_report_is_not_an_empty_success():
    window = SimpleNamespace(
        findChildren=lambda cls: [TextWidget("Python console", "secret")]
    )
    qt = SimpleNamespace(QTextEdit=TextWidget, QPlainTextEdit=PlainWidget)
    result = diagnostics.read_report_view(window, qt)
    assert not result["success"]
    assert "text" not in result


@pytest.mark.parametrize("limit", [0, -1, 100001, True, 1.5])
def test_invalid_report_limits(limit):
    with pytest.raises(ValueError, match="max_chars"):
        diagnostics.read_report_view(None, None, limit)
