"""Verify wheel contents and exercise installed CLI commands outside a checkout."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile


def exercise(source: Path):
    import switcher_cli
    installed = Path(switcher_cli.__file__).resolve().parent
    if installed == source:
        raise SystemExit("Verification imported the checkout instead of the installed package.")
    spec = importlib.util.spec_from_file_location("portable_installed", source / "tests/test_portable_cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROJECT = installed
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    return 0 if result.wasSuccessful() else 1


def main():
    source = Path(__file__).resolve().parents[1]
    if sys.argv[1:2] == ["--exercise"]:
        return exercise(source)
    wheel = Path(sys.argv[1]).resolve()
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if not (name.endswith(".py") and "/" not in name or ".dist-info/" in name):
                raise SystemExit("Unexpected wheel file: " + name)
    with tempfile.TemporaryDirectory(prefix="yog installed spaces ") as directory:
        base = Path(directory)
        environment = base / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], check=True)
        env = os.environ.copy()
        for tool in ("codex", "claude", "agy"):
            env[tool.upper() + "_SWITCHER_HOME"] = str(base / tool)
            env[tool.upper() + "_SWITCHER_" + tool.upper()] = str(base / "absent-cli")
        env["PYTHONUTF8"] = "1"
        for command in ("yog-sothoth", "switcher", "codex-switch", "claude-switch", "agy-switch"):
            executable = scripts / (command + (".exe" if os.name == "nt" else ""))
            subprocess.run([str(executable), "--help"], cwd=base, env=env, check=True, stdout=subprocess.DEVNULL)
        command = str(scripts / ("yog-sothoth.exe" if os.name == "nt" else "yog-sothoth"))
        subprocess.run([command, "--version"], cwd=base, env=env, check=True)
        output = subprocess.check_output([command, "doctor", "--json"], cwd=base, env=env, text=True)
        assert all(not tool["available"] for tool in json.loads(output)["tools"])
        # Isolated mode prevents PYTHONPATH or the source cwd from masking a
        # missing module or a launch plan that still depends on repo scripts.
        subprocess.run([str(python), "-I", str(Path(__file__).resolve()), "--exercise"],
                       cwd=base, env=env, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
