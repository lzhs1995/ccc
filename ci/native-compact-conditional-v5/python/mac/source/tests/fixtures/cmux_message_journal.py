"""Test-only authoritative-receipt double; not native delivery evidence."""
import json


def verified_no_input(bridge, surface, text, marker, *, commit=None):
    """This CLI double has no canonical journal and cannot prove zero input.

    Real-journal settlement is covered by test_helper_no_input.py.
    """
    return None


def verified_receipt(bridge, surface, text, marker):
    bridge.event(dict(op="verify", marker=marker))
    data = bridge.config()
    if data.get("mutate_on_verify"):
        bridge.mutate(data["mutate_on_verify"])
    if data.get("malformed_proof"):
        return {"source": "screen_guess", "confirmed": True}
    path = bridge.ROOT / "fixture-receipts" / (marker + ".json")
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    expected = dict(marker=marker, payload=text, surface=bridge.normalize(surface),
                    identity=bridge.pin_workspace(surface))
    for key in value["identity"]:
        value["identity"][key] = bridge.normalize(value["identity"][key])
        expected["identity"][key] = bridge.normalize(expected["identity"][key])
    if value != expected:
        return None
    return dict(source="revalidated_message_dispatch_v1", identity=expected["identity"],
                attempt=["fixture-only", 1], receipt=["fixture-only", 1])
