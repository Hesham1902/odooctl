def parse_module_states(psql_output):
    """Parse `psql -At -F|` output of name/state/version into {name: (state, version)}."""
    states = {}
    for line in psql_output.splitlines():
        if "|" not in line:
            continue
        parts = line.split("|")
        if not parts[0]:
            continue
        state = parts[1] if len(parts) > 1 else "-"
        version = parts[2] if len(parts) > 2 else "-"
        states[parts[0]] = (state, version)
    return states


def compare(states_a, states_b):
    """Diff two {name: (state, version)} maps.

    Returns {"changed": {name: (tuple_a, tuple_b)}, "only_a": [...], "only_b": [...]}.
    """
    changed = {}
    for name in sorted(set(states_a) & set(states_b)):
        a, b = states_a[name], states_b[name]
        if a != b:
            changed[name] = (a, b)
    return {
        "changed": changed,
        "only_a": sorted(set(states_a) - set(states_b)),
        "only_b": sorted(set(states_b) - set(states_a)),
    }
