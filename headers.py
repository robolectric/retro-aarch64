"""Applies AOSP's updates of its x86_64 sysroot headers to the aarch64 sysroot.

AOSP's x86_64 host prebuilt is glibc 2.17 from Ubuntu 13.04 packages, but AOSP keeps its headers
current: newer kernel UAPI headers, glibc headers backported from later releases, polyfills.
Code builds against those, so the aarch64 sysroot needs the same updates.

The updates are what the x86_64 sysroot has beyond the packages its PACKAGE_SOURCES lists, so
they are derived from the tree's own prebuilt, and follow it from one AOSP version to the next.
Each changed header is patched into the aarch64 sysroot when the change applies to the CentOS
version, and otherwise replaced by AOSP's version (wholesale updates, such as a kernel header
from a newer kernel). x86 kernel headers (asm/) are architecture-specific and left alone.
"""

import difflib
import hashlib
import io
import lzma
import gzip
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

X86_PREBUILT = "prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8"
MULTIARCH = "x86_64-linux-gnu"
LAUNCHPAD_FILES = "https://launchpad.net/ubuntu/+archive/primary/+files"
MARKER = "AOSP_HEADER_UPDATES"


def log(message):
    print(message, flush=True)


def baseline(x86, cache):
    """usr/include of the amd64 packages the x86_64 prebuilt was made from."""
    root = cache / "x86-sysroot-baseline" / hashlib.sha1(
        (x86 / "PACKAGE_SOURCES").read_bytes()).hexdigest()[:12]
    include = root / "usr/include"
    if (root / ".complete").exists():
        return include
    shutil.rmtree(root, ignore_errors=True)
    for line in (x86 / "PACKAGE_SOURCES").read_text().splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].endswith("_amd64.deb"):
            continue
        name, sha1 = parts
        deb = cache / "debs" / name
        if not deb.exists() or hashlib.sha1(deb.read_bytes()).hexdigest() != sha1:
            deb.parent.mkdir(parents=True, exist_ok=True)
            log(f"Downloading {name}")
            with urllib.request.urlopen(f"{LAUNCHPAD_FILES}/{name}", timeout=600) as response:
                data = response.read()
            if hashlib.sha1(data).hexdigest() != sha1:
                raise SystemExit(f"{name}: SHA-1 does not match {x86 / 'PACKAGE_SOURCES'}")
            deb.write_bytes(data)
        extract_deb(deb, root)
    (root / ".complete").touch()
    return include


def extract_deb(deb, root):
    data, offset = deb.read_bytes(), 8
    while offset < len(data):
        header = data[offset:offset + 60]
        name, size = header[:16].decode().strip().rstrip("/"), int(header[48:58])
        body = data[offset + 60:offset + 60 + size]
        if name.startswith("data.tar"):
            body = lzma.decompress(body) if name.endswith(".xz") else (
                gzip.decompress(body) if name.endswith(".gz") else body)
            with tarfile.open(fileobj=io.BytesIO(body)) as tar:
                members = [m for m in tar.getmembers() if m.isfile()
                           and m.name.lstrip("./").startswith("usr/include/")]
                tar.extractall(root, members=members, filter="data")
        offset += 60 + size + size % 2


def apply(aosp, sysroot_prebuilt, cache):
    """Applies the x86_64 prebuilt's header updates to the aarch64 sysroot, once."""
    marker = sysroot_prebuilt / MARKER
    if marker.exists():
        return
    x86 = aosp / X86_PREBUILT
    original = baseline(x86, cache)
    aosp_include = x86 / "sysroot/usr/include"
    arm_include = sysroot_prebuilt / "sysroot/usr/include"
    patched, replaced, added = [], [], []
    for header in sorted(aosp_include.rglob("*")):
        relative = header.relative_to(aosp_include)
        if not header.is_file() or (relative.parts[0].endswith("-linux-gnu")
                                    and relative.parts[0] != MULTIARCH):
            continue  # other multiarch directories (i386)
        base = original / relative
        if base.exists() and base.read_bytes() == header.read_bytes():
            continue  # not an update
        target_relative = relative.relative_to(MULTIARCH) if relative.parts[0] == MULTIARCH \
            else relative
        if target_relative.parts[0] == "asm":
            continue  # x86 kernel headers
        target = arm_include / target_relative
        if base.exists() and target.exists():
            change = "".join(difflib.unified_diff(
                base.read_bytes().decode("latin-1").splitlines(True),
                header.read_bytes().decode("latin-1").splitlines(True), "a", "b"))
            result = subprocess.run(["patch", "-s", "-f", "--no-backup-if-mismatch", str(target)],
                                    input=change.encode("latin-1"), capture_output=True)
            if result.returncode == 0:
                patched.append(str(target_relative))
                continue
            replaced.append(str(target_relative))
        else:
            added.append(str(target_relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(header, target)
    marker.write_text(f"Header updates of {X86_PREBUILT}, applied by aosp-linux-aarch64:\n"
                      + "".join(f"patched  {h}\n" for h in patched)
                      + "".join(f"replaced {h}\n" for h in replaced)
                      + "".join(f"added    {h}\n" for h in added))
    log(f"Applied AOSP's x86_64 header updates: {len(patched)} patched, {len(replaced)} "
        f"replaced, {len(added)} added")
