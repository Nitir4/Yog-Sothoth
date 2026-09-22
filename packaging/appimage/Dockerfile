FROM ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    python3 python3-gi python3-pip python3-venv gir1.2-gtk-4.0 \
    ca-certificates curl squashfs-tools desktop-file-utils xvfb xauth dbus-x11 \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --no-cache-dir 'setuptools>=77' build

WORKDIR /src
COPY . /src
RUN python3 -m unittest discover -s tests -q
RUN python3 -m build --wheel --outdir /output
RUN python3 scripts/verify_install.py /output/*.whl
RUN PYTHONPATH=/src python3 packaging/appimage/build.py
RUN python3 scripts/verify_appimage.py /output/*.AppImage

CMD ["sh", "-c", "cp /output/* /artifacts/"]
