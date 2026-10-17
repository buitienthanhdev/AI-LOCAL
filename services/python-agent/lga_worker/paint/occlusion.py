"""Occlusion (who is in front of whom) as a graph: layers come from the relations, not from a hand-ordered list.

An edge `(back, front)` says "`front` is drawn after `back` and covers it where they meet". `assign_layers` turns an
acyclic edge set into the smallest layer numbers that respect every edge; `find_cycle` reports an impossible set;
`order_problems` checks a finished list of objects against required edges (the regression guard for layering bugs
such as an arm drawn on top of the torso).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

Edge = tuple[str, str]  # (back, front)


class OcclusionCycle(ValueError):
    def __init__(self, cycle: list[str]) -> None:
        super().__init__("occlusion cycle: " + " -> ".join(cycle))
        self.cycle = cycle


def find_cycle(edges: Iterable[Edge]) -> list[str] | None:
    graph: dict[str, list[str]] = {}
    for back, front in edges:
        graph.setdefault(back, []).append(front)
        graph.setdefault(front, [])
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for nxt in graph[node]:
            if state.get(nxt, 0) == 1:
                return path[path.index(nxt):] + [nxt]
            if state.get(nxt, 0) == 0:
                found = visit(nxt)
                if found:
                    return found
        state[node] = 2
        path.pop()
        return None

    for n in list(graph):
        if state.get(n, 0) == 0:
            found = visit(n)
            if found:
                return found
    return None


def assign_layers(objects: Sequence[str], edges: Iterable[Edge], *, floor: Mapping[str, int] | None = None) -> dict[str, int]:
    """Layer number per object: 0 for objects nothing has to be behind, otherwise 1 + the deepest object behind it.
    `floor` lets an object stay at least at a given layer (a stage the caller wants to keep). Raises
    `OcclusionCycle` for an impossible relation set."""
    edge_list = [e for e in edges if e[0] in set(objects) and e[1] in set(objects)]
    cycle = find_cycle(edge_list)
    if cycle:
        raise OcclusionCycle(cycle)
    behind: dict[str, list[str]] = {o: [] for o in objects}
    for back, front in edge_list:
        behind[front].append(back)
    layers: dict[str, int] = {}

    def depth(node: str) -> int:
        if node in layers:
            return layers[node]
        d = max((depth(b) + 1 for b in behind[node]), default=0)
        d = max(d, (floor or {}).get(node, 0))
        layers[node] = d
        return d

    for o in objects:
        depth(o)
    return layers


def order_problems(layers: Mapping[str, int], edges: Iterable[Edge], *, positions: Mapping[str, int] | None = None) -> list[str]:
    """Edges the finished ordering breaks: `front` must be on a higher layer than `back` (or, on the same layer, later
    in `positions`). An edge naming an object that is missing is a problem too."""
    out: list[str] = []
    for back, front in edges:
        if back not in layers or front not in layers:
            missing = back if back not in layers else front
            out.append(f"{missing!r} is missing but the relation {back!r} behind {front!r} needs it")
            continue
        if layers[front] > layers[back]:
            continue
        if layers[front] == layers[back] and positions and positions.get(front, 0) > positions.get(back, 0):
            continue
        out.append(f"{front!r} (layer {layers[front]}) must be drawn after {back!r} (layer {layers[back]})")
    return out
