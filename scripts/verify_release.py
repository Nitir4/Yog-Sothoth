"""Check release asset inventory, checksums, and absence of private workspace files."""

import hashlib
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from switcher_runtime import VERSION

MODULES = {"switcher_cli", "switcher_manager", "switcher_gui", "switcher_qt", "switcher_runtime",
           "switcher_setup", "codex_switcher", "codex_history", "claude_switcher", "agy_switcher", "local_history"}
FORBIDDEN = {".git", ".agents", ".codex", ".aws", "accounts", "auth.json", ".credentials.json",
             "antigravity-oauth-token", "selected.json", "history.json", "progress.md", "implementation.md", "agents.md"}


def check_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(p.lower() in FORBIDDEN for p in path.parts):
        raise SystemExit("Unexpected private or unsafe package path: " + name)


def main():
    root = Path(sys.argv[1]).resolve()
    if sys.argv[2] != "v" + VERSION:
        raise SystemExit("Release tag does not match the application version")
    image = f"Yog-Sothoth-{VERSION}-x86_64.AppImage"
    wheel = f"yog_sothoth_switcher-{VERSION}-py3-none-any.whl"
    archives = [f"Yog-Sothoth-{VERSION}-{suffix}.zip" for suffix in
                ("Windows-x86_64", "macOS-arm64", "macOS-x86_64")]
    expected = {image, image + ".sha256", wheel, *archives, *(a + ".sha256" for a in archives)}
    if {p.name for p in root.iterdir()} != expected:
        raise SystemExit("Release assets do not match the expected inventory")
    for name in [image, *archives]:
        digest, target = (root / (name + ".sha256")).read_text().strip().split(maxsplit=1)
        if target != name or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise SystemExit("Checksum mismatch: " + name)
    with zipfile.ZipFile(root / wheel) as archive:
        for name in archive.namelist():
            check_path(name)
            if not (name.endswith(".py") and "/" not in name and name[:-3] in MODULES
                    or ".dist-info/" in name):
                raise SystemExit("Unexpected wheel content: " + name)
    for name in archives:
        with zipfile.ZipFile(root / name) as archive:
            for member in archive.namelist():
                check_path(member)
            names = archive.namelist()
            assert any(p.endswith("licenses/Python/LICENSE") for p in names)
            assert any(p.endswith("licenses/qt-qtbase/LICENSES/LGPL-3.0-only.txt") for p in names)
            assert any(p.endswith("yog-sothoth.exe" if "Windows" in name else "/yog-sothoth") for p in names)
    with tempfile.TemporaryDirectory(prefix="release-image-") as directory:
        extracted = Path(directory) / "image"
        (root / image).chmod(0o755)
        offset = int(subprocess.check_output([str(root / image), "--appimage-offset"], text=True).strip())
        subprocess.run(["unsquashfs", "-q", "-d", str(extracted), "-offset", str(offset), str(root / image)], check=True,
                       stdout=subprocess.DEVNULL)
        for path in extracted.rglob("*"):
            check_path(path.relative_to(extracted).as_posix())
    print("Release inventory, checksums, notices, and private-file exclusions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
