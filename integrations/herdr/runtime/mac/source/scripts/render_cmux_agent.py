#!/usr/bin/env python3
"""Render a pinned cmux-agent helper without its obsolete sending functions.

The integration owner installs the result after reviewing the baseline diff.
This script never replaces the live helper or changes client configuration.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

BASELINE_SHA256 = "38d095d8065a72538cfa32c5e6efd3cc9308372f1d728b90aefd55580ab336b3"


def verify_route(raw, release_root):
    """只读验证完整 shell；路径文本或一个看似正确的 exec 不能证明唯一路由。"""
    release_root = Path(release_root).resolve()
    text = raw.decode("utf-8")
    lines = re.findall(r'^    exec (.+) "\$@"$', text, re.MULTILINE)
    if len(lines) != 1:
        raise ValueError("HELPER_SINGLE_ADAPTER_ROUTE_REQUIRED")
    args = shlex.split(lines[0])
    adapter = release_root / "scripts/cmux_agent_adapter.py"
    if (len(args) != 4 or args[1:3] != ["-I", "-B"]
            or args[3] != str(adapter) or not Path(args[0]).is_absolute()):
        raise ValueError("HELPER_FIXED_RELEASE_ROUTE_REQUIRED")
    baseline = release_root / "tests/fixtures/cmux-agent-legacy.sh"
    expected = render(baseline.read_bytes(), expected_sha256=BASELINE_SHA256,
                      python=args[0], adapter=adapter)
    if raw != expected:
        raise ValueError("HELPER_RENDERED_BYTES_CHANGED")
    return {"adapter": str(adapter), "adapter_sha256": sha(adapter.read_bytes()),
            "python": args[0], "baseline_sha256": BASELINE_SHA256,
            "helper_sha256": sha(raw), "route_verified": True}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def remove_between(text, start, end):
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("HELPER_BASELINE_SHAPE_CHANGED: " + start)
    left, right = text.index(start), text.index(end)
    if right <= left:
        raise ValueError("HELPER_BASELINE_ORDER_CHANGED")
    return text[:left] + text[right:]


def render(original, *, expected_sha256, python, adapter):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or sha(original) != expected_sha256:
        raise ValueError("HELPER_BASELINE_SHA_CHANGED")
    for path in (python, adapter):
        if not Path(path).is_absolute() or not Path(path).is_file():
            raise ValueError("HELPER_FIXED_ABSOLUTE_FILE_REQUIRED: " + str(path))
    if not os.access(python, os.X_OK):
        raise ValueError("HELPER_PYTHON_NOT_EXECUTABLE")
    text = original.decode("utf-8")
    prefix = "#!/usr/bin/env bash\nset -euo pipefail\n"
    if not text.startswith(prefix):
        raise ValueError("HELPER_BASELINE_HEADER_CHANGED")
    route = (
        "\n# Sending has exactly one route; never fall back to raw cmux or screen guesses.\n"
        'case "${1:-}" in\n'
        "  self|ask|send|broadcast|reconcile)\n"
        # 保留虚拟环境入口；resolve() 会越过符号链接，丢失该环境的依赖。
        "    exec " + shlex.quote(str(Path(python))) + " -I -B "
        + shlex.quote(str(Path(adapter).resolve())) + ' "$@"\n'
        "    ;;\n"
        "esac\n"
    )
    text = prefix + route + text[len(prefix):]
    text = remove_between(text, "canonical_surface() {\n", "require_args() {\n")
    text = remove_between(text, "terminal_surfaces_except_self() {\n", "read_surface() {\n")
    text = remove_between(text, "new_delivery_id() {\n", "protocol() {\n")
    old_protocol = text[text.index("protocol() {\n"):text.index('cmd="${1:-help}"')]
    new_protocol = """protocol() {
  cat <<'PROTOCOL'
cmux agent collaboration protocol

Use ask/send for authorized same-workspace coordination. Formal task dispatch
and completion callbacks use the bridge's task-pack and callback entrypoints.
ask/send/broadcast/reconcile all use the installed, pinned bridge/journal.
Exit 0 requires revalidation of the original journal's native user receipt.
Enter, an empty composer, screen activity, and a queued_command are not delivery.
Exit 75 preserves the original intent. Reconcile reads only: no key or repaste.
Reconcile requires --intent <absolute original intent> to confirm that request.
Without it, only the pending intent path is reported; exit 75 preserves pending.
Repeated identical requests keep their marker. --request-id is for a deliberately
new authorized request, never a retry workaround. Unknown old pending is kept.
Broadcast is bound to its original same-workspace terminal target set.
Payload still in compose is recovered only by its original guarded controller,
under the original binding and bounded budget, never by this helper.
STATUS:, DONE:, and BLOCKED: are message conventions, not success evidence.
PROTOCOL
}

"""
    text = text.replace(old_protocol, new_protocol, 1)
    text = remove_between(text, "  reconcile)\n", "  read)\n")
    text = remove_between(text, "  send)\n", "  status)\n")
    text = text.replace("identify|tree|read-screen|send|send-key)", "identify|tree|read-screen)")
    text = text.replace("(tracked submission, not consumption)", "(native receipt required)")
    text = text.replace("(raw submit, no marker confirmation)", "(native receipt required)")
    text = text.replace("  cmux-agent reconcile <surface-ref>  (read-only pending delivery check)",
                        "  cmux-agent reconcile <surface-ref> --intent <absolute-original-intent>  (read-only)")
    text = text.replace("  cmux-agent broadcast <message...>\n",
                        "  cmux-agent broadcast <message...>  (original same-workspace targets)\n")
    text = text.replace("Message protocol:\n", "Retry the same message with the same --request-id; reconcile never sends input.\n\nMessage protocol:\n")
    for obsolete in ("RAW_UNVERIFIED", "canonical_surface()", "submit_text()", "send_text()",
                     "reconcile_surface()", "ask_surface()", "compose_key()", "atomic_paste()",
                     'cmux send ', 'cmux send-key ', "DISPATCH_SUBMITTED", "DISPATCH_QUEUED"):
        if obsolete in text:
            raise ValueError("HELPER_OBSOLETE_SENDER_REMAINS: " + obsolete)
    return text.encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--baseline-sha256", required=True)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.output.is_absolute():
        parser.error("output must be absolute")
    output = render(args.baseline.read_bytes(), expected_sha256=args.baseline_sha256,
                    python=args.python, adapter=args.adapter)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a live helper, unrelated candidate, or previous evidence.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    with os.fdopen(fd, "wb") as handle:
        handle.write(output)
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(dict(baseline=str(args.baseline), baseline_sha256=args.baseline_sha256,
                         python=str(args.python), adapter=str(args.adapter.resolve()),
                         adapter_sha256=sha(args.adapter.read_bytes()),
                         output=str(args.output), output_sha256=sha(output),
                         live_helper_replaced=False), ensure_ascii=False))


if __name__ == "__main__":
    main()
