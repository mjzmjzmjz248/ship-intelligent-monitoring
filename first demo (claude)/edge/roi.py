"""区域判断。ROI 用 0~1 归一化坐标；限速区用 [经度, 纬度]。点在边界上算区域内。"""
from __future__ import annotations

from collections.abc import Sequence

Point = Sequence[float]


def _on_segment(p: Point, a: Point, b: Point, eps: float = 1e-9) -> bool:
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > eps:
        return False
    return (min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
            and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps)


def point_in_polygon(p: Point, poly: Sequence[Point]) -> bool:
    n = len(poly)
    inside = False
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if _on_segment(p, a, b):
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if p[0] < x:
                inside = not inside
    return inside


def anchor_point(box: Sequence[float], width: int, height: int, anchor: str) -> tuple[float, float]:
    """人框 (x1, y1, x2, y2，像素) → 归一化锚点。"""
    x1, y1, x2, y2 = box[:4]
    x = (x1 + x2) / 2 / width
    y = (y2 if anchor == "foot" else (y1 + y2) / 2) / height
    return x, y


def count_in_roi(boxes: Sequence[Sequence[float]], width: int, height: int,
                 roi_points: Sequence[Point], anchor: str) -> int:
    return sum(1 for b in boxes if point_in_polygon(anchor_point(b, width, height, anchor), roi_points))
