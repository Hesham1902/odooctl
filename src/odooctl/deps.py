def build_graph(manifests):
    """{name: manifest dict} -> {name: [direct depends]}."""
    return {name: list(mf.get("depends") or []) for name, mf in manifests.items()}


def transitive_deps(graph, module):
    seen = set()

    def visit(mod):
        for dep in graph.get(mod, []):
            if dep not in seen:
                seen.add(dep)
                visit(dep)

    visit(module)
    return seen


def dependents(graph):
    """Inverted graph: {module: set(direct dependents inside custom_addons)}."""
    rev = {}
    for mod, deps in graph.items():
        for dep in deps:
            rev.setdefault(dep, set()).add(mod)
    return rev


def find_cycle(graph, start):
    """Return a cycle path [a, b, ..., a] through start, or None."""
    stack = [(start, [start])]
    while stack:
        node, path = stack.pop()
        for dep in graph.get(node, []):
            if dep == start:
                return path + [start]
            if dep in path:
                continue
            if len(path) > len(graph) + 1:
                continue
            stack.append((dep, path + [dep]))
    return None
