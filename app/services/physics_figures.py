"""Physics figures drawn by code from a structured description: ray diagrams, refraction, magnetic fields.

Like math_figures, the AI supplies numbers and this module checks and draws them, so positions follow the
real lens / mirror formula and Snell's law. check_figure() gives a plain reason when a figure cannot be
drawn; the caller then leaves a labelled placeholder instead of failing the paper.

A figure for a question never shows what the student must find: rays and the image are drawn only when
show_rays / show_image / show_path are set (answer keys and explanations), not by default.
"""
import math
from io import BytesIO
from typing import Any, Dict, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

PHYSICS_FIGURE_TYPES = ("ray_diagram", "refraction", "magnetic_field")
ELEMENTS = ("convex_lens", "concave_lens", "concave_mirror", "convex_mirror", "plane_mirror")
_BLUE, _RED, _GREEN = "#1565c0", "#c62828", "#2e7d32"


def _png(fig) -> bytes:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _pos(value, high) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= high


# ------------------------------------------------------------------ ray diagrams

def image_info(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Image position, magnification and nature from the lens or mirror formula (Cartesian sign convention).
    The object is on the left at distance object_distance; the lens or mirror is at x = 0.
    Returns None when the image forms at infinity."""
    element, d = spec["element"], float(spec["object_distance"])
    h = float(spec.get("object_height", 2))
    u = -d
    if element == "plane_mirror":
        v, m, real = d, 1.0, False
    else:
        focal = abs(float(spec["focal_length"]))
        if element == "convex_lens":
            f = focal
            denom = u + f
            if abs(denom) < 1e-9:
                return None
            v = f * u / denom
            m = v / u
            real = v > 0
        elif element == "concave_lens":
            f = -focal
            v = f * u / (u + f)
            m = v / u
            real = False
        elif element == "concave_mirror":
            f = -focal
            denom = u - f
            if abs(denom) < 1e-9:
                return None
            v = f * u / denom
            m = -v / u
            real = v < 0
        else:  # convex_mirror
            f = focal
            v = f * u / (u - f)
            m = -v / u
            real = False
    return {"v": v, "m": m, "height": m * h, "real": real, "upright": m > 0}


def _check_ray(spec) -> Optional[str]:
    if spec.get("element") not in ELEMENTS:
        return "element must be one of: " + ", ".join(ELEMENTS)
    if not _pos(spec.get("object_distance"), 500):
        return "object_distance must be a positive number up to 500"
    if spec["element"] != "plane_mirror" and not _pos(spec.get("focal_length"), 200):
        return "focal_length must be a positive number up to 200 (its sign comes from the element)"
    if spec.get("object_height") is not None and not _pos(spec["object_height"], 50):
        return "object_height must be a positive number up to 50"
    unit = spec.get("unit", "cm")
    if not isinstance(unit, str) or len(unit) > 4:
        return "unit must be a short text such as cm"
    return None


def _render_ray(spec) -> bytes:
    element = spec["element"]
    d, h = float(spec["object_distance"]), float(spec.get("object_height", 2))
    focal = abs(float(spec.get("focal_length", d)))
    unit = spec.get("unit", "cm")
    info = image_info(spec)
    is_lens = element.endswith("lens")
    show_rays, show_image = bool(spec.get("show_rays")), bool(spec.get("show_image"))
    reach = max(d, 2.2 * focal if element != "plane_mirror" else d, abs(info["v"]) if info else 0) * 1.25
    ih = abs(info["height"]) if info else 0
    half = max(h, ih) * 1.9
    fig, ax = plt.subplots(figsize=(8.4, 4.2))
    ax.set_xlim(-reach, reach); ax.set_ylim(-half, half)
    ax.axis("off")
    ax.annotate("", xy=(reach, 0), xytext=(-reach, 0), arrowprops=dict(arrowstyle="-", color="black", lw=1.1))

    sag = reach * 0.05
    if is_lens:
        convex = element == "convex_lens"
        ax.plot([0, 0], [-half * 0.85, half * 0.85], color="black", lw=2)
        delta = half * 0.1
        for sign in (1, -1):
            tip = sign * half * 0.85
            heads = dict(arrowstyle="-|>", color="black", lw=2, mutation_scale=16)
            if convex:      # arrow heads point outwards
                ax.annotate("", xy=(0, tip), xytext=(0, tip - sign * delta), arrowprops=heads)
            else:           # arrow heads point inwards
                ax.annotate("", xy=(0, tip - sign * delta), xytext=(0, tip), arrowprops=heads)
    else:
        L = half * 0.85
        ys = np.linspace(-L, L, 60)
        curve = {"concave_mirror": -1.0, "convex_mirror": 1.0}.get(element, 0.0)   # edges bend towards / away from the object
        ax.plot(curve * sag * (ys / L) ** 2, ys, color="black", lw=2.4)
        for y in np.linspace(-L, L, 14):
            x0 = curve * sag * (y / L) ** 2
            ax.plot([x0, x0 + reach * 0.03], [y, y - half * 0.05], color="gray", lw=0.8)

    if spec.get("mark_points", True) and element != "plane_mirror":
        if is_lens:
            pts = [(-2 * focal, "2F"), (-focal, "F"), (focal, "F"), (2 * focal, "2F")]
            ax.text(0, -half * 0.12, "O", ha="center", va="top", fontsize=11, weight="bold")
        elif element == "concave_mirror":
            pts = [(-focal, "F"), (-2 * focal, "C")]
            ax.text(0, -half * 0.12, "P", ha="center", va="top", fontsize=11, weight="bold")
        else:
            pts = [(focal, "F"), (2 * focal, "C")]
            ax.text(0, -half * 0.12, "P", ha="center", va="top", fontsize=11, weight="bold")
        for x, name in pts:
            if abs(x) <= reach:
                ax.plot(x, 0, "o", color="black", ms=4)
                ax.text(x, -half * 0.12, name, ha="center", va="top", fontsize=11, weight="bold")

    ax.annotate("", xy=(-d, h), xytext=(-d, 0), arrowprops=dict(arrowstyle="-|>", color=_BLUE, lw=2.4))
    ax.text(-d, h + half * 0.05, "Object", ha="center", va="bottom", fontsize=10, color=_BLUE)
    ax.annotate("", xy=(-d, -half * 0.55), xytext=(0, -half * 0.55), arrowprops=dict(arrowstyle="<->", color="gray", lw=0.9))
    ax.text(-d / 2, -half * 0.6, f"{d:g} {unit}", ha="center", va="top", fontsize=9, color="gray")

    def ray(p, q, dashed=False):
        ax.plot([p[0], q[0]], [p[1], q[1]], color=_GREEN, lw=1.6, ls="--" if dashed else "-")

    def beyond(p, direction, x_end):
        t = (x_end - p[0]) / direction[0]
        return (x_end, p[1] + t * direction[1])

    if show_rays:
        T = (-d, h)
        if element == "plane_mirror":
            for y1 in (0.0, h * 0.5):
                ray(T, (0, y1))
                ray((0, y1), beyond((0, y1), (-d, y1 - h), -reach))   # reflected: the x direction flips
                ray((0, y1), (d, h), dashed=True)                     # appears to come from the image tip
        else:
            ray(T, (0, h)); ray(T, (0, 0))
            if is_lens:
                if element == "convex_lens":
                    ray((0, h), beyond((0, h), (focal, -h), reach))   # parallel ray goes through the far focus
                else:
                    ray((0, h), beyond((0, h), (focal, h), reach)); ray((0, h), (-focal, 0), dashed=True)
                ray((0, 0), beyond((0, 0), (d, -h), reach))          # the ray through the centre is undeviated
            else:
                if element == "concave_mirror":
                    ray((0, h), beyond((0, h), (-focal, -h), -reach))   # parallel ray reflects through F
                else:
                    ray((0, h), beyond((0, h), (-focal, h), -reach)); ray((0, h), (focal, 0), dashed=True)
                ray((0, 0), beyond((0, 0), (-d, -h), -reach))        # the ray to the pole reflects symmetrically
            if info and not info["real"]:
                ray((0, h), (info["v"], info["height"]), dashed=True); ray((0, 0), (info["v"], info["height"]), dashed=True)
    if show_image and info:
        x, ht = info["v"], info["height"]
        ax.annotate("", xy=(x, ht), xytext=(x, 0), arrowprops=dict(arrowstyle="-|>", color=_RED, lw=2.4, ls="-" if info["real"] else "--"))
        ax.text(x, ht + (half * 0.05 if ht > 0 else -half * 0.05), "Image", ha="center", va="bottom" if ht > 0 else "top", fontsize=10, color=_RED)
    if spec.get("title"):
        ax.set_title(str(spec["title"]), fontsize=11)
    return _png(fig)


# ------------------------------------------------------------------ refraction

def _unit(v):
    v = np.array(v, dtype=float)
    return v / (np.linalg.norm(v) or 1.0)


def _rotate(v, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1]])


def _refract(d, normal, n1, n2):
    """Vector Snell's law. `normal` is the unit normal pointing in the direction the ray travels across the surface.
    Returns the refracted unit direction, or None on total internal reflection."""
    eta = n1 / n2
    cos_i = float(np.dot(d, normal))
    k = 1 - eta * eta * (1 - cos_i * cos_i)
    if k < 0:
        return None
    return _unit(eta * d + (math.sqrt(k) - eta * cos_i) * normal)


def _line_hit(p, d, a, b):
    """Parameter s along segment a-b and t along the ray p + t d, or None when parallel."""
    m = np.array([[d[0], a[0] - b[0]], [d[1], a[1] - b[1]]])
    if abs(np.linalg.det(m)) < 1e-12:
        return None
    t, s = np.linalg.solve(m, np.array([a[0] - p[0], a[1] - p[1]]))
    return t, s


def _prism_path(spec):
    """Geometry of a ray through a prism: dict of points and directions, or a plain-text reason string."""
    A, i, n = float(spec["prism_angle"]), float(spec["angle_of_incidence"]), float(spec["n"])
    H = 3.0
    half_base = H * math.tan(math.radians(A / 2))
    apex, bl, br = np.array([0.0, H]), np.array([-half_base, 0.0]), np.array([half_base, 0.0])
    centroid = np.array([0.0, H / 3])
    face_l = (bl, apex)
    face_r = (br, apex)

    def out_normal(face):
        a, b = face
        t = _unit(b - a)
        nrm = np.array([-t[1], t[0]])
        return nrm if np.dot((a + b) / 2 - centroid, nrm) > 0 else -nrm

    n_l, n_r = out_normal(face_l), out_normal(face_r)
    p1 = bl + 0.4 * (apex - bl)
    for sign in (1, -1):
        d_in = _unit(_rotate(-n_l, sign * i))
        inside = _refract(d_in, -n_l, 1.0, n)
        hit = _line_hit(p1, inside, *face_r) if inside is not None else None
        if hit is None or hit[0] <= 1e-9 or not 0.02 <= hit[1] <= 0.98:
            continue
        p2 = br + hit[1] * (apex - br)
        out = _refract(inside, n_r, n, 1.0)
        if out is None:
            return "the ray undergoes total internal reflection at the second face; lower the angle or the apex angle"
        return {"apex": apex, "bl": bl, "br": br, "p1": p1, "p2": p2, "d_in": d_in, "inside": inside, "out": out,
                "n_l": n_l, "n_r": n_r, "A": A, "i": i}
    return "the ray does not cross the prism with these angles"


def _check_refraction(spec) -> Optional[str]:
    kind = spec.get("kind")
    if kind not in ("slab", "prism"):
        return "kind must be slab or prism"
    i, n = spec.get("angle_of_incidence"), spec.get("n")
    if not _pos(i, 89) or not _pos(n, 3) or n <= 1:
        return "angle_of_incidence must be between 0 and 89 degrees and n must be above 1 (up to 3)"
    if kind == "prism":
        if not _pos(spec.get("prism_angle"), 120) or spec["prism_angle"] < 20:
            return "prism_angle must be between 20 and 120 degrees"
        result = _prism_path(spec)
        if isinstance(result, str):
            return result
    return None


def _arc_label(ax, centre, start_deg, end_deg, text, r=0.7, color="black"):
    lo, hi = sorted((start_deg, end_deg))
    ax.add_patch(mpatches.Arc(centre, 2 * r, 2 * r, theta1=lo, theta2=hi, color=color, lw=1.3))
    mid = math.radians((lo + hi) / 2)
    ax.text(centre[0] + (r + 0.32) * math.cos(mid), centre[1] + (r + 0.32) * math.sin(mid), text, ha="center", va="center", fontsize=10, color=color)


def _render_slab(spec) -> bytes:
    i, n = float(spec["angle_of_incidence"]), float(spec["n"])
    show = spec.get("show_path", True)
    W, T = 7.0, 2.6
    fig, ax = plt.subplots(figsize=(6.4, 5.0))
    ax.set_aspect("equal"); ax.axis("off")
    ax.add_patch(mpatches.Rectangle((-W / 2, -T / 2), W, T, fc="#bbdefb", ec="#1565c0", lw=1.6, alpha=0.6))
    ax.text(W / 2 - 0.2, -T / 2 + 0.25, f"n = {n:g}", ha="right", fontsize=10, color="#1565c0")
    p1 = np.array([-0.8, T / 2])
    d_in = np.array([math.sin(math.radians(i)), -math.cos(math.radians(i))])
    start = p1 - 3.0 * d_in
    ax.plot([start[0], p1[0]], [start[1], p1[1]], color=_GREEN, lw=1.8)
    ax.plot([p1[0], p1[0]], [p1[1] - 1.2, p1[1] + 1.6], "k--", lw=0.9)
    _arc_label(ax, p1, 90, 90 + i, f"i = {i:g}°", r=0.9)
    if show:
        d_r = _refract(d_in, np.array([0.0, -1.0]), 1.0, n)
        r_deg = math.degrees(math.asin(math.sin(math.radians(i)) / n))
        p2 = p1 + d_r * (T / abs(d_r[1]))
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color=_GREEN, lw=1.8)
        ax.plot([p2[0], p2[0]], [p2[1] - 1.6, p2[1] + 1.2], "k--", lw=0.9)
        end = p2 + 3.0 * d_in
        ax.plot([p2[0], end[0]], [p2[1], end[1]], color=_GREEN, lw=1.8)
        _arc_label(ax, p1, -90, -90 + r_deg, "r", r=0.8)
        _arc_label(ax, p2, -90, -90 + i, "e", r=0.9)
    ax.set_xlim(-W / 2 - 0.5, W / 2 + 0.5); ax.set_ylim(-T / 2 - 2.6, T / 2 + 3.2)
    ax.text(-W / 2 + 0.2, T / 2 + 0.25, "Air", fontsize=10); ax.text(-W / 2 + 0.2, -T / 2 - 0.45, "Air", fontsize=10)
    return _png(fig)


def _render_prism(spec) -> bytes:
    path = _prism_path(spec)
    show = spec.get("show_path", True)
    fig, ax = plt.subplots(figsize=(6.4, 5.0))
    ax.set_aspect("equal"); ax.axis("off")
    ax.add_patch(mpatches.Polygon([path["bl"], path["br"], path["apex"]], closed=True, fc="#bbdefb", ec="#1565c0", lw=1.6, alpha=0.6))
    p1, p2 = path["p1"], path["p2"]
    start = p1 - 3.2 * path["d_in"]
    ax.plot([start[0], p1[0]], [start[1], p1[1]], color=_GREEN, lw=1.8)
    nl = path["n_l"]
    ax.plot([p1[0] - nl[0] * 1.3, p1[0] + nl[0] * 1.3], [p1[1] - nl[1] * 1.3, p1[1] + nl[1] * 1.3], "k--", lw=0.9)
    ang_n = math.degrees(math.atan2(nl[1], nl[0]))
    ang_r = math.degrees(math.atan2(-path["d_in"][1], -path["d_in"][0]))
    _arc_label(ax, p1, ang_n, ang_r if abs(ang_r - ang_n) < 180 else ang_r - 360 * np.sign(ang_r - ang_n), f"i = {path['i']:g}°", r=0.9)
    ax.text(path["apex"][0], path["apex"][1] + 0.25, f"A = {path['A']:g}°", ha="center", fontsize=10)
    if show:
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]], color=_GREEN, lw=1.8)
        nr = path["n_r"]
        ax.plot([p2[0] - nr[0] * 1.0, p2[0] + nr[0] * 1.6], [p2[1] - nr[1] * 1.0, p2[1] + nr[1] * 1.6], "k--", lw=0.9)
        end = p2 + 3.2 * path["out"]
        ax.plot([p2[0], end[0]], [p2[1], end[1]], color=_GREEN, lw=1.8)
        ang_n2 = math.degrees(math.atan2(nr[1], nr[0]))
        ang_e = math.degrees(math.atan2(path["out"][1], path["out"][0]))
        _arc_label(ax, p2, ang_n2, ang_e if abs(ang_e - ang_n2) < 180 else ang_e - 360 * np.sign(ang_e - ang_n2), "e", r=0.9)
    pts = np.array([path["bl"], path["br"], path["apex"], start, p2 + 3.2 * path["out"]])
    ax.set_xlim(pts[:, 0].min() - 0.8, pts[:, 0].max() + 0.8); ax.set_ylim(min(pts[:, 1].min(), -0.5) - 0.5, pts[:, 1].max() + 0.8)
    return _png(fig)


# ------------------------------------------------------------------ magnetic fields

def _check_magnetic(spec) -> Optional[str]:
    kind = spec.get("kind")
    if kind not in ("bar_magnet", "solenoid", "straight_wire"):
        return "kind must be bar_magnet, solenoid or straight_wire"
    if spec.get("north", "left") not in ("left", "right"):
        return "north must be left or right"
    if spec.get("direction", "out") not in ("out", "in"):
        return "direction must be out or in"
    return None


def _render_dipole(spec) -> bytes:
    """Field lines of a bar magnet or solenoid: the dipole curves r = r0 sin^2(theta), drawn as closed loops
    that leave the N end and return to the S end."""
    kind = spec["kind"]
    north_left = spec.get("north", "left") == "left"
    L, w = 2.0, 0.55
    flip = 1.0 if north_left else -1.0
    fig, ax = plt.subplots(figsize=(8, 4.8))
    ax.set_aspect("equal"); ax.axis("off")
    grey = "#555555"
    north, south = np.array([-flip * 1.5, 0.0]), np.array([flip * 1.5, 0.0])

    def field(p):
        total = np.zeros(2)
        for pole, charge in ((north, 1.0), (south, -1.0)):
            r = p - pole
            total += charge * r / (np.linalg.norm(r) ** 3 + 1e-12)
        return total

    def trace(start):
        p, pts = np.array(start, dtype=float), [np.array(start, dtype=float)]
        for _ in range(3500):
            b = field(p)
            mid = p + 0.5 * 0.04 * b / (np.linalg.norm(b) + 1e-12)
            b2 = field(mid)
            p = p + 0.04 * b2 / (np.linalg.norm(b2) + 1e-12)
            pts.append(p.copy())
            if np.linalg.norm(p - south) < 0.1 or np.linalg.norm(p) > 12:
                break
        return np.array(pts)

    angles = [100, 112, 124, 134, 142]
    for angle in angles + [360 - a for a in angles]:          # upper and lower loops; the axis line is drawn below
        a_rad = math.radians(angle)
        direction = np.array([math.cos(a_rad), math.sin(a_rad)]) * -flip
        line = trace(north + 0.12 * direction)
        ax.plot(line[:, 0], line[:, 1], color=grey, lw=1.1)
        k = len(line) // 2
        ax.annotate("", xy=line[k + 1], xytext=line[k], arrowprops=dict(arrowstyle="-|>", color=grey, lw=1.1, mutation_scale=13))
    for sx in (-1.0, 1.0):                                   # the lines along the axis, outside the magnet
        xs = np.linspace(L + 0.05, 6.4, 2) * sx
        ax.plot(xs, [0, 0], color=grey, lw=1.1)
        mid = 4.7 * sx
        direction = -sx if (sx > 0) == north_left else sx   # towards S, away from N
        ax.annotate("", xy=(mid + direction * 0.05, 0), xytext=(mid - direction * 0.05, 0),
                    arrowprops=dict(arrowstyle="-|>", color=grey, lw=1.1, mutation_scale=13))
    if kind == "bar_magnet":
        left_c, right_c = ("#e53935", "#1e88e5") if north_left else ("#1e88e5", "#e53935")
        ax.add_patch(mpatches.Rectangle((-L, -w), L, 2 * w, fc=left_c, ec="black", lw=1.4, zorder=3))
        ax.add_patch(mpatches.Rectangle((0, -w), L, 2 * w, fc=right_c, ec="black", lw=1.4, zorder=3))
        ax.text(-L / 2, 0, "N" if north_left else "S", ha="center", va="center", fontsize=16, color="white", weight="bold", zorder=4)
        ax.text(L / 2, 0, "S" if north_left else "N", ha="center", va="center", fontsize=16, color="white", weight="bold", zorder=4)
    else:
        ax.add_patch(mpatches.Rectangle((-L, -w), 2 * L, 2 * w, fc="#fff3e0", ec="black", lw=1.4, zorder=3))
        for x in np.linspace(-L + 0.15, L - 0.15, 11):
            ax.plot([x - 0.08, x + 0.08], [-w, w], color="#b26a00", lw=2, zorder=4)
        inside = -flip                                       # inside the coil the field runs from S towards N
        for yy in (-0.2, 0.0, 0.2):
            ax.annotate("", xy=(inside * 1.2, yy), xytext=(-inside * 1.2, yy),
                        arrowprops=dict(arrowstyle="-|>", color=grey, lw=1.1, mutation_scale=12), zorder=5)
        ax.text(0, -w - 0.3, "Solenoid", ha="center", fontsize=10, zorder=6, bbox=dict(fc="white", ec="none", pad=1.5))
    ax.set_xlim(-6.6, 6.6); ax.set_ylim(-4.1, 4.1)
    return _png(fig)


def _render_wire(spec) -> bytes:
    out = spec.get("direction", "out") == "out"
    fig, ax = plt.subplots(figsize=(4.8, 4.8))
    ax.set_aspect("equal"); ax.axis("off")
    for radius in (0.9, 1.6, 2.3):
        ax.add_patch(mpatches.Circle((0, 0), radius, fill=False, ec="#444444", lw=1.3))
        theta = math.radians(60)
        # field is anticlockwise when the current comes out of the page, clockwise when it goes in
        step = 0.12 if out else -0.12
        p = (radius * math.cos(theta), radius * math.sin(theta))
        q = (radius * math.cos(theta + step), radius * math.sin(theta + step))
        ax.annotate("", xy=q, xytext=p, arrowprops=dict(arrowstyle="-|>", color="#444444", lw=1.3, mutation_scale=14))
    ax.add_patch(mpatches.Circle((0, 0), 0.22, fc="white", ec="black", lw=1.6))
    if out:
        ax.plot(0, 0, "o", color="black", ms=4)
    else:
        ax.plot([-0.11, 0.11], [-0.11, 0.11], color="black", lw=1.6); ax.plot([-0.11, 0.11], [0.11, -0.11], color="black", lw=1.6)
    ax.text(0.32, -0.38, "I", fontsize=12, weight="bold")
    ax.set_xlim(-2.8, 2.8); ax.set_ylim(-2.8, 2.8)
    return _png(fig)


# ------------------------------------------------------------------ entry points

_CHECKS = {"ray_diagram": _check_ray, "refraction": _check_refraction, "magnetic_field": _check_magnetic}


def check_figure(spec: Dict[str, Any]) -> Optional[str]:
    """None when the figure can be drawn; otherwise a short reason."""
    check = _CHECKS.get(str(spec.get("type")))
    if not check:
        return None
    try:
        return check(spec)
    except Exception as exc:
        return f"figure description could not be read ({type(exc).__name__})"


def draw_figure(spec: Dict[str, Any]) -> bytes:
    reason = check_figure(spec)
    if reason:
        raise ValueError(reason)
    kind = spec["type"]
    if kind == "ray_diagram":
        return _render_ray(spec)
    if kind == "refraction":
        return _render_slab(spec) if spec["kind"] == "slab" else _render_prism(spec)
    return _render_wire(spec) if spec["kind"] == "straight_wire" else _render_dipole(spec)
