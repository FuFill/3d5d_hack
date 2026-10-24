"""Exercise the bundled shell's real HTTP/MCP -> worker -> Qt -> STE engine.

Uses original organizer case 3, an isolated profile, and no LLM/network request.
Session credentials and raw projects stay in ignored .runtime.
"""
import hashlib
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugin"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing-project", action="store_true", help="Load/move a real model, then apply to that same object")
    args = parser.parse_args()
    folder = ROOT / ".runtime" / ("assistant_check_" + uuid.uuid4().hex[:10])
    folder.mkdir(parents=True)
    connection, done = folder / "connection.json", folder / "done"
    job = {"commands": [{"command": "assistant_start", "connection_file": str(connection),
                          "done_file": str(done), "snapshot_dir": str(folder), "timeout_seconds": 300},
                         {"command": "state"}, {"command": "undo"}, {"command": "state"},
                         {"command": "redo"}, {"command": "state"}, {"command": "quit"}]}
    (folder / "job.json").write_text(json.dumps(job), encoding="utf-8")
    report = {"scope": "native shell integration, original organizer STL/TZ, no physical qualification",
              "checks": {}, "physical_print_approval": False}
    log = open(folder / "launcher.log", "w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(ROOT / "plugin/tools/launch_ste_runtime.py"),
                                "--assistant", "--job", str(folder / "job.json"), "--timeout", "380"],
                               stdout=log, stderr=subprocess.STDOUT)
    try:
        start = time.monotonic()
        while not connection.exists():
            errors = list(folder.glob("*_assistant_start.json"))
            if errors:
                raise RuntimeError(errors[0].read_text(encoding="utf-8"))
            if process.poll() is not None or time.monotonic() - start > 90:
                raise RuntimeError("STE assistant did not start: %s" % folder)
            time.sleep(0.25)
        session = json.loads(connection.read_text(encoding="utf-8"))
        base = "http://127.0.0.1:%s" % session["port"]

        def http(path, body=None, authenticated=True):
            headers = {"X-Token": session["token"]} if authenticated else {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            request = Request(base + path, data=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers)
            with urlopen(request, timeout=220) as response:
                return json.loads(response.read().decode("utf-8"))

        def call(name, arguments, expect_ok=True):
            reply = http("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})
            if "error" in reply:
                raise RuntimeError(reply["error"])
            result = reply["result"]
            if bool(result.get("isError")) == expect_ok:
                raise RuntimeError("%s: %s" % (name, result["structuredContent"]))
            return result["structuredContent"]

        try:
            http("/api/state", authenticated=False)
            raise AssertionError("Missing token was accepted")
        except HTTPError as exc:
            assert exc.code == 401
        report["checks"]["http_authentication"] = "pass"
        state = http("/api/state")
        assert "api_key" not in state["settings"]
        report["checks"]["api_key_not_exposed"] = "pass"
        # Actual HTTP question/answer card roundtrip; this is a software test answer.
        answers = {}
        def ask():
            answers.update(call("ask_user", {"question": "Какой материал доступен для этого задания?",
                                            "options": ["Материал текущего проекта", "Указать другой"]}))
        question_thread = threading.Thread(target=ask, daemon=True)
        question_thread.start()
        for _ in range(100):
            pending = [event for event in http("/api/state")["events"]
                       if event["type"] == "question" and event.get("status") == "pending"]
            if pending:
                break
            time.sleep(0.05)
        assert pending, "Question card was not published"
        http("/api/answer", {"seq": pending[-1]["seq"], "text": "Материал текущего проекта", "choice": 0})
        question_thread.join(5)
        assert answers.get("answer") == "Материал текущего проекта"
        report["checks"]["question_card_http_roundtrip"] = "pass"
        if state["settings"].get("has_key"):
            test = http("/api/test", {})
            assert test["ok"], "Native Timeweb connection test failed: %s" % test.get("error")
            report["checks"]["native_timeweb_qwen_connection"] = "pass"
        from ste_planner.requirements import read_docx_text
        case = next((ROOT / "data/examples").glob("3.*"))
        stl = next(p for p in case.iterdir() if p.suffix.lower() == ".stl")
        docx = next(case.glob("*.docx"))
        text = read_docx_text(str(docx))
        upload_request = Request(base + "/api/upload?name=" + quote(stl.name), data=stl.read_bytes(),
                                 headers={"X-Token": session["token"], "Content-Type": "application/octet-stream"})
        with urlopen(upload_request, timeout=30) as response:
            upload = json.load(response)
        sources = call("list_model_sources", {})
        assert any(item["attachment_id"] == upload["id"] and item["can_plan_original_stl"]
                   for item in sources["attachments"])
        spec_request = Request(base + "/api/upload?name=" + quote(docx.name), data=docx.read_bytes(),
                               headers={"X-Token": session["token"], "Content-Type": "application/octet-stream"})
        with urlopen(spec_request, timeout=30) as response:
            spec = json.load(response)
        assert spec["kind"] == "spec" and spec["chars"] == len(text)
        report["checks"]["original_docx_upload"] = "pass"
        plan_args = {"spec_attachment_id": spec["id"]}
        before_object = None
        if args.existing_project:
            source = next(item for item in sources["attachments"] if item["attachment_id"] == upload["id"])
            call("load_model", {"path": source["source_path"]})
            obj = call("list_objects", {})["objects"][0]
            call("move_object", {"id": obj["id"], "x": 20.0, "y": 15.0, "z": 0.0})
            call("rotate_object", {"id": obj["id"], "axis": "z", "angle": 17.0})
            time.sleep(2)
            before_object = call("list_objects", {})["objects"][0]
            plan_args["object_id"] = before_object["id"]
            report["checks"]["project_position_and_rotation_edits"] = "pass"
        else:
            plan_args["attachment_id"] = upload["id"]
        plan = call("plan_print", plan_args)
        report["checks"]["chat_upload_id_to_original_stl"] = "pass"
        report.update(organizer_case=case.name, geometry_sha256=hashlib.sha256(stl.read_bytes()).hexdigest(),
                      planner_status=plan["status"], route=plan["route"], decision_sha256=plan["decision_sha256"])
        assert plan["geometry_sha256"] == report["geometry_sha256"]
        assert plan["route"]["mode"] == "classic"
        report["checks"]["original_source_binding"] = "pass"
        call("apply_plan", {"plan_id": plan["plan_id"]})
        time.sleep(4)  # Native asynchronous settings/scene propagation, not a model simulation.
        readback = call("verify_plan", {"plan_id": plan["plan_id"]})
        (folder / "readback.json").write_text(json.dumps(readback, ensure_ascii=False, indent=2), encoding="utf-8")
        assert readback["status"] == "pass", readback
        report["checks"]["settled_readback"] = "pass"
        assert plan["base_preset"]["native_preset"]
        report["base_preset"] = plan["base_preset"]
        report["checks"]["native_preset_base_readback"] = "pass"
        if before_object is not None:
            objects = call("list_objects", {})["objects"]
            assert len(objects) == 1 and objects[0]["id"] == before_object["id"]
            snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("snapshot_*.json"))]
            assert snapshots[3]["history_count"] == snapshots[2]["history_count"] + 1
            assert snapshots[3]["scene_object_ids"] == snapshots[2]["scene_object_ids"]
            report["checks"]["same_scene_object_one_undo_operation"] = "pass"
        sliced = call("slice", {})
        report["slice"] = sliced
        assert sliced["print_time_s"] > 0 and sliced["material_g"] > 0
        report["checks"]["native_engine_slice"] = "pass"
        call("export_gcode", {"path": str(folder / "result.gcode")})
        call("save_project", {"path": str(folder / "project.3mf")})
        import zipfile
        assert zipfile.is_zipfile(folder / "project.3mf")
        assert (folder / "result.gcode").stat().st_size > 1000
        report["checks"]["native_gcode_and_3mf"] = "pass"
        sources = call("list_model_sources", {})
        source = next(item for item in sources["scene_models"] if item["can_plan_original_stl"])
        scene_plan = call("plan_print", {"task_text": text, "object_id": source["object_id"]})
        assert scene_plan["effective_settings"] == plan["effective_settings"]
        assert scene_plan["geometry_sha256"] == plan["geometry_sha256"]
        report["checks"]["scene_object_id_to_original_stl"] = "pass"
        # Original source validation: a made-up plan id cannot mutate a project.
        call("apply_plan", {"plan_id": "not-an-application-owned-id"}, expect_ok=False)
        report["checks"]["rejects_foreign_plan"] = "pass"
        done.touch()
        assert process.wait(timeout=40) == 0
        states = [json.loads((folder / ("%03d_state.json" % index)).read_text(encoding="utf-8")) for index in (2, 4, 6)]
        assert states[0]["nodes"] and states[2]["nodes"]
        assert states[0]["actual"] == states[2]["actual"]
        assert states[0]["nodes"] == states[2]["nodes"]
        if before_object is None:
            assert not states[1]["nodes"] and states[0]["history_count"] == 1
        else:
            assert states[1]["nodes"] and states[1]["nodes"] != states[0]["nodes"]
        report["checks"]["single_operation_undo_redo"] = "pass"
        report["status"] = "pass"
    except Exception as exc:
        report.update(status="fail", error="%s: %s" % (type(exc).__name__, exc))
        done.touch()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.terminate()
    finally:
        log.close()
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    print("Local artifacts: %s" % folder)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
