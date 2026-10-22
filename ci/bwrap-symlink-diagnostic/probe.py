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
    # Candidate construction, not product adoption: create a device directory
    # without the absolute fd symlink, then apply the original immutable mask.
    # Measure nested PTY compatibility separately before considering this route.
    filtered = ["--unshare-user", "--unshare-pid", "--as-pid-1", "--ro-bind", "/", "/", "--tmpfs", "/dev"]
    for name in ("null", "zero", "full", "random", "urandom", "tty"):
        filtered += ["--dev-bind", "/dev/" + name, "/dev/" + name]
    for name, descriptor in (("stdin", "0"), ("stdout", "1"), ("stderr", "2")):
        filtered += ["--symlink", "/proc/self/fd/" + descriptor, "/dev/" + name]
    filtered += ["--dir", "/dev/shm", "--symlink", "/proc/kcore", "/dev/core"]
    cases["filtered_dev_denial"] = filtered + mask + ["--proc", "/proc", "--", *check]
    io_check = ["/bin/sh", "-c", "printf ok >/dev/null && test -c /dev/zero && test -c /dev/urandom && printf IO_OK"]
    cases["filtered_dev_io"] = filtered + mask + ["--proc", "/proc", "--", *io_check]
    pty_check = ["/usr/bin/python3", "-I", "-B", "-c", "import os; a,b=os.openpty(); print(os.ttyname(b)); os.close(a); os.close(b)"]
    cases["filtered_dev_pty"] = filtered + mask + ["--proc", "/proc", "--", *pty_check]
    cases["filtered_dev_nested_private_pty"] = filtered + ["--dev", "/dev/pts", "--symlink", "pts/pts/ptmx", "/dev/ptmx"] + mask + ["--proc", "/proc", "--", *pty_check]
    private_pts = ["--dev", "/dev/.codex-pty", "--symlink", ".codex-pty/pts", "/dev/pts", "--symlink", "pts/ptmx", "/dev/ptmx"]
    alias_check = ["/usr/bin/python3", "-I", "-B", "-c", "import os,stat; a,b=os.openpty(); name=os.ttyname(b); n=name.rsplit('/',1)[1]; c=os.open('/dev/pts/'+n,os.O_RDWR|os.O_NOCTTY); assert os.fstat(b).st_rdev==os.fstat(c).st_rdev; assert not stat.S_ISLNK(os.lstat('/dev/fd').st_mode); os.close(c); os.close(a); os.close(b); print('PRIVATE_PTY_STANDARD_PATH_OK')"]
    cases["filtered_dev_alias_private_pty"] = filtered + private_pts + mask + ["--proc", "/proc", "--", *alias_check]
    cases["filtered_dev_alias_denial"] = filtered + private_pts + mask + ["--proc", "/proc", "--", *check]
    cases["normal_dev_pty_control"] = base + ["--proc", "/proc", "--", *pty_check]
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
    denial = next(item for item in results if item["name"] == "filtered_dev_denial")
    assert denial["rc"] == 0 and denial["stdout"].strip() == "DENIED", "candidate must deny the original logical path"


if __name__ == "__main__":
    main()
