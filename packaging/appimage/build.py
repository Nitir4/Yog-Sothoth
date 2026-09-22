"""Build a Linux x86_64 AppImage from a clean Ubuntu container filesystem."""

import fnmatch
import hashlib
from pathlib import Path
import shutil
import subprocess

from switcher_runtime import VERSION

SOURCE = Path("/src")
APP = Path("/build/Yog-Sothoth.AppDir")
OUTPUT = Path("/output")
RUNTIME_URL = "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64"
RUNTIME_SHA256 = "156f4bdbde9c52d01814600013e0a273f0118dc2de98975f3c8c63427ec79074"


def copy_tree(source, destination, ignore=None):
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True, ignore=ignore)


def main():
    OUTPUT.mkdir(exist_ok=True)
    (APP / "usr/bin").mkdir(parents=True)
    shutil.copy2("/usr/bin/python3.10", APP / "usr/bin/python3.10")
    copy_tree("/usr/lib/python3.10", APP / "usr/lib/python3.10",
              shutil.ignore_patterns("__pycache__", "test", "tests", "idlelib", "tkinter"))
    copy_tree("/usr/lib/python3/dist-packages/gi", APP / "usr/lib/python3/dist-packages/gi",
              shutil.ignore_patterns("__pycache__"))

    # Use the host's glibc and graphics drivers. Bundling a second libc or Mesa
    # driver stack can break host integrations. Ubuntu 22.04 sets glibc >= 2.35.
    excluded = ("ld-*", "libc.so*", "libm.so*", "libpthread*", "libdl.so*", "librt.so*",
                "libresolv*", "libnss*", "libutil*", "libanl*", "libBrokenLocale*",
                "libGL*", "libEGL*", "libOpenGL*", "libgbm*", "libdrm*", "libvulkan*",
                "dri", "__pycache__", "*.a")
    def ignore(_directory, names):
        return {name for name in names if any(fnmatch.fnmatch(name, pattern) for pattern in excluded)}
    copy_tree("/usr/lib/x86_64-linux-gnu", APP / "usr/lib/x86_64-linux-gnu", ignore)
    for directory in ("/usr/lib/girepository-1.0", "/usr/share/glib-2.0/schemas",
                      "/usr/share/icons/Adwaita", "/usr/share/icons/hicolor", "/usr/share/mime"):
        if Path(directory).exists():
            copy_tree(directory, APP / directory.lstrip("/"))
    for notice in Path("/usr/share/doc").glob("*/copyright"):
        destination = APP / notice.relative_to("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(notice, destination)
    (APP / "usr/share/yog-sothoth-build-packages.txt").write_text(
        subprocess.check_output(["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"], text=True))
    subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--silent", "--show-error",
                    "https://raw.githubusercontent.com/AppImage/type2-runtime/8f39b89e2ac31e1640b3d3f7e9a5108e6ce805fa/LICENSE",
                    "--output", str(APP / "usr/share/AppImage-runtime-LICENSE")], check=True)
    code = APP / "usr/share/yog-sothoth"
    code.mkdir(parents=True)
    # This directory was staged from an explicit source-file allowlist.
    for path in SOURCE.glob("*.py"):
        shutil.copy2(path, code / path.name)
    for name in ("AppRun", "yog-sothoth.desktop", "yog-sothoth.svg"):
        shutil.copy2(SOURCE / "packaging/appimage" / name, APP / name)
    (APP / "AppRun").chmod(0o755)
    (APP / ".DirIcon").symlink_to("yog-sothoth.svg")
    subprocess.run(["desktop-file-validate", str(APP / "yog-sothoth.desktop")], check=True)
    subprocess.run([str(APP / "AppRun"), "--version"], check=True)

    runtime = Path("/build/runtime-x86_64")
    subprocess.run(["curl", "--fail", "--location", "--retry", "3", RUNTIME_URL, "--output", str(runtime)], check=True)
    if hashlib.sha256(runtime.read_bytes()).hexdigest() != RUNTIME_SHA256:
        raise SystemExit("AppImage runtime checksum changed. Review the upstream release before updating the pin.")
    filesystem = Path("/build/yog.squashfs")
    subprocess.run(["mksquashfs", str(APP), str(filesystem), "-noappend", "-all-root", "-no-xattrs",
                    "-comp", "gzip", "-processors", "2", "-no-progress"], check=True)
    image = OUTPUT / f"Yog-Sothoth-{VERSION}-x86_64.AppImage"
    with image.open("wb") as target:
        for source in (runtime, filesystem):
            with source.open("rb") as handle:
                shutil.copyfileobj(handle, target)
    image.chmod(0o755)
    subprocess.run([str(image), "--appimage-extract-and-run", "--version"], check=True)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (OUTPUT / (image.name + ".sha256")).write_text(digest + "  " + image.name + "\n")


if __name__ == "__main__":
    main()
