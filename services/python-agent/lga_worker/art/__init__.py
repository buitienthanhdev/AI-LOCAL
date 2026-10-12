"""The drawing brain: one application-neutral picture (`scene.Scene`) drawn by any application that can draw.

Sources (image tracing, SVG written by the planner, templates, hand-placed objects) all produce a Scene; backends
(Paint, AutoCAD, PowerPoint/Word/Excel shapes, SVG/PNG files) each draw it with what the application is good at.
"""
