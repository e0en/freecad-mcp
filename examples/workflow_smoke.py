"""Validate all four tools over MCP stdio against a running FreeCAD addon.

Run `uv run python examples/workflow_smoke.py` from the checkout. Uses a new,
unsaved disposable document; never opens, edits or saves an existing document.
"""

import asyncio
import json
import sys
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def is_error(result):
    # MCP 2.x exposes snake_case fields; 1.x uses protocol camelCase.
    return getattr(result, "is_error", False) or getattr(result, "isError", False)


async def run():
    name = "WorkflowSmoke_" + uuid4().hex
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from freecad_mcp.server import main; main()",
            "--host",
            "127.0.0.1",
        ],
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            expected = {
                "inspect_document",
                "measure_objects",
                "check_document",
                "update_parameters",
            }
            assert expected <= tools
            setup = f"""
import FreeCAD as App
_workflow_smoke_previous = App.ActiveDocument.Name if App.ActiveDocument else None
_workflow_smoke_doc = App.newDocument({name!r})
_workflow_smoke_params = _workflow_smoke_doc.addObject('App::VarSet', 'Params')
_workflow_smoke_params.addProperty('App::PropertyLength', 'Width')
_workflow_smoke_params.Width = 10
_workflow_smoke_box = _workflow_smoke_doc.addObject('Part::Box', 'Box')
_workflow_smoke_box.setExpression('Length', 'Params.Width')
_workflow_smoke_other = _workflow_smoke_doc.addObject('Part::Box', 'Other')
_workflow_smoke_other.Placement.Base.x = 30
_workflow_smoke_doc.recompute()
_workflow_smoke_doc.commitTransaction()
"""
            created = False

            async def query(tool, **options):
                result = await session.call_tool(tool, {"doc_name": name, **options})
                assert not is_error(result), result.content
                return json.loads(result.content[0].text)

            try:
                setup_result = await session.call_tool(
                    "execute_code", {"code": setup, "include_screenshot": False}
                )
                assert (
                    "Failed" not in setup_result.content[0].text
                ), setup_result.content
                created = True
                overview = await query("inspect_document", include_property_names=True)
                assert overview["total"] == 3
                filtered = await query(
                    "inspect_document", type_ids=["Part::Box"], name_pattern="B*"
                )
                assert (
                    filtered["total"] == 1 and filtered["objects"][0]["name"] == "Box"
                )
                edited = await query(
                    "update_parameters",
                    changes=[
                        {"object": "Params", "property": "Width", "value": "12 mm"},
                        {"object": "Other", "property": "Height", "value": "15 mm"},
                    ],
                )
                assert edited["success"] and not edited["saved"]
                measured = await query("measure_objects", objects=["Box", "Other"])
                assert measured["minimum_distance_mm"] == 18
                checked = await query(
                    "check_document", objects=["Box", "Other"], collisions=True
                )
                assert checked["passed"]
                brief = await query(
                    "check_document",
                    objects=["Box", "Other"],
                    minimum_clearance_mm=20,
                    details="summary",
                )
                assert (
                    not brief["passed"]
                    and brief["summary"]["clearance_violations"] == 1
                )
                assert (
                    not brief["clearance_violations"]
                    and brief["omitted"]["clearance_violations"] == 1
                )
                failures = await query(
                    "check_document",
                    objects=["Box", "Other"],
                    minimum_clearance_mm=20,
                    details="failures",
                    max_details=1,
                )
                assert failures["clearance_violations"][0]["distance_mm"] == 18
                failed = await session.call_tool(
                    "update_parameters",
                    {
                        "doc_name": name,
                        "changes": [
                            {"object": "Params", "property": "Width", "value": "22 mm"},
                            {
                                "object": "Other",
                                "property": "Height",
                                "value": "not a length",
                            },
                        ],
                    },
                )
                assert (
                    is_error(failed) and '"rolled_back":true' in failed.content[0].text
                )
                restored = await query(
                    "inspect_document", objects=["Params"], properties=["Width"]
                )
                assert (
                    restored["objects"][0]["properties"]["Width"]["value"]["value"]
                    == 12
                )
                print(
                    json.dumps(
                        {
                            "transport": "MCP stdio → XML-RPC → FreeCAD GUI thread",
                            "tools": sorted(expected),
                            "passed": True,
                            "rollback_tool_error_verified": True,
                            "filtered_queries_and_clearance_summary_verified": True,
                            "sample_update_timings": edited["timings"],
                        },
                        indent=2,
                    )
                )
            finally:
                if created:
                    cleanup = f"""
App.closeDocument({name!r})
if _workflow_smoke_previous and _workflow_smoke_previous in App.listDocuments():
    App.setActiveDocument(_workflow_smoke_previous)
"""
                    result = await session.call_tool(
                        "execute_code", {"code": cleanup, "include_screenshot": False}
                    )
                    assert "Failed" not in result.content[0].text, result.content


if __name__ == "__main__":
    asyncio.run(run())
