# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Material comparison: when the material is not fixed, several are prepared and the user picks one.

The core ranks the feasible materials of the part (material_options of plan_print; a
mandatory requirement that fails excludes a material). The best COUNT of them, without the
ones the user wants to replace, are each planned with their own STE preset and settings and
estimated before slicing (estimate.py: print time and material, marked "≈"). Nothing in the
open project changes while they are compared. A choice card shows them; the chosen plan is
applied, and the answer is the user's consent to execute it. A free answer goes back to the
model unchanged.
"""

import re

from . import estimate as E
from .tools import ToolError
from .versions import duration_text

COUNT = 3
SOURCE_KEYS = ("task_text", "spec_attachment_id", "attachment_id", "object_id", "mode", "orientation", "llm_items")


def _words(text):
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _excluded(option, exclude):
    """"PLA" excludes every PLA, "Generic PLA" only that one (the words of the name are in the id)."""
    words = _words(option.get("id"))
    return any(name and _words(name) <= words for name in exclude)


class MaterialComparison:
    def __init__(self, registry):
        self._r = registry

    def run(self, args, ctx):
        r = self._r
        if r.questions is None:
            raise ToolError("The chat is needed to choose a material")
        source = dict((k, args[k]) for k in SOURCE_KEYS if args.get(k) not in (None, ""))
        if "object_id" in source:  # the part on the plate wins over a second source
            source.pop("attachment_id", None)
        with r.core.side_plans():  # trial plans; the chosen material is planned for real below
            base, variants, skipped = self._variants(source, args, ctx)
        return self._choose(source, base, variants, skipped, ctx)

    def _variants(self, source, args, ctx):
        r = self._r
        exclude = [str(x) for x in args.get("exclude") or [] if _words(x)]
        if args.get("replace_current"):  # "another material": the one in the printer is not offered
            state = r.call("get_state", {}, ctx)
            for extruder in ((state.get("result") or {}).get("extruders") or [])[:1]:
                exclude += [x for x in (extruder.get("material_id"), extruder.get("material")) if _words(x)]
        count = max(2, min(4, int(args.get("count") or COUNT)))
        base = self._plan(source, None, ctx)
        base_stats = self._estimate(base["plan_id"])  # now: a later plan supersedes this one
        options = [o for o in base.get("material_options") or [] if o.get("id") and not _excluded(o, exclude)]
        if not options:
            raise ToolError("No other feasible material for this task" if exclude else
                            "The core found no feasible material for this task")
        variants, skipped = [], []
        base_material = (base.get("chosen_material") or {}).get("id")
        for option in options:  # the next ones in the core's ranking fill in for a material that does not fit
            if len(variants) >= count:
                break
            if (ctx or {}).get("cancelled", lambda: False)():
                raise ToolError("Cancelled")
            plan = base if option["id"] == base_material else self._plan(source, option["id"], ctx, quiet=True)
            if plan is None or plan.get("status") in ("infeasible", "needs_input") or \
                    (plan.get("base_preset") or {}).get("status") == "needs_input":
                skipped.append({"material": option["id"], "reason": (plan or {}).get("status") or "plan failed"})
                continue
            stats = base_stats if plan is base else self._estimate(plan["plan_id"])
            variants.append({"material_id": option["id"], "material": self._material(plan) or option["id"],
                             "preset": (plan.get("base_preset") or {}).get("profile") or "",
                             "mode": (plan.get("route") or {}).get("mode"),
                             "plan_id": plan["plan_id"], "print_time_s": stats.get("print_time_s"),
                             "material_g": stats.get("material_g"), "estimate": True})
        if not variants:
            raise ToolError("No material could be prepared: %s" % skipped)
        return base, variants, skipped

    def _choose(self, source, base, variants, skipped, ctx):
        r = self._r
        ru = r.language() == "ru"
        labels = [self._label(v, i == 0, ru) for i, v in enumerate(variants)]
        if len(variants) == 1:
            # The answer is the consent to apply it: one material is asked about too.
            text = ("Подходит только один материал; время и расход — оценка до нарезки. Применить его?" if ru else
                    "Only one material fits; time and use are estimates before slicing. Apply it?")
            options = labels + ["Не применять" if ru else "Do not apply"]
        else:
            text = ("Какой материал печатаем? Подготовил %d варианта, у каждого свой пресет; время и расход — "
                    "оценка до нарезки." if ru else
                    "Which material do we print? %d variants are prepared, each with its preset; time and use are "
                    "estimates before slicing.") % len(variants)
            options = labels
        try:
            answers, _seq = r.questions.ask([{"text": text, "options": options, "allow_custom": True}],
                                            (ctx or {}).get("cancelled"))
        except Exception:
            raise ToolError("The user cancelled the choice")
        choice, answer = answers[0].get("choice"), answers[0].get("text")
        if len(variants) == 1 and choice == 1:
            return {"status": "declined", "variants": variants, "skipped": skipped,
                    "next_step": "The user chose not to apply the only feasible material. Nothing was applied; stay "
                                 "in planning and do not apply, slice or save in this turn."}
        if choice is None or not 0 <= choice < len(variants):
            return {"status": "user_answer", "answer": answer, "variants": variants, "skipped": skipped,
                    "next_step": "The user typed an own answer instead of a variant: follow it (e.g. plan_print with "
                                 "preferred_material) and ask nothing already answered."}
        chosen = variants[choice]
        # The current plan for the rest of the task (verify, revert): the chosen material, planned for real.
        final = self._plan(source, chosen["material_id"], ctx)
        applied = r.call("apply_plan", {"plan_id": final["plan_id"],
                                       "confirm_unverified_5d": chosen["mode"] != "classic"}, ctx)
        if not applied.get("ok") and self._other_models(applied):
            applied = r.call("apply_plan_to_project", {"plan_id": final["plan_id"]}, ctx)
        if not applied.get("ok"):
            raise ToolError("The chosen variant could not be applied: %s" % applied["result"].get("error"))
        summary = r.call("get_plan", {"plan_id": final["plan_id"]}, ctx)
        return {"status": "applied", "chosen": chosen, "plan_id": final["plan_id"], "variants": variants,
                "skipped": skipped, "plan": summary["result"] if summary.get("ok") else {"plan_id": final["plan_id"]},
                "next_step": "The user chose this variant in the card: that is the consent to execute it. The plan is "
                             "ALREADY APPLIED: do not call apply_plan again. Continue with verify_plan (this plan_id), "
                             "slice, export_gcode and save_project without asking again."}

    def _plan(self, source, material, ctx, quiet=False):
        request = dict(source)
        if material:
            request["preferred_material"] = material
        outcome = self._r.call("plan_print", request, ctx)
        if not outcome.get("ok"):
            if quiet:
                return None
            raise ToolError(outcome["result"].get("error") or "plan_print failed")
        return outcome["result"]

    def _material(self, plan):
        material = plan.get("chosen_material") or {}
        return material.get("label") or material.get("id")

    @staticmethod
    def _other_models(outcome):
        error = str((outcome.get("result") or {}).get("error") or "")
        return "other models" in error or "already contains models" in error or "apply_plan_to_project" in error

    def _estimate(self, plan_id):
        """Print time and material of a plan before slicing; {} when the core's record has no placement."""
        try:
            record = self._r.core.decision_record(plan_id)
            decision = record["decision"]
            material = (decision.get("chosen_material") or {}).get("id")
            return E.estimate(record["request"]["mesh_path"], decision.get("effective_settings") or {},
                              decision.get("orientation") or {}, material)
        except Exception:
            return {}

    @staticmethod
    def _label(variant, recommended, ru):
        parts = [variant["material"]]
        if variant.get("preset"):
            parts[0] += " · " + variant["preset"]
        approx = "≈" if variant.get("estimate") else ""
        numbers = [approx + duration_text(variant.get("print_time_s"), "ru" if ru else "en")
                   if variant.get("print_time_s") else ""]
        if variant.get("material_g"):
            numbers.append(approx + ("%s г" if ru else "%s g") % ("%.0f" % float(variant["material_g"])))
        numbers = [n for n in numbers if n] or ["время не посчитано" if ru else "time not estimated"]
        label = parts[0] + " — " + " · ".join(numbers)
        if variant.get("mode") not in (None, "classic"):
            # Choosing it accepts the 5D warning the application would otherwise ask about.
            label += (" · 5D: кинематика и столкновения не проверены" if ru else
                      " · 5D: kinematics and collisions unverified")
        if recommended:
            label += " (рекомендуется)" if ru else " (recommended)"
        return label
