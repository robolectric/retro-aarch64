#!/usr/bin/env python3
"""Builds a Linux aarch64 glibc 2.17 sysroot, the counterpart of AOSP's x86_64 host prebuilt.

AOSP builds Linux host binaries against
prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8, a glibc 2.17 sysroot with a GCC 4.8
runtime. There is no aarch64 counterpart, so Soong cannot build host libraries such as the
Robolectric native runtime for Linux aarch64. This script assembles one from the CentOS 7
AltArch aarch64 packages (glibc 2.17, GCC 4.8.5), so the result has the same glibc floor as the
x86_64 build.

Usage:
  make_sysroot.py --checkout <checkout>        # install next to the x86_64 prebuilt
  make_sysroot.py --out <dir>                  # build into a directory
  make_sysroot.py --update-lock                # re-resolve package versions and hashes
  make_sysroot.py ... --self-test              # link test programs against the result

Output layout (--gcc-toolchain=<out> --sysroot=<out>/sysroot):
  sysroot/                            the extracted packages
  lib/gcc/aarch64-redhat-linux/4.8.2  -> sysroot/usr/lib/gcc/aarch64-redhat-linux/4.8.2
  PACKAGE_SOURCES                     package file names and SHA-256, like the x86_64 prebuilt
"""

import argparse
import gzip
import hashlib
import io
import json
import lzma
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOCK_FILE = SCRIPT_DIR / "sysroot.lock.json"
# Public key of the CentOS AltArch SIG for AArch64, from /etc/pki/rpm-gpg of the
# centos-release package. Fingerprint EF8F 3CA6 6EFD F32B 36CD  ADF7 6C7C B6EF 305D 49D6.
SIGNING_KEY = SCRIPT_DIR / "RPM-GPG-KEY-CentOS-7-aarch64"
SIGNING_KEY_FINGERPRINT = "ef8f3ca66efdf32b36cdadf76c7cb6ef305d49d6"
PREBUILT_DIR = "prebuilts/gcc/linux-x86/host/aarch64-linux-glibc2.17-4.8"

VAULT = "https://vault.centos.org/altarch/7.9.2009"
REPOS = ["os", "updates"]
ARCH = "aarch64"
TRIPLE = "aarch64-linux-gnu"
GCC_TRIPLE = "aarch64-redhat-linux"
GLIBC_FLOOR = (2, 17)

# What C and C++ host libraries need at link time. Unlike the x86_64 prebuilt, which also
# serves the emulator, this leaves out X11, audio and GL. `gcc` supplies crtbegin.o and
# libgcc.a; its compiler binaries are not extracted.
PACKAGES = ["glibc", "glibc-devel", "glibc-headers", "kernel-headers", "libgcc", "gcc"]

# Parts of the packages a sysroot does not need. Licence texts are kept.
DROPPED_PREFIXES = (
    "usr/bin/",
    "usr/sbin/",
    "sbin/",
    "bin/",
    "etc/",
    "var/",
    "usr/libexec/",
    "usr/share/man/",
    "usr/share/info/",
    "usr/share/locale/",
    "usr/share/i18n/",
    "usr/lib/locale/",
    "usr/lib64/gconv/",
    "usr/lib/gcc/aarch64-redhat-linux/4.8.2/plugin/",
    "usr/lib/gcc/aarch64-redhat-linux/4.8.2/libcloog-isl.so",
    "usr/lib/gcc/aarch64-redhat-linux/4.8.2/rpmver",
)
KEPT_DOCS = re.compile(r"usr/share/(doc|licenses)/[^/]+/(COPYING[^/]*|LICENSES?|copyright)$")

RPM_HEADER_MAGIC = b"\x8e\xad\xe8"
RPMTAG_PAYLOADCOMPRESSOR = 1125


def log(message):
    print(message, file=sys.stderr)


def fetch(url, timeout=300):
    request = urllib.request.Request(url, headers={"User-Agent": "robolectric-make-sysroot"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# --- RPM version ordering (rpmvercmp) -------------------------------------------------------


def rpmvercmp(a, b):
    """Compares two version or release strings the way rpm does."""
    if a == b:
        return 0
    segments_a = re.findall(r"\d+|[a-zA-Z]+", a)
    segments_b = re.findall(r"\d+|[a-zA-Z]+", b)
    for x, y in zip(segments_a, segments_b):
        if x.isdigit() != y.isdigit():
            return 1 if x.isdigit() else -1  # a numeric segment is newer
        if x.isdigit():
            x, y = int(x), int(y)
        if x != y:
            return 1 if x > y else -1
    return (len(segments_a) > len(segments_b)) - (len(segments_a) < len(segments_b))


def evr_key(package):
    epoch, version, release = package["epoch"], package["version"], package["release"]

    class Key:
        def __lt__(self, other):
            for x, y in zip((epoch, version, release), other.parts):
                c = rpmvercmp(x, y)
                if c:
                    return c < 0
            return False

    key = Key()
    key.parts = (epoch, version, release)
    return key


# --- Lock file -------------------------------------------------------------------------------

REPO_NS = {"repo": "http://linux.duke.edu/metadata/repo"}
COMMON_NS = {"c": "http://linux.duke.edu/metadata/common"}


def repo_packages(repo):
    base = f"{VAULT}/{repo}/{ARCH}"
    repomd = ElementTree.fromstring(fetch(f"{base}/repodata/repomd.xml"))
    primary = repomd.find("repo:data[@type='primary']", REPO_NS)
    location = primary.find("repo:location", REPO_NS).attrib["href"]
    expected = primary.find("repo:checksum", REPO_NS)
    data = fetch(f"{base}/{location}")
    if expected.attrib["type"] != "sha256" or sha256(data) != expected.text:
        raise SystemExit(f"{repo}: primary metadata checksum mismatch")
    root = ElementTree.fromstring(gzip.decompress(data))
    for element in root.findall("c:package", COMMON_NS):
        if element.find("c:arch", COMMON_NS).text != ARCH:
            continue
        checksum = element.find("c:checksum", COMMON_NS)
        if checksum.attrib["type"] != "sha256":
            continue
        version = element.find("c:version", COMMON_NS).attrib
        yield {
            "name": element.find("c:name", COMMON_NS).text,
            "epoch": version.get("epoch", "0"),
            "version": version["ver"],
            "release": version["rel"],
            "url": f"{base}/{element.find('c:location', COMMON_NS).attrib['href']}",
            "size": int(element.find("c:size", COMMON_NS).attrib["package"]),
            "sha256": checksum.text,
        }


def update_lock():
    candidates = {name: [] for name in PACKAGES}
    for repo in REPOS:
        for package in repo_packages(repo):
            if package["name"] in candidates:
                candidates[package["name"]].append(package)
    packages = []
    for name in PACKAGES:
        if not candidates[name]:
            raise SystemExit(f"{name}: not found in {REPOS}")
        newest = max(candidates[name], key=evr_key)
        log(f"  {name} {newest['version']}-{newest['release']}")
        packages.append(newest)
    # glibc's subpackages must come from the same build.
    glibc_releases = {p["release"] for p in packages if p["name"].startswith("glibc")}
    if len(glibc_releases) != 1:
        raise SystemExit(f"glibc packages from different builds: {glibc_releases}")
    LOCK_FILE.write_text(json.dumps({"vault": VAULT, "packages": packages}, indent=2) + "\n")
    log(f"Wrote {LOCK_FILE}")


def read_lock():
    if not LOCK_FILE.exists():
        raise SystemExit(f"{LOCK_FILE} is missing; run with --update-lock first")
    lock = json.loads(LOCK_FILE.read_text())
    locked = [p["name"] for p in lock["packages"]]
    if sorted(locked) != sorted(PACKAGES):
        raise SystemExit(f"{LOCK_FILE} lists {locked}, expected {PACKAGES}; run --update-lock")
    return lock


# --- Download --------------------------------------------------------------------------------


def download(package, cache_dir):
    """Downloads the package into its own cache directory and checks its hash."""
    rpm_dir = cache_dir / package["sha256"]
    rpm = rpm_dir / posixpath.basename(package["url"])
    if rpm.exists() and sha256(rpm.read_bytes()) == package["sha256"]:
        return rpm
    rpm_dir.mkdir(parents=True, exist_ok=True)
    log(f"Downloading {package['url']}")
    data = fetch(package["url"], timeout=900)
    actual = sha256(data)
    if actual != package["sha256"]:
        raise SystemExit(f"{rpm.name}: SHA-256 {actual}, expected {package['sha256']}")
    rpm.write_bytes(data)
    return rpm


def check_signatures(rpms):
    """Checks the RPM signatures with rpmkeys if it is installed; the hashes are always checked."""
    rpmkeys = shutil.which("rpmkeys")
    if not rpmkeys:
        log("rpmkeys not found; relying on the SHA-256 hashes in the lock file")
        return
    with tempfile.TemporaryDirectory() as root:
        subprocess.run([rpmkeys, "--root", root, "--import", str(SIGNING_KEY)], check=True)
        for rpm in rpms:
            # rpm 6 no longer checks the SHA-1 and MD5 digests that CentOS 7 packages carry, so
            # its overall verdict is "digests NOT OK" even for a good package. Require a good
            # header signature from the pinned key instead; the lock file's SHA-256 covers the
            # whole file.
            output = subprocess.run(
                [rpmkeys, "--root", root, "--checksig", "--verbose", str(rpm)],
                capture_output=True,
                text=True,
            ).stdout
            good = re.search(
                rf"Header .*signature, key fingerprint: {SIGNING_KEY_FINGERPRINT}: OK", output
            )
            if not good or re.search(r"signature.*: (BAD|NOKEY|NOTTRUSTED)", output):
                raise SystemExit(f"{rpm.name}: bad signature:\n{output.strip()}")
    log(f"Checked the signatures of {len(rpms)} packages")


# --- RPM extraction --------------------------------------------------------------------------


def rpm_header(data, offset):
    """Parses an RPM header structure at `offset`; returns (tags, offset past the header)."""
    if data[offset : offset + 3] != RPM_HEADER_MAGIC:
        raise SystemExit("bad RPM header")
    count = int.from_bytes(data[offset + 8 : offset + 12], "big")
    store_size = int.from_bytes(data[offset + 12 : offset + 16], "big")
    index = offset + 16
    store = index + 16 * count
    tags = {}
    for i in range(count):
        entry = data[index + 16 * i : index + 16 * (i + 1)]
        tag, kind, position = (int.from_bytes(entry[j : j + 4], "big") for j in (0, 4, 8))
        if kind == 6:  # string
            end = data.index(b"\0", store + position)
            tags[tag] = data[store + position : end].decode()
    return tags, store + store_size


def rpm_payload(rpm):
    data = rpm.read_bytes()
    if data[:4] != b"\xed\xab\xee\xdb":
        raise SystemExit(f"{rpm.name}: not an RPM")
    _, offset = rpm_header(data, 96)  # signature header, padded to 8 bytes
    offset = (offset + 7) & ~7
    tags, offset = rpm_header(data, offset)
    compressor = tags.get(RPMTAG_PAYLOADCOMPRESSOR, "gzip")
    payload = data[offset:]
    if compressor in ("xz", "lzma"):
        return lzma.decompress(payload)
    if compressor == "gzip":
        return gzip.decompress(payload)
    raise SystemExit(f"{rpm.name}: unsupported payload compressor {compressor}")


def cpio_entries(payload):
    """Yields (name, mode, data) for each entry of a newc cpio archive.

    The members of a hard-link group share an inode and only the last one carries the data, so
    the earlier names are held back and yielded with it.
    """
    offset = 0
    pending_links = {}
    while True:
        header = payload[offset : offset + 110]
        if header[:6] != b"070701":
            raise SystemExit("bad cpio header")
        fields = [int(header[6 + 8 * i : 14 + 8 * i], 16) for i in range(13)]
        inode, mode, links, file_size, name_size = (fields[i] for i in (0, 1, 4, 6, 11))
        name_start = offset + 110
        name = payload[name_start : name_start + name_size - 1].decode()
        data_start = (name_start + name_size + 3) & ~3
        offset = (data_start + file_size + 3) & ~3
        if name == "TRAILER!!!":
            if pending_links:
                raise SystemExit("cpio archive ends inside a hard-link group")
            return
        data = payload[data_start : data_start + file_size]
        if stat.S_ISREG(mode) and links > 1:
            group = pending_links.setdefault(inode, [])
            group.append(name)
            if not file_size:
                continue
            del pending_links[inode]
            for linked_name in group:
                yield linked_name, mode, data
            continue
        yield name, mode, data


def inside(sysroot, path):
    return os.path.commonpath([sysroot, path]) == str(sysroot)


def relative_link_target(sysroot, link_path, target):
    """Returns `target` relative to `link_path`'s directory, or None if it leaves the sysroot."""
    if posixpath.isabs(target):
        absolute = sysroot / posixpath.normpath(target).lstrip("/")
    else:
        absolute = Path(os.path.normpath(link_path.parent / target))
    if not inside(sysroot, absolute):
        return None
    return os.path.relpath(absolute, link_path.parent)


def extract(rpm, sysroot):
    for name, mode, data in cpio_entries(rpm_payload(rpm)):
        normalized = posixpath.normpath("/" + name).lstrip("/")
        if not normalized or normalized.startswith(DROPPED_PREFIXES):
            continue
        if normalized.startswith(("usr/share/doc/", "usr/share/licenses/")) and not KEPT_DOCS.match(
            normalized
        ):
            continue
        path = sysroot / normalized
        if not inside(sysroot, path):
            raise SystemExit(f"{rpm.name}: {name} escapes the sysroot")
        if stat.S_ISDIR(mode):
            continue  # created on demand
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.exists():
            path.unlink()
        if stat.S_ISLNK(mode):
            target = relative_link_target(sysroot, path, data.decode())
            if target is None:
                log(f"  skipping {normalized} -> {data.decode()} (outside the sysroot)")
                continue
            path.symlink_to(target)
        elif stat.S_ISREG(mode):
            path.write_bytes(data)
            os.chmod(path, mode & 0o755)


def remove_dangling_links(sysroot):
    for path in sorted(sysroot.rglob("*")):
        if path.is_symlink() and not path.exists():
            log(f"  removing dangling link {path.relative_to(sysroot)}")
            path.unlink()
    # Remove the directories that are left empty, deepest first.
    for path in sorted(sysroot.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()


# --- Build -----------------------------------------------------------------------------------


def gcc_version(sysroot):
    # RHEL 7's GCC 4.8.5 keeps its runtime in a directory named 4.8.2, with 4.8.5 linking to it.
    versions = [
        p
        for p in (sysroot / "usr/lib/gcc" / GCC_TRIPLE).iterdir()
        if not p.is_symlink() and (p / "crtbegin.o").exists()
    ]
    if len(versions) != 1:
        raise SystemExit(f"expected one GCC runtime in the sysroot, found {versions}")
    return versions[0].name


def build(out, cache_dir):
    lock = read_lock()
    rpms = [download(package, cache_dir) for package in lock["packages"]]
    check_signatures(rpms)
    staging = Path(tempfile.mkdtemp(prefix=out.name + ".", dir=out.parent))
    try:
        sysroot = staging / "sysroot"
        sysroot.mkdir()
        for rpm in rpms:
            log(f"Extracting {rpm.name}")
            extract(rpm, sysroot)
        remove_dangling_links(sysroot)

        # clang looks for the GCC runtime under <--gcc-toolchain>/lib/gcc/<triple>/<version>.
        version = gcc_version(sysroot)
        gcc_dir = staging / "lib/gcc" / GCC_TRIPLE
        gcc_dir.mkdir(parents=True)
        (gcc_dir / version).symlink_to(
            os.path.relpath(sysroot / "usr/lib/gcc" / GCC_TRIPLE / version, gcc_dir)
        )
        (staging / "PACKAGE_SOURCES").write_text(
            "".join(
                f"{posixpath.basename(p['url']):<56} {p['sha256']}\n" for p in lock["packages"]
            )
        )
        (staging / "README.md").write_text(
            textwrap.dedent(
                f"""\
                Linux aarch64 glibc {'.'.join(map(str, GLIBC_FLOOR))} sysroot with a GCC {version}
                runtime, the counterpart of x86_64-linux-glibc2.17-4.8. Generated by
                aosp-linux-aarch64's make_sysroot.py from the CentOS 7 AltArch packages listed in
                PACKAGE_SOURCES.
                """
            )
        )
        if out.exists():
            shutil.rmtree(out)
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    log(f"Built {out}")


# --- Self-test -------------------------------------------------------------------------------

TEST_C = r"""
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>

static void *worker(void *arg) { return arg; }

double robolectric_sysroot_test(double x) { return cos(x) + (double)strlen("ok"); }

int main(void) {
  pthread_t thread;
  pthread_create(&thread, NULL, worker, NULL);
  pthread_join(thread, NULL);
  printf("%f\n", robolectric_sysroot_test(0.0));
  return 0;
}
"""


def glibc_versions(readelf, binary):
    output = subprocess.run(
        [readelf, "--dyn-syms", "--version-info", "-W", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {
        tuple(int(part) for part in match.split("."))
        for match in re.findall(r"GLIBC_(\d+(?:\.\d+)+)", output)
    }


def self_test(out, clang, readelf):
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        source = work / "test.c"
        source.write_text(TEST_C)
        flags = [
            f"--target={TRIPLE}",
            f"--sysroot={out / 'sysroot'}",
            f"--gcc-toolchain={out}",
            "-fuse-ld=lld",
            "-O2",
        ]
        outputs = {
            "shared library": (work / "libtest.so", ["-shared", "-fPIC"]),
            "executable": (work / "test", ["-pie", "-fPIE"]),
        }
        for kind, (binary, extra) in outputs.items():
            command = [clang, *flags, *extra, str(source), "-lm", "-lpthread", "-o", str(binary)]
            subprocess.run(command, check=True)
            header = subprocess.run(
                [readelf, "-h", str(binary)], check=True, capture_output=True, text=True
            ).stdout
            if "AArch64" not in header:
                raise SystemExit(f"self-test {kind}: not an AArch64 ELF")
            highest = max(glibc_versions(readelf, binary), default=None)
            if highest is None or highest > GLIBC_FLOOR:
                raise SystemExit(f"self-test {kind}: requires GLIBC_{highest}")
            log(f"Self-test {kind}: AArch64, newest symbol version GLIBC_{'.'.join(map(str, highest))}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="directory to create")
    parser.add_argument("--checkout", type=Path, help=f"Android checkout; installs to {PREBUILT_DIR}")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/aosp-linux-aarch64")
    parser.add_argument("--update-lock", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--readelf", default="llvm-readelf")
    args = parser.parse_args()

    if args.update_lock:
        update_lock()
        if not (args.out or args.checkout):
            return
    if args.checkout:
        out = (args.checkout / PREBUILT_DIR).resolve()
    elif args.out:
        out = args.out.resolve()
    else:
        parser.error("one of --out, --checkout or --update-lock is required")
    out.parent.mkdir(parents=True, exist_ok=True)
    build(out, args.cache)
    if args.self_test:
        self_test(out, args.clang, args.readelf)


if __name__ == "__main__":
    main()
