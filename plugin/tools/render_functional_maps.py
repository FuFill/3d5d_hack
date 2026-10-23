"""Render a saved functional Decision; no recomputation, physical claims or GUI dependencies."""

import argparse
import json

import numpy as np


def render(decision, output_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import cm, colors
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    value = decision["functional_analysis"]["value"]
    selected, maps = value["selected"], value["maps"]
    if not selected or not maps:
        raise ValueError("Decision has no selected functional map")
    case = maps["cases"][0]
    points = np.asarray(maps["nodes_mm"]).dot(np.asarray(selected["rotation"]).T) + selected["translation"]
    displacement = np.linalg.norm(np.asarray(case["nodal_displacement_mm"]), axis=1)
    polygons, energies, displacements, failure = [], [], [], []
    for owner, face in zip(maps["boundary_element_ids"], maps["boundary_faces"]):
        if len(face) == 3:
            triangles = [face]
        else:
            a, b, c, ab, bc, ca = face
            triangles = [[a, ab, ca], [ab, b, bc], [ca, bc, c], [ab, bc, ca]]
        for tri in triangles:
            polygons.append(points[tri])
            energies.append(case["element_compliance_density_MPa"][owner])
            displacements.append(float(displacement[tri].mean()))
            failure.append(case["element_failure_index"][owner])
    fig = plt.figure(figsize=(15, 5.8), facecolor="#f6f8fb")
    fig.suptitle("Карты выбранного варианта после объёмного расчёта", fontsize=17, y=0.98)
    fig.text(0.5, 0.90, "Профиль: %s · нагружение: %s · %d конечных элементов · свойства — программный эталон" %
             (selected["profile_id"], case["id"], len(maps["elements"])), ha="center", color="#475569", fontsize=10)
    lo, hi = points.min(axis=0), points.max(axis=0)
    size = hi - lo
    fields = [(energies, "inferno", "Вклад в податливость χ", "МПа"),
              (displacements, "viridis", "Перемещения |u|", "мм"),
              (failure, "RdYlGn_r", "Индекс повреждения FI", "доля предела материала")]
    for index, (field, cmap_name, title, unit) in enumerate(fields, 1):
        axis = fig.add_subplot(1, 3, index, projection="3d", facecolor="#f6f8fb")
        # A fixed physical FI scale keeps a safe part green instead of stretching
        # every plot's own maximum to the failure colour.
        upper = max(1.0, max(field)) if index == 3 else (max(field) or 1)
        norm = colors.Normalize(0, upper)
        cmap = matplotlib.colormaps[cmap_name]
        axis.add_collection3d(Poly3DCollection(polygons, facecolors=cmap(norm(field)), edgecolor="none"))
        axis.set_xlim(lo[0], hi[0])
        axis.set_ylim(lo[1], hi[1])
        axis.set_zlim(lo[2], hi[2])
        axis.set_box_aspect(np.maximum(size, 1) / max(size))
        axis.view_init(elev=24, azim=-65)
        axis.set_axis_off()
        axis.set_title(title, fontsize=12, pad=0)
        bar = fig.colorbar(cm.ScalarMappable(norm=norm, cmap=cmap), ax=axis, orientation="horizontal",
                           fraction=0.04, pad=-0.02, shrink=0.8)
        bar.set_ticks(np.linspace(0, norm.vmax, 4))
        bar.ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%.3g"))
        bar.set_label(unit, fontsize=9)
    fig.text(0.5, 0.05, "Карта энергии показывает вклад в податливость при данном нагружении.\n"
             "Расчётные поля не заменяют испытания. Ra и плоскостность без измерений не определены.",
             ha="center", color="#475569", fontsize=10, linespacing=1.5)
    fig.subplots_adjust(left=0.02, right=0.98, bottom=0.22, top=0.82, wspace=0.03)
    fig.savefig(str(output_path), dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decision")
    parser.add_argument("output")
    args = parser.parse_args()
    with open(args.decision, encoding="utf-8") as f:
        render(json.load(f), args.output)
