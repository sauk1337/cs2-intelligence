"""Historical price chart as SVG (no dependencies)."""
from __future__ import annotations

import html
import time

COLORS = {"skinport": "#2d8cf0", "steam": "#7a7a7a", "csfloat": "#19be6b", "buff163": "#ff9900", "pricempire": "#9c27b0"}


def render_svg(title: str, series: dict[str, list], currency: str = "EUR", w: int = 720, h: int = 300) -> str:
    pts = [(t, p) for s in series.values() for t, p in s if p is not None]
    pad_l, pad_r, pad_t, pad_b = 60, 110, 30, 30
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'font-family="sans-serif" font-size="11"><rect width="100%" height="100%" fill="#fff"/>'
            f'<text x="{pad_l}" y="18" font-size="13" font-weight="bold">{html.escape(title)}</text>')
    if len(pts) < 2:
        return head + f'<text x="{pad_l}" y="{h // 2}">Not enough history yet</text></svg>'
    t0, t1 = min(p[0] for p in pts), max(p[0] for p in pts)
    lo, hi = min(p[1] for p in pts), max(p[1] for p in pts)
    if hi == lo:
        hi, lo = hi * 1.05 + 0.01, lo * 0.95
    sx = lambda t: pad_l + (t - t0) / max(1, t1 - t0) * (w - pad_l - pad_r)
    sy = lambda p: pad_t + (hi - p) / (hi - lo) * (h - pad_t - pad_b)
    out = [head]
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = sy(v)
        out.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="#eee"/>'
                   f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{v:.2f}</text>')
    for i, t in enumerate((t0, (t0 + t1) / 2, t1)):
        out.append(f'<text x="{sx(t):.1f}" y="{h - 10}" text-anchor="middle">'
                   f'{time.strftime("%d/%m", time.gmtime(t))}</text>')
    for n, (src, s) in enumerate(series.items()):
        s = [(t, p) for t, p in s if p is not None]
        if len(s) < 2:
            continue
        c = COLORS.get(src, "#333")
        d = " ".join(f"{sx(t):.1f},{sy(p):.1f}" for t, p in s)
        out.append(f'<polyline fill="none" stroke="{c}" stroke-width="1.6" points="{d}"/>')
        out.append(f'<text x="{w - pad_r + 10}" y="{pad_t + 14 * n + 10}" fill="{c}">{html.escape(src)} '
                   f'{s[-1][1]:.2f} {currency}</text>')
    out.append("</svg>")
    return "".join(out)
