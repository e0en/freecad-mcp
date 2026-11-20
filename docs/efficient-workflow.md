# Efficient FreeCAD MCP workflow

Four tools provide project-independent queries and scalar edits on any open
FreeCAD document. They require no project paths, naming conventions, VarSet, or
additional workbench. A VarSet is recommended for new parametric designs; existing
primitive, sketch dimension properties and feature properties remain editable
where exposed as supported scalar properties. Sketch geometry/constraint-list
changes and feature construction continue through `execute_code`.

## Install and discover

Update the MCP server checkout and copy `addon/FreeCADMCP` to the FreeCAD addon
location described in the repository README. Restart FreeCAD to load addon changes,
then restart/reconnect the MCP client to refresh its tool catalog. Both sides must
be updated. An older addon will reject the new RPC method rather than silently
falling back to a slower or nontransactional edit.

The new tools return compact JSON text, without screenshots. Use existing
`get_view` with `focus_object` for visual inspection at useful milestones. Numeric
checks do not replace reviewing sections, flow paths and assembly views.

## Inspect and measure

```json
{"doc_name":"MyDocument","limit":20,"include_property_names":true}
```

Call `inspect_document` and follow `next_offset` until null. Use internal object
`name`, not its editable `label`, for all requests. Property names are optional;
request only relevant values. Optional `type_ids` (exact FreeCAD TypeIds) and
`name_pattern` (case-sensitive glob on internal Names) filter before pagination.
For example, `{"doc_name":"MyDocument","type_ids":["PartDesign::Body"],"name_pattern":"Leg*"}`
finds matching Bodies without paging through sketches and historical features.
Filters combine with explicit `objects`; `total` and `next_offset` describe the
filtered set. Unknown explicit Names still produce an error.

Request selected values:

```json
{"doc_name":"MyDocument","objects":["Dimensions"],"properties":["Width","Wall"]}
```

Quantities return a numeric `value` in FreeCAD internal units and their dimensional
`unit`. Length is mm, angle is degrees. String values are unchanged. No query
recomputes a document, changes visibility, exports files or creates a mesh.
`pending_recompute` identifies stale document objects.

`measure_objects` accepts 1–200 explicit object Names:

```json
{"doc_name":"MyDocument","objects":["Housing","Rotor"]}
```

It returns per-object volume (mm³), area (mm²), axis-aligned bounds/size (mm), and
solid/face/edge counts. With exactly two objects it also returns minimum shape
distance (mm). Measurement responses also report document errors and pending
recompute without modifying the document. Measurements account for enclosing App::Part placements. Select
final Bodies or solid objects; selecting a Body and its Tip duplicates geometry.
Meshes, arbitrary workbench objects without Shape, and face/edge selections are
not supported by this tool. Assembly-specific Link transforms and scaled instances
have not been validated; verify them before relying on these measurements.

## Batch parameter changes

`update_parameters` performs 1–200 existing scalar property/quantity or expression
edits in one GUI-thread RPC request, followed by one final document recompute.
It never saves the file.

```json
{
  "doc_name":"MyDocument",
  "changes":[
    {"object":"Dimensions","property":"Width","value":"32 mm"},
    {"object":"Dimensions","property":"Wall","value":"2 mm"}
  ]
}
```

Use `expression` instead of `value` to set a formula. Replacing an existing formula
with a literal requires `"expression":null` together with `value`; formulas are
never silently discarded. Hidden/read-only, nonscalar and duplicate properties
are rejected. This API does not change feature topology or create objects.

The document must have UndoMode enabled and no pending user transaction edits.
Preflight validates the entire batch before opening a transaction. Assignment,
recompute or non-null shape validity failures abort it and recompute the restored
document. Successful edits form one undo entry. Failure responses identify the
phase/index where available and `rolled_back`; parameter values/expressions and
document errors are checked after abort. A rollback failure is explicit and must
be investigated before retrying. A transport timeout is not proof of rollback:
use `get_rpc_status`, wait for the GUI operation to finish, and inspect state.

FreeCAD document transactions cannot reverse files, network calls, or other
external effects in custom FeaturePython callbacks. Restrict those callbacks
accordingly. Null construction shapes are permitted during edits; run final-part
checks explicitly. Document edits/recompute must never use `execute_code_async`.

Responses include edited values/expressions, `edit_ms`, `recompute_ms`,
`validation_ms`, and `total_ms`. Total time includes preflight and result handling;
it excludes MCP transport and model reasoning. Validation inspects non-null shapes
throughout the document, so large history trees may spend more time in validation
than in the edits. These timings expose that cost rather than promising faster
geometry operations.

## Quick checks and final validation

```json
{"doc_name":"MyDocument","objects":["Housing","Rotor","MountSketch"],"collisions":true}
```

`check_document` checks document errors, pending recompute, valid single solids,
fully constrained selected sketches, and optional pairwise volumetric overlaps.
`success` means the query executed; `passed` is the check verdict. Unsupported
objects are marked skipped and cannot produce a pass. Explicit final-part
selection avoids confusing historical features or reference geometry with parts.
Hidden objects are not automatically excluded. No printer, material, clearance,
fastener or physical requirement is invented.

Overlap tolerance is in mm³ (default 1e-6), not a linear clearance. Touching faces
are permitted. To require separation, pass `minimum_clearance_mm` with a positive
value. Equality passes; touching and intersecting solids fail positive clearance.
This checks nominal CAD geometry, without adding a manufacturing tolerance.

```json
{"doc_name":"MyDocument","objects":["Housing","Rotor"],"collisions":true,"minimum_clearance_mm":0.3,"details":"failures","max_details":10}
```

All selected objects and eligible shape pairs are checked before output is reduced:

- `details: "all"` (default) includes passing and failing object rows.
- `details: "failures"` includes failed/skipped object rows and pair violations.
- `details: "summary"` returns counts and the verdict without detail rows.

`max_details` caps each detail list at 0–200 rows (default 200), including document
errors and pending objects. `summary` always gives complete counts. `omitted`
counts all unreturned entries, including passing object rows excluded by failure
mode. Truncation cannot turn a failure into a pass. Use explicit smaller selections
to investigate omitted pairs. Null or empty selections for checks and measurements
are rejected; only inspection can default to all objects.

Bounding-box separation and overlap volume bound the expensive distance/Boolean
calculations. Pairs already meeting the clearance and pairs whose overlap cannot
exceed the volume tolerance skip the corresponding exact calculation. Overlapping
bounds alone do not imply a collision or inadequate clearance. `boolean_tests`,
`distance_tests`, pair counts and `timings` report the actual work. Pairwise tests grow
quadratically; keep the selection focused. A quick subset check does not certify
the whole assembly. Run project-specific full assembly, motion, insertion/tool
path, parameter regression, mesh and slicing checks before releasing outputs.
These tools do not implement dependency-based test selection or cached evidence.

## Validation and performance evidence

`tests/test_workflow.py` covers preflight, transaction policy, rollback reporting,
compact transport and stale-state handling without FreeCAD. To exercise geometry,
run the following on FreeCAD's GUI thread through `execute_code` with screenshots
disabled (replace the checkout path):

```python
import importlib.util
import json
spec = importlib.util.spec_from_file_location(
    "workflow_validation", "/path/to/freecad-mcp/tests/freecad_workflow_integration.py"
)
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
print(json.dumps(tests.run()))
```

The suite creates and closes its own disposable document and restores the active
document. It checks VarSet expressions, PartDesign solids, nested placements,
sketches, overlap detection, one-step undo and real failed-edit/recompute recovery.
It compares compact identities against legacy full object serialization on that
fixture; the two responses intentionally contain different levels of detail.
Byte counts are not token counts. In-process timings are single samples and do
not establish end-to-end speedup or performance on a large assembly. Actual token
usage and task completion latency need measurement in representative agent sessions.

For an end-to-end smoke test through an actual MCP client and XML-RPC dispatcher,
run `uv run python examples/workflow_smoke.py` with the updated addon running.
The script creates a disposable document, calls all four registered tools, verifies
that a failed batch is an MCP tool error carrying rollback details, checks restored
values, and closes the test document. MCP SDK 1.x and 2.x are supported.

Local validation records are in [workflow-validation.json](workflow-validation.json),
[workflow-smoke.json](workflow-smoke.json) (SDK 1.x) and
[workflow-smoke-mcp2.json](workflow-smoke-mcp2.json) (SDK 2.x).
The extended clearance/filter/summary geometry checks and a reproducible
80-part pair-loop comparison are in `tests/freecad_workflow_efficiency.py`; run
its `run()` through the same GUI-thread loader above. Results are recorded in
[workflow-efficiency.json](workflow-efficiency.json). The synthetic contact fixture
checks agreement with the earlier pair-loop implementation. Timings are single
samples, and response byte reduction is not an actual token measurement.

The final default test suite passed 92 tests. Ruff passes for the new workflow,
wrappers and tests. The existing RPC dispatcher file retains two unused imports
(`validate_allowed_ips`, `save_settings`); both findings reproduce on its unchanged
HEAD version. Existing file formatting outside the edited ranges was preserved.

## Runtime diagnostics

Use `inspect_sketch` with `doc_name`, `object_name`, and optional `offset`/`limit`
to read constraints, stored driving flags, attachment targets, source/consumer
Names, expressions, and current solver status. Follow `next_offset` until null.
Geometric constraints are included without calling the datum-only `getDriving`
API. Attachment property differences are handled in the addon. This query does
not solve the sketch or validate stale geometry.

`inspect_document` includes property types and `unit` for requested properties.
A null unit means no physical unit is exposed, including `App::PropertyFloat`;
a geometric label alone does not make a float a length. Use `update_parameters`
for expression edits: unit errors and cyclic references are failures, with
rollback status returned. It uses FreeCAD's expression engine, not a parallel
parser or a promise that arbitrary Python edits are transactional.

`get_report_view(max_chars=20000)` returns the retained Report view tail and
`total_chars`/`omitted_chars`. It preserves warnings, tracebacks, and surrounding
output without clearing the view or assigning guessed severity. An unavailable
view is an error, while an empty view is a successful empty result. The log is
session history, not a current model verdict or a persistent log file.

Report retrieval needs the GUI thread. After timeouts, first use
`get_rpc_status`; inspect the log and document once dispatch is available.
A timeout does not cancel a running operation. Existing dispatch tests cover
busy/stuck/completed states. `tests/freecad_diagnostics_integration.py` exposes
`run()` for GUI-thread tests of the actual sketch APIs, units, expression/cycle
rollback, and Report view. It creates and closes its own disposable document.
