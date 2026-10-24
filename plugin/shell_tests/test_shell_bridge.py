"""Process-boundary regressions on the seven original organizer parts.

These compare transport with the unchanged planner, not with measured print
quality. Native Qt/slice/export/Undo are checked by check_ai_assistant_runtime.py.
"""
import ast
import json
from pathlib import Path
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
sys.path.insert(0, str(ROOT / "plugin"))
sys.path.insert(0, str(ROOT / "plugin/tests"))
from agent.core_bridge import CoreBridge
from agent.core_worker import calculate, digest
from ste_planner import planner
from ste_planner.requirements import read_docx_text
from testutil import example_cases


def bridge():
    value = CoreBridge(None, lambda: {"worker_python": sys.executable})
    value._snapshot = lambda: {"signature": "unchanged-project", "machine_id": planner.DEFAULT_MACHINE,
                               "resources_dir": planner.default_resources_dir()}
    return value


@pytest.mark.parametrize("case", example_cases(), ids=lambda case: case[0].split(".")[0])
def test_real_organizer_process_matches_canonical_core(case):
    _, docx, stl = case
    text = read_docx_text(docx)
    value = bridge()
    summary = value.plan({"task_text": text, "mesh_path": stl})
    transported = value._plans[summary["plan_id"]]["decision"]
    expected = planner.plan_case(text, stl, source="AI Assistant")
    assert transported == json.loads(json.dumps(expected))
    assert summary["physical_print_approval"] is False
    assert summary["geometry_sha256"] == digest(stl)
    # Returned summaries cannot alter the application-owned decision.
    summary["effective_settings"]["layer_height"] = 999
    assert transported["effective_settings"]["layer_height"] != 999


def test_cancelled_worker_cannot_publish_or_apply():
    _, docx, stl = example_cases()[3]
    value = bridge()
    with pytest.raises(ValueError, match="cancel"):
        value.plan({"task_text": read_docx_text(docx), "mesh_path": stl}, {"cancelled": lambda: True})
    assert not value._plans
    assert value._process is None
    with pytest.raises(ValueError, match="plan_id"):
        value.apply({"plan_id": "cancelled"})


def test_changed_project_rejects_completed_worker_response():
    _, docx, stl = example_cases()[3]
    value = bridge()
    original = value._snapshot
    calls = []
    def snapshot():
        result = original()
        calls.append(1)
        result["signature"] = str(len(calls))
        return result
    value._snapshot = snapshot
    with pytest.raises(ValueError, match="Project changed"):
        value.plan({"task_text": read_docx_text(docx), "mesh_path": stl})
    assert not value._plans


def test_worker_rejects_wrong_source_hash_before_calculation():
    stl = example_cases()[0][2]
    with pytest.raises(ValueError, match="Source STL changed"):
        calculate({"mesh_path": stl, "geometry_sha256": "0" * 64})


def test_cancel_invalidates_existing_plan():
    value = bridge()
    value._plans["old"] = {"generation": 0}
    value.cancel()
    with pytest.raises(ValueError, match="superseded"):
        value.summary("old")


def test_only_application_owned_physical_input_keys(tmp_path):
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps({"noise_spec": {"id": "measured-noise"}}), encoding="utf-8")
    value = CoreBridge(None, lambda: {"planning_inputs_file": str(path)})
    options, provenance = value._inputs()
    assert options == {"noise_spec": {"id": "measured-noise"}}
    assert provenance["sha256"] == digest(str(path))
    path.write_text(json.dumps({"setting_changes": {"layer_height": 999}}), encoding="utf-8")
    with pytest.raises(ValueError, match="allow only"):
        value._inputs()


def test_expired_qt_callback_cannot_mutate_later():
    from agent.mainthread import run_on_main, MainThreadTimeout
    queued, mutations, errors = [], [], []
    class App:
        def callLater(self, func):
            queued.append(func)
    def invoke():
        try:
            run_on_main(App(), lambda: mutations.append("mutated"), _timeout=0.01)
        except MainThreadTimeout:
            errors.append("timeout")
    thread = threading.Thread(target=invoke)
    thread.start()
    thread.join(1)
    assert errors == ["timeout"]
    queued[0]()
    assert not mutations


def test_all_shell_sources_parse_as_python35():
    for source in (ROOT / "AIAssistant").rglob("*.py"):
        ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source), feature_version=(3, 5))


def test_installed_bundle_core_is_byte_identical(tmp_path):
    sys.path.insert(0, str(ROOT / "plugin/tools"))
    from build_ai_assistant import build
    output = build(tmp_path / "AIAssistant")
    manifest = json.loads((output / "bundle_manifest.json").read_text(encoding="utf-8"))
    assert manifest["canonical_core_sha256"]
    for name, checksum in manifest["canonical_core_sha256"].items():
        assert digest(str(output / name)) == checksum
    assert not list(output.rglob(".env*"))
