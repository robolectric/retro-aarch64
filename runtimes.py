"""Builds the aarch64 prebuilts that an aarch64 host library links but AOSP only ships for x86_64.

The rewritten ninja file refers to aarch64 counterparts of the toolchain prebuilts the x86_64
build links: compiler-rt runtimes, and in newer versions libc++ and the Rust standard library.
Each missing one is built from the same sources and with the same compilers as the x86_64 one:
compiler-rt and libc++ from the llvm-project revision the clang prebuilt was built from, with
that clang, and the Rust standard library from the rust-src of the Rust prebuilt, with its rustc.
"""

import io
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

TRIPLE = "aarch64-linux-gnu"
RUST_TRIPLE = "aarch64-unknown-linux-gnu"
GITILES = "https://android.googlesource.com/toolchain/llvm-project/+archive"


def log(message):
    print(message, flush=True)


def run(command, **kwargs):
    subprocess.run([str(c) for c in command], check=True, **kwargs)


def llvm_revision(clang):
    for manifest in sorted(clang.glob("manifest_*.xml")):
        match = re.search(r'path="toolchain/llvm-project"[^>]*revision="([0-9a-f]{40})"',
                          manifest.read_text())
        if match:
            return match.group(1)
    raise SystemExit(f"{clang}: no manifest with the toolchain/llvm-project revision")


def llvm_sources(clang, cache, subdirs):
    """Fetches subdirectories of llvm-project at the clang prebuilt's revision (Gitiles)."""
    revision = llvm_revision(clang)
    root = cache / f"llvm-project-{revision[:12]}"
    for subdir in subdirs:
        destination = root / subdir
        if (destination / ".complete").exists():
            continue
        log(f"Fetching toolchain/llvm-project {revision[:12]} {subdir}")
        request = urllib.request.Request(f"{GITILES}/{revision}/{subdir}.tar.gz",
                                         headers={"User-Agent": "aosp-linux-aarch64"})
        with urllib.request.urlopen(request, timeout=900) as response:
            data = response.read()
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(destination, filter="data")
        (destination / ".complete").touch()
    return root


class Builder:
    def __init__(self, aosp, clang, sysroot, cache, ninja):
        self.aosp, self.clang, self.sysroot, self.cache, self.ninja = aosp, clang, sysroot, cache, ninja
        self.cross = [f"--sysroot={sysroot / 'sysroot'}", f"--gcc-toolchain={sysroot}"]

    def cmake(self, source, build, *defines, link_tests=True, cxx_flags=""):
        cmake = shutil.which("cmake")
        if not cmake:
            raise SystemExit("cmake is required to build the llvm runtimes")
        shutil.rmtree(build, ignore_errors=True)
        flags = " ".join(self.cross)
        run([cmake, "-G", "Ninja", "-S", source, "-B", build,
             f"-DCMAKE_MAKE_PROGRAM={self.ninja}", "-DCMAKE_BUILD_TYPE=Release",
             "-DCMAKE_SYSTEM_NAME=Linux", "-DCMAKE_SYSTEM_PROCESSOR=aarch64",
             f"-DCMAKE_C_COMPILER={self.clang / 'bin/clang'}",
             f"-DCMAKE_CXX_COMPILER={self.clang / 'bin/clang++'}",
             f"-DCMAKE_ASM_COMPILER={self.clang / 'bin/clang'}",
             *(f"-DCMAKE_{lang}_COMPILER_TARGET={TRIPLE}" for lang in ("C", "CXX", "ASM")),
             *(f"-DCMAKE_{lang}_FLAGS={flags}" for lang in ("C", "ASM")),
             f"-DCMAKE_CXX_FLAGS={flags} {cxx_flags}",
             f"-DCMAKE_AR={self.clang / 'bin/llvm-ar'}",
             f"-DCMAKE_RANLIB={self.clang / 'bin/llvm-ranlib'}",
             f"-DCMAKE_NM={self.clang / 'bin/llvm-nm'}",
             # Feature checks link against the sysroot (glibc 2.17), except for the builtins,
             # which are what such links would need.
             *([] if link_tests else ["-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY"]),
             "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", *defines], stdout=subprocess.DEVNULL)
        run([self.ninja, "-C", build], stdout=subprocess.DEVNULL)

    def builtins(self):
        source = llvm_sources(self.clang, self.cache, ["compiler-rt", "llvm/cmake", "cmake"])
        build = self.cache / f"{self.clang.name}-builtins"
        self.cmake(source / "compiler-rt/lib/builtins", build,
                   "-DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON",
                   "-DCOMPILER_RT_EXCLUDE_ATOMIC_BUILTIN=ON",  # like the x86_64 Linux builtins
                   f"-DLLVM_CMAKE_DIR={source / 'llvm/cmake/modules'}", link_tests=False)
        return next(build.glob("lib/**/libclang_rt.builtins*.a"))

    def ubsan_minimal(self):
        source = llvm_sources(self.clang, self.cache, ["compiler-rt"])
        work = self.cache / f"{self.clang.name}-ubsan_minimal"
        work.mkdir(parents=True, exist_ok=True)
        obj, archive = work / "ubsan_minimal_handlers.o", work / "libclang_rt.ubsan_minimal.a"
        run([self.clang / "bin/clang++", f"--target={TRIPLE}", *self.cross, "-O3", "-std=c++17",
             "-fPIC", "-fno-builtin", "-fno-exceptions", "-fno-rtti", "-fomit-frame-pointer",
             "-funwind-tables", "-fno-stack-protector", "-fvisibility=hidden", "-fno-lto",
             "-nostdinc++", f"-I{source / 'compiler-rt/lib'}", "-c",
             source / "compiler-rt/lib/ubsan_minimal/ubsan_minimal_handlers.cpp", "-o", obj])
        archive.unlink(missing_ok=True)
        run([self.clang / "bin/llvm-ar", "rcsD", archive, obj])
        return archive

    def libcxx(self):
        """libc++ with libc++abi merged in, unwinding left to libgcc, like the x86_64 one."""
        if getattr(self, "_libcxx", None):
            return self._libcxx
        source = llvm_sources(self.clang, self.cache, [
            "runtimes", "libcxx", "libcxxabi", "libunwind", "cmake", "llvm/cmake",
            "llvm/utils/llvm-lit", "third-party", "libc"])
        build = self.cache / f"{self.clang.name}-libcxx"
        self.cmake(source / "runtimes", build, "-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi",
                   f"-DLLVM_DEFAULT_TARGET_TRIPLE={RUST_TRIPLE}",
                   # Like the x86_64 libraries: libc++abi merged into libc++.a and libc++.so,
                   # libgcc linked statically, so libc++.so needs only glibc.
                   "-DLIBCXX_ENABLE_SHARED=ON", "-DLIBCXXABI_ENABLE_SHARED=OFF",
                   "-DLIBCXX_ENABLE_STATIC_ABI_LIBRARY=ON",
                   "-DLIBCXX_STATICALLY_LINK_ABI_IN_SHARED_LIBRARY=ON",
                   "-DCMAKE_SHARED_LINKER_FLAGS=-static-libgcc",
                   "-DLIBCXXABI_USE_LLVM_UNWINDER=OFF",
                   "-DLIBCXX_INCLUDE_TESTS=OFF", "-DLIBCXX_INCLUDE_BENCHMARKS=OFF",
                   "-DLIBCXXABI_INCLUDE_TESTS=OFF", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
                   "-DLIBCXX_HARDENING_MODE=none",
                   cxx_flags="-nostdlib++")  # libc++ itself never links libstdc++
        self._libcxx = build
        return build

    def rust_std(self, toolchain):
        """Builds the Rust standard library for aarch64 glibc with the prebuilt's own rustc."""
        bin_dir = toolchain / "bin"
        work = self.cache / f"rust-std-{toolchain.name}"
        shutil.rmtree(work, ignore_errors=True)
        (work / "src").mkdir(parents=True)
        (work / "Cargo.toml").write_text(
            '[package]\nname = "std_for_target"\nversion = "0.0.0"\nedition = "2021"\n'
            '[lib]\npath = "src/lib.rs"\ncrate-type = ["staticlib"]\n'
            '[profile.release]\nlto = "thin"\ndebug = 2\n')  # bitcode for Soong's -C lto=thin
        (work / "src/lib.rs").write_text("")
        linker = work / "linker"
        linker.write_text(f"#!/bin/sh\nexec {self.clang / 'bin/clang'} --target={TRIPLE} "
                          f"{' '.join(self.cross)} -fuse-ld=lld \"$@\"\n")
        linker.chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(work), "RUSTC_BOOTSTRAP": "1",
               "RUSTC": str(bin_dir / "rustc"), "CARGO_HOME": str(self.cache / "cargo-home"),
               f"CARGO_TARGET_{RUST_TRIPLE.upper().replace('-', '_')}_LINKER": str(linker),
               f"CC_{RUST_TRIPLE.replace('-', '_')}": str(linker),
               "CARGO_PROFILE_RELEASE_DEBUG": "2"}
        log(f"Building the Rust standard library for {RUST_TRIPLE} with {bin_dir / 'rustc'}")
        run([bin_dir / "cargo", "build", "--release", "-Zbuild-std=std,panic_unwind,panic_abort",
             "--target", RUST_TRIPLE], cwd=work, env=env, stdout=subprocess.DEVNULL)
        return work / "target" / RUST_TRIPLE / "release/deps"


def install(aosp, clang, sysroot, cache, ninja, missing):
    """Builds and installs the missing aarch64 prebuilts (paths relative to `aosp`)."""
    builder = Builder(aosp, clang, sysroot, cache, ninja)
    unknown = []
    rust = {}
    for path in sorted(set(missing)):
        full = aosp / path
        name = full.name
        if "/rustlib/" in path:  # the Rust toolchain is the directory that has bin/rustc
            rust[next(p for p in full.parents if (p / "bin/rustc").exists())] = True
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith("libclang_rt.builtins"):
            shutil.copy2(builder.builtins(), full)
        elif name.startswith("libclang_rt.ubsan_minimal"):
            shutil.copy2(builder.ubsan_minimal(), full)
        elif name in ("libc++.a", "libc++.so", "libc++abi.a", "__config_site") or path.endswith(
                "/c++/v1"):
            build = builder.libcxx()
            lib_dir = clang / "lib" / "aarch64-unknown-linux-gnu"
            include_dir = clang / "include" / "aarch64-unknown-linux-gnu/c++/v1"
            lib_dir.mkdir(parents=True, exist_ok=True)
            for library in ("libc++.a", "libc++abi.a", "libc++.so"):
                built = next(build.glob(f"**/lib/{library}")).resolve()
                shutil.copy2(built, lib_dir / library)
            # The per-target include directory has what the x86_64 one has (__config_site).
            config = next(build.glob("**/include/**/c++/v1/__config_site"))
            reference_dir = clang / "include/x86_64-unknown-linux-gnu/c++/v1"
            include_dir.mkdir(parents=True, exist_ok=True)
            for reference in reference_dir.iterdir():
                built = config.parent / reference.name
                if reference.name == "__config_site" and built.read_text() != reference.read_text():
                    raise SystemExit(f"{built} differs from {reference}")
                shutil.copy2(built, include_dir / reference.name)
        else:
            unknown.append(path)
            continue
        log(f"Installed {path}")
    if unknown:
        raise SystemExit(f"no recipe for the aarch64 prebuilts {unknown}")
    for toolchain in rust:
        deps = builder.rust_std(toolchain)
        rustlib_dir = toolchain / "lib/rustlib" / RUST_TRIPLE / "lib"
        rustlib_dir.mkdir(parents=True, exist_ok=True)
        for rlib in deps.glob("*.rlib"):
            if not rlib.name.startswith("libstd_for_target"):
                shutil.copy2(rlib, rustlib_dir / rlib.name)
        log(f"Installed the Rust standard library in {rustlib_dir.relative_to(aosp)}")
