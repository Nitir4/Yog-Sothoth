#!/bin/sh
# Stage only reviewed application source. Account stores never enter Docker.
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir=${1:-"$source_dir/artifacts"}
mkdir -p "$output_dir"
output_dir=$(CDPATH= cd -- "$output_dir" && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT HUP INT TERM
for file in switcher_cli.py switcher_manager.py switcher_gui.py switcher_qt.py switcher_runtime.py switcher_setup.py codex_switcher.py codex_history.py claude_switcher.py agy_switcher.py local_history.py pyproject.toml README.md switcher codex-switch claude-switch agy-switch; do
    cp "$source_dir/$file" "$stage/$file"
done
mkdir -p "$stage/packaging/appimage" "$stage/tests"
for file in Dockerfile AppRun build.py yog-sothoth.desktop yog-sothoth.svg; do
    cp "$source_dir/packaging/appimage/$file" "$stage/packaging/appimage/$file"
done
mkdir -p "$stage/scripts"
for file in verify_install.py verify_appimage.py; do
    cp "$source_dir/scripts/$file" "$stage/scripts/$file"
done
for file in "$source_dir"/tests/test_*.py; do
    cp "$file" "$stage/tests/"
done
docker_cmd() {
    if docker info >/dev/null 2>&1; then docker "$@"; else sudo -n docker "$@"; fi
}
docker_cmd build -t yog-sothoth-appimage-builder -f "$stage/packaging/appimage/Dockerfile" "$stage"
container=$(docker_cmd create yog-sothoth-appimage-builder)
trap 'docker_cmd rm "$container" >/dev/null; rm -rf "$stage"' EXIT HUP INT TERM
mkdir "$stage/output"
docker_cmd cp "$container:/output/." - | tar -x --no-same-owner -C "$stage/output"
for file in "$stage/output"/*; do
    cp --remove-destination "$file" "$output_dir/"
    case "$file" in *.AppImage) chmod 755 "$output_dir/${file##*/}" ;; esac
done
printf 'Artifacts written to %s\n' "$output_dir"
