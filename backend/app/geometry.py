def point_in_rect(point: tuple, rect: list) -> bool:
    """Return True if point (x, y) lies inside or on the boundary of rect.

    rect: [[x1, y1], [x2, y2]] with x1 <= x2 and y1 <= y2.
    All coordinates must be in [0, 1].
    """
    x, y = point
    x1, y1 = rect[0]
    x2, y2 = rect[1]
    return x1 <= x <= x2 and y1 <= y <= y2


def point_in_polygon(point: tuple, polygon: list) -> bool:
    """Return True if point (x, y) lies inside polygon using ray casting.

    polygon: list of [x, y] vertices in order (open or closed — the closing
    edge is added implicitly).

    Boundary behavior follows standard ray-casting conventions:
    a point on a horizontal edge may test as inside or outside depending on
    which edge it falls on. Points on non-horizontal edges are treated as
    inside. Callers should not rely on exact boundary results.
    """
    x, y = point
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            intersect_x = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < intersect_x:
                inside = not inside
        j = i
    return inside
