"""Collect upstream license texts for the dependencies in native desktop bundles."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib import metadata
import io
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import urllib.request


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def collect(destination):
    from PySide6.QtCore import qVersion
    versions = {
        "qt/qtbase": qVersion(),
        "qt/qtsvg": qVersion(),
        "qt/qtimageformats": qVersion(),
        "pyside/pyside-setup": metadata.version("PySide6_Essentials"),
    }
    targets = [(repo, "https://github.com/" + repo + "/archive/refs/tags/v" + version + ".tar.gz")
               for repo, version in versions.items()]
    destination.mkdir(parents=True, exist_ok=True)
    sources = []

    def archive_notices(target):
        repo, url = target
        payload = fetch(url)
        count = 0
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                relative = PurePosixPath(*path.parts[1:])
                if not member.isfile() or ".." in relative.parts or relative.is_absolute():
                    continue
                name = relative.name.lower()
                is_notice = ("licenses" in (p.lower() for p in relative.parts)
                             or name.startswith(("license", "licence", "copying", "copyright", "notice"))
                             or name == "qt_attribution.json")
                if not is_notice or member.size > 2 * 1024 * 1024:
                    continue
                output = destination / repo.replace("/", "-") / Path(str(relative))
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.extractfile(member).read())
                count += 1
        if not count:
            raise RuntimeError("No upstream license notices found in " + repo)
        return f"{repo}\nSource: {url}\nSHA-256: {hashlib.sha256(payload).hexdigest()}\nNotices: {count}\n"

    with ThreadPoolExecutor(max_workers=4) as executor:
        sources.extend(executor.map(archive_notices, targets))
    python_version = ".".join(str(n) for n in sys.version_info[:3])
    for name in ("LICENSE", "Doc/license.rst"):
        url = f"https://raw.githubusercontent.com/python/cpython/v{python_version}/{name}"
        output = destination / "Python" / Path(name)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(fetch(url))
        sources.append("Python " + python_version + "\nSource: " + url + "\n")
    for name in ("LICENSE.txt", "LICENSE"):
        source = Path(sys.base_prefix) / name
        if source.is_file():
            shutil.copy2(source, destination / "Python" / ("installed-" + name))
    (destination / "SOURCES.txt").write_text("Third-party source repositories and license notices\n\n" + "\n".join(sources))
    print("Collected upstream Qt, PySide, and Python license notices.", flush=True)
