"""Direct Linux measurement; this diagnostic does not approve product source."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    output.mkdir()
    binary = Path(shutil.which("bwrap")).resolve()
    help_text = subprocess.check_output([str(binary), "--help"], text=True)
    identity = {
        "path": str(binary),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "version": subprocess.check_output([str(binary), "--version"], text=True).strip(),
        "capabilities": {key: key in help_text for key in ("--as-pid-1", "--perms", "--ro-bind-fd")},
        "host_dev_fd": os.readlink("/dev/fd"),
        "kernel": os.uname().release,
    }
    (output / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    base = ["--unshare-user", "--unshare-pid", "--as-pid-1", "--ro-bind", "/", "/", "--dev", "/dev"]
    mask = ["--dir", "/dev/fd", "--perms", "000", "--tmpfs", "/dev/fd", "--remount-ro", "/dev/fd"]
    check = ["/bin/sh", "-c", 'if ls /dev/fd >/dev/null 2>&1; then echo EXPOSED; exit 42; fi; echo DENIED']
    cases = {
        "working_unmasked_control": base + ["--proc", "/proc", "--", "/bin/true"],
        "original_order": base + mask + ["--proc", "/proc", "--", *check],
        "proc_before_logical_mask": base + ["--proc", "/proc"] + mask + ["--", *check],
        "proc_before_physical_mask": base + ["--proc", "/proc", "--perms", "000", "--tmpfs", "/proc/self/fd", "--remount-ro", "/proc/self/fd", "--", *check],
    }
    results = []
    with tempfile.TemporaryDirectory() as empty:
        cases["readonly_empty_bind"] = base + ["--ro-bind", empty, "/dev/fd", "--proc", "/proc", "--", *check]
        for name, argv in cases.items():
            command = [str(binary), *argv]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            item = {"name": name, "argv": command, "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            results.append(item)
            print(json.dumps(item), flush=True)
            if name in ("original_order", "proc_before_logical_mask", "proc_before_physical_mask"):
                traced = subprocess.run(["strace", "-f", "-s", "256", "-e", "trace=mount,mkdir,newfstatat,symlink,readlink,pivot_root,execve", "-o", str(output / (name + ".strace")), *command], capture_output=True, text=True, timeout=20)
                (output / (name + ".trace-result.json")).write_text(json.dumps({"rc": traced.returncode, "stdout": traced.stdout, "stderr": traced.stderr}, indent=2) + "\n")
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    assert results[0]["rc"] == 0, "unmasked environment control must succeed"
    assert results[1]["rc"] != 0 and "Can't mount tmpfs on /newroot/dev/fd" in results[1]["stderr"], "original fault must reproduce"


if __name__ == "__main__":
    main()
