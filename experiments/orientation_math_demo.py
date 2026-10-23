"""Analytical orientation demonstration; illustrative properties, no STL/FEA claim.

Run from the repository root: python experiments/orientation_math_demo.py
NumPy is required; Matplotlib produces the surface-map illustration.
This experiment does not change the production planner or provide material data.
"""

import json
import math
from pathlib import Path

import numpy as np


def cantilever(length, width, height, force, modulus, strength):
    """Euler-Bernoulli bending of a solid rectangular beam under one tip force."""
    values = (length, width, height, force, modulus, strength)
    if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("finite positive dimensions, force and printed properties required")
    inertia = width * height ** 3 / 12.0
    displacement = force * length ** 3 / (3 * modulus * inertia)
    stress = force * length * height / (2 * inertia)
    # Independent energy evaluation: Gaussian integration of the volume field.
    # Three-point quadrature integrates these polynomial fields exactly.
    nodes, weights = np.polynomial.legendre.leggauss(3)
    x = (nodes + 1) * length / 2
    z = nodes * height / 2
    sigma = force * (length - x[:, None]) * z[None, :] / inertia
    compliance_integral = float(np.sum(weights[:, None] * weights[None, :] * sigma ** 2 / modulus)
                                * length / 2 * height / 2 * width)
    return {
        "I_mm4": inertia,
        "tip_deflection_mm": displacement,
        "effective_stiffness_N_per_mm": force / displacement,
        "sigma_max_MPa": stress,
        "failure_index_max": stress / strength,
        "safety_factor_min": strength / stress,
        "compliance_Nmm": force * displacement,
        "compliance_volume_integral_Nmm": compliance_integral,
        "strain_energy_Nmm": compliance_integral / 2,
        "compliance_density_max_MPa": stress ** 2 / modulus,
    }


def verify_math():
    """Exercise equilibrium/energy identities and physical scaling, not printing evidence."""
    args = (100.0, 20.0, 10.0, 20.0, 2000.0, 40.0)
    baseline = cantilever(*args)
    across = cantilever(100.0, 20.0, 10.0, 20.0, 800.0, 12.0)
    double_force = cantilever(100.0, 20.0, 10.0, 40.0, 2000.0, 40.0)
    double_modulus = cantilever(100.0, 20.0, 10.0, 20.0, 4000.0, 40.0)

    def close(a, b):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)

    checks = {
        "energy_equals_force_times_displacement": close(baseline["compliance_volume_integral_Nmm"],
                                                       baseline["compliance_Nmm"]),
        "XY_tip_2_mm_Z_tip_5_mm": close(baseline["tip_deflection_mm"], 2.0)
                                    and close(across["tip_deflection_mm"], 5.0),
        "both_directions_sigma_6_MPa": close(baseline["sigma_max_MPa"], 6.0)
                                       and close(across["sigma_max_MPa"], 6.0),
        "double_force_quadruples_compliance": close(double_force["compliance_Nmm"],
                                                    4 * baseline["compliance_Nmm"]),
        "double_force_doubles_displacement_and_FI": close(double_force["tip_deflection_mm"],
                                                          2 * baseline["tip_deflection_mm"])
                                                      and close(double_force["failure_index_max"],
                                                                2 * baseline["failure_index_max"]),
        "double_E_halves_displacement_and_compliance": close(double_modulus["tip_deflection_mm"],
                                                             baseline["tip_deflection_mm"] / 2)
                                                         and close(double_modulus["compliance_Nmm"],
                                                                   baseline["compliance_Nmm"] / 2),
    }
    if not all(checks.values()):
        raise AssertionError(checks)
    return baseline, across, checks


def surface_patches(length, width, height):
    """Subdivide the six faces of the same beam for surface field colouring."""
    polygons = []
    ranges = (np.linspace(0, length, 51), np.linspace(-width / 2, width / 2, 9),
              np.linspace(-height / 2, height / 2, 9))
    for fixed in range(3):
        first, second = [i for i in range(3) if i != fixed]
        for boundary in (ranges[fixed][0], ranges[fixed][-1]):
            for i in range(len(ranges[first]) - 1):
                for j in range(len(ranges[second]) - 1):
                    points = []
                    for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1)):
                        point = [0.0, 0.0, 0.0]
                        point[fixed] = boundary
                        point[first] = ranges[first][i + di]
                        point[second] = ranges[second][j + dj]
                        points.append(point)
                    polygons.append(points)
    return np.asarray(polygons)


def render_maps(path, result):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import cm, colors
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    length, width, height, force, modulus, strength = 100.0, 20.0, 10.0, 20.0, 2000.0, 40.0
    patches = surface_patches(length, width, height)
    centers = patches.mean(axis=1)
    x, z = centers[:, 0], centers[:, 2]
    inertia = result["I_mm4"]
    sigma = -force * (length - x) * z / inertia
    energy_density_twice = sigma ** 2 / modulus
    deflection = force * x ** 2 * (3 * length - x) / (6 * modulus * inertia)
    axial = -z * force * x * (2 * length - x) / (2 * modulus * inertia)
    displacement = np.hypot(deflection, axial)
    utilization = 2.0 * np.abs(sigma) / strength

    fig = plt.figure(figsize=(15, 5.9), facecolor="#f6f8fb")
    fig.suptitle("Карты на одной детали: податливость, перемещения, прочность", fontsize=17, y=0.98)
    fig.text(0.5, 0.89, "Аналитическая консоль 100 × 20 × 10 мм; F = 20 Н; условные свойства XY: E = 2000 МПа, X = 40 МПа",
             ha="center", fontsize=10.5, color="#485466")
    fields = [
        (energy_density_twice, result["compliance_density_max_MPa"], "inferno",
         "Вклад в податливость χ = σ : ε", "χ, МПа = Н/мм²", "Усиливать прежде всего возле зажима"),
        (displacement, float(displacement.max()), "viridis",
         "Перемещения |u|", "мм", "Максимальный прогиб — у свободного конца"),
        (utilization, 1.0, "RdYlGn_r", "Использование прочности: 2 × FI",
         "доля допуска при требуемом запасе 2", "Максимум 0,30 < 1; расчётный запас 6,67"),
    ]
    for index, (field, upper, cmap_name, title, unit, caption) in enumerate(fields, 1):
        axis = fig.add_subplot(1, 3, index, projection="3d", facecolor="#f6f8fb")
        norm = colors.Normalize(0, upper)
        cmap = matplotlib.colormaps[cmap_name]
        collection = Poly3DCollection(patches, facecolors=cmap(norm(field)), edgecolor="none", shade=False)
        axis.add_collection3d(collection)
        axis.set_xlim(-3, length + 4)
        axis.set_ylim(-width / 2 - 3, width / 2 + 3)
        axis.set_zlim(-height / 2 - 4, height / 2 + 4)
        axis.set_box_aspect((3.5, 1.1, 0.8))
        axis.view_init(elev=26, azim=-65)
        axis.set_axis_off()
        axis.set_title(title, fontsize=12, pad=-5)
        clamp = np.asarray([[0, -width / 2 - 2, -height / 2 - 2],
                            [0, width / 2 + 2, -height / 2 - 2],
                            [0, width / 2 + 2, height / 2 + 2],
                            [0, -width / 2 - 2, height / 2 + 2]])
        axis.add_collection3d(Poly3DCollection([clamp], facecolor="#607080", alpha=0.35))
        axis.quiver(length, 0, height / 2 + 4, 0, 0, -8, color="#225ea8", arrow_length_ratio=0.3)
        axis.text(length, 0, height / 2 + 5, "F", color="#225ea8", fontsize=10)
        bar = fig.colorbar(cm.ScalarMappable(norm=norm, cmap=cmap), ax=axis,
                           orientation="horizontal", fraction=0.04, pad=-0.01, shrink=0.8)
        bar.set_label(unit, fontsize=9)
        bar.ax.tick_params(labelsize=8)
        bar.set_ticks(np.linspace(0, upper, 4))
        bar.ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%.3g"))
        fig.text((index - 0.5) / 3, 0.115, caption, ha="center", fontsize=9.2)
    fig.text(0.5, 0.025, "Высокая энергия не означает низкий модуль материала. Карты относятся к одному нагружению.\n"
             "Иллюстрация на сплошной балке; без заполнения, теплового коробления и расчёта реального STL.",
             ha="center", fontsize=10, color="#485466", linespacing=1.6)
    fig.subplots_adjust(left=0.015, right=0.985, bottom=0.23, top=0.82, wspace=0.03)
    fig.savefig(path, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)


def main():
    root = Path(__file__).resolve().parent.parent
    baseline, across, checks = verify_math()
    report = {
        "source": "analytical_software_demonstration",
        "physical_test": False,
        "model": "solid_rectangular_Euler_Bernoulli_cantilever",
        "inputs": {"length_mm": 100, "width_mm": 20, "height_mm": 10, "tip_force_N": 20,
                   "illustrative_E_XY_MPa": 2000, "illustrative_E_Z_MPa": 800,
                   "illustrative_strength_XY_MPa": 40, "illustrative_strength_Z_MPa": 12},
        "load_along_layer_plane": baseline,
        "load_across_layer_interfaces": across,
        "checks": checks,
        "limitations": ["illustrative printed properties, not material calibration", "solid beam, no infill model",
                        "small deflections, bending only, no shear or clamp concentration",
                        "not a general STL finite-element solver or a surface roughness prediction"],
    }
    report_path = root / "experiments" / "orientation_math_results.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    figure_path = root / "data" / "research" / "figures" / "functional_maps.png"
    render_maps(figure_path, baseline)
    print("Mathematical checks: {}/{} passed".format(sum(checks.values()), len(checks)))
    print(str(report_path))
    print(str(figure_path))


if __name__ == "__main__":
    main()
