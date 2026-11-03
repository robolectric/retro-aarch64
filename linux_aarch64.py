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
    # trunk_staging where the branch has it, otherwise the release the branch ships.
    release = aosp / "build/release"
    if (release / "release_configs/trunk_staging.textproto").exists():
        combo = f"{product}-trunk_staging-eng"
    elif release.exists():
        combo = f"{product}-aosp_current-eng"
    else:
        combo = f"{product}-eng"
    log(f"m {target} ({combo}, OUT_DIR={out_dir})")
    # Public trees can name modules AOSP does not publish (android16-release's conscrypt APEX
    # names "opensourcerequest"); with this a missing module only fails what builds it.
    subprocess.run(["bash", "-c", f"export OUT_DIR={out_dir} ALLOW_MISSING_DEPENDENCIES=true;"
                    " source build/envsetup.sh >/dev/null"
                    f" && lunch {combo} >/dev/null && m {target}"], cwd=aosp, check=True)


def host_library(aosp, product, directory, name):
    """The path of the library's x86_64 host build, from Soong's graph (newer versions put the
    variant's outputs under a hash directory)."""
    ninja = ninja_rewrite.soong_ninja(aosp, "out", product)
    if not ninja.exists():
        soong(aosp, product, "nothing")
        ninja = ninja_rewrite.soong_ninja(aosp, "out", product)
    path = re.compile(rf"out/soong/\.intermediates/{re.escape(directory)}/{re.escape(name)}/"
                      rf"{ninja_rewrite.HOST}_shared/(?:[0-9a-f]{{32}}/)?{re.escape(name)}\.so"
                      r"(?=[\s:|]|$)")
    stem = ninja.name.removesuffix(".ninja")  # shards: build.<product>.ninja.N or .N.ninja
    shards = sorted({*ninja.parent.glob(f"{stem}.ninja.[0-9]*"), *ninja.parent.glob(f"{stem}.[0-9]*.ninja")})
    for shard in [ninja, *shards]:
        with open(shard, errors="replace") as lines:
            for line in lines:
                if f"/{name}.so" in line and (match := path.search(line)):
                    return match.group(0)
    raise SystemExit(f"{directory}:{name} has no {ninja_rewrite.HOST} shared library in {ninja}")


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


def rust_std_crates(aosp):
    """The crates of the Rust standard library (core, alloc, std and the crates std uses)."""
    rlibs = aosp.glob("prebuilts/rust*/linux-x86/*/lib/rustlib/x86_64-unknown-linux-gnu/"
                      "lib/lib*.rlib")
    return {re.sub(r"^lib|-[0-9a-f]+\.rlib$", "", rlib.name) for rlib in rlibs}


def rust_crate(symbol):
    """The crate a v0-mangled Rust symbol belongs to (the first crate root in its path)."""
    match = re.search(r"C(?:s[0-9A-Za-z]*_)?(\d+)", symbol) if symbol.startswith("_R") else None
    return match and symbol[match.end():match.end() + int(match.group(1))]


def verify(clang_bin, arm_library, x86_library, std_crates):
    if "AArch64" not in readelf(clang_bin, "-h", str(arm_library)):
        raise SystemExit(f"{arm_library}: not an AArch64 ELF")
    versions = set(re.findall(r"GLIBC_(\d+(?:\.\d+)+)",
                              readelf(clang_bin, "--dyn-syms", "-W", str(arm_library))))
    newest = max(versions, key=lambda v: tuple(map(int, v.split("."))), default="none")
    if versions and tuple(map(int, newest.split("."))) > make_sysroot.GLIBC_FLOOR:
        raise SystemExit(f"{arm_library}: requires GLIBC_{newest}")
    needed = re.findall(r"Shared library: \[(.+?)\]", readelf(clang_bin, "-d", str(arm_library)))
    # Everything the x86_64 library exports, except its x86 SIMD code (the arm64 build has NEON
    # code instead), with va_list mangled the aarch64 way, and without the crate hashes in Rust
    # names (the aarch64 standard library is a different build of the same crates). What the
    # Rust standard library's own code exports depends on how it was built, not on the library.
    abi = lambda symbols: {re.sub(r"Cs[0-9A-Za-z]+_|17h[0-9a-f]{16}E", "",
                                  s.replace("P13__va_list_tag", "St9__va_list")) for s in symbols
                           if rust_crate(s) not in std_crates}
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


def built_dependencies(aosp, clang_bin, sysroot, library):
    """The directories of the shared libraries the library needs that are not glibc's: the ones
    the aarch64 build made (some branches link some AOSP libraries as shared libraries)."""
    graph = (aosp / OUT_NINJA).read_text(errors="replace")
    directories, pending, seen = set(), [library], set()
    while pending:
        for needed in re.findall(r"Shared library: \[(.+?)\]", readelf(clang_bin, "-d", str(pending.pop()))):
            if needed in seen or list((sysroot / "sysroot").glob(f"*/{needed}")) + \
                    list((sysroot / "sysroot").glob(f"usr/*/{needed}")):
                continue
            seen.add(needed)
            match = re.search(rf"out/soong/\.intermediates/\S*?/{ninja_rewrite.ARM_HOST}_shared/"
                              rf"(?:[0-9a-f]{{32}}/)?{re.escape(needed)}(?=[\s:|]|$)", graph, re.M)
            if match:
                directories.add(str((aosp / match.group(0)).parent))
                pending.append(aosp / match.group(0))
    if seen:
        log(f"Not self-contained: needs AOSP shared libraries {sorted(seen)}")
    return sorted(directories)


def load_test(clang, sysroot, library, dependencies=()):
    """dlopens the library under qemu-aarch64 with the sysroot's glibc and finds JNI_OnLoad."""
    qemu = next((q for q in ("qemu-aarch64", "qemu-aarch64-static") if shutil.which(q)), None)
    if not qemu:
        log("qemu-aarch64 not found; skipping the load test")
        return
    environment = ["-E", f"LD_LIBRARY_PATH={':'.join(dependencies)}"] if dependencies else []
    with tempfile.TemporaryDirectory() as work:
        source, loader = Path(work) / "load.c", Path(work) / "load"
        source.write_text(LOAD_TEST)
        subprocess.run([str(clang / "bin/clang"), "--target=aarch64-linux-gnu",
                        f"--sysroot={sysroot / 'sysroot'}", f"--gcc-toolchain={sysroot}",
                        "-fuse-ld=lld", str(source), "-ldl", "-o", str(loader)], check=True)
        result = subprocess.run([qemu, "-L", str(sysroot / "sysroot"), *environment, str(loader),
                                 str(library)],
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

    # 1. The sysroot: the CentOS packages, then this tree's updates of its x86_64 headers.
    sysroot = aosp / make_sysroot.PREBUILT_DIR
    if not (sysroot / headers.MARKER).exists():
        make_sysroot.build(sysroot, args.cache)
        headers.apply(aosp, sysroot, args.cache)
    gcc_version = next((sysroot / "lib/gcc" / make_sysroot.GCC_TRIPLE).iterdir()).name

    # 2. The x86_64 host build (also the reference for the exports) and the arm64 device graph.
    x86_target = host_library(aosp, args.host_product, directory, name)
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
    verify(clang / "bin", aosp / arm_target, aosp / x86_target, rust_std_crates(aosp))
    load_test(clang, sysroot, aosp / arm_target,
              built_dependencies(aosp, clang / "bin", sysroot, aosp / arm_target))


if __name__ == "__main__":
    main()
