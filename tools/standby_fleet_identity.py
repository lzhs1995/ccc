"""Join already observed fleet identities; does not certify live acceptance."""


def identity(row):
    pid, birth = row['pid'], row['birth']
    if (type(pid) is not int or pid <= 0 or not isinstance(birth, list)
            or len(birth) != 2 or any(type(n) is not int for n in birth)
            or birth[0] <= 0 or not 0 <= birth[1] < 1000000):
        raise ValueError('exact original process generation required')
    if any(not isinstance(row[k], str) or not row[k] for k in ('session_id', 'surface_id')):
        raise ValueError('original session and surface required')
    return row['session_id'], row['surface_id'], pid, tuple(birth)


def verify(*, witnesses, metrics, settled, completed, overlap, resources, check=lambda: None):
    """Reject count-preserving substitutions between independently collected evidence."""
    keys = set(witnesses)
    if len(keys) != 10 or set(metrics) != keys:
        raise ValueError('exact ten original cohorts required')
    originals, seen = {}, [set() for _ in range(3)]
    for key, rows in witnesses.items():
        check()
        if set(rows) != set(range(50)) or any(type(i) is not int for i in rows):
            raise ValueError('exact fifty original indices required')
        originals[key] = {}
        for index, row in rows.items():
            value = identity(row)
            for values, field in zip(seen, (value[0], value[1], value[2])):
                if field in values:
                    raise ValueError('duplicate original fleet identity')
                values.add(field)
            originals[key][index] = value
    flat = {v for rows in originals.values() for v in rows.values()}
    paths = {}
    for phase, observation in (('settled', settled), ('completed', completed)):
        check()
        if (observation['phase'] != phase or set(observation['writers']) != keys
                or set(observation['cohorts']) != keys):
            raise ValueError('original cohort observation mismatch')
        for key, rows in observation['writers'].items():
            actual = {}
            for row in rows:
                index = row['index']
                if type(index) is not int or index in actual:
                    raise ValueError('duplicate or invalid writer index')
                actual[index] = identity(row)
                path = row['transcript']
                if not isinstance(path, str) or not path.startswith('/'):
                    raise ValueError('absolute original transcript required')
                slot = (key, index)
                if slot in paths and paths[slot] != path:
                    raise ValueError('original transcript changed between observations')
                paths[slot] = path
            if actual != originals[key]:
                raise ValueError('writer observation differs from original fleet')
    check()
    observed = [identity(r) for r in overlap['originals']]
    if len(observed) != 500 or set(observed) != flat:
        raise ValueError('process overlap differs from original fleet')
    native = [r for r in resources['processes'] if r['role'] == 'native']
    generations = []
    for row in native:
        # Apply the same strict generation validation without inventing session proof.
        value = identity(dict(row, session_id='resource', surface_id='resource'))
        generations.append(value[2:])
    if len(native) != 500 or set(generations) != {v[2:] for v in flat}:
        raise ValueError('resource sample differs from original fleet')
    for key, result in metrics.items():
        check()
        bindings = result['transcript_bindings']
        chains = result['chains']
        if len(bindings) != 50 or len(chains) != 50:
            raise ValueError('exact original performance bindings required')
        indices = set()
        for binding, chain in zip(bindings, chains):
            index = binding['index']
            if type(index) is not int or index in indices or index not in originals[key]:
                raise ValueError('duplicate or foreign performance index')
            indices.add(index)
            if (binding['original'] != paths[key, index]
                    or (chain['session_id'], chain['surface_id']) != originals[key][index][:2]):
                raise ValueError('performance differs from observed original writer')
    check()
    return dict(cohorts=10, original_sessions=500, identity_join_verified=True,
                full_500_acceptance=False,
                scope='Cross-evidence identity join only; not raw replay, live continuity or terminal acceptance.')
