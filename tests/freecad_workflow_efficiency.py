"""Run run() through GUI-thread execute_code for geometry and efficiency checks."""

import importlib.util
from itertools import combinations
import json
from pathlib import Path
from time import perf_counter


MODULE = Path(__file__).resolve().parents[1] / "addon/FreeCADMCP/rpc_server/workflow.py"


def run():
    import FreeCAD as App
    import Part

    spec = importlib.util.spec_from_file_location("workflow_efficiency", MODULE)
    workflow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workflow)
    previous = App.ActiveDocument
    doc = App.newDocument("MCPWorkflowEfficiency")
    checks = []

    def check(condition, label):
        assert condition, label
        checks.append(label)

    def part(name, shape):
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = shape
        return obj

    def query(names, **options):
        result = workflow.run(doc, "check", {"objects": names, **options})
        assert result["success"], result
        return result

    try:
        a = part("BoxA", Part.makeBox(10, 10, 10))
        b = part("BoxB", Part.makeBox(10, 10, 10, App.Vector(10, 0, 0)))
        doc.recompute()
        touching = query([a.Name, b.Name], collisions=True)
        check(
            touching["passed"] and touching["boolean_tests"] == 0,
            "face contact skips Boolean and passes overlap check",
        )
        contact_gap = query([a.Name, b.Name], minimum_clearance_mm=0.2)
        check(
            not contact_gap["passed"]
            and contact_gap["clearance_violations"][0]["distance_mm"] == 0,
            "face contact fails positive clearance",
        )
        b.Placement.Base.x = 0.25
        doc.recompute()
        sufficient = query([a.Name, b.Name], minimum_clearance_mm=0.2)
        insufficient = query([a.Name, b.Name], minimum_clearance_mm=0.3)
        check(
            sufficient["passed"] and sufficient["distance_tests"] == 0,
            "sufficient box separation skips exact distance",
        )
        check(
            not insufficient["passed"]
            and abs(insufficient["clearance_violations"][0]["distance_mm"] - 0.25)
            < 1e-8,
            "quarter-millimetre gap measured exactly when needed",
        )
        ring = part("Ring", Part.makeCylinder(10, 10).cut(Part.makeCylinder(8, 10)))
        pin = part("Pin", Part.makeCylinder(7, 10))
        doc.recompute()
        ring_clear = query(
            [ring.Name, pin.Name], collisions=True, minimum_clearance_mm=0.5
        )
        ring_tight = query([ring.Name, pin.Name], minimum_clearance_mm=1.1)
        check(
            ring_clear["passed"]
            and ring_clear["boolean_tests"] == ring_clear["distance_tests"] == 1,
            "overlapping bounds fall back to exact geometry",
        )
        check(
            not ring_tight["passed"]
            and abs(ring_tight["clearance_violations"][0]["distance_mm"] - 1) < 1e-8,
            "ring-to-pin radial gap is one millimetre",
        )
        inside = part("Inside", Part.makeBox(2, 2, 2, App.Vector(4, 4, 4)))
        doc.recompute()
        containment = query(
            [a.Name, inside.Name], collisions=True, minimum_clearance_mm=0.1
        )
        check(
            not containment["passed"]
            and containment["summary"]["collisions"] == 1
            and containment["summary"]["clearance_violations"] == 1,
            "solid containment fails overlap and clearance",
        )
        a.touch()
        before = list(a.State)
        measured = workflow.run(doc, "measure", {"objects": [a.Name]})
        check(
            a.Name in measured["pending_recompute"] and list(a.State) == before,
            "measure exposes stale state without recompute",
        )
        doc.recompute()
        crowded = [part(f"Crowded{i}", Part.makeBox(2, 2, 2)).Name for i in range(6)]
        doc.recompute()
        full = query(crowded, collisions=True)
        limited = query(crowded, collisions=True, details="failures", max_details=1)
        summary = query(crowded, collisions=True, details="summary")
        check(
            not limited["passed"]
            and limited["summary"] == full["summary"] == summary["summary"],
            "bounded reports retain complete verdict and counts",
        )
        check(
            limited["summary"]["collisions"] == 15
            and limited["omitted"]["collisions"] == 14,
            "all fifteen overlaps counted with one detail",
        )
        check(
            not summary["collisions"] and summary["omitted"]["collisions"] == 15,
            "summary omits details without hiding overlap count",
        )
        line = [
            part(f"Line{i:03d}", Part.makeBox(10, 10, 10, App.Vector(i * 10, 30, 0)))
            for i in range(80)
        ]
        doc.recompute()
        filtered = workflow.run(
            doc,
            "inspect",
            {"type_ids": ["Part::Feature"], "name_pattern": "Line*", "limit": 200},
        )
        check(
            filtered["total"] == 80 and len(filtered["objects"]) == 80,
            "type and name filtering select intended parts",
        )
        # Baseline uses the previous pair-loop algorithm on the same global shapes.
        shapes = [workflow._shape(obj) for obj in line]
        started = perf_counter()
        baseline_booleans, baseline_overlaps = 0, 0
        for left, right in combinations(shapes, 2):
            if left.BoundBox.intersect(right.BoundBox):
                baseline_booleans += 1
                baseline_overlaps += left.common(right).Volume > 1e-6
        baseline_ms = (perf_counter() - started) * 1000
        all_details = query([obj.Name for obj in line], collisions=True)
        brief = query([obj.Name for obj in line], collisions=True, details="summary")
        check(
            all_details["passed"] and brief["passed"] and baseline_overlaps == 0,
            "optimized and baseline overlap verdicts agree",
        )
        check(
            all_details["boolean_tests"] == 0 and baseline_booleans > 0,
            "touching-part fixture eliminates unnecessary Boolean calls",
        )
        all_bytes = len(json.dumps(all_details, separators=(",", ":")).encode())
        summary_bytes = len(json.dumps(brief, separators=(",", ":")).encode())
        check(summary_bytes < all_bytes, "summary reduces response bytes")
        return {
            "freecad": App.Version(),
            "passed": len(checks),
            "checks": checks,
            "fixture": {"parts": 80, "pairs": 3160},
            "comparison": {
                "baseline_boolean_tests": baseline_booleans,
                "optimized_boolean_tests": all_details["boolean_tests"],
                "baseline_pair_ms": baseline_ms,
                "optimized_pair_ms": all_details["timings"]["pair_checks_ms"],
                "all_details_bytes": all_bytes,
                "summary_bytes": summary_bytes,
            },
            "limits": [
                "Synthetic fixture and single timing samples, not total agent task time",
                "Response bytes are not token counts",
                "Nominal CAD clearance excludes print tolerances",
            ],
        }
    finally:
        App.closeDocument(doc.Name)
        if previous is not None:
            App.setActiveDocument(previous.Name)
