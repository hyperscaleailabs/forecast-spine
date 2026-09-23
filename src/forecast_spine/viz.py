"""Chart chrome for `notebooks/exploration.ipynb`.

Colour choices are not taste. The categorical slots below are the first three
of a palette whose colourblind separation was validated rather than eyeballed
(all-pairs worst CVD deltaE 9.2, normal-vision 24.0 against the light surface).

One consequence is encoded here: `SERIES_NAIVE` sits at 2.74:1 against the
surface, below the 3:1 bar, so every chart that uses it carries a visible
direct label or an accompanying table rather than relying on the colour alone.

Conventions the charts follow:
  - one y-axis, ever; two measures of different scale become two charts
  - colour follows the entity, so "actual" is the same blue in every figure
  - solid hairline gridlines; dashing is reserved for thresholds, which are
    always labelled
  - selective direct labels, never a number on every point
"""

from __future__ import annotations

import matplotlib as mpl
import matplotlib.pyplot as plt

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

# Categorical slots, assigned in fixed order and never cycled.
SERIES_ACTUAL = "#2a78d6"     # slot 1, blue
SERIES_ERCOT = "#eb6834"      # slot 2, orange
SERIES_NAIVE = "#1baf7a"      # slot 3, aqua -- needs a direct label (see above)

# Emphasis: the subject keeps its hue, the context recedes to grey.
CONTEXT = "#c9c8c2"

# Status tokens. Reserved for pass/fail meaning; never used as a series.
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"

# Sequential ramp for magnitude: one hue, light to dark.
SEQUENTIAL = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec",
    "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95",
]


def apply_style() -> None:
    """Recessive chrome, thin marks, generous padding."""
    mpl.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "figure.dpi": 130,
            "font.family": "sans-serif",
            "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
            "font.size": 9,
            "axes.edgecolor": BASELINE,
            "axes.linewidth": 0.8,
            "axes.labelcolor": INK_SECONDARY,
            "axes.titlecolor": INK,
            "axes.titlesize": 11,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlepad": 14,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "grid.linestyle": "-",  # dashing is reserved for thresholds
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
            "xtick.labelcolor": INK_SECONDARY,
            "ytick.labelcolor": INK_SECONDARY,
            "xtick.major.size": 0,
            "ytick.major.size": 0,
            "legend.frameon": False,
            "legend.fontsize": 8.5,
            "lines.linewidth": 2.0,
            "lines.markersize": 4.5,
            "lines.solid_capstyle": "round",
        }
    )


def frame(ax: plt.Axes, *, subtitle: str | None = None, x_grid: bool = False) -> plt.Axes:
    """Strip the box, keep a hairline baseline, add a subtitle line."""
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.grid(axis="y")
    ax.grid(axis="x", visible=x_grid)
    if subtitle:
        ax.set_title(subtitle, fontsize=8.5, color=INK_MUTED, loc="left", pad=6, weight="normal")
    return ax


def title(ax: plt.Axes, headline: str, subtitle: str | None = None) -> None:
    """Headline above, quiet explanatory line beneath it."""
    ax.set_title(headline, fontsize=11, color=INK, loc="left", weight="bold", pad=22)
    if subtitle:
        ax.annotate(
            subtitle,
            xy=(0, 1),
            xytext=(0, 8),
            xycoords="axes fraction",
            textcoords="offset points",
            fontsize=8.5,
            color=INK_MUTED,
            va="bottom",
        )


def direct_label(ax: plt.Axes, x, y, text: str, color: str, *, dx: int = 6, dy: int = 0) -> None:
    """Name a series at its endpoint instead of leaning on the legend alone."""
    ax.annotate(
        text,
        xy=(x, y),
        xytext=(dx, dy),
        textcoords="offset points",
        color=color,
        fontsize=8.5,
        weight="bold",
        va="center",
    )


def threshold(
    ax: plt.Axes, y: float, label: str, *, color: str = INK_MUTED, ha: str = "right"
) -> None:
    """A labelled dashed rule -- the one place dashing is allowed.

    `ha` puts the label at whichever end of the rule is free of marks; a
    threshold label sitting on top of a bar is worse than no label.
    """
    ax.axhline(y, color=color, linewidth=1.2, linestyle=(0, (4, 3)), zorder=1)
    ax.annotate(
        label,
        xy=(1 if ha == "right" else 0, y),
        xytext=(0 if ha == "right" else 2, 4),
        xycoords=("axes fraction", "data"),
        textcoords="offset points",
        ha=ha,
        va="bottom",
        fontsize=8,
        color=color,
    )


def endpoint_labels(ax: plt.Axes, items: list[tuple], *, min_gap_points: float = 11.0) -> None:
    """Direct-label several series at their endpoints without collisions.

    `items` is [(x, y, text, colour)]. Labels are nudged apart vertically so
    that identity never rests on colour alone even when two series end close
    together.
    """
    if not items:
        return
    # Positions must be read after the final layout pass, so call this last.
    figure = ax.figure
    figure.canvas.draw()
    to_points = 72.0 / figure.dpi
    transform = ax.transData
    ordered = sorted(items, key=lambda item: item[1])
    placed: list[float] = []
    previous = None
    for _, y, _, _ in ordered:
        point = transform.transform((0, y))[1] * to_points
        if previous is not None and point - previous < min_gap_points:
            point = previous + min_gap_points
        placed.append(point)
        previous = point
    for (x, y, text, color), point in zip(ordered, placed):
        shift = point - transform.transform((0, y))[1] * to_points
        ax.annotate(
            text,
            xy=(x, y),
            xytext=(7, shift),
            textcoords="offset points",
            color=color,
            fontsize=8.5,
            weight="bold",
            va="center",
            annotation_clip=False,
        )
