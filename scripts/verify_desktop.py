"""Exercise packaged executables outside the checkout with synthetic accounts."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    package = Path(sys.argv[1]).resolve()
    if sys.platform == "darwin":
        bin_dir = package / "Contents/MacOS"
    else:
        bin_dir = package
    cli = bin_dir / ("yog-sothoth.exe" if os.name == "nt" else "yog-sothoth")
    gui = bin_dir / ("Yog-Sothoth-Desktop.exe" if os.name == "nt" else "Yog-Sothoth-Desktop")
    docs = package / "Contents/Resources" if sys.platform == "darwin" else package
    assert (docs / "licenses/qt-qtbase/LICENSES/LGPL-3.0-only.txt").is_file()
    assert (docs / "licenses/Python/LICENSE").is_file()
    spec = importlib.util.spec_from_file_location("portable", PROJECT / "tests/test_portable_cli.py")
    portable = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(portable)

    class PackagedCliTest(portable.PortableCliTest):
        def invoke(self, tool, *arguments, expected=0):
            env = dict(self.env, FIXTURE_TOOL=tool)
            result = subprocess.run([str(cli), tool, *arguments], cwd=self.base, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            return result

        def test_packaged_gui_and_console_helper(self):
            env = dict(self.env, HOME=str(self.base), USERPROFILE=str(self.base))
            for tool in ("codex", "claude", "agy"):
                env[tool.upper() + "_SWITCHER_" + tool.upper()] = str(self.base / "absent-cli")
            subprocess.run([str(gui), "--check"], cwd=self.base, env=env, check=True, timeout=60)
            self.invoke("codex", "add", "personal")
            self.invoke("codex", "add", "work")
            self.invoke("codex", "use", "work")
            env = dict(self.env, FIXTURE_TOOL="codex")
            prompt = 'literal spaces; $(do-not-execute) & "quoted"'
            result = subprocess.run([str(cli), "_terminal", "--cwd", str(self.base),
                                     "codex", "run", "--", prompt], cwd=self.base,
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))
            self.assertEqual(Path(data["home"]).name, "work")
            self.assertEqual(data["args"][-1], prompt)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PackagedCliTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
