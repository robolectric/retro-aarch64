#!/usr/bin/env python3
"""Builds a Soong host library for Linux aarch64 in an existing AOSP checkout.

Soong only builds Linux host code for x86_64. This adds what an aarch64 build needs and derives
one from Soong's own build graphs, without changing any AOSP source:

  1. sysroot    prebuilts/gcc/linux-x86/host/aarch64-linux-glibc2.17-4.8 (make_sysroot.py),
                with the tree's updates of its x86_64 sysroot headers (headers.py)
  2. graphs     Soong's x86_64 host build of the library, and an arm64 device build graph
  3. ninja      out/linux_glibc_arm64.ninja, the aarch64 build (ninja_rewrite.py)
  4. prebuilts  the aarch64 runtimes that build links but AOSP lacks (runtimes.py)
  5. build      the library, checked to be AArch64 with the exports of the x86_64 one

Usage:
  linux_aarch64.py --checkout <checkout> [--module <directory>:<cc_library_shared name>]
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import headers  # noqa: E402
import make_sysroot  # noqa: E402
import ninja_graph  # noqa: E402
import ninja_rewrite  # noqa: E402
import runtimes  # noqa: E402

DEVICE_ARM_OUT = "out-arm64"
OUT_NINJA = "out/linux_glibc_arm64.ninja"
CFG = "g.android.soong.cc.config."


def log(message):
    print(message, flush=True)


def soong(aosp, product, target, out_dir="out"):
    """Runs `m target` for `product` with build/envsetup.sh, which needs bash."""
    # Newer branches select a release configuration too: lunch <product>-<release>-<variant>.
    combo = f"{product}-trunk_staging-eng" if (aosp / "build/release").exists() else f"{product}-eng"
    log(f"m {target} ({combo}, OUT_DIR={out_dir})")
    subprocess.run(["bash", "-c", f"export OUT_DIR={out_dir}; source build/envsetup.sh >/dev/null"
                    f" && lunch {combo} >/dev/null && m {target}"], cwd=aosp, check=True)


def clang_dir(aosp, host_ninja):
    _, variables = ninja_graph.read(host_ninja, lambda f, l: False,
                                    lambda n: n.startswith(CFG + "Clang"), aosp)
    expand = lambda name: ninja_rewrite.expand("${" + CFG + name + "}", variables)
    if CFG + "ClangPath" in variables:
        return aosp / expand("ClangPath").replace("${g.android.soong.cc.config.HostPrebuiltTag}",
                                                  "linux-x86")
    return aosp / expand("ClangBase") / "linux-x86" / expand("ClangVersion")


def readelf(clang_bin, *args):
    return subprocess.run([str(clang_bin / "llvm-readelf"), *args], check=True,
                          capture_output=True, text=True).stdout


def exports(clang_bin, library):
    output = readelf(clang_bin, "--dyn-syms", "-W", str(library))
    return {line.split()[-1].split("@")[0] for line in output.splitlines()
            if " GLOBAL " in line and " UND " not in line}


def verify(clang_bin, arm_library, x86_library):
    if "AArch64" not in readelf(clang_bin, "-h", str(arm_library)):
        raise SystemExit(f"{arm_library}: not an AArch64 ELF")
    versions = set(re.findall(r"GLIBC_(\d+(?:\.\d+)+)",
                              readelf(clang_bin, "--dyn-syms", "-W", str(arm_library))))
    newest = max(versions, key=lambda v: tuple(map(int, v.split("."))), default="none")
    if versions and tuple(map(int, newest.split("."))) > make_sysroot.GLIBC_FLOOR:
        raise SystemExit(f"{arm_library}: requires GLIBC_{newest}")
    needed = re.findall(r"Shared library: \[(.+?)\]", readelf(clang_bin, "-d", str(arm_library)))
    # Everything the x86_64 library exports, except its x86 SIMD code (the arm64 build has NEON
    # code instead), with va_list mangled the aarch64 way.
    abi = lambda symbols: {s.replace("P13__va_list_tag", "St9__va_list") for s in symbols}
    missing = {s for s in abi(exports(clang_bin, x86_library)) - abi(exports(clang_bin, arm_library))
               if not re.search(r"SSE\d|SSSE3|AVX", s)}
    if missing:
        raise SystemExit(f"{arm_library}: does not export {sorted(missing)[:20]}")
    log(f"Verified {arm_library}: AArch64, newest symbol GLIBC_{newest}, needs {needed}, "
        f"exports what the x86_64 library does")


LOAD_TEST = r"""
#include <dlfcn.h>
#include <stdio.h>
int main(int argc, char **argv) {
  void *library = dlopen(argv[1], RTLD_NOW);
  if (!library) { printf("%s\n", dlerror()); return 1; }
  if (!dlsym(library, "JNI_OnLoad")) { printf("no JNI_OnLoad\n"); return 1; }
  printf("dlopen with RTLD_NOW and the sysroot's glibc succeeded; JNI_OnLoad found\n");
  return 0;
}
"""


def load_test(clang, sysroot, library):
    """dlopens the library under qemu-aarch64 with the sysroot's glibc and finds JNI_OnLoad."""
    qemu = next((q for q in ("qemu-aarch64", "qemu-aarch64-static") if shutil.which(q)), None)
    if not qemu:
        log("qemu-aarch64 not found; skipping the load test")
        return
    with tempfile.TemporaryDirectory() as work:
        source, loader = Path(work) / "load.c", Path(work) / "load"
        source.write_text(LOAD_TEST)
        subprocess.run([str(clang / "bin/clang"), "--target=aarch64-linux-gnu",
                        f"--sysroot={sysroot / 'sysroot'}", f"--gcc-toolchain={sysroot}",
                        "-fuse-ld=lld", str(source), "-ldl", "-o", str(loader)], check=True)
        result = subprocess.run([qemu, "-L", str(sysroot / "sysroot"), str(loader), str(library)],
                                capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"load test failed: {result.stdout}{result.stderr}")
    log(f"Load test: {result.stdout.strip()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--module", default="frameworks/base/core/jni:librobolectric-nativeruntime",
                        help="<directory>:<name> of a cc_library_shared that builds for the host")
    parser.add_argument("--host-product", default="aosp_x86_64")
    parser.add_argument("--device-arm-product", default="aosp_arm64")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/aosp-linux-aarch64")
    args = parser.parse_args()
    aosp = args.checkout.resolve()
    directory, name = args.module.split(":")
    x86_target = (f"out/soong/.intermediates/{directory}/{name}/"
                  f"{ninja_rewrite.HOST}_shared/{name}.so")

    # 1. The sysroot: the CentOS packages, then this tree's updates of its x86_64 headers.
    sysroot = aosp / make_sysroot.PREBUILT_DIR
    if not (sysroot / headers.MARKER).exists():
        make_sysroot.build(sysroot, args.cache)
        headers.apply(aosp, sysroot, args.cache)
    gcc_version = next((sysroot / "lib/gcc" / make_sysroot.GCC_TRIPLE).iterdir()).name

    # 2. The x86_64 host build (also the reference for the exports) and the arm64 device graph.
    if not (aosp / x86_target).exists():
        soong(aosp, args.host_product, x86_target)
    device_arm_ninja = ninja_rewrite.soong_ninja(aosp, DEVICE_ARM_OUT, args.device_arm_product)
    if not device_arm_ninja.exists():
        soong(aosp, args.device_arm_product, "nothing", DEVICE_ARM_OUT)
        device_arm_ninja = ninja_rewrite.soong_ninja(aosp, DEVICE_ARM_OUT, args.device_arm_product)
    host_ninja = ninja_rewrite.soong_ninja(aosp, "out", args.host_product)
    clang = clang_dir(aosp, host_ninja)

    # 3. The aarch64 ninja file, and 4. the aarch64 prebuilts it needs. Rewriting again after
    # building them resolves names that depend on the build (Rust standard library hashes).
    rewrite = lambda: ninja_rewrite.rewrite(aosp, x86_target, host_ninja, device_arm_ninja,
                                            DEVICE_ARM_OUT, OUT_NINJA, gcc_version)
    arm_target, missing = rewrite()
    if missing:
        log(f"Missing aarch64 prebuilts: {missing}")
        ninja = aosp / "prebuilts/build-tools/linux-x86/bin/ninja"
        runtimes.install(aosp, clang, sysroot, args.cache, ninja, missing)
        arm_target, missing = rewrite()
        if missing:
            raise SystemExit(f"still missing aarch64 prebuilts: {missing}")

    # 5. The build and its checks.
    log(f"ninja {arm_target}")
    subprocess.run([str(aosp / "prebuilts/build-tools/linux-x86/bin/ninja"), "-f", OUT_NINJA,
                    arm_target], cwd=aosp, check=True)
    verify(clang / "bin", aosp / arm_target, aosp / x86_target)
    load_test(clang, sysroot, aosp / arm_target)


if __name__ == "__main__":
    main()
