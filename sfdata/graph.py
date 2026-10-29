"""Ordre de chargement d'apres les relations : parents avant enfants, cycles et auto-references differes."""


def load_order(refs: dict) -> tuple[list, dict]:
    """refs: {objet: {champ: {objets cibles}}} -> (ordre, {objet: {champs differes}})."""
    order, state, deferred = [], {}, {}

    def visit(n):
        state[n] = 1
        for field, targets in sorted(refs.get(n, {}).items()):
            for t in sorted(targets):
                if t == n or state.get(t) == 1:
                    deferred.setdefault(n, set()).add(field)
                elif t not in state:
                    visit(t)
        state[n] = 2
        order.append(n)

    for n in sorted(refs):
        if n not in state:
            visit(n)
    return order, deferred
