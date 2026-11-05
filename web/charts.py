"""Server-drawn SVG line charts, so the site ships no chart library for its first pages."""

import datetime as dt
from html import escape

W, H, PAD = 640, 180, 28


def line(points: list[tuple[dt.datetime, float]], unit: str = "%", label: str = "", fmt: str = "%b %d %H:%M") -> str:
    """One series as an inline SVG with a zero line and min/max labels. Colours come from CSS."""
    if len(points) < 2:
        return '<p class="muted">Not enough points to draw yet.</p>'
    t0, t1 = points[0][0].timestamp(), points[-1][0].timestamp()
    ys = [v for _, v in points]
    lo, hi = min(min(ys), 0.0), max(max(ys), 0.0)
    span_t, span_y = (t1 - t0) or 1.0, (hi - lo) or 1.0

    def x(t: dt.datetime) -> float:
        return PAD + (t.timestamp() - t0) / span_t * (W - 2 * PAD)

    def y(v: float) -> float:
        return H - PAD - (v - lo) / span_y * (H - 2 * PAD)

    path = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in points)
    last = ys[-1]
    tone = "up" if last >= 0 else "down"
    return (
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">'
        f'<line class="zero" x1="{PAD}" x2="{W - PAD}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>'
        f'<polyline class="series {tone}" points="{path}"/>'
        f'<text class="axis" x="{PAD}" y="{PAD - 10}">{hi:.2f}{unit}</text>'
        f'<text class="axis" x="{PAD}" y="{H - 8}">{lo:.2f}{unit}</text>'
        f'<text class="axis end" x="{W - PAD}" y="{H - 8}">{escape(points[-1][0].strftime(fmt))}</text>'
        f'<text class="axis" x="{PAD + 90}" y="{H - 8}">{escape(points[0][0].strftime(fmt))}</text>'
        "</svg>"
    )
