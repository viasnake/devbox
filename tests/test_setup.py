"""Check the native setup task without installing host packages."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(
    os.name == "nt" and shutil.which("mise"), "Requires native Windows and mise"
)
class WindowsSetup(unittest.TestCase):
    def check_winget_result(self, winget_exit, expected_success):
        with tempfile.TemporaryDirectory(prefix="devbox-setup-") as temporary:
            root = Path(temporary)
            # A batch stub preserves WinGet's signed 32-bit exit code on Windows.
            (root / "winget.cmd").write_text(
                f"@echo off\necho WINGET_STUB %*\nexit /b {winget_exit}\n",
                encoding="ascii",
            )
            env = dict(os.environ)
            env.update(
                PATH=str(root) + os.pathsep + env["PATH"],
                MISE_STATE_DIR=str(root / "state"),
                MISE_CACHE_DIR=str(root / "cache"),
                MISE_OFFLINE="1",
            )
            result = subprocess.run(
                [shutil.which("mise"), "run", "--skip-tools", "setup-qemu"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertIn(
                "WINGET_STUB install --id SoftwareFreedomConservancy.QEMU", output
            )
            self.assertIn("--no-upgrade", output)
            self.assertEqual(result.returncode == 0, expected_success, output)

    def test_install_success(self):
        self.check_winget_result(0, True)

    def test_existing_package_is_success(self):
        self.check_winget_result(-1978335135, True)

    def test_other_install_failure_is_preserved(self):
        self.check_winget_result(23, False)
