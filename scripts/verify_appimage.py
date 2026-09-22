"""Exercise the packaged GTK window and CLI without using local account data."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def main():
    image = Path(sys.argv[1]).resolve()
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="yog appimage spaces ") as directory:
        base = Path(directory)
        env = os.environ.copy()
        env["HOME"] = str(base / "home")
        Path(env["HOME"]).mkdir()
        env["XDG_CONFIG_HOME"] = str(base / "config")
        env["XDG_DATA_HOME"] = str(base / "data")
        env["GSETTINGS_BACKEND"] = "memory"
        for tool in ("codex", "claude", "agy"):
            env[tool.upper() + "_SWITCHER_HOME"] = str(base / tool)
            env[tool.upper() + "_SWITCHER_" + tool.upper()] = str(base / "absent")
        output = subprocess.check_output([str(image), "--appimage-extract-and-run", "doctor", "--json"],
                                         env=env, cwd=base, text=True)
        assert all(not tool["available"] for tool in json.loads(output)["tools"])
        subprocess.run([str(image), "--appimage-extract"], cwd=base, env=env,
                       check=True, stdout=subprocess.DEVNULL)
        app = base / "squashfs-root"
        assert sorted(p.name for p in (app / "usr/share/yog-sothoth").iterdir()) == sorted(
            ["switcher_cli.py", "switcher_manager.py", "switcher_gui.py", "switcher_runtime.py", "switcher_setup.py",
             "codex_switcher.py", "codex_history.py", "claude_switcher.py", "agy_switcher.py", "local_history.py"])
        bundled = dict(env, APPDIR=str(app), APPIMAGE=str(image), YOG_SOTHOTH_BUNDLED="1",
                       LD_LIBRARY_PATH=str(app / "usr/lib/x86_64-linux-gnu"), PYTHONHOME=str(app / "usr"),
                       PYTHONPATH=f"{app}/usr/share/yog-sothoth:{app}/usr/lib/python3/dist-packages",
                       GI_TYPELIB_PATH=f"{app}/usr/lib/x86_64-linux-gnu/girepository-1.0:{app}/usr/lib/girepository-1.0",
                       GSETTINGS_SCHEMA_DIR=str(app / "usr/share/glib-2.0/schemas"), GSK_RENDERER="cairo")
        python = str(app / "usr/bin/python3.10")
        # The real-window test switches every supported tool's dropdown and
        # checks conversation filtering/resume without calling a real login.
        command = ["xvfb-run", "-a", python, "-m", "unittest", "discover", "-s", str(source / "tests"),
                   "-p", "test_manager_gui.py", "-v"]
        subprocess.run(command, cwd=base, env=bundled, check=True)
        # Fresh startup additionally exercises automatic first-run setup.
        launched = subprocess.Popen(["xvfb-run", "-a", str(image), "--appimage-extract-and-run", "gui"],
                                    env=env, cwd=base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
        try:
            time.sleep(4)
            if launched.poll() is not None:
                stdout, stderr = launched.communicate()
                raise SystemExit("AppImage GUI failed to stay open:\n" + stdout + stderr)
        finally:
            import signal
            os.killpg(launched.pid, signal.SIGTERM)
            stdout, stderr = launched.communicate(timeout=10)
        if "Traceback" in stderr:
            raise SystemExit("GUI startup raised an exception:\n" + stderr)
    print("AppImage CLI, bundled GTK controls, and fresh GUI startup passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
