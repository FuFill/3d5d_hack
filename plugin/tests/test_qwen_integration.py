# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
import io
import json
import os
import tempfile
from contextlib import contextmanager
from urllib.error import HTTPError
from testutil import assert_close, raises, make_box, resources_dir
from ste_planner.qwen_client import QwenClient, QwenError, load_env_file, tls_context
from ste_planner.qwen_assistant import PlanningAssistant
from ste_planner.llm_requirements import prepare_requirements
from ste_planner.causal_bridge import PLREffect, CausalRegistry


def call(name, args, ident="call1"):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@contextmanager
def box_model():
    """Real temporary STL, including bytes for the core's geometry hash."""
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "box.stl")
        with open(path, "w", encoding="ascii") as handle:
            handle.write("solid fixture\n")
            for triangle in make_box(10, 10, 10).triangles:
                handle.write("facet normal 0 0 0\nouter loop\n")
                for vertex in triangle:
                    handle.write("vertex %g %g %g\n" % tuple(vertex))
                handle.write("endloop\nendfacet\n")
            handle.write("endsolid fixture\n")
        yield path


class FakeClient(object):
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def complete(self, messages, tools=None, **kwargs):
        self.requests.append(json.loads(json.dumps(messages)))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def core_stub(text, mesh_path, **options):
    reqs, accepted, rejected = prepare_requirements(text, options.get("llm_items"))
    return {"status": "needs_calibration", "explanation": "Нужна калибровка.", "requirements": reqs.to_dict(),
            "effective_settings": {"material_print_temperature": 240, "machine_nozzle_size": 0.4},
            "chosen_material": {"id": "test_sku"}, "route": {"mode": "classic"},
            "trace": [{"step": "inspect_machine", "machine": "test_machine"}],
            "verification": [{"status": "unknown"}], "setting_patch": {"payload": []}}


def model(reviewed=True):
    return PLREffect("test_model", "material_print_temperature", "degC", "tensile_strength", "MPa",
                     0.2, [0.1, 0.3], [230, 260],
                     {"machine_id": "test_machine", "material_id": "test_sku", "mode": "classic", "nozzle_mm": 0.4},
                     identification_reviewed=reviewed, support_reviewed=reviewed)


def test_timeweb_transport_and_secret_redaction():
    requests = []
    def transport(req, timeout):
        requests.append(req)
        assert timeout == 60
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "secret-value"}, "finish_reason": "stop"}]}).encode())
    client = QwenClient("secret-value", opener=transport)
    reply = client.complete([{"role": "user", "content": "ping"}])
    assert reply["content"] == "<redacted>"
    assert "secret-value" not in repr(client)
    assert requests[0].full_url == "https://api.timeweb.ai/v1/chat/completions"
    body = json.loads(requests[0].data.decode())
    assert body["model"] == "dashscope/qwen3.8-flash" and body["stream"] is False
    assert body["enable_thinking"] is False
    raises(QwenError, QwenClient, "x", base_url="http://example.com")
    raises(QwenError, QwenClient, "x", base_url="https://user:password@example.com")


def test_api_failure_does_not_echo_provider_error_or_key():
    def transport(req, timeout):
        raise HTTPError(req.full_url, 401, "secret-value", {}, io.BytesIO(b"secret-value"))
    client = QwenClient("secret-value", opener=transport)
    try:
        client.complete([])
    except QwenError as exc:
        assert exc.code == "authentication" and "secret-value" not in str(exc)
    else:
        raise AssertionError("authentication error expected")


def test_truncated_or_malformed_response_rejected():
    def transport(req, timeout):
        return io.BytesIO(b'{"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}')
    raises(QwenError, QwenClient("key", opener=transport).complete, [])


def test_llm_cannot_use_raw_value_bypass_or_wrong_units():
    text = "Наименование детали: X\nМасса не более 50 г."
    rs, accepted, rejected = prepare_requirements(text, [
        {"property": "part_mass", "fragment": "Масса не более 50 г.", "value": 80, "unit": "g", "comparator": "<=", "value_as_written": 50},
        {"property": "part_mass", "fragment": "Масса не более 50 г.", "value": 50, "unit": "MPa", "comparator": "<="},
        {"property": "part_mass", "fragment": "Масса не более 50 г.", "value": 0.05, "unit": "kg", "comparator": "<="}])
    assert accepted == [2] and len(rejected) == 2
    assert_close(rs.first("part_mass").value, 50)
    assert rs.first("part_mass").unit == "g"


def test_unknown_properties_nonfinite_and_optional_requirements():
    text = "Желательно наличие ESD."
    rs, accepted, rejected = prepare_requirements(text, [
        {"property": "esd", "fragment": text, "value": True, "mandatory": True},
        {"property": "tensile_strength", "fragment": text, "value": float("nan"), "unit": "MPa", "comparator": ">="},
        {"property": "arbitrary_python", "fragment": text, "value": True}])
    assert len(rejected) >= 2
    assert all(not r.mandatory for r in rs.by_property("esd"))


def test_llm_cannot_invert_bound_or_relabel_quantity_as_proven():
    text = "Масса не более 50 г.\nДавление до 5 MPa."
    rs, accepted, rejected = prepare_requirements(text, [
        {"property": "part_mass", "fragment": "Масса не более 50 г.", "value": 50, "unit": "g", "comparator": ">="},
        {"property": "tensile_strength", "fragment": "Давление до 5 MPa.", "value": 5, "unit": "MPa", "comparator": "<="}])
    assert len(rejected) == 1
    relabeled = rs.first("tensile_strength")
    assert relabeled.interpretation_status == "assumed" and not relabeled.mandatory


def test_existing_env_alias_and_tls_verification():
    import ssl
    client = QwenClient.from_env(environ={"LLM_API_KEY": "test-only", "LLM_BASE_URL": "https://api.timeweb.ai/v1",
                                         "LLM_MODEL": "test-model"})
    assert client.model == "test-model" and "test-only" not in repr(client)
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_doubleml_adapter_rejects_other_estimands():
    class DoubleMLIRM(object):
        pass
    raises(ValueError, PLREffect.from_fitted_doubleml, DoubleMLIRM())


def test_doubleml_plr_export_contract():
    import numpy
    class Interval(object):
        values = numpy.array([[0.1, 0.3]])
        index = ["material_print_temperature"]
    class DoubleMLPLR(object):
        coef = numpy.array([0.2])
        params_names = ["ml_l", "ml_m"]
        def confint(self, level):
            assert level == 0.95
            return Interval()
    metadata = {"model_id": "contract_fixture_only", "treatment": "material_print_temperature",
                "treatment_unit": "degC", "outcome": "tensile_strength", "outcome_unit": "MPa",
                "treatment_bounds": [230, 260], "context": model().context,
                "identification_reviewed": True, "support_reviewed": True}
    exported = PLREffect.from_fitted_doubleml(DoubleMLPLR(), **metadata)
    assert_close(exported.estimate(240, 250, metadata["context"])["effect"], 2)
    restored = PLREffect(**json.loads(json.dumps(exported.to_record(), allow_nan=False)))
    assert restored.to_record() == exported.to_record()
    metadata["treatment"] = "infill_sparse_density"
    raises(ValueError, PLREffect.from_fitted_doubleml, DoubleMLPLR(), **metadata)


def test_malformed_tool_envelope_has_deterministic_fallback():
    fake = FakeClient([{"tool_calls": [{"id": "bad", "type": "function", "function": []}]}])
    result = PlanningAssistant(fake, plan_fn=core_stub).run("Тест", "x.stl")
    assert result["llm_error"]["code"] == "protocol" and result["decision"]


def test_causal_contrast_and_reversed_interval():
    m = model()
    result = m.estimate(250, 240, m.context)
    assert result["status"] == "computed"
    assert_close(result["effect"], -2)
    assert result["confidence_interval"] == [-3, -1]
    assert not result["absolute_prediction"] and not result["setting_approval"]
    assert m.estimate(240, 300, m.context)["status"] == "out_of_domain"
    assert m.estimate(240, 250, dict(m.context, mode="cylindrical"))["status"] == "out_of_domain"
    assert model(False).estimate(240, 250, m.context)["status"] == "unknown"
    assert CausalRegistry().estimate("absent", "x", "y", 0, 1, {})["status"] == "needs_calibration"
    assert CausalRegistry([m]).estimate(m.model_id, m.treatment, m.outcome, 240, 250,
                                        m.context, treatment_unit="K")["status"] == "invalid_input"


def test_agent_tool_roundtrip_and_authoritative_core_status():
    client = FakeClient([
        {"tool_calls": [call("propose_requirements", {"items": []})]},
        {"tool_calls": [call("plan_print", {}, "plan")]},
        {"tool_calls": [call("estimate_setting_effect", {"model_id": "test_model", "treatment": "material_print_temperature",
                                                         "outcome": "tensile_strength", "to_value": 250}, "effect")]},
        {"content": "Все требования выполнены, qualified!"}])
    registry = CausalRegistry([model()])
    result = PlanningAssistant(client, registry, core_stub).run("Материал должен быть прочным.", "caller-model.stl")
    assert result["decision"]["status"] == "needs_calibration"
    assert result["explanation"] == "Нужна калибровка."
    assert not result["assistant_text_verified"]
    assert result["causal_estimates"][0]["effect"] == 2
    assert result["llm_tool_calls"] == 3
    assert client.requests[-1][-1]["role"] == "tool"


def test_agent_rejects_injected_paths_and_context():
    client = FakeClient([
        {"tool_calls": [call("plan_print", {"mesh_path": "other.stl"})]},
        {"tool_calls": [call("execute_python", {"code": "arbitrary"})]},
        {"content": "OK"}])
    result = PlanningAssistant(client, plan_fn=core_stub).run("Тест", "selected.stl")
    assert all(e["result"]["status"] == "invalid_input" for e in result["events"])
    assert result["decision"]["status"] == "needs_calibration"


def test_agent_fallback_on_api_error_or_budget():
    result = PlanningAssistant(FakeClient([QwenError("network", "Unavailable")]), plan_fn=core_stub).run("Тест", "x.stl")
    assert result["llm_error"]["code"] == "network" and result["decision"]
    loop = FakeClient([{"tool_calls": [call("list_causal_models", {})]}] * 2)
    result = PlanningAssistant(loop, plan_fn=core_stub, max_rounds=2).run("Тест", "x.stl")
    assert result["llm_error"]["code"] == "round_limit"


def test_planner_accepts_grounded_llm_input_before_validation():
    from ste_planner import planner
    with box_model() as path:
        decision = planner.plan_case("Наименование детали: X\nМасса не более 50 г.", path,
                                     resources_dir=resources_dir(), llm_items=[
                                         {"property": "part_mass", "fragment": "Масса не более 50 г.",
                                          "value": 50, "unit": "g", "comparator": "<="}])
    added = [r for r in decision["requirements"] if r["property"] == "part_mass"]
    assert len(added) == 1 and added[0]["value"] == 50
    assert any(t["step"] == "merge_llm_requirements" for t in decision["trace"])
