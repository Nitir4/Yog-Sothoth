"""Verify pinned upstream Antigravity binaries with disposable native profiles.

No Google sign-in or production accounts are used. The executable compatibility
allowlist is reviewed separately; this check exercises real directory flags and
native settings isolation on each supported CI platform.
"""

import hashlib
import io
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import agy_switcher

BASE = "https://storage.googleapis.com/antigravity-public/antigravity-cli/1.3.1-4582356770750464/"
PACKAGES = {
    ("linux", "x86_64"): "linux-x64/cli_linux_x64.tar.gz",
    ("darwin", "x86_64"): "darwin-x64/cli_mac_x64.tar.gz",
    ("darwin", "arm64"): "darwin-arm/cli_mac_arm64.tar.gz",
    ("win32", "x86_64"): "windows-x64/cli_windows_x64.exe",
    ("win32", "arm64"): "windows-arm/cli_windows_arm64.exe",
}


def main():
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    url = BASE + PACKAGES[(sys.platform, arch)]
    with tempfile.TemporaryDirectory(prefix="agy native spaces ") as directory:
        root = Path(directory)
        binary = root / ("agy.exe" if os.name == "nt" else "agy")
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read()
        if url.endswith(".tar.gz"):
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                payload = archive.extractfile("antigravity").read()
        binary.write_bytes(payload)
        binary.chmod(0o700)
        agy_switcher.require_compatible(str(binary))
        print("Inspected Antigravity 1.3.1:", sys.platform, arch, hashlib.sha256(payload).hexdigest(), flush=True)
        original = root / "original"
        original.mkdir()
        env = agy_switcher.environment()
        env.update(HOME=str(original), USERPROFILE=str(original), APPDATA=str(original / "roaming"),
                   LOCALAPPDATA=str(original / "local"), XDG_CONFIG_HOME=str(original / "config"),
                   XDG_DATA_HOME=str(original / "data"))
        for name in ("personal", "work"):
            home = root / name
            argv = agy_switcher.command(str(binary), home,
                ["mcp", "add", "synthetic-" + name, "--", "echo", "synthetic-" + name])
            result = subprocess.run(argv, env=env, cwd=root, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise SystemExit(result.stdout + result.stderr)
            settings = home / "config/mcp_config.json"
            assert settings.is_file(), "Native settings escaped the profile directory"
            value = settings.read_text()
            assert "synthetic-" + name in value
            assert "synthetic-" + ("work" if name == "personal" else "personal") not in value
        for name in ("personal", "work"):
            result = subprocess.run(agy_switcher.command(str(binary), root / name, ["mcp", "list"]),
                                    env=env, cwd=root, capture_output=True, text=True, timeout=60, check=True)
            assert "synthetic-" + name in result.stdout
            assert "synthetic-" + ("work" if name == "personal" else "personal") not in result.stdout
        assert not any(original.rglob("*")), "Native probes wrote to the original home"
    print("Native directory flags and two-profile settings isolation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
