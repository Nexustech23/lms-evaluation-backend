"""Maths figures drawn by code from a structured description (never by the AI as a picture).

The AI writes the figure's data; this module checks it and draws it, so lengths, labels and shapes are exact.
Seven types: geometry_figure, bar_chart, pie_chart, histogram, ogive, number_line, coordinate_plane.
check_figure() returns a plain reason when a description cannot be drawn reliably; the caller then
shows a labelled placeholder for the faculty to fill in instead of failing the whole paper.
"""
import math
from io import BytesIO
from typing import Any, Dict, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

FIGURE_TYPES = ("geometry_figure", "bar_chart", "pie_chart", "histogram", "ogive", "number_line", "coordinate_plane")
_COLORS = ["#1565c0", "#c62828", "#2e7d32", "#6a1b9a", "#e65100", "#00838f"]
_LIMIT = 1000.0


def _png(fig) -> bytes:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and abs(value) <= _LIMIT * 100


def _pair(value) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == 2 and all(_num(v) for v in value)


def _text(value, limit=40) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def _numlist(value, low=1, high=40) -> bool:
    return isinstance(value, list) and low <= len(value) <= high and all(_num(v) for v in value)


def _segment_ends(item):
    """A segment is [A, B] or {"from": A, "to": B, "dashed": true}."""
    if isinstance(item, (list, tuple)) and len(item) == 2:
        return item[0], item[1], False
    if isinstance(item, dict):
        return item.get("from"), item.get("to"), bool(item.get("dashed"))
    return None, None, False


# ------------------------------------------------------------------ checks

def _check_geometry(spec) -> Optional[str]:
    points = spec.get("points")
    if not isinstance(points, dict) or not 1 <= len(points) <= 15:
        return "points must list 1 to 15 named points"
    for name, xy in points.items():
        if not _text(name, 3) or not _pair(xy):
            return f"point {name!r} needs a short name and [x, y] numbers"
    names = set(points)
    segments = spec.get("segments") or []
    if not isinstance(segments, list) or len(segments) > 30:
        return "segments must be a list of at most 30"
    for item in segments:
        a, b, _ = _segment_ends(item)
        if a not in names or b not in names or a == b:
            return f"segment {item!r} must join two different listed points"
    circles = spec.get("circles") or []
    if not isinstance(circles, list) or len(circles) > 4:
        return "circles must be a list of at most 4"
    for circle in circles:
        if not isinstance(circle, dict) or circle.get("center") not in names or not _num(circle.get("radius")) or circle["radius"] <= 0:
            return "each circle needs a listed center point and a positive radius"
    labels = spec.get("labels") or []
    if not isinstance(labels, list) or len(labels) > 20:
        return "labels must be a list of at most 20"
    for label in labels:
        at = label.get("at") if isinstance(label, dict) else None
        ok_at = (isinstance(at, str) and at in names) or (isinstance(at, list) and len(at) == 2 and all(p in names for p in at) and at[0] != at[1])
        if not ok_at or not _text(label.get("text"), 30):
            return "each label needs a listed point or segment and short text"
    angles = spec.get("angles") or []
    if not isinstance(angles, list) or len(angles) > 8:
        return "angles must be a list of at most 8"
    for angle in angles:
        if not isinstance(angle, dict):
            return "each angle must be an object"
        trio = [angle.get("vertex"), angle.get("from"), angle.get("to")]
        if any(p not in names for p in trio) or len(set(trio)) != 3:
            return "each angle needs three different listed points: vertex, from, to"
        if angle.get("text") is not None and not _text(angle.get("text"), 20):
            return "angle text must be short"
    if not segments and not circles:
        return "a geometry figure needs at least one segment or circle"
    return None


def _check_bar(spec) -> Optional[str]:
    cats, series = spec.get("categories"), spec.get("series")
    if not isinstance(cats, list) or not 1 <= len(cats) <= 12 or not all(_text(c, 25) for c in cats):
        return "categories must be 1 to 12 short labels"
    if not isinstance(series, list) or not 1 <= len(series) <= 4:
        return "series must list 1 to 4 data sets"
    for s in series:
        if not isinstance(s, dict) or not _text(s.get("label"), 25) or not _numlist(s.get("values"), len(cats), len(cats)):
            return "each series needs a label and one number per category"
    return None


def _check_pie(spec) -> Optional[str]:
    labels, values = spec.get("labels"), spec.get("values")
    if not isinstance(labels, list) or not 2 <= len(labels) <= 10 or not all(_text(l, 25) for l in labels):
        return "labels must be 2 to 10 short names"
    if not _numlist(values, len(labels), len(labels)) or any(v <= 0 for v in values):
        return "values must be positive numbers, one per label"
    return None


def _check_hist(spec) -> Optional[str]:
    edges, freq = spec.get("edges"), spec.get("frequencies")
    if not _numlist(edges, 3, 16) or any(b <= a for a, b in zip(edges, edges[1:])):
        return "edges must be 3 to 16 increasing class limits"
    if not _numlist(freq, len(edges) - 1, len(edges) - 1) or any(f < 0 for f in freq):
        return "frequencies must be one non-negative number per class"
    return None


def _check_ogive(spec) -> Optional[str]:
    series = spec.get("series")
    if not isinstance(series, list) or not 1 <= len(series) <= 2:
        return "series must list 1 or 2 cumulative curves"
    for s in series:
        if not isinstance(s, dict) or not _text(s.get("label"), 25):
            return "each curve needs a label"
        x, y = s.get("x"), s.get("y")
        if not _numlist(x, 2, 20) or not _numlist(y, len(x), len(x)):
            return "each curve needs equal-length x and y number lists"
        if any(b < a for a, b in zip(x, x[1:])):
            return "x values must not decrease"
    return None


def _check_number_line(spec) -> Optional[str]:
    low, high = spec.get("min"), spec.get("max")
    if not _num(low) or not _num(high) or high <= low or high - low > 200:
        return "min and max must be numbers with max above min, at most 200 apart"
    if spec.get("step") is not None and (not _num(spec["step"]) or spec["step"] <= 0 or (high - low) / spec["step"] > 60):
        return "step must be positive and give at most 60 marks"
    points = spec.get("points") or []
    if not isinstance(points, list) or len(points) > 10:
        return "points must be a list of at most 10"
    for p in points:
        if not isinstance(p, dict) or not _num(p.get("value")) or not low <= p["value"] <= high or (p.get("label") is not None and not _text(p["label"], 15)):
            return "each point needs a value inside min and max and a short label"
    intervals = spec.get("intervals") or []
    if not isinstance(intervals, list) or len(intervals) > 4:
        return "intervals must be a list of at most 4"
    for i in intervals:
        if not isinstance(i, dict) or not _num(i.get("from")) or not _num(i.get("to")) or i["from"] >= i["to"]:
            return "each interval needs from below to"
    return None


def _check_plane(spec) -> Optional[str]:
    for key in ("xrange", "yrange"):
        r = spec.get(key)
        if not _pair(r) or r[1] <= r[0] or r[1] - r[0] > 400:
            return f"{key} must be [min, max] with max above min"
    points = spec.get("points") or []
    if not isinstance(points, list) or not 1 <= len(points) <= 12:
        return "points must list 1 to 12 plotted points"
    names = set()
    for p in points:
        if not isinstance(p, dict) or not _num(p.get("x")) or not _num(p.get("y")) or not _text(p.get("name"), 3):
            return "each point needs a short name, x and y"
        names.add(p["name"])
    for item in spec.get("segments") or []:
        a, b, _ = _segment_ends(item)
        if a not in names or b not in names or a == b:
            return f"segment {item!r} must join two different plotted points"
    for poly in spec.get("polygons") or []:
        if not isinstance(poly, list) or len(poly) < 3 or any(n not in names for n in poly):
            return "each polygon needs at least 3 plotted point names"
    for line in spec.get("lines") or []:
        if not isinstance(line, dict) or not isinstance(line.get("through"), list) or len(line["through"]) != 2 \
                or not all(_pair(p) for p in line["through"]) or tuple(line["through"][0]) == tuple(line["through"][1]):
            return "each line needs two different [x, y] points it passes through"
    return None


_CHECKS = {"geometry_figure": _check_geometry, "bar_chart": _check_bar, "pie_chart": _check_pie, "histogram": _check_hist,
           "ogive": _check_ogive, "number_line": _check_number_line, "coordinate_plane": _check_plane}


def check_figure(spec: Dict[str, Any]) -> Optional[str]:
    """None when the figure can be drawn; otherwise a short reason."""
    check = _CHECKS.get(str(spec.get("type")))
    if not check:
        return None
    try:
        return check(spec)
    except Exception as exc:  # a malformed shape we did not anticipate is still just "cannot draw"
        return f"figure description could not be read ({type(exc).__name__})"


# ------------------------------------------------------------------ drawing

def _render_geometry(spec) -> bytes:
    pts = {k: np.array(v, dtype=float) for k, v in spec["points"].items()}
    xs = [p[0] for p in pts.values()]; ys = [p[1] for p in pts.values()]
    for c in spec.get("circles") or []:
        cx, cy = pts[c["center"]]; r = float(c["radius"])
        xs += [cx - r, cx + r]; ys += [cy - r, cy + r]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    centre = np.array([sum(p[0] for p in pts.values()), sum(p[1] for p in pts.values())]) / len(pts)
    fig, ax = plt.subplots(figsize=(5, 4.2))
    ax.set_aspect("equal"); ax.axis("off")
    for item in spec.get("segments") or []:
        a, b, dashed = _segment_ends(item)
        ax.plot([pts[a][0], pts[b][0]], [pts[a][1], pts[b][1]], color="black", lw=1.6, ls="--" if dashed else "-")
    for c in spec.get("circles") or []:
        ax.add_patch(mpatches.Circle(pts[c["center"]], float(c["radius"]), fill=False, ec="black", lw=1.6))
    for name, p in pts.items():
        ax.plot(*p, "o", color="black", ms=3.5)
        if spec.get("show_point_names", True):
            away = p - centre
            norm = np.linalg.norm(away) or 1.0
            q = p + away / norm * span * 0.05
            ax.text(q[0], q[1], name, ha="center", va="center", fontsize=12, weight="bold")
    for label in spec.get("labels") or []:
        at = label["at"]
        if isinstance(at, str):
            q = pts[at] + np.array([0.0, -span * 0.09])
        else:
            a, b = pts[at[0]], pts[at[1]]
            mid, d = (a + b) / 2, b - a
            normal = np.array([-d[1], d[0]]) / (np.linalg.norm(d) or 1.0)
            if np.dot(mid + normal - centre, normal) < 0:
                normal = -normal
            q = mid + normal * span * 0.06
        ax.text(q[0], q[1], label["text"], ha="center", va="center", fontsize=11, color="#1565c0")
    for angle in spec.get("angles") or []:
        v, f, t = pts[angle["vertex"]], pts[angle["from"]], pts[angle["to"]]
        u1, u2 = (f - v) / (np.linalg.norm(f - v) or 1.0), (t - v) / (np.linalg.norm(t - v) or 1.0)
        r = span * 0.08
        if angle.get("right"):
            ax.add_patch(mpatches.Polygon([v + u1 * r, v + (u1 + u2) * r, v + u2 * r], closed=False, fill=False, ec="black", lw=1.2))
            mid = (u1 + u2) / 2
        else:
            a1 = math.degrees(math.atan2(u1[1], u1[0])); a2 = math.degrees(math.atan2(u2[1], u2[0]))
            lo, hi = a1, a2
            if (hi - lo) % 360 > 180:
                lo, hi = hi, lo
            ax.add_patch(mpatches.Arc(v, 2 * r, 2 * r, theta1=lo, theta2=lo + (hi - lo) % 360, color="#c62828", lw=1.4))
            mid = (u1 + u2) / 2 if np.linalg.norm(u1 + u2) > 1e-9 else np.array([-u1[1], u1[0]])
        if angle.get("text"):
            direction = mid / (np.linalg.norm(mid) or 1.0)
            q = v + direction * r * 1.9
            ax.text(q[0], q[1], angle["text"], ha="center", va="center", fontsize=10, color="#c62828")
    pad = span * 0.18
    ax.set_xlim(min(xs) - pad, max(xs) + pad); ax.set_ylim(min(ys) - pad, max(ys) + pad)
    return _png(fig)


def _axes(spec, size=(6.4, 4.2)):
    fig, ax = plt.subplots(figsize=size)
    ax.set_facecolor("#fafafa")
    ax.grid(True, ls="--", alpha=0.4)
    ax.set_xlabel(spec.get("xlabel", ""), fontsize=11); ax.set_ylabel(spec.get("ylabel", ""), fontsize=11)
    return fig, ax


def _render_bar(spec) -> bytes:
    cats, series = spec["categories"], spec["series"]
    fig, ax = _axes(spec)
    width = 0.8 / len(series)
    x = np.arange(len(cats))
    for i, s in enumerate(series):
        ax.bar(x + (i - (len(series) - 1) / 2) * width, s["values"], width, label=s["label"], color=_COLORS[i % len(_COLORS)])
    ax.set_xticks(x); ax.set_xticklabels(cats, rotation=0 if len(cats) < 7 else 30)
    if len(series) > 1:
        ax.legend()
    return _png(fig)


def _render_pie(spec) -> bytes:
    fig, ax = plt.subplots(figsize=(5, 5))
    show = spec.get("show_values", True)
    ax.pie(spec["values"], labels=spec["labels"], colors=_COLORS * 2, startangle=90, counterclock=False,
           autopct=(lambda p: f"{p:.0f}%") if show else None, wedgeprops={"edgecolor": "white"})
    ax.axis("equal")
    return _png(fig)


def _render_hist(spec) -> bytes:
    edges, freq = spec["edges"], spec["frequencies"]
    fig, ax = _axes(spec)
    ax.bar(edges[:-1], freq, width=np.diff(edges), align="edge", color="#90caf9", edgecolor="#1565c0")
    ax.set_xticks(edges)
    return _png(fig)


def _render_ogive(spec) -> bytes:
    fig, ax = _axes(spec)
    for i, s in enumerate(spec["series"]):
        ax.plot(s["x"], s["y"], "o-", color=_COLORS[i % len(_COLORS)], label=s["label"], lw=1.8, ms=4)
    ax.legend()
    return _png(fig)


def _render_number_line(spec) -> bytes:
    low, high = float(spec["min"]), float(spec["max"])
    step = float(spec.get("step") or max((high - low) / 10.0, 1e-9))
    fig, ax = plt.subplots(figsize=(7, 1.9))
    ax.axis("off")
    ax.annotate("", xy=(high + step * 0.6, 0), xytext=(low - step * 0.6, 0), arrowprops=dict(arrowstyle="<|-|>", color="black", lw=1.5))
    ticks = np.arange(low, high + step / 2, step)
    for t in ticks:
        ax.plot([t, t], [-0.06, 0.06], color="black", lw=1)
        ax.text(t, -0.2, f"{t:g}", ha="center", va="top", fontsize=10)
    for i in spec.get("intervals") or []:
        ax.plot([i["from"], i["to"]], [0, 0], color="#c62828", lw=5, alpha=0.6, solid_capstyle="butt")
    for p in spec.get("points") or []:
        ax.plot(p["value"], 0, "o", ms=9, mfc="none" if p.get("open") else "#1565c0", mec="#1565c0", mew=1.8)
        if p.get("label"):
            ax.text(p["value"], 0.22, p["label"], ha="center", va="bottom", fontsize=11, weight="bold")
    ax.set_xlim(low - step, high + step); ax.set_ylim(-0.7, 0.8)
    return _png(fig)


def _render_plane(spec) -> bytes:
    (x0, x1), (y0, y1) = spec["xrange"], spec["yrange"]
    fig, ax = plt.subplots(figsize=(5.6, 5.0))
    ax.set_aspect("equal")
    ax.set_xlim(x0, x1); ax.set_ylim(y0, y1)
    ax.grid(True, ls=":", alpha=0.6)
    ax.axhline(0, color="black", lw=1.2); ax.axvline(0, color="black", lw=1.2)
    ax.set_xticks(range(math.ceil(x0), math.floor(x1) + 1, max(1, round((x1 - x0) / 14))))
    ax.set_yticks(range(math.ceil(y0), math.floor(y1) + 1, max(1, round((y1 - y0) / 14))))
    pts = {p["name"]: (float(p["x"]), float(p["y"])) for p in spec["points"]}
    for item in spec.get("segments") or []:
        a, b, dashed = _segment_ends(item)
        ax.plot([pts[a][0], pts[b][0]], [pts[a][1], pts[b][1]], color="#1565c0", lw=1.8, ls="--" if dashed else "-")
    for poly in spec.get("polygons") or []:
        ax.add_patch(mpatches.Polygon([pts[n] for n in poly], closed=True, fill=True, fc="#90caf9", ec="#1565c0", alpha=0.35, lw=1.8))
    for line in spec.get("lines") or []:
        (ax1, ay1), (ax2, ay2) = line["through"]
        if ax1 == ax2:
            ax.plot([ax1, ax1], [y0, y1], color="#c62828", lw=1.6)
        else:
            slope = (ay2 - ay1) / (ax2 - ax1)
            ax.plot([x0, x1], [ay1 + slope * (x0 - ax1), ay1 + slope * (x1 - ax1)], color="#c62828", lw=1.6)
        if line.get("label"):
            ax.text(x1 - (x1 - x0) * 0.05, ax.get_ylim()[1] - (y1 - y0) * 0.06, str(line["label"]), color="#c62828", fontsize=10, ha="right")
    for name, (px, py) in pts.items():
        ax.plot(px, py, "o", color="black", ms=4)
        ax.annotate(name, (px, py), textcoords="offset points", xytext=(6, 6), fontsize=11, weight="bold")
    return _png(fig)


_RENDERERS = {"geometry_figure": _render_geometry, "bar_chart": _render_bar, "pie_chart": _render_pie, "histogram": _render_hist,
              "ogive": _render_ogive, "number_line": _render_number_line, "coordinate_plane": _render_plane}


def draw_figure(spec: Dict[str, Any]) -> bytes:
    """PNG bytes for one of FIGURE_TYPES. Raises ValueError with the reason when the description cannot be drawn."""
    reason = check_figure(spec)
    if reason:
        raise ValueError(reason)
    return _RENDERERS[spec["type"]](spec)
