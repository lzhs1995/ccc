"""Validate recorded terminal identities independently of pane placement.

The live workspace guard authenticates these pins before gate creation. This
offline check verifies their consistency; it does not claim a fresh live read.
"""
import uuid


VERSION = 1


def check_gate(gate):
    """New gates must prove distinct terminals, even when their panes coincide."""
    result = dict(policy="distinct-terminal-surfaces-v1", status="FAIL", **{"pass": False})
    try:
        def require(ok, reason):
            if not ok:
                raise ValueError(reason)

        def uid(value):
            return str(uuid.UUID(value)).upper()

        def reference(value):
            require(isinstance(value, str) and bool(value), "missing reference")
            return value

        require(type(gate.get("terminal_channels_version")) is int
                and gate["terminal_channels_version"] == VERSION, "channel version")
        workspace = uid(gate["workspace_uuid"])
        workspace_ref = reference(gate["workspace_ref"])
        supervisor = dict(surface_ref=reference(gate["supervisor"]),
                          surface_uuid=uid(gate["supervisor_surface_uuid"]),
                          pane_ref=reference(gate["supervisor_pane_ref"]),
                          pane_uuid=uid(gate["supervisor_pane_uuid"]))
        require(gate.get("supervisor_surface_type") == "terminal", "supervisor type")
        peers = gate["executors"]
        require(isinstance(peers, list) and bool(peers), "missing executors")
        proofs = gate["workspace_proofs"]
        require(isinstance(proofs, dict), "missing workspace proofs")
        require(set(proofs) == {p["surface_ref"] for p in peers}, "workspace proof targets")
        surface_refs = {supervisor["surface_ref"]}
        surface_ids = {supervisor["surface_uuid"]}
        pane_refs, pane_ids = {}, {}
        participants = [supervisor]
        for peer in peers:
            row = dict(surface_ref=reference(peer["surface_ref"]),
                       surface_uuid=uid(peer["surface_uuid"]),
                       pane_ref=reference(peer["pane_ref"]), pane_uuid=uid(peer["pane_uuid"]))
            require(peer.get("surface_type") == "terminal", "executor type")
            require(uid(peer["workspace_uuid"]) == workspace
                    and peer["workspace_ref"] == workspace_ref, "executor workspace")
            require(row["surface_ref"] not in surface_refs
                    and row["surface_uuid"] not in surface_ids, "duplicate terminal identity")
            surface_refs.add(row["surface_ref"])
            surface_ids.add(row["surface_uuid"])
            proof = proofs[row["surface_ref"]]
            for key, expected in dict(workspace_uuid=workspace,
                                      caller_surface_uuid=supervisor["surface_uuid"],
                                      caller_pane_uuid=supervisor["pane_uuid"],
                                      target_surface_uuid=row["surface_uuid"],
                                      target_pane_uuid=row["pane_uuid"]).items():
                require(uid(proof[key]) == expected, "workspace proof " + key)
            for key, expected in dict(workspace_ref=workspace_ref,
                                      caller_surface_ref=supervisor["surface_ref"],
                                      target_surface_ref=row["surface_ref"]).items():
                require(proof[key] == expected, "workspace proof " + key)
            participants.append(row)

        # A pane ref and UUID must name the same pane throughout the snapshot.
        # Sharing that exact pair is supported; alias disagreement is not.
        for row in participants:
            pref, puid = row["pane_ref"], row["pane_uuid"]
            require(pane_refs.setdefault(pref, puid) == puid
                    and pane_ids.setdefault(puid, pref) == pref, "pane alias disagreement")
        first = peers[0]
        for singular, plural in (("executor", "surface_ref"),
                                 ("executor_pane_ref", "pane_ref"),
                                 ("executor_surface_uuid", "surface_uuid")):
            require(gate[singular] == first[plural], "singular executor disagreement")
        shared = [pref for pref in pane_refs
                  if sum(p["pane_ref"] == pref for p in participants) > 1]
        require(gate.get("side_panel") is (not shared), "side_panel layout disagreement")
        result.update(status="PASS", **{"pass": True}, shared_panes=shared,
                      executor_count=len(peers), input_serialization="pane-and-target-mutex")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        result["reason"] = str(exc)
    return result
