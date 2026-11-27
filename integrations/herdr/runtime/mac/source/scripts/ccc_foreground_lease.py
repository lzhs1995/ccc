"""Pure schema2 lease validation shared by prospective CCC/MAC readers."""
import uuid

LEASE_MS = 5000


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("ambiguous foreground observation")
        result[name] = value
    return result


def validate(data, key, epoch, stamp, thread, root_identity, directory, *,
             cache, wall_ms, local_mono, wire_ns):
    """Return an uncommitted cache candidate and heartbeat-stable proof.

    The caller validates actual files, native process/role and opt-in, then
    validates the final read before committing this candidate to its cache.
    CLOCK_MONOTONIC wire expiry is authoritative even for a first reader
    after a wall-clock adjustment. No replay or transport action occurs here.
    """
    expires = data.get("expires_at_ms")
    generation = data.get("selection_generation")
    connection = data.get("connection_epoch")
    control = data.get("control_eligible")
    status = data.get("selection_status")
    mono_stamp = data.get("monotonic_published_ns")
    mono_expires = data.get("monotonic_expires_ns")
    if (type(expires) is not int or expires - stamp != LEASE_MS or expires <= wall_ms
            or stamp > wall_ms + 1000
            or type(generation) is not int or not 1 <= generation < 2**63
            or type(control) is not bool):
        raise ValueError("invalid or expired foreground lease")
    if (data.get("monotonic_clock") != "clock_monotonic"
            or type(mono_stamp) is not int or mono_stamp < 0 or mono_stamp > wire_ns
            or type(mono_expires) is not int
            or mono_expires - mono_stamp != LEASE_MS * 1_000_000
            or mono_expires <= wire_ns):
        raise ValueError("invalid or expired native monotonic lease")
    if connection is not None and (not isinstance(connection, str)
            or str(uuid.UUID(connection)) != connection):
        raise ValueError("invalid native connection epoch")
    if ((thread is None and (control or status != "clear"))
            or (thread is not None and (not control or status != "live" or connection is None))):
        raise ValueError("selection is not a live native attachment")
    semantic = (epoch, connection, generation, thread, control, status)
    previous = cache.get(key)
    if previous is not None:
        if (previous["epoch"] != epoch or generation < previous["generation"]
                or stamp < previous["stamp"] or mono_stamp < previous["mono_stamp"]
                or (generation == previous["generation"] and semantic != previous["semantic"])):
            raise ValueError("native selection generation regressed or changed without commit")
        if mono_stamp == previous["mono_stamp"]:
            deadline = previous["deadline"]
        else:
            deadline = local_mono + (mono_expires - wire_ns) / 1_000_000_000
    else:
        if len(cache) >= 4096:
            raise ValueError("foreground generation cache exhausted")
        deadline = local_mono + (mono_expires - wire_ns) / 1_000_000_000
    if local_mono >= deadline:
        raise ValueError("foreground lease expired on monotonic clock")
    candidate = {"epoch": epoch, "generation": generation, "semantic": semantic,
                 "stamp": stamp, "mono_stamp": mono_stamp, "deadline": deadline}
    evidence = ("native-foreground-lease-v2", key, *semantic, root_identity[:4], str(directory))
    return candidate, evidence
