"""Paint drawing engine (pure part): scene model, geometry, reference raster, pixel verification.

The part that drives Paint's window is a separate, live-verified layer; nothing in this package touches the
screen, so it is fully unit-testable.
"""
