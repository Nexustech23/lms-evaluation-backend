"""A library of standard biology and chemistry figures, drawn by code with labelled or lettered parts.

The AI names a figure from this list; it cannot draw one itself. Each figure is a clear schematic of the kind used in
school textbooks, not a detailed anatomical drawing. Parts are shown by name, or as letters A, B, C ... so a question
can ask students to name them. The letters follow the order of the figure's part list (see catalog_text()), which is
also what the AI is told, so the answer key is known exactly. Maps are deliberately not included: an official
boundary must come from an official image, which the editor lets faculty insert.
"""
import math
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mp
import matplotlib.pyplot as plt
import numpy as np

LIBRARY_TYPE = "library_figure"
_LETTERS = "ABCDEFGHIJKLMNOP"
Part = Tuple[str, Tuple[float, float], str, float]       # name, anchor on the figure, side of the label, label height
_FIGURES: Dict[str, Dict[str, Any]] = {}


def figure(key: str, title: str, subject: str, parts: List[Part]):
    def wrap(fn: Callable):
        _FIGURES[key] = {"title": title, "subject": subject, "parts": parts, "draw": fn}
        return fn
    return wrap


def _png(fig) -> bytes:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _rot_rect(cx, cy, length, width, angle_deg):
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    pts = [(-length / 2, -width / 2), (length / 2, -width / 2), (length / 2, width / 2), (-length / 2, width / 2)]
    return [(cx + x * c - y * s, cy + x * s + y * c) for x, y in pts]


# ------------------------------------------------------------------ biology

@figure("plant_cell", "Plant cell", "Biology", [
    ("Cell wall", (9.0, 4.8), "right", 9.0), ("Cell membrane", (1.25, 6.4), "left", 9.0), ("Cytoplasm", (1.65, 4.0), "left", 6.4),
    ("Vacuole", (4.0, 4.4), "left", 3.8), ("Nucleus", (7.5, 7.0), "right", 7.0), ("Nucleolus", (7.5, 6.6), "right", 5.3),
    ("Chloroplast", (7.6, 3.7), "right", 3.4), ("Mitochondrion", (6.1, 2.55), "right", 1.4)])
def _plant_cell(ax):
    ax.add_patch(mp.FancyBboxPatch((1, 1.5), 8, 7, boxstyle="round,pad=0,rounding_size=0.6", fc="#c8e6c9", ec="#33691e", lw=4.5))
    ax.add_patch(mp.FancyBboxPatch((1.25, 1.75), 7.5, 6.5, boxstyle="round,pad=0,rounding_size=0.5", fc="#f1f8e9", ec="#689f38", lw=1.6))
    ax.add_patch(mp.Ellipse((4.0, 4.6), 4.2, 3.8, fc="#e1f5fe", ec="#0288d1", lw=1.6))
    ax.add_patch(mp.Circle((7.5, 6.8), 0.95, fc="#ce93d8", ec="#6a1b9a", lw=1.6))
    ax.add_patch(mp.Circle((7.5, 6.6), 0.32, fc="#6a1b9a"))
    for x, y in ((2.5, 7.2), (2.0, 3.0), (5.4, 7.4), (7.6, 3.7)):
        ax.add_patch(mp.Ellipse((x, y), 1.1, 0.6, fc="#43a047", ec="#1b5e20", lw=1.2))
    ax.add_patch(mp.Ellipse((6.1, 2.55), 0.95, 0.5, fc="#ffcc80", ec="#e65100", lw=1.2))


@figure("animal_cell", "Animal cell", "Biology", [
    ("Cell membrane", (7.97, 7.47), "right", 9.4), ("Cytoplasm", (7.6, 4.6), "right", 5.0), ("Nucleus", (4.1, 6.7), "left", 8.6),
    ("Nucleolus", (5.2, 5.5), "left", 4.9), ("Mitochondrion", (2.6, 6.3), "left", 6.9), ("Golgi apparatus", (6.75, 5.95), "right", 7.2),
    ("Endoplasmic reticulum", (3.3, 3.9), "left", 2.8), ("Lysosome", (6.6, 2.3), "right", 1.8)])
def _animal_cell(ax):
    ax.add_patch(mp.Ellipse((5, 5), 8.4, 7.0, fc="#ffe0b2", ec="#e65100", lw=3))
    ax.add_patch(mp.Circle((5, 5.6), 1.5, fc="#ce93d8", ec="#6a1b9a", lw=1.6))
    ax.add_patch(mp.Circle((5.2, 5.5), 0.5, fc="#6a1b9a"))
    for cx, cy in ((2.6, 6.3), (7.6, 3.3)):
        ax.add_patch(mp.Ellipse((cx, cy), 1.5, 0.8, fc="#ffcc80", ec="#e65100", lw=1.2))
        ax.plot([cx - 0.5, cx - 0.2, cx + 0.1, cx + 0.5], [cy, cy + 0.2, cy - 0.2, cy], color="#e65100", lw=1)
    for r in (0.5, 0.75, 1.0, 1.25):
        ax.add_patch(mp.Arc((7.0, 7.0), 2 * r, 2 * r, theta1=200, theta2=300, color="#00838f", lw=2))
    for k in range(3):
        xs = np.linspace(2.4, 4.2, 40)
        ax.plot(xs, 3.4 + 0.25 * k + 0.12 * np.sin(xs * 6), color="#5d4037", lw=1.4)
    for x, y in ((6.6, 2.3), (3.2, 2.6)):
        ax.add_patch(mp.Circle((x, y), 0.3, fc="#ef9a9a", ec="#c62828", lw=1))


@figure("human_heart", "Human heart", "Biology", [
    ("Superior vena cava", (2.9, 9.0), "left", 9.3), ("Pulmonary artery", (4.2, 8.3), "left", 7.7), ("Right atrium", (3.4, 6.2), "left", 6.0),
    ("Right ventricle", (4.0, 3.7), "left", 3.4), ("Aorta", (7.7, 8.95), "right", 9.3), ("Pulmonary vein", (9.0, 7.1), "right", 7.6),
    ("Left atrium", (7.0, 6.3), "right", 5.9), ("Left ventricle", (6.7, 3.6), "right", 3.3)])
def _heart(ax):
    blue, red = "#5c6bc0", "#e53935"
    kw = dict(solid_capstyle="round")
    ax.plot([2.9, 2.9], [6.6, 9.6], color=blue, lw=15, **kw)
    t = np.linspace(0, 1, 40)
    ax.plot(4.5 - 0.5 * t ** 2, 6.0 + 3.2 * t, color="#7986cb", lw=14, **kw)
    arch = np.linspace(math.pi, 0, 60)
    ax.plot(np.concatenate([[5.7, 5.7], 6.8 + 1.1 * np.cos(arch), [7.9, 7.9]]),
            np.concatenate([[6.7, 8.2], 8.2 + 1.1 * np.sin(arch), [8.2, 6.8]]), color=red, lw=15, **kw)
    ax.plot([8.0, 9.4], [6.6, 7.4], color="#ef5350", lw=12, **kw)
    ax.add_patch(mp.Ellipse((3.6, 6.2), 2.7, 2.4, fc="#7986cb", ec="#283593", lw=1.8))
    ax.add_patch(mp.Ellipse((7.0, 6.3), 2.5, 2.3, fc="#ef5350", ec="#b71c1c", lw=1.8))
    ax.add_patch(mp.Ellipse((4.1, 3.8), 3.1, 3.8, angle=8, fc="#5c6bc0", ec="#283593", lw=1.8))
    ax.add_patch(mp.Ellipse((6.5, 3.7), 3.4, 4.5, angle=-8, fc="#e53935", ec="#b71c1c", lw=1.8))
    ax.plot([5.3, 5.3], [1.7, 5.3], color="white", lw=2)


@figure("human_eye", "Human eye", "Biology", [
    ("Ciliary muscle", (3.9, 7.4), "left", 9.5), ("Iris", (3.45, 6.8), "left", 7.9), ("Cornea", (1.1, 5.0), "left", 6.4),
    ("Aqueous humour", (2.4, 5.0), "left", 4.2), ("Pupil", (3.4, 5.0), "left", 2.6), ("Lens", (4.1, 4.2), "left", 1.0),
    ("Sclera", (5.8, 8.6), "right", 9.5), ("Retina", (8.74, 6.7), "right", 7.9), ("Vitreous humour", (6.2, 5.2), "right", 5.5),
    ("Optic nerve", (10.0, 3.9), "right", 3.2)])
def _eye(ax):
    ax.add_patch(mp.Circle((5.8, 5.0), 3.6, fc="#e8eaf6", ec="#757575", lw=4))
    ax.add_patch(mp.Arc((5.8, 5.0), 6.8, 6.8, theta1=-52, theta2=52, color="#ef5350", lw=6))
    ax.add_patch(mp.Ellipse((2.0, 5.0), 1.9, 3.6, fc="#b3e5fc", ec="#0288d1", lw=2))
    ax.add_patch(mp.Rectangle((3.25, 5.9), 0.35, 1.7, fc="#6d4c41", ec="#3e2723"))
    ax.add_patch(mp.Rectangle((3.25, 2.4), 0.35, 1.7, fc="#6d4c41", ec="#3e2723"))
    ax.add_patch(mp.Ellipse((4.1, 5.0), 1.0, 2.9, fc="#fff59d", ec="#f9a825", lw=1.6))
    ax.add_patch(mp.Ellipse((3.9, 7.3), 0.7, 0.9, fc="#a1887f", ec="#4e342e"))
    ax.add_patch(mp.Ellipse((3.9, 2.7), 0.7, 0.9, fc="#a1887f", ec="#4e342e"))
    ax.plot([8.9, 10.6], [4.2, 3.4], color="#fbc02d", lw=9, solid_capstyle="round")


@figure("flower_section", "Longitudinal section of a flower", "Biology", [
    ("Petal", (2.5, 5.3), "left", 8.2), ("Sepal", (3.0, 2.0), "left", 2.4), ("Anther", (3.75, 5.3), "left", 6.2),
    ("Filament", (3.8, 3.7), "left", 4.4), ("Stigma", (5.0, 6.3), "right", 9.0), ("Style", (5.0, 5.0), "right", 7.4),
    ("Ovary", (5.9, 3.5), "right", 5.4), ("Ovule", (5.35, 2.9), "right", 3.6), ("Receptacle", (5.6, 1.5), "right", 1.6)])
def _flower(ax):
    ax.add_patch(mp.Rectangle((4.85, 0), 0.3, 1.0, fc="#7cb342", ec="#33691e"))
    ax.add_patch(mp.Ellipse((5, 1.5), 2.4, 1.0, fc="#9ccc65", ec="#33691e", lw=1.5))
    for sx in (-1, 1):
        ax.add_patch(mp.Ellipse((5 + sx * 2.0, 2.2), 2.6, 0.8, angle=-sx * 22, fc="#66bb6a", ec="#2e7d32", lw=1.4))
        ax.add_patch(mp.Ellipse((5 + sx * 2.5, 5.0), 1.5, 4.0, angle=sx * 16, fc="#f8bbd0", ec="#ad1457", lw=1.5))
        ax.plot([5 + sx * 1.0, 5 + sx * 1.25], [2.4, 5.0], color="#558b2f", lw=1.6)
        ax.add_patch(mp.Ellipse((5 + sx * 1.25, 5.3), 0.5, 0.95, fc="#fdd835", ec="#f57f17", lw=1.2))
    ax.add_patch(mp.Ellipse((5, 3.1), 1.9, 2.0, fc="#c5e1a5", ec="#558b2f", lw=1.6))
    for x, y in ((4.65, 2.9), (5.35, 2.9), (5.0, 3.5)):
        ax.add_patch(mp.Circle((x, y), 0.2, fc="#827717"))
    ax.add_patch(mp.Rectangle((4.9, 4.0), 0.2, 2.1, fc="#aed581", ec="#558b2f"))
    ax.add_patch(mp.Ellipse((5, 6.3), 1.0, 0.55, fc="#ff8a65", ec="#bf360c", lw=1.4))


@figure("digestive_system", "Human digestive system", "Biology", [
    ("Mouth", (5.0, 9.4), "left", 9.4), ("Oesophagus", (5.0, 8.2), "left", 8.0), ("Liver", (3.2, 6.9), "left", 6.5),
    ("Gall bladder", (4.0, 5.85), "left", 4.9), ("Stomach", (6.4, 7.0), "right", 8.0), ("Pancreas", (5.8, 5.95), "right", 6.3),
    ("Small intestine", (5.3, 3.9), "right", 4.4), ("Large intestine", (7.2, 3.3), "right", 2.6)])
def _digestive(ax):
    ax.add_patch(mp.Ellipse((5, 9.4), 2.0, 1.0, fc="#ef9a9a", ec="#c62828", lw=1.4))
    ax.plot([5, 5], [8.9, 7.5], color="#e57373", lw=7, solid_capstyle="round")
    ax.add_patch(mp.Ellipse((3.3, 6.9), 3.6, 2.0, angle=-12, fc="#8d6e63", ec="#4e342e", lw=1.5))
    ax.add_patch(mp.Ellipse((6.4, 7.0), 2.4, 1.9, angle=35, fc="#ffab91", ec="#bf360c", lw=1.5))
    ax.add_patch(mp.Ellipse((5.7, 5.95), 3.0, 0.55, angle=-6, fc="#fff176", ec="#f9a825", lw=1.3))
    ax.add_patch(mp.Ellipse((4.0, 5.85), 0.5, 0.85, fc="#66bb6a", ec="#2e7d32", lw=1.2))
    ax.plot([3.0, 3.0, 7.2, 7.2, 6.3, 5.1], [2.2, 5.0, 5.0, 2.2, 1.7, 1.1], color="#bcaaa4", lw=10, solid_joinstyle="round", solid_capstyle="round")
    xs = np.linspace(3.9, 6.4, 120)
    ax.plot(xs, 3.5 + 0.9 * np.sin(xs * 5.2) * np.cos(xs * 1.5), color="#f48fb1", lw=6, solid_capstyle="round")


@figure("neuron", "Neuron", "Biology", [
    ("Dendrite", (0.9, 6.4), "left", 8.8), ("Cell body", (2.6, 4.3), "left", 2.0), ("Nucleus", (2.6, 5.0), "left", 7.2),
    ("Axon", (5.1, 5.0), "right", 8.4), ("Myelin sheath", (6.4, 5.45), "right", 7.0), ("Node of Ranvier", (7.15, 5.0), "right", 3.2),
    ("Axon terminal", (9.8, 6.2), "right", 5.0)])
def _neuron(ax):
    for ex, ey in ((0.3, 7.6), (0.2, 6.2), (0.3, 4.0), (0.6, 2.4)):
        ax.plot([1.7, (1.7 + ex) / 2 + 0.2, ex], [5.0, (5.0 + ey) / 2, ey], color="#5d4037", lw=2)
    ax.plot([1.0, 0.45], [6.5, 6.9], color="#5d4037", lw=1.5)
    ax.add_patch(mp.Circle((2.6, 5.0), 1.1, fc="#fff176", ec="#f57f17", lw=1.8))
    ax.add_patch(mp.Circle((2.6, 5.0), 0.45, fc="#7e57c2"))
    ax.plot([3.7, 8.7], [5.0, 5.0], color="#5d4037", lw=3)
    for x0 in (4.3, 5.8, 7.3):
        ax.add_patch(mp.FancyBboxPatch((x0, 4.55), 1.2, 0.9, boxstyle="round,pad=0,rounding_size=0.4", fc="#ffcc80", ec="#e65100", lw=1.3))
    for ex, ey in ((9.9, 6.4), (10.1, 5.0), (9.9, 3.6)):
        ax.plot([8.7, ex], [5.0, ey], color="#5d4037", lw=2)
        ax.add_patch(mp.Circle((ex, ey), 0.22, fc="#5d4037"))


@figure("leaf_section", "Cross section of a leaf", "Biology", [
    ("Cuticle", (3.0, 8.65), "left", 9.2), ("Upper epidermis", (2.0, 8.0), "left", 7.6), ("Palisade mesophyll", (2.6, 6.4), "left", 5.9),
    ("Spongy mesophyll", (2.4, 4.2), "left", 4.2), ("Lower epidermis", (2.2, 2.75), "left", 2.6),
    ("Vascular bundle", (6.0, 4.9), "right", 8.4), ("Xylem", (6.0, 5.2), "right", 6.8), ("Phloem", (6.0, 4.55), "right", 5.4),
    ("Guard cell", (7.7, 2.75), "right", 3.6), ("Stoma", (8.2, 2.75), "right", 2.0)])
def _leaf(ax):
    ax.plot([0.8, 9.4], [8.62, 8.62], color="#2e7d32", lw=3)
    for i in range(9):
        ax.add_patch(mp.Rectangle((0.8 + i * 0.95, 7.6), 0.95, 1.0, fc="#c5e1a5", ec="#558b2f", lw=1.1))
        ax.add_patch(mp.Rectangle((0.8 + i * 0.95 + 0.1, 5.7), 0.75, 1.9, fc="#81c784", ec="#2e7d32", lw=1.1))
    rng = np.random.RandomState(4)
    for _ in range(26):
        ax.add_patch(mp.Ellipse((rng.uniform(1.2, 8.9), rng.uniform(3.3, 5.3)), rng.uniform(0.7, 1.2), rng.uniform(0.55, 0.9), fc="#a5d6a7", ec="#2e7d32", lw=0.9, alpha=0.9))
    ax.add_patch(mp.Circle((6.0, 4.9), 0.95, fc="#e8f5e9", ec="#33691e", lw=1.6, zorder=3))
    ax.add_patch(mp.Circle((6.0, 5.2), 0.33, fc="#ffe082", ec="#f57f17", lw=1.2, zorder=4))
    ax.add_patch(mp.Circle((6.0, 4.55), 0.33, fc="#80cbc4", ec="#00695c", lw=1.2, zorder=4))
    for i in range(9):
        if i != 7:
            ax.add_patch(mp.Rectangle((0.8 + i * 0.95, 2.2), 0.95, 1.0, fc="#c5e1a5", ec="#558b2f", lw=1.1))
    for x in (7.35, 8.05):
        ax.add_patch(mp.Ellipse((x + 0.17, 2.7), 0.55, 1.0, fc="#388e3c", ec="#1b5e20", lw=1.2, zorder=3))


# ------------------------------------------------------------------ chemistry

@figure("simple_distillation", "Simple distillation", "Chemistry", [
    ("Thermometer", (2.0, 7.0), "left", 9.0), ("Distillation flask", (1.0, 3.2), "left", 5.4), ("Heat source", (2.0, 0.6), "left", 2.2),
    ("Condenser", (5.6, 4.7), "right", 8.4), ("Water outlet", (4.0, 6.0), "right", 9.6), ("Water inlet", (7.5, 3.55), "right", 6.2),
    ("Receiver", (9.2, 1.8), "right", 3.2)])
def _distillation(ax):
    ax.add_patch(mp.Circle((2.0, 3.2), 1.4, fc="#fffde7", ec="#424242", lw=2))
    ax.add_patch(mp.Wedge((2.0, 3.2), 1.4, 180, 360, fc="#ffe082", ec="#424242", lw=2))
    ax.add_patch(mp.Rectangle((1.75, 4.5), 0.5, 2.0, fc="#fffde7", ec="#424242", lw=2))
    ax.add_patch(mp.Rectangle((1.6, 6.4), 0.8, 0.35, fc="#8d6e63", ec="#3e2723"))
    ax.add_patch(mp.Rectangle((1.93, 4.9), 0.14, 2.4, fc="white", ec="#424242", lw=1.2, zorder=4))
    ax.add_patch(mp.Circle((2.0, 4.95), 0.14, fc="#e53935", ec="#b71c1c", zorder=5))
    ax.plot([2.25, 3.6], [6.0, 5.6], color="#424242", lw=6, solid_capstyle="butt")
    ax.plot([2.25, 3.6], [6.0, 5.6], color="white", lw=3.5, solid_capstyle="butt")
    ax.add_patch(mp.Polygon(_rot_rect(5.9, 4.6, 5.0, 1.5, -22), fc="#e3f2fd", ec="#424242", lw=2))
    ax.add_patch(mp.Polygon(_rot_rect(5.9, 4.6, 5.2, 0.55, -22), fc="white", ec="#424242", lw=1.4))
    ax.plot([4.1, 4.0], [5.4, 6.4], color="#1e88e5", lw=3); ax.plot([7.5, 7.7], [3.8, 3.0], color="#1e88e5", lw=3)
    ax.add_patch(mp.Polygon([(8.5, 0.6), (9.9, 0.6), (9.35, 2.4), (9.05, 2.4)], fc="#e0f7fa", ec="#424242", lw=2))
    ax.add_patch(mp.Polygon([(8.7, 0.6), (9.7, 0.6), (9.5, 1.1), (8.9, 1.1)], fc="#4dd0e1"))
    ax.plot([8.1, 9.1], [3.15, 2.45], color="#424242", lw=1.6)
    ax.add_patch(mp.Polygon([(1.4, 0.8), (2.6, 0.8), (2.2, 1.9), (1.8, 1.9)], fc="#9e9e9e", ec="#424242"))
    ax.add_patch(mp.Polygon([(1.85, 1.9), (2.15, 1.9), (2.0, 2.4)], fc="#ff9800", ec="#e65100"))


@figure("electrolysis_of_water", "Electrolysis of water", "Chemistry", [
    ("Battery", (5.0, 0.35), "left", 1.3), ("Cathode (-)", (3.4, 1.7), "left", 3.6), ("Anode (+)", (6.6, 1.7), "right", 3.6),
    ("Hydrogen gas", (3.4, 6.4), "left", 7.4), ("Oxygen gas", (6.6, 7.2), "right", 7.4), ("Water", (2.0, 2.6), "left", 5.2),
    ("Test tube", (6.05, 8.0), "right", 9.4)])
def _electrolysis(ax):
    ax.add_patch(mp.Rectangle((1.2, 1.0), 7.6, 5.8, fc="#b3e5fc", ec="#424242", lw=2))
    for cx, level, col in ((3.4, 4.2, "#b3e5fc"), (6.6, 6.2, "#b3e5fc")):
        ax.add_patch(mp.Rectangle((cx - 0.55, 1.6), 1.1, 6.6, fc="#fafafa", ec="#424242", lw=2))
        ax.add_patch(mp.Rectangle((cx - 0.55, 1.6), 1.1, level - 1.6, fc=col, ec="none"))
        ax.plot([cx - 0.55, cx + 0.55], [level, level], color="#0288d1", lw=1.2)
    ax.plot([3.4, 3.4], [0.5, 2.6], color="#424242", lw=4); ax.plot([6.6, 6.6], [0.5, 2.6], color="#424242", lw=4)
    ax.plot([3.4, 3.4, 4.3], [0.5, -0.3, -0.3], color="black", lw=1.4); ax.plot([6.6, 6.6, 5.7], [0.5, -0.3, -0.3], color="black", lw=1.4)
    ax.add_patch(mp.Rectangle((4.3, -0.65), 1.4, 0.7, fc="#eeeeee", ec="#424242", lw=1.6))
    ax.text(4.55, -0.3, "−", ha="center", va="center", fontsize=13, weight="bold"); ax.text(5.45, -0.3, "+", ha="center", va="center", fontsize=13, weight="bold")
    for gx, gy in ((3.2, 5.0), (3.55, 5.6), (3.3, 6.3), (3.6, 6.9), (3.25, 7.5)):
        ax.plot(gx, gy, "o", ms=3, mfc="none", mec="#546e7a")
    for gx, gy in ((6.45, 6.8), (6.8, 7.3)):
        ax.plot(gx, gy, "o", ms=3, mfc="none", mec="#546e7a")


# ------------------------------------------------------------------ checking and drawing

def figure_keys() -> List[str]:
    return list(_FIGURES)


def part_names(key: str) -> List[str]:
    return [p[0] for p in _FIGURES[key]["parts"]]


def lettered_parts(spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """[(letter, part name)] for the parts a figure shows as letters, in the figure's own part order."""
    key = spec["figure"]
    if spec.get("labels", "names") == "letters":
        hidden = set(part_names(key))
    else:
        hidden = set(spec.get("hide") or [])
    names = [n for n in part_names(key) if n in hidden]
    return list(zip(_LETTERS, names))


def check_figure(spec: Dict[str, Any]) -> Optional[str]:
    if spec.get("type") != LIBRARY_TYPE:
        return None
    key = spec.get("figure")
    if key not in _FIGURES:
        return "figure must be one of: " + ", ".join(_FIGURES)
    if spec.get("labels", "names") not in ("names", "letters"):
        return "labels must be names or letters"
    hide = spec.get("hide")
    if hide is not None:
        if not isinstance(hide, list) or not all(isinstance(h, str) for h in hide):
            return "hide must be a list of part names"
        unknown = [h for h in hide if h not in part_names(key)]
        if unknown:
            return "unknown part name(s) in hide: " + ", ".join(unknown) + ". Parts of this figure: " + ", ".join(part_names(key))
    return None


def draw_figure(spec: Dict[str, Any]) -> bytes:
    reason = check_figure(spec)
    if reason:
        raise ValueError(reason)
    key = spec["figure"]
    entry = _FIGURES[key]
    letters = {name: letter for letter, name in lettered_parts(spec)}
    fig, ax = plt.subplots(figsize=(7.4, 5.6))
    ax.set_aspect("equal"); ax.axis("off")
    entry["draw"](ax)
    for name, anchor, side, y in entry["parts"]:
        x = -0.4 if side == "left" else 10.9
        text = letters.get(name, name)
        ax.annotate(text, xy=anchor, xytext=(x, y), ha="right" if side == "left" else "left", va="center",
                    fontsize=12 if name in letters else 10, weight="bold" if name in letters else "normal",
                    arrowprops=dict(arrowstyle="-", color="#212121", lw=0.9, shrinkA=2, shrinkB=0))
        ax.plot(*anchor, "o", color="#212121", ms=2.2, zorder=10)
    ax.set_xlim(-5.0, 15.6); ax.set_ylim(-1.3, 10.4)
    return _png(fig)


def catalog_text() -> str:
    """What the AI is told: every figure, its parts in order (letters follow this order)."""
    lines = []
    for key, entry in _FIGURES.items():
        lines.append(f'      "{key}" ({entry["title"]}, {entry["subject"]}): ' + ", ".join(f"{i + 1}. {p[0]}" for i, p in enumerate(entry["parts"])))
    return "\n".join(lines)
