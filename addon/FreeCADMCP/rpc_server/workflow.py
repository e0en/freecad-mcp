"""Project-independent CAD queries and transactional scalar parameter edits.

Called on the GUI thread. Queries never recompute, mesh, save, or change views.
All lengths/areas/volumes use FreeCAD's internal mm/mm²/mm³ units.
"""

from fnmatch import fnmatchcase
from itertools import combinations
import math
from time import perf_counter


SCALAR_TYPES = {
    "App::PropertyLength",
    "App::PropertyDistance",
    "App::PropertyAngle",
    "App::PropertyFloat",
    "App::PropertyInteger",
    "App::PropertyBool",
    "App::PropertyString",
    "App::PropertyQuantity",
    "App::PropertyFloatConstraint",
    "App::PropertyIntegerConstraint",
    "App::PropertyPercent",
}


def _select(doc, names, allow_all=False):
    if names is None:
        if allow_all:
            return list(doc.Objects)
        raise ValueError("Explicit nonempty object Names are required")
    if not names or len(names) != len(set(names)):
        raise ValueError("objects must be a nonempty list of unique internal Names")
    result = []
    for name in names:
        obj = doc.getObject(name)
        if obj is None:
            raise ValueError(f"Unknown object Name: {name}")
        result.append(obj)
    return result


def _errors(doc):
    return [
        {"name": obj.Name, "status": obj.getStatusString()}
        for obj in doc.Objects
        if "Invalid" in obj.State
    ]


def _pending(doc):
    return [obj.Name for obj in doc.Objects if "Touched" in obj.State]


def _value(value):
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite property value")
        return value
    if hasattr(value, "Value") and hasattr(value, "Unit"):
        return {"value": float(value.Value), "unit": str(value.Unit)}
    if isinstance(value, tuple):
        return [_value(v) for v in value]
    raise ValueError("Property is not a supported scalar or quantity")


def inspect_document(
    doc,
    objects=None,
    properties=None,
    offset=0,
    limit=50,
    include_property_names=False,
    type_ids=None,
    name_pattern=None,
):
    """Page through object identities; opt into named scalar properties."""
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset >= 0 and 1 <= limit <= 200 required")
    selected = _select(doc, objects, allow_all=True)
    if type_ids is not None:
        if not type_ids or not all(isinstance(t, str) and t for t in type_ids):
            raise ValueError("type_ids must be a nonempty list of exact TypeIds")
        selected = [obj for obj in selected if obj.TypeId in type_ids]
    if name_pattern is not None:
        if not isinstance(name_pattern, str) or not name_pattern:
            raise ValueError("name_pattern must be a nonempty glob for internal Names")
        selected = [obj for obj in selected if fnmatchcase(obj.Name, name_pattern)]
    rows = []
    for obj in selected[offset : offset + limit]:
        row = {"name": obj.Name, "label": obj.Label, "type": obj.TypeId}
        if include_property_names:
            row["scalar_properties"] = [
                name
                for name in obj.PropertiesList
                if obj.getTypeIdOfProperty(name) in SCALAR_TYPES
            ]
        if properties is not None:
            row["properties"] = {}
            expressions = dict(getattr(obj, "ExpressionEngine", []))
            for name in properties:
                if name not in obj.PropertiesList:
                    raise ValueError(f"Unknown property: {obj.Name}.{name}")
                row["properties"][name] = {
                    "type": obj.getTypeIdOfProperty(name),
                    "value": _value(getattr(obj, name)),
                    "expression": expressions.get(name),
                }
        rows.append(row)
    end = offset + len(rows)
    return {
        "document": doc.Name,
        "total": len(selected),
        "objects": rows,
        "next_offset": end if end < len(selected) else None,
        "document_errors": _errors(doc),
        "pending_recompute": _pending(doc),
    }


def _shape(obj):
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(f"No non-null Shape: {obj.Name}")
    # Object.Shape uses the object's Placement, but not enclosing App::Part
    # placements. Work in document coordinates, including enclosing containers.
    shape = shape.copy()
    shape.Placement = (
        obj.getGlobalPlacement()
        .multiply(obj.Placement.inverse())
        .multiply(shape.Placement)
    )
    return shape


def _metrics(obj, shape):
    box = shape.BoundBox
    return {
        "name": obj.Name,
        "volume_mm3": shape.Volume,
        "area_mm2": shape.Area,
        "bounds_mm": [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax],
        "size_mm": [box.XLength, box.YLength, box.ZLength],
        "solids": len(shape.Solids),
        "faces": len(shape.Faces),
        "edges": len(shape.Edges),
    }


def measure_objects(doc, objects):
    selected = _select(doc, objects)
    if len(selected) > 200:
        raise ValueError("Measure at most 200 objects per request")
    shapes = [_shape(obj) for obj in selected]
    result = {
        "objects": [_metrics(o, s) for o, s in zip(selected, shapes)],
        "document_errors": _errors(doc),
        "pending_recompute": _pending(doc),
    }
    if len(shapes) == 2:
        result["minimum_distance_mm"] = shapes[0].distToShape(shapes[1])[0]
    return result


def _failed(row):
    return bool(row.get("skipped")) or not row.get(
        "valid_single_solid", row.get("fully_constrained", False)
    )


def _box_relation(a, b):
    """Conservative separation lower bound and overlap-volume upper bound."""
    gaps, overlap = [], 1.0
    for axis in ("X", "Y", "Z"):
        amin, amax = getattr(a, axis + "Min"), getattr(a, axis + "Max")
        bmin, bmax = getattr(b, axis + "Min"), getattr(b, axis + "Max")
        gaps.append(max(amin - bmax, bmin - amax, 0.0))
        overlap *= max(min(amax, bmax) - max(amin, bmin), 0.0)
    return math.hypot(*gaps), overlap


def check_document(
    doc,
    objects,
    collisions=False,
    tolerance_mm3=1e-6,
    minimum_clearance_mm=None,
    details="all",
    max_details=200,
):
    """Check explicit final parts with bounded output and optional clearance.

    Every selected object/pair is evaluated regardless of detail mode or limit.
    The nominal shape separation must be >= minimum_clearance_mm when requested.
    """
    if not math.isfinite(tolerance_mm3) or tolerance_mm3 < 0:
        raise ValueError("tolerance_mm3 must be finite and nonnegative")
    if minimum_clearance_mm is not None and (
        not math.isfinite(minimum_clearance_mm) or minimum_clearance_mm <= 0
    ):
        raise ValueError("minimum_clearance_mm must be finite and positive")
    if details not in ("all", "failures", "summary"):
        raise ValueError("details must be all, failures or summary")
    if (
        isinstance(max_details, bool)
        or not isinstance(max_details, int)
        or not 0 <= max_details <= 200
    ):
        raise ValueError("max_details must be an integer between 0 and 200")
    selected = _select(doc, objects)
    if len(selected) > 200:
        raise ValueError("Check at most 200 objects per request")
    rows, shapes = [], []
    started = perf_counter()
    for obj in selected:
        row = {"name": obj.Name}
        if obj.TypeId == "Sketcher::SketchObject":
            row["fully_constrained"] = bool(obj.FullyConstrained)
        elif hasattr(obj, "Shape"):
            if obj.Shape.isNull():
                row["valid_single_solid"] = False
            else:
                shape = _shape(obj)
                row["valid_single_solid"] = shape.isValid() and len(shape.Solids) == 1
                shapes.append((obj, shape, shape.BoundBox))
        else:
            row["skipped"] = "No Shape or sketch constraint state"
        rows.append(row)
    validity_ms = (perf_counter() - started) * 1000
    overlaps, clearance_violations = [], []
    tested = boolean_tests = distance_tests = 0
    started = perf_counter()
    if collisions or minimum_clearance_mm is not None:
        for (a, sa, ba), (b, sb, bb) in combinations(shapes, 2):
            tested += 1
            lower_distance, upper_volume = _box_relation(ba, bb)
            if collisions and upper_volume > tolerance_mm3:
                boolean_tests += 1
                volume = sa.common(sb).Volume
                if volume > tolerance_mm3:
                    overlaps.append({"a": a.Name, "b": b.Name, "volume_mm3": volume})
            if (
                minimum_clearance_mm is not None
                and lower_distance < minimum_clearance_mm
            ):
                distance_tests += 1
                distance = sa.distToShape(sb)[0]
                if distance < minimum_clearance_mm:
                    clearance_violations.append(
                        {"a": a.Name, "b": b.Name, "distance_mm": distance}
                    )
    pair_ms = (perf_counter() - started) * 1000
    errors, pending = _errors(doc), _pending(doc)
    failed = [row for row in rows if _failed(row)]
    full = {
        "objects": rows,
        "document_errors": errors,
        "pending_recompute": pending,
        "collisions": overlaps,
        "clearance_violations": clearance_violations,
    }
    output = {
        key: (failed if key == "objects" and details == "failures" else values)[
            :max_details
        ]
        if details != "summary"
        else []
        for key, values in full.items()
    }
    return {
        "passed": not errors
        and not pending
        and not overlaps
        and not clearance_violations
        and not failed,
        **output,
        "summary": {
            "checked_objects": len(rows),
            "failed_objects": len(failed),
            "document_errors": len(errors),
            "pending_recompute": len(pending),
            "collisions": len(overlaps),
            "clearance_violations": len(clearance_violations),
        },
        "details": details,
        "omitted": {
            key: len(values) - len(output[key]) for key, values in full.items()
        },
        "collision_pairs_tested": tested if collisions else 0,
        "clearance_pairs_tested": tested if minimum_clearance_mm is not None else 0,
        "boolean_tests": boolean_tests,
        "distance_tests": distance_tests,
        "collisions_checked": collisions,
        "tolerance_mm3": tolerance_mm3,
        "minimum_clearance_mm": minimum_clearance_mm,
        "timings": {"validity_ms": validity_ms, "pair_checks_ms": pair_ms},
        "evidence": "Current in-memory CAD only; no recompute, mesh, slicing or physical validation",
    }


def update_parameters(doc, changes):
    """Set existing scalar properties/expressions atomically, then recompute once.

    Expression-driven properties require an explicit expression update/clear.
    Does not save. A pre-existing transaction is rejected to preserve user edits.
    """
    if not changes or len(changes) > 200:
        raise ValueError("Provide between 1 and 200 changes")
    if doc.HasPendingTransaction:
        raise ValueError("Finish the existing document transaction first")
    if not doc.UndoMode:
        raise ValueError("Document UndoMode must be enabled for rollback")
    before_errors = _errors(doc)
    if before_errors:
        raise ValueError(f"Document already has errors: {before_errors}")
    prepared, seen = [], set()
    for change in changes:
        if set(change) - {"object", "property", "value", "expression"}:
            raise ValueError("Unknown change fields")
        obj = _select(doc, [change["object"]])[0]
        prop = change["property"]
        key = (obj.Name, prop)
        if key in seen:
            raise ValueError(f"Duplicate property: {key}")
        seen.add(key)
        if (
            prop not in obj.PropertiesList
            or obj.getTypeIdOfProperty(prop) not in SCALAR_TYPES
        ):
            raise ValueError(f"Not an editable scalar property: {obj.Name}.{prop}")
        if obj.getEditorMode(prop) not in (0, []):
            raise ValueError(f"Property is read-only or hidden: {obj.Name}.{prop}")
        has_value, has_expression = "value" in change, "expression" in change
        expr = change.get("expression")
        if not has_value and not has_expression:
            raise ValueError("A value or expression is required")
        if (
            has_expression
            and expr is not None
            and (not isinstance(expr, str) or not expr.strip())
        ):
            raise ValueError("expression must be a nonempty string or null")
        if has_value:
            if not isinstance(change["value"], (str, int, float, bool)):
                raise ValueError("value must be a scalar or a quantity string")
            _value(change["value"])
            if has_expression and expr is not None:
                raise ValueError(
                    "Use either value or expression; null may clear an expression before setting a value"
                )
            if prop in dict(obj.ExpressionEngine) and not has_expression:
                raise ValueError(
                    "Explicitly clear the existing expression with expression: null"
                )
        prepared.append((obj, prop, change))
    previous = [
        (obj, prop, _value(getattr(obj, prop)), dict(obj.ExpressionEngine).get(prop))
        for obj, prop, _ in prepared
    ]
    doc.openTransaction("MCP parameter batch")
    timings = {}
    phase = "edit"
    try:
        started = perf_counter()
        for index, (obj, prop, change) in enumerate(prepared):
            if "expression" in change:
                obj.setExpression(prop, change["expression"])
            if "value" in change:
                setattr(obj, prop, change["value"])
        timings["edit_ms"] = (perf_counter() - started) * 1000
        started = perf_counter()
        phase = "recompute"
        recomputed = doc.recompute()
        timings["recompute_ms"] = (perf_counter() - started) * 1000
        if recomputed is False or _errors(doc) or _pending(doc):
            raise ValueError(
                f"Recompute failed: errors={_errors(doc)}, pending={_pending(doc)}"
            )
        phase = "validate"
        started = perf_counter()
        # Reject invalid non-null shapes throughout the document. Empty
        # construction objects are permitted; final-part checks are explicit.
        for obj in doc.Objects:
            shape = getattr(obj, "Shape", None)
            if shape is not None and not shape.isNull() and not shape.isValid():
                raise ValueError(f"Invalid Shape after recompute: {obj.Name}")
        result = [
            {
                "object": obj.Name,
                "property": prop,
                "value": _value(getattr(obj, prop)),
                "expression": dict(obj.ExpressionEngine).get(prop),
            }
            for obj, prop, _ in prepared
        ]
        timings["validation_ms"] = (perf_counter() - started) * 1000
        phase = "commit"
        doc.commitTransaction()
        return {"success": True, "changes": result, "timings": timings, "saved": False}
    except Exception as exc:
        try:
            doc.abortTransaction()
            rollback_recomputed = doc.recompute()
            rollback_errors = _errors(doc)
            if rollback_recomputed is False or _pending(doc):
                rollback_errors.append(
                    {
                        "status": "Rollback recompute incomplete",
                        "pending": _pending(doc),
                    }
                )
            for obj, prop, value, expression in previous:
                if (
                    _value(getattr(obj, prop)) != value
                    or dict(obj.ExpressionEngine).get(prop) != expression
                ):
                    rollback_errors.append(
                        {
                            "name": obj.Name,
                            "property": prop,
                            "status": "Original parameter not restored",
                        }
                    )
            return {
                "success": False,
                "error": str(exc),
                "failed_index": index if phase == "edit" else None,
                "failed_phase": phase,
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
                "timings": timings,
            }
        except Exception as rollback_exc:
            return {
                "success": False,
                "error": str(exc),
                "rolled_back": False,
                "rollback_error": str(rollback_exc),
                "timings": timings,
            }


def run(doc, action, options):
    started = perf_counter()
    handlers = {
        "inspect": inspect_document,
        "measure": measure_objects,
        "check": check_document,
        "update": update_parameters,
    }
    try:
        result = handlers[action](doc, **options)
        result.setdefault("success", True)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
    result["total_ms"] = (perf_counter() - started) * 1000
    return result
