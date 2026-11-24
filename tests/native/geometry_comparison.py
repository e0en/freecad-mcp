"""Regression for comparing real App::Link instances in FreeCAD."""

import math
from pathlib import Path
import sys

import FreeCAD as App

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addon/FreeCADMCP"))
from rpc_server.workflow import run


def run_regression() -> None:
    baseline = App.newDocument("CompareBaseline")
    edited = App.newDocument("CompareEdited")
    try:
        source_part = edited.addObject("App::Part", "SourcePart")
        source_part.Placement.Base = App.Vector(200, 0, 0)
        source = source_part.newObject("Part::Box", "Source")
        source.Length, source.Width, source.Height = 10, 4, 6

        outer = edited.addObject("App::Part", "Outer")
        outer.Placement.Base = App.Vector(100, 20, 0)
        inner = outer.newObject("App::Part", "Inner")
        rotation = App.Rotation(App.Vector(0, 0, 1), 90)
        inner.Placement = App.Placement(App.Vector(30, 0, 0), rotation)
        nested = inner.newObject("App::Link", "Nested")
        standalone = edited.addObject("App::Link", "Standalone")
        for link in (nested, standalone):
            link.setLink(source)
            link.LinkPlacement.Base = App.Vector(5, 7, 0)

        # Build independent references at the known instance placements,
        # excluding the source container's 200 mm translation.
        for name, position, orientation in (
            ("NestedReference", App.Vector(123, 25, 0), rotation),
            ("StandaloneReference", App.Vector(5, 7, 0), App.Rotation()),
        ):
            box = baseline.addObject("Part::Box", name)
            box.Length, box.Width, box.Height = 10, 4, 6
            box.Placement = App.Placement(position, orientation)
        baseline.recompute()
        edited.recompute()

        options = {
            "edited_doc_name": edited.Name,
            "pairs": [
                {"baseline": "NestedReference", "edited": "Nested"},
                {"baseline": "StandaloneReference", "edited": "Standalone"},
            ],
        }
        equivalent = run(baseline, "compare", options)
        assert equivalent["success"] and equivalent["passed"], equivalent
        assert equivalent["checked"] == 2, equivalent
        for pair in equivalent["pairs"]:
            assert abs(pair["removed_mm3"]) <= 1e-6, pair
            assert abs(pair["added_mm3"]) <= 1e-6, pair

        inspected = run(
            edited, "inspect", {"objects": ["Nested"], "include_references": True}
        )
        assert inspected["success"], inspected
        refs = inspected["objects"][0]["references"]
        assert {"document": edited.Name, "name": "Source"} in refs["sources"], refs
        assert {"document": edited.Name, "name": "Inner"} in refs["consumers"], refs

        # Shift the instance 2 mm along its parent's local X axis. Equal total
        # volumes still differ by two 2 × 4 × 6 mm strips.
        nested.LinkPlacement.Base = App.Vector(7, 7, 0)
        stale = run(baseline, "compare", options)
        assert not stale["success"] and "Recompute" in stale["error"], stale
        edited.recompute()
        changed = run(baseline, "compare", options)
        assert changed["success"] and not changed["passed"], changed
        moved, unchanged = changed["pairs"]
        assert not moved["passed"] and unchanged["passed"], changed
        assert math.isclose(moved["removed_mm3"], 48, abs_tol=1e-6), moved
        assert math.isclose(moved["added_mm3"], 48, abs_tol=1e-6), moved
    finally:
        App.closeDocument(edited.Name)
        App.closeDocument(baseline.Name)

    print("NATIVE_GEOMETRY_COMPARISON_OK", ".".join(App.Version()[:3]))
