"""Offline regression for existing Rust on PATH without ~/.cargo/bin."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import release_build as build
import source_contract as shared

# Exact upstream installer fixture, authenticated before use.
INSTALLER_SCRIPT = b'#!/usr/bin/env bash\nset -euo pipefail\n\nexport PATH="$HOME/.cargo/bin:$PATH"\n\nif ! command -v rustup >/dev/null 2>&1; then\n  curl --proto \'=https\' --tlsv1.2 -sSf https://sh.rustup.rs \\\n    | sh -s -- -y --profile minimal --default-toolchain stable\n  export PATH="$HOME/.cargo/bin:$PATH"\nfi\n\nif [ -n "${BASH_ENV:-}" ]; then\n  echo \'export PATH="$HOME/.cargo/bin:$PATH"\' >> "$BASH_ENV"\nfi\n\nif [ -n "${GITHUB_PATH:-}" ]; then\n  echo "$HOME/.cargo/bin" >> "$GITHUB_PATH"\nfi\n\nif ! rustup show active-toolchain >/dev/null 2>&1; then\n  rustup default stable\nfi\n\nrustup target add aarch64-apple-darwin x86_64-apple-darwin\ncargo --version\nrustc --version\n\nif [ -f Native/DiffSidecar/rust-toolchain.toml ]; then\n  DIFF_RUST_TOOLCHAIN="$(awk -F \'"\' \'/^[[:space:]]*channel[[:space:]]*=/{print $2; exit}\' Native/DiffSidecar/rust-toolchain.toml)"\n  rustup toolchain install "$DIFF_RUST_TOOLCHAIN" --profile minimal --component clippy,rustfmt\n  rustup target add --toolchain "$DIFF_RUST_TOOLCHAIN" aarch64-apple-darwin x86_64-apple-darwin\n  rustup run "$DIFF_RUST_TOOLCHAIN" cargo --version\n  rustup run "$DIFF_RUST_TOOLCHAIN" rustc --version\nfi\n'


class RustPathRegression(unittest.TestCase):
    def test_pinned_installer_reuses_package_manager_path_without_cargo_home(self):
        installer = INSTALLER_SCRIPT
        self.assertEqual(hashlib.sha256(installer).hexdigest(),
                         "6ef6ece19cd8c4372ec9bad79e1cd9b2f9c41bce6fdeb87f7c3f2b13030d87f3")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "scripts").mkdir(parents=True)
            installed = source / "scripts/install-rust-ci.sh"
            installed.write_bytes(installer)
            installed.chmod(0o700)
            account = root / "account"
            account.mkdir()
            tools = root / "package-manager/bin"
            tools.mkdir(parents=True)
            # The original shell script is real. Only its Rust executables are
            # fixtures, so the regression performs no install or network access.
            (tools / "rustup").write_text("#!/bin/sh\nexit 0\n")
            for name in ("cargo", "rustc"):
                (tools / name).write_text("#!/bin/sh\n[ \"$1\" = --version ] || exit 91\n"
                                         "printf '%s\\n' '" + name + " 1.99.1 (fixture 2026-10-13)'\n")
            for executable in tools.iterdir():
                executable.chmod(0o700)
            env = {"HOME": str(account), "PATH": str(tools) + os.pathsep + "/usr/bin:/bin",
                   "GITHUB_PATH": str(root / "toolchain.path")}
            commands = shared.Commands(root / "commands", cwd=source, environment=env)
            def run(argv, timeout=60):
                return commands.text(argv, timeout=timeout)
            build.prepare_rust_environment(env, run)
            self.assertFalse((account / ".cargo/bin").exists())
            self.assertEqual((root / "toolchain.path").read_text(), str(account / ".cargo/bin") + "\n")
            for number, name in ((2, "cargo"), (3, "rustc")):
                receipt = json.loads((commands.root / f"{number:03d}.result.json").read_text())
                self.assertEqual(receipt["argv"], [name, "--version"])
                self.assertEqual(receipt["returncode"], 0)
                self.assertTrue((commands.root / receipt["stdout"]["file"]).read_text().startswith(name + " 1.99.1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
