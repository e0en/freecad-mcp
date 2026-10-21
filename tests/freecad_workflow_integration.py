"""Run run() inside FreeCAD's GUI-thread execute_code, never on a user document."""

import importlib.util
from pathlib import Path
import time


MODULE = Path(__file__).resolve().parents[1] / "addon/FreeCADMCP/rpc_server/workflow.py"


def run():
    import FreeCAD as App
    import Part
    import Sketcher

    spec = importlib.util.spec_from_file_location("workflow_integration", MODULE)
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    previous = App.ActiveDocument
    doc = App.newDocument("MCPWorkflowIntegration")
    assertions = []

    def check(condition, label):
        assert condition, label
        assertions.append(label)

    try:
        params = doc.addObject("App::VarSet", "Params")
        params.addProperty("App::PropertyLength", "Width")
        params.Width = "10 mm"
        a = doc.addObject("Part::Box", "Box")
        a.setExpression("Length", "Params.Width")
        b = doc.addObject("Part::Box", "Other")
        b.Placement.Base.x = 30
        container = doc.addObject("App::Part", "Container")
        container.Placement.Base.x = 100
        nested = doc.addObject("Part::Box", "Nested")
        container.addObject(nested)
        nested.Placement.Base.x = 5
        body = doc.addObject("PartDesign::Body", "Body")
        solid = body.newObject("PartDesign::AdditiveBox", "AdditiveBox")
        solid.Length, solid.Width, solid.Height = 7, 8, 9
        body.Placement.Base.y = 50
        sketch = doc.addObject("Sketcher::SketchObject", "Sketch")
        sketch.addGeometry(Part.Circle(App.Vector(), App.Vector(0, 0, 1), 5), False)
        sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 3, -1, 1))
        sketch.addConstraint(Sketcher.Constraint("Radius", 0, 5))
        doc.recompute()
        doc.commitTransaction()  # Commit fixture construction in the disposable document.
        before_undo = doc.UndoCount
        page = workflow.run(
            doc, "inspect", {"limit": 2, "include_property_names": True}
        )
        check(page["next_offset"] == 2 and len(page["objects"]) == 2, "pagination")
        check("Width" in page["objects"][0]["scalar_properties"], "parameter discovery")
        values = workflow.run(
            doc, "inspect", {"objects": ["Box"], "properties": ["Length"]}
        )
        check(
            values["objects"][0]["properties"]["Length"]["expression"]
            == "Params.Width",
            "expression discovery",
        )
        check(doc.UndoCount == before_undo, "queries do not edit document")
        measure = workflow.run(doc, "measure", {"objects": ["Box", "Other"]})
        check(abs(measure["minimum_distance_mm"] - 20) < 1e-8, "shape distance")
        measure = workflow.run(doc, "measure", {"objects": ["Nested"]})
        check(
            abs(measure["objects"][0]["bounds_mm"][0] - 105) < 1e-8,
            "container global coordinates",
        )
        measure = workflow.run(doc, "measure", {"objects": ["Body"]})
        check(
            abs(measure["objects"][0]["volume_mm3"] - 504) < 1e-8
            and measure["objects"][0]["bounds_mm"][1] == 50,
            "PartDesign body measurement",
        )
        result = workflow.run(
            doc,
            "update",
            {
                "changes": [
                    {"object": "Params", "property": "Width", "value": "12 mm"},
                    {"object": "Other", "property": "Height", "value": "15 mm"},
                ]
            },
        )
        check(
            result["success"] and a.Length.Value == 12 and b.Height.Value == 15,
            "batch edits and dependent recompute",
        )
        check(doc.UndoCount == before_undo + 1, "one undo entry")
        doc.undo()
        doc.recompute()
        check(
            a.Length.Value == 10 and b.Height.Value == 10, "single undo restores batch"
        )
        result = workflow.run(
            doc,
            "update",
            {"changes": [{"object": "Box", "property": "Length", "value": 13}]},
        )
        check(
            not result["success"] and a.Length.Value == 10,
            "driven value overwrite rejected",
        )
        result = workflow.run(
            doc,
            "update",
            {
                "changes": [
                    {"object": "Params", "property": "Width", "value": 22},
                    {"object": "Other", "property": "Height", "value": "not a length"},
                ]
            },
        )
        check(
            not result["success"]
            and result["rolled_back"]
            and params.Width.Value == 10
            and a.Length.Value == 10,
            "runtime failure rolls back values and geometry",
        )
        result = workflow.run(
            doc,
            "update",
            {
                "changes": [
                    {
                        "object": "Box",
                        "property": "Length",
                        "expression": None,
                        "value": "14 mm",
                    }
                ]
            },
        )
        check(
            result["success"] and a.Length.Value == 14 and not a.ExpressionEngine,
            "explicit expression removal",
        )
        result = workflow.run(
            doc,
            "update",
            {
                "changes": [
                    {
                        "object": "Box",
                        "property": "Length",
                        "expression": "Params.Width * 2",
                    }
                ]
            },
        )
        check(result["success"] and a.Length.Value == 20, "expression edit")
        result = workflow.run(
            doc,
            "update",
            {
                "changes": [
                    {"object": "Box", "property": "Length", "expression": "0 mm"}
                ]
            },
        )
        check(
            not result["success"] and result["rolled_back"] and a.Length.Value == 20,
            "invalid recompute rolls back geometry and expression",
        )
        check(
            dict(a.ExpressionEngine)["Length"] == "Params.Width * 2",
            "rollback restores original expression",
        )
        doc.openTransaction("User transaction")
        params.Label = "User label edit"
        result = workflow.run(
            doc,
            "update",
            {"changes": [{"object": "Params", "property": "Width", "value": 17}]},
        )
        check(
            not result["success"] and doc.HasPendingTransaction,
            "existing user transaction preserved",
        )
        doc.abortTransaction()
        report = workflow.run(
            doc,
            "check",
            {
                "objects": ["Box", "Other", "Body", "Nested", "Sketch"],
                "collisions": True,
            },
        )
        check(
            report["passed"] and report["collision_pairs_tested"] == 6,
            "solid sketch and no-overlap checks",
        )
        b.Placement.Base.x = 5
        doc.recompute()
        report = workflow.run(
            doc, "check", {"objects": ["Box", "Other"], "collisions": True}
        )
        check(
            not report["passed"] and report["collisions"][0]["volume_mm3"] > 0,
            "overlap detected",
        )
        report = workflow.run(doc, "check", {"objects": ["Params"]})
        check(
            not report["passed"] and report["objects"][0]["skipped"],
            "unsupported object cannot pass",
        )
        # Compare compact object identities with legacy full property serialization.
        from rpc_server.serialize import serialize_object
        import json

        started = time.perf_counter()
        legacy = json.dumps([serialize_object(o) for o in doc.Objects], default=str)
        legacy_ms = (time.perf_counter() - started) * 1000
        compact = workflow.run(doc, "inspect", {})
        compact_json = json.dumps(compact, separators=(",", ":"))
        check(
            len(compact_json) < len(legacy),
            "compact response smaller than full serialization",
        )
        return {
            "passed": len(assertions),
            "checks": assertions,
            "sample_response_bytes": {
                "legacy": len(legacy.encode()),
                "compact": len(compact_json.encode()),
            },
            "sample_ms": {
                "legacy_serialization": legacy_ms,
                "compact_inspect": compact["total_ms"],
            },
        }
    finally:
        App.closeDocument(doc.Name)
        if previous is not None:
            App.setActiveDocument(previous.Name)
