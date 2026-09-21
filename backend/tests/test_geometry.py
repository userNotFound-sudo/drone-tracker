import unittest
from app.geometry import point_in_polygon, point_in_rect

# Shared fixtures
_UNIT_RECT     = [[0.0, 0.0], [1.0, 1.0]]
_SMALL_RECT    = [[0.2, 0.3], [0.6, 0.8]]
_UNIT_SQUARE   = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
_TRIANGLE      = [[0.0, 0.0], [1.0, 0.0], [0.5, 1.0]]
# L-shape: full unit square minus the bottom-right quadrant
_L_SHAPE = [
    [0.0, 0.0], [0.5, 0.0], [0.5, 0.5],
    [1.0, 0.5], [1.0, 1.0], [0.0, 1.0],
]


class TestPointInRect(unittest.TestCase):

    # --- inside ---
    def test_center_is_inside(self):
        self.assertTrue(point_in_rect((0.5, 0.5), _UNIT_RECT))

    def test_off_center_is_inside(self):
        self.assertTrue(point_in_rect((0.4, 0.5), _SMALL_RECT))

    # --- outside ---
    def test_right_of_rect(self):
        self.assertFalse(point_in_rect((1.1, 0.5), _UNIT_RECT))

    def test_left_of_rect(self):
        self.assertFalse(point_in_rect((-0.1, 0.5), _UNIT_RECT))

    def test_above_rect(self):
        self.assertFalse(point_in_rect((0.5, 1.1), _UNIT_RECT))

    def test_below_rect(self):
        self.assertFalse(point_in_rect((0.5, -0.1), _UNIT_RECT))

    def test_outside_small_rect(self):
        self.assertFalse(point_in_rect((0.1, 0.5), _SMALL_RECT))

    # --- boundary (all four edges inclusive) ---
    def test_left_edge(self):
        self.assertTrue(point_in_rect((0.0, 0.5), _UNIT_RECT))

    def test_right_edge(self):
        self.assertTrue(point_in_rect((1.0, 0.5), _UNIT_RECT))

    def test_bottom_edge(self):
        self.assertTrue(point_in_rect((0.5, 0.0), _UNIT_RECT))

    def test_top_edge(self):
        self.assertTrue(point_in_rect((0.5, 1.0), _UNIT_RECT))

    def test_corner_origin(self):
        self.assertTrue(point_in_rect((0.0, 0.0), _UNIT_RECT))

    def test_corner_opposite(self):
        self.assertTrue(point_in_rect((1.0, 1.0), _UNIT_RECT))


class TestPointInPolygon(unittest.TestCase):

    # --- inside ---
    def test_center_inside_square(self):
        self.assertTrue(point_in_polygon((0.5, 0.5), _UNIT_SQUARE))

    def test_near_center_inside_square(self):
        self.assertTrue(point_in_polygon((0.25, 0.75), _UNIT_SQUARE))

    def test_inside_triangle(self):
        self.assertTrue(point_in_polygon((0.5, 0.3), _TRIANGLE))

    def test_inside_l_shape_top_left(self):
        self.assertTrue(point_in_polygon((0.2, 0.8), _L_SHAPE))

    def test_inside_l_shape_left_arm(self):
        self.assertTrue(point_in_polygon((0.2, 0.2), _L_SHAPE))

    # --- outside ---
    def test_right_of_square(self):
        self.assertFalse(point_in_polygon((1.5, 0.5), _UNIT_SQUARE))

    def test_left_of_square(self):
        self.assertFalse(point_in_polygon((-0.5, 0.5), _UNIT_SQUARE))

    def test_above_square(self):
        self.assertFalse(point_in_polygon((0.5, 1.5), _UNIT_SQUARE))

    def test_below_square(self):
        self.assertFalse(point_in_polygon((0.5, -0.5), _UNIT_SQUARE))

    def test_outside_triangle_far(self):
        self.assertFalse(point_in_polygon((0.9, 0.9), _TRIANGLE))

    def test_outside_triangle_below(self):
        self.assertFalse(point_in_polygon((0.5, -0.1), _TRIANGLE))

    def test_l_shape_indentation_is_outside(self):
        # the bottom-right quadrant is cut out of the L
        self.assertFalse(point_in_polygon((0.75, 0.25), _L_SHAPE))

    # --- boundary ---
    def test_boundary_returns_bool(self):
        # Ray casting has implementation-defined behavior exactly on edges;
        # we only assert the return type is bool, not which value.
        result = point_in_polygon((0.5, 0.0), _UNIT_SQUARE)
        self.assertIsInstance(result, bool)

    def test_vertex_returns_bool(self):
        result = point_in_polygon((0.0, 0.0), _UNIT_SQUARE)
        self.assertIsInstance(result, bool)

    # --- simple polygon (not square) ---
    def test_diamond_inside(self):
        diamond = [[0.5, 0.0], [1.0, 0.5], [0.5, 1.0], [0.0, 0.5]]
        self.assertTrue(point_in_polygon((0.5, 0.5), diamond))

    def test_diamond_corner_outside(self):
        diamond = [[0.5, 0.0], [1.0, 0.5], [0.5, 1.0], [0.0, 0.5]]
        self.assertFalse(point_in_polygon((0.0, 0.0), diamond))


if __name__ == '__main__':
    unittest.main()
