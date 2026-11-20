"""Run run() on FreeCAD's GUI thread; all edits use a disposable document."""

import importlib.util
from pathlib import Path


def run():
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    import Sketcher
    from PySide import QtWidgets

    root = Path(__file__).resolve().parents[1] / "addon/FreeCADMCP/rpc_server"
    modules = {}
    for name in ("workflow", "diagnostics"):
        spec = importlib.util.spec_from_file_location(
            "diagnostic_test_" + name, root / (name + ".py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[name] = module
    workflow = modules["workflow"]
    previous = App.ActiveDocument
    doc = App.newDocument("MCPDiagnosticsRegression")
    checks = []
    try:
        doc.UndoMode = 1
        sketch = doc.addObject("Sketcher::SketchObject", "Profile")
        sketch.addGeometry(Part.Circle(App.Vector(), App.Vector(0, 0, 1), 5))
        sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 3, -1, 1))
        radius = sketch.addConstraint(Sketcher.Constraint("Radius", 0, 5))
        sketch.renameConstraint(radius, "Radius")
        sketch.setDriving(radius, False)
        params = doc.addObject("App::FeaturePython", "Parameters")
        params.addProperty("App::PropertyFloat", "Offset")
        params.Offset = 0.6
        box = doc.addObject("Part::Box", "Box")
        doc.recompute()
        doc.commitTransaction()
        states = [(o.Name, list(o.State)) for o in doc.Objects]
        undo = doc.UndoCount
        result = workflow.inspect_sketch(doc, sketch.Name)
        assert result["constraints"][0]["type"] == "Coincident"
        assert result["constraints"][radius]["driving"] is False
        assert result["constraints"][radius]["name"] == "Radius"
        assert result["attachment_property"] in ("AttachmentSupport", "Support")
        assert states == [(o.Name, list(o.State)) for o in doc.Objects]
        assert undo == doc.UndoCount
        checks.append(
            "geometric/reference constraints and attachment read without mutation"
        )
        info = workflow.inspect_document(
            doc, objects=[params.Name], properties=["Offset"]
        )
        assert info["objects"][0]["properties"]["Offset"]["unit"] is None
        info = workflow.inspect_document(doc, objects=[box.Name], properties=["Length"])
        assert info["objects"][0]["properties"]["Length"]["unit"] is not None
        checks.append("dimensionless offset distinguished from length")
        for expression in ("Parameters.Offset + 9.4 mm", "Box.Length"):
            original = box.Length.Value
            result = workflow.run(
                doc,
                "update",
                {
                    "changes": [
                        {
                            "object": "Box",
                            "property": "Length",
                            "expression": expression,
                        }
                    ]
                },
            )
            assert not result["success"], result
            assert result["rolled_back"], result
            assert box.Length.Value == original and not box.ExpressionEngine
            checks.append("rollback: " + expression)
        report = modules["diagnostics"].read_report_view(
            Gui.getMainWindow(), QtWidgets, 1000
        )
        assert report["success"] and len(report["text"]) <= 1000
        assert report["total_chars"] == report["omitted_chars"] + len(report["text"])
        checks.append("live Report view bounded read")
        return {"passed": len(checks), "checks": checks}
    finally:
        App.closeDocument(doc.Name)
        if previous is not None:
            App.setActiveDocument(previous.Name)
