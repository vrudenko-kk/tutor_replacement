"""Min-cost max-flow с остаточными рёбрами и отрицательными стоимостями."""
from dataclasses import dataclass


@dataclass
class Edge:
    to: int
    reverse: int
    capacity: int
    cost: int


class FlowGraph:
    def __init__(self):
        self.ids = {}
        self.graph = []

    def node(self, name):
        if name not in self.ids:
            self.ids[name] = len(self.graph)
            self.graph.append([])
        return self.ids[name]

    def add(self, source, target, capacity, cost=0):
        u, v = self.node(source), self.node(target)
        forward = Edge(v, len(self.graph[v]), capacity, cost)
        backward = Edge(u, len(self.graph[u]), 0, -cost)
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return forward

    def solve(self, source, target, required):
        s, t = self.node(source), self.node(target)
        flow = cost = 0
        n = len(self.graph)
        while flow < required:
            distance = [float("inf")] * n
            parent = [None] * n
            distance[s] = 0
            # Bellman–Ford: учитывает отрицательные стоимости истории и предпочтений.
            for _ in range(n - 1):
                changed = False
                for u, edges in enumerate(self.graph):
                    if distance[u] == float("inf"):
                        continue
                    for i, edge in enumerate(edges):
                        value = distance[u] + edge.cost
                        if edge.capacity and value < distance[edge.to]:
                            distance[edge.to] = value
                            parent[edge.to] = (u, i)
                            changed = True
                if not changed:
                    break
            if parent[t] is None:
                break
            v = t
            while v != s:
                u, i = parent[v]
                edge = self.graph[u][i]
                edge.capacity -= 1
                self.graph[v][edge.reverse].capacity += 1
                v = u
            flow += 1
            cost += int(distance[t])
        return flow, cost
