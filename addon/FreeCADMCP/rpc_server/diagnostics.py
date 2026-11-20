"""Read retained Report view text on the GUI thread; never clear the log."""


def read_report_view(main_window, qt_widgets, max_chars=20000):
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1 <= max_chars <= 100000
    ):
        raise ValueError("max_chars must be an integer between 1 and 100000")
    widget = next(
        (
            w
            for cls in (qt_widgets.QTextEdit, qt_widgets.QPlainTextEdit)
            for w in main_window.findChildren(cls)
            if w.objectName() == "Report view"
        ),
        None,
    )
    if widget is None:
        return {"success": False, "error": "Report view is unavailable"}
    text = widget.toPlainText()
    return {
        "success": True,
        "source": "Report view",
        "text": text[-max_chars:],
        "total_chars": len(text),
        "omitted_chars": max(0, len(text) - max_chars),
    }
