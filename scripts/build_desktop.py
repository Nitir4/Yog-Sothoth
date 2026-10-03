"""Build the native Qt GUI and independent console CLI on the current OS."""

import hashlib
from importlib import metadata
import platform
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from switcher_runtime import VERSION


def build():
    output = PROJECT / "dist/desktop"
    work = PROJECT / "build/desktop"
    output.mkdir(parents=True, exist_ok=True)
    shared = ["--noconfirm", "--clean", "--noupx", "--paths", str(PROJECT),
              "--distpath", str(output), "--workpath", str(work), "--specpath", str(work)]
    work.mkdir(parents=True, exist_ok=True)
    for module in ("codex_switcher", "claude_switcher", "agy_switcher"):
        shared += ["--hidden-import", module]
    cli = ["--onefile", "--console", "--name", "yog-sothoth",
           "--exclude-module", "PySide6", "--exclude-module", "gi",
           "--exclude-module", "switcher_gui", "--exclude-module", "switcher_qt"]
    subprocess.run([sys.executable, "-m", "PyInstaller", *shared, *cli,
                    str(PROJECT / "packaging/desktop/cli_entry.py")], check=True, cwd=PROJECT)
    gui = ["--onedir", "--windowed", "--name", "Yog-Sothoth-Desktop", "--exclude-module", "gi"]
    if sys.platform == "darwin":
        gui += ["--osx-bundle-identifier", "io.github.nitir4.yog-sothoth"]
    subprocess.run([sys.executable, "-m", "PyInstaller", *shared, *gui,
                    str(PROJECT / "packaging/desktop/gui_entry.py")], check=True, cwd=PROJECT)
    executable = output / ("yog-sothoth.exe" if sys.platform == "win32" else "yog-sothoth")
    package = output / ("Yog-Sothoth-Desktop.app" if sys.platform == "darwin" else "Yog-Sothoth-Desktop")
    destination = package / "Contents/MacOS" if sys.platform == "darwin" else package
    shutil.copy2(executable, destination / executable.name)
    docs = package / "Contents/Resources" if sys.platform == "darwin" else package
    shutil.copy2(PROJECT / "README.md", docs / "README.md")
    # Preserve Qt/PySide notices alongside the bundled libraries.
    licenses = docs / "licenses"
    from desktop_licenses import collect
    collect(licenses)
    for distribution in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "pyinstaller"):
        try:
            dist = metadata.distribution(distribution)
        except metadata.PackageNotFoundError:
            continue
        for file in dist.files or []:
            if "license" in str(file).lower() or "copying" in str(file).lower():
                source = Path(dist.locate_file(file))
                if source.is_file():
                    target = licenses / distribution / Path(str(file))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    if sys.platform == "darwin":
        # Adding the console helper changes the bundle after PyInstaller signs it.
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(package)], check=True)
    return package


def archive(package):
    artifacts = PROJECT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    system = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux-Qt")
    architecture = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    name = f"Yog-Sothoth-{VERSION}-{system}-{architecture}"
    if sys.platform == "darwin":
        target = artifacts / (name + ".zip")
        # ditto preserves app bundle symlinks and executable permissions.
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(package), str(target)], check=True)
    else:
        target = Path(shutil.make_archive(str(artifacts / name), "zip", package.parent, package.name))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(f"{digest}  {target.name}\n")
    print(f"Desktop package: {target}")
    return target


if __name__ == "__main__":
    package = build()
    subprocess.run([sys.executable, str(PROJECT / "scripts/verify_desktop.py"), str(package)], check=True)
    archive(package)
