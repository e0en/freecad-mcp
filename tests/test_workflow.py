"""Input, failure and response contracts; geometry is covered in FreeCAD live tests."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


PATH = Path(__file__).resolve().parents[1] / "addon/FreeCADMCP/rpc_server/workflow.py"
spec = importlib.util.spec_from_file_location("workflow", PATH)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class Parameter:
    Name = "Params"
    Label = "Parameters"
    TypeId = "App::VarSet"
    PropertiesList = ["Width", "Height"]
    State = []

    def __init__(self):
        self.Width = 10.0
        self.Height = 20.0
        self.ExpressionEngine = []
        self.mode = []

    def getTypeIdOfProperty(self, name):
        return "App::PropertyFloat"

    def getEditorMode(self, name):
        return self.mode

    def setExpression(self, prop, expression):
        expressions = dict(self.ExpressionEngine)
        if expression is None:
            expressions.pop(prop, None)
        else:
            expressions[prop] = expression
        self.ExpressionEngine = list(expressions.items())

    def getStatusString(self):
        return "Invalid"


class Document:
    Name = "AnyProject"
    UndoMode = 1
    HasPendingTransaction = False

    def __init__(self):
        self.Objects = [Parameter()]
        self.opened = self.committed = self.aborted = self.recomputed = 0
        self.fail_recompute = False
        self.fail_abort = False

    def getObject(self, name):
        return next((o for o in self.Objects if o.Name == name), None)

    def openTransaction(self, label):
        self.opened += 1
        self.snapshot = copy.deepcopy(self.Objects[0].__dict__)

    def commitTransaction(self):
        self.committed += 1

    def abortTransaction(self):
        self.aborted += 1
        if self.fail_abort:
            raise RuntimeError("Cannot abort")
        self.Objects[0].__dict__ = self.snapshot

    def recompute(self):
        self.recomputed += 1
        if self.fail_recompute and self.recomputed == 1:
            return False
        return True


@pytest.fixture
def doc():
    return Document()


def edit(**extra):
    return {"object": "Params", "property": "Width", "value": 12, **extra}


@pytest.mark.parametrize(
    "change",
    [
        edit(object="Missing"),
        edit(property="Missing"),
        edit(unknown=42),
        edit(value=float("nan")),
        edit(value=float("inf")),
        edit(value=[]),
        edit(expression="Width*2"),
        edit(expression=""),
        {"object": "Params", "property": "Width"},
    ],
)
def test_preflight_rejects_without_transaction(doc, change):
    result = workflow.run(doc, "update", {"changes": [change]})
    assert not result["success"]
    assert doc.opened == 0
    assert doc.Objects[0].Width == 10


@pytest.mark.parametrize("changes", [[], [edit(), edit()]])
def test_empty_or_duplicate_edits_rejected(doc, changes):
    assert not workflow.run(doc, "update", {"changes": changes})["success"]
    assert not doc.opened


@pytest.mark.parametrize("mode", [["ReadOnly"], ["Hidden"], 1, 2])
def test_noneditable_property_rejected(doc, mode):
    doc.Objects[0].mode = mode
    assert not workflow.run(doc, "update", {"changes": [edit()]})["success"]
    assert not doc.opened


def test_batch_uses_single_recompute_and_commit(doc):
    result = workflow.run(
        doc, "update", {"changes": [edit(), edit(property="Height", value=25)]}
    )
    assert result["success"] and not result["saved"]
    assert (doc.opened, doc.committed, doc.recomputed, doc.aborted) == (1, 1, 1, 0)
    assert (doc.Objects[0].Width, doc.Objects[0].Height) == (12, 25)
    assert set(result["timings"]) == {"edit_ms", "recompute_ms", "validation_ms"}


def test_failed_recompute_restores_parameters(doc):
    doc.fail_recompute = True
    result = workflow.run(doc, "update", {"changes": [edit()]})
    assert not result["success"] and result["rolled_back"]
    assert result["failed_phase"] == "recompute" and result["failed_index"] is None
    assert doc.Objects[0].Width == 10
    assert doc.committed == 0 and doc.aborted == 1


def test_failed_rollback_is_not_reported_as_success(doc):
    doc.fail_recompute = doc.fail_abort = True
    result = workflow.run(doc, "update", {"changes": [edit()]})
    assert not result["success"] and not result["rolled_back"]
    assert result["rollback_error"] == "Cannot abort"


@pytest.mark.parametrize("attribute", ["HasPendingTransaction", "UndoMode"])
def test_preserve_user_transactions_and_disabled_undo(doc, attribute):
    setattr(doc, attribute, attribute == "HasPendingTransaction")
    assert not workflow.run(doc, "update", {"changes": [edit()]})["success"]
    assert not doc.opened


def test_expressions_require_explicit_removal(doc):
    doc.Objects[0].ExpressionEngine = [("Width", "Height/2")]
    assert not workflow.run(doc, "update", {"changes": [edit()]})["success"]
    assert workflow.run(doc, "update", {"changes": [edit(expression=None)]})["success"]
    assert not doc.Objects[0].ExpressionEngine


def test_inspection_is_bounded_and_does_not_recompute(doc):
    result = workflow.run(
        doc, "inspect", {"properties": ["Width"], "include_property_names": True}
    )
    assert result["objects"][0]["properties"]["Width"]["value"] == 10
    assert result["objects"][0]["scalar_properties"] == ["Width", "Height"]
    assert doc.recomputed == 0
    assert not workflow.run(doc, "inspect", {"limit": 201})["success"]
    assert not workflow.run(doc, "inspect", {"objects": []})["success"]


def test_stale_state_cannot_pass_check(doc):
    obj = doc.Objects[0]
    obj.TypeId = "Sketcher::SketchObject"
    obj.FullyConstrained = True
    obj.State = ["Touched"]
    result = workflow.run(doc, "check", {"objects": [obj.Name]})
    assert result["success"] and not result["passed"]
    assert result["pending_recompute"] == [obj.Name]


def test_unsupported_checks_and_missing_shapes_are_explicit(doc):
    result = workflow.run(doc, "check", {"objects": ["Params"]})
    assert not result["passed"] and result["objects"][0]["skipped"]
    assert not workflow.run(doc, "measure", {"objects": ["Params"]})["success"]


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_overlap_tolerance_rejected(doc, value):
    result = workflow.run(doc, "check", {"objects": ["Params"], "tolerance_mm3": value})
    assert not result["success"]


def test_mcp_routes_one_rpc_and_preserves_error_details(monkeypatch):
    import json
    from freecad_mcp import server

    calls = []

    def request(*args):
        calls.append(args)
        return {"success": True, "objects": []}

    monkeypatch.setattr(
        server,
        "get_freecad_connection",
        lambda: SimpleNamespace(document_workflow=request),
    )
    response = server.inspect_document("Any", limit=7)
    assert json.loads(response[0].text)["success"]
    assert calls[0][0:2] == ("Any", "inspect") and calls[0][2]["limit"] == 7
    monkeypatch.setattr(
        server,
        "get_freecad_connection",
        lambda: SimpleNamespace(
            document_workflow=lambda *_: {
                "success": False,
                "rolled_back": False,
                "error": "abort failed",
            }
        ),
    )
    failed = server.update_parameters("Any", [edit()])
    assert failed.isError
    assert json.loads(failed.content[0].text) == {
        "success": False,
        "rolled_back": False,
        "error": "abort failed",
    }


def test_incomplete_rollback_recompute_is_failure(doc):
    doc.recompute = lambda: False
    result = workflow.run(doc, "update", {"changes": [edit()]})
    assert not result["success"] and not result["rolled_back"]
    assert result["rollback_errors"][0]["status"] == "Rollback recompute incomplete"


def test_filters_apply_before_pagination_and_combine(doc):
    other = Parameter()
    other.Name, other.TypeId = "Other", "Part::Box"
    second = Parameter()
    second.Name = "Params2"
    doc.Objects += [other, second]
    result = workflow.run(
        doc,
        "inspect",
        {"type_ids": ["App::VarSet"], "name_pattern": "Params*", "limit": 1},
    )
    assert result["total"] == 2 and result["next_offset"] == 1
    result = workflow.run(
        doc,
        "inspect",
        {
            "type_ids": ["App::VarSet"],
            "name_pattern": "Params*",
            "limit": 1,
            "offset": 1,
        },
    )
    assert result["objects"][0]["name"] == "Params2" and result["next_offset"] is None
    assert not doc.recomputed


@pytest.mark.parametrize(
    "options", [{"type_ids": []}, {"type_ids": [""]}, {"name_pattern": ""}]
)
def test_invalid_filters_are_explicit(doc, options):
    assert not workflow.run(doc, "inspect", options)["success"]


def test_failure_after_output_limit_is_still_counted(doc):
    doc.Objects = []
    for i in range(5):
        obj = Parameter()
        obj.Name = f"Sketch{i}"
        obj.TypeId = "Sketcher::SketchObject"
        obj.FullyConstrained = i < 4
        doc.Objects.append(obj)
    full = workflow.run(
        doc, "check", {"objects": [o.Name for o in doc.Objects], "max_details": 1}
    )
    assert not full["passed"] and full["summary"]["failed_objects"] == 1
    assert full["omitted"]["objects"] == 4
    failures = workflow.run(
        doc,
        "check",
        {
            "objects": [o.Name for o in doc.Objects],
            "details": "failures",
            "max_details": 1,
        },
    )
    assert failures["objects"] == [{"name": "Sketch4", "fully_constrained": False}]
    summary = workflow.run(
        doc, "check", {"objects": [o.Name for o in doc.Objects], "details": "summary"}
    )
    assert not summary["passed"] and not summary["objects"]
    assert summary["summary"] == full["summary"] and summary["omitted"]["objects"] == 5


@pytest.mark.parametrize(
    "options",
    [{"minimum_clearance_mm": v} for v in [0, -1, float("nan"), float("inf")]]
    + [{"max_details": v} for v in [-1, 201, True, 1.5]]
    + [{"details": "unknown"}],
)
def test_invalid_check_options_rejected(doc, options):
    assert not workflow.run(doc, "check", {"objects": ["Params"], **options})["success"]


def test_global_error_counts_survive_summary_and_zero_limit(doc):
    doc.Objects[0].State = ["Invalid", "Touched"]
    report = workflow.run(doc, "check", {"objects": ["Params"], "max_details": 0})
    assert not report["passed"]
    assert (
        report["summary"]["document_errors"]
        == report["summary"]["pending_recompute"]
        == 1
    )
    assert not report["document_errors"] and report["omitted"]["document_errors"] == 1


def test_clearance_broad_phase_and_exact_fallback(doc, monkeypatch):
    class Shape:
        Solids = [object()]

        def __init__(self, xmin, xmax):
            self.BoundBox = SimpleNamespace(
                XMin=xmin, XMax=xmax, YMin=0, YMax=10, ZMin=0, ZMax=10
            )
            self.booleans = self.distances = 0

        def isNull(self):
            return False

        def isValid(self):
            return True

        def common(self, other):
            self.booleans += 1
            return SimpleNamespace(Volume=0)

        def distToShape(self, other):
            self.distances += 1
            return (0.4,)

    a, b = Parameter(), Parameter()
    a.Name, b.Name = "A", "B"
    a.Shape, b.Shape = Shape(0, 10), Shape(20, 30)
    doc.Objects = [a, b]
    monkeypatch.setattr(workflow, "_shape", lambda obj: obj.Shape)
    options = {"objects": ["A", "B"], "collisions": True, "minimum_clearance_mm": 1}
    far = workflow.run(doc, "check", options)
    assert far["passed"] and far["boolean_tests"] == far["distance_tests"] == 0
    # Exact gap at the requested boundary is sufficient; no distance call needed.
    b.Shape = Shape(11, 21)
    boundary = workflow.run(doc, "check", options)
    assert boundary["passed"] and boundary["distance_tests"] == 0
    # Intersecting bounds alone cannot decide actual surface separation.
    b.Shape = Shape(5, 15)
    near = workflow.run(doc, "check", options)
    assert not near["passed"] and near["distance_tests"] == near["boolean_tests"] == 1
    assert near["clearance_violations"][0]["distance_mm"] == 0.4
    assert a.Shape.distances == a.Shape.booleans == 1


def test_touching_bounds_skip_boolean_but_not_clearance(doc, monkeypatch):
    a, b = Parameter(), Parameter()
    a.Name, b.Name = "A", "B"

    def shape(xmin, xmax):
        return SimpleNamespace(
            isNull=lambda: False,
            isValid=lambda: True,
            Solids=[1],
            BoundBox=SimpleNamespace(
                XMin=xmin, XMax=xmax, YMin=0, YMax=10, ZMin=0, ZMax=10
            ),
            distToShape=lambda other: (0.0,),
        )

    a.Shape, b.Shape = shape(0, 10), shape(10, 20)
    doc.Objects = [a, b]
    monkeypatch.setattr(workflow, "_shape", lambda obj: obj.Shape)
    report = workflow.run(
        doc,
        "check",
        {"objects": ["A", "B"], "collisions": True, "minimum_clearance_mm": 0.1},
    )
    assert not report["passed"] and report["boolean_tests"] == 0
    assert (
        report["distance_tests"] == 1 and report["summary"]["clearance_violations"] == 1
    )


@pytest.mark.parametrize("action", ["check", "measure"])
@pytest.mark.parametrize("objects", [None, []])
def test_explicit_nonempty_selection_required(doc, action, objects):
    result = workflow.run(doc, action, {"objects": objects})
    assert not result["success"]
