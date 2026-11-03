# retro-aarch64

Builds an Android host shared library, such as Robolectric's native runtime, for Linux aarch64
(glibc) in an existing Android checkout.

Soong only builds Linux host code for x86_64. This derives an aarch64 build from Soong's own
build graphs, so it follows each branch's sources and flags, and changes no Android source.

```
./linux_aarch64.py --checkout <checkout> --module <directory>:<cc_library_shared name>
```

| Tree | Module | Result |
|---|---|---|
| `android12-hostruntime-dev` | `frameworks/base/core/jni:librobolectric-nativeruntime` (the default) | builds and passes the checks |
| `android15-release`, `android15-qpr2-release` | `frameworks/base/core/jni:libandroid_runtime` | builds and passes the checks |
| `android16-release` | `frameworks/base/core/jni:libandroid_runtime` | builds and passes the checks |
| `android17-release` | `frameworks/base/core/jni:libandroid_runtime` | builds and passes the checks |

The build links what the tree links: where a branch links Android libraries as shared libraries on
the host (the public Android 15 and 16 branches do), the checks name them, and the load test
finds them in the build.

## What it does

1. **Sysroot** (`make_sysroot.py`, `headers.py`): `prebuilts/gcc/linux-x86/host/aarch64-linux-glibc2.17-4.8`,
   the counterpart of `x86_64-linux-glibc2.17-4.8`: glibc 2.17 and the GCC 4.8.5 runtime from
   the CentOS 7 AltArch aarch64 packages (pinned in `sysroot.lock.json`, signed with
   `RPM-GPG-KEY-CentOS-7-aarch64`), plus the updates made to the x86_64 sysroot's headers
   (what that sysroot has beyond the packages its `PACKAGE_SOURCES` lists).
2. **Graphs**: Soong's x86_64 host build of the library, and an arm64 device build graph in
   `out-arm64/` (`m nothing`).
3. **Ninja** (`ninja_rewrite.py`): `out/linux_glibc_arm64.ninja`, which includes Soong's ninja
   file and adds a `linux_glibc_arm64` variant, by three rules:
   1. Copy the architecture-specific statements of the library's x86_64 closure: compiling
      (C/C++, Rust, bindgen), reading an x86_64 toolchain prebuilt, and whatever reads their
      outputs. Code generators, executables and Rust proc macros run on the build machine and
      are shared.
   2. Give each module its arm64 difference: its `android_arm64` device build against its
      `android_x86_64` one (sources, flags), less what most modules share (the device
      toolchains), and only where the host build has the x86_64 side.
   3. Retarget the toolchain: aarch64 copies of the `Linux*` toolchain variables, and x86_64
      triples, sysroot paths and runtime names replaced.
4. **Prebuilts** (`runtimes.py`): the aarch64 toolchain prebuilts the rewritten build refers to
   but the checkout lacks, built from the same sources with the same compilers as the x86_64 ones:
   compiler-rt builtins and `ubsan_minimal`, libc++ (newer branches link it as a prebuilt)
   from the llvm-project revision of the clang prebuilt, and the Rust standard library
   (`cargo -Zbuild-std` with the Rust prebuilt's `rustc` and `rust-src`).
5. **Build and checks**: the library is an AArch64 ELF, needs at most `GLIBC_2.17`, exports what
   the x86_64 one does (less x86 SIMD code), and, if `qemu-aarch64` is installed, loads with
   `RTLD_NOW` under the sysroot's glibc and has `JNI_OnLoad`.

## Requirements

Python 3.12 or newer, `cmake`, `patch`, network access (CentOS vault, Launchpad, Gitiles,
crates.io), and the disk and memory for two Soong analyses. `rpmkeys` is used to check the
CentOS package signatures if it is installed; the lock file's SHA-256 hashes are always checked.

## What it adds to the checkout

- `prebuilts/gcc/linux-x86/host/aarch64-linux-glibc2.17-4.8/`
- aarch64 runtimes next to the x86_64 ones in the clang prebuilt (and, on newer branches,
  `lib/rustlib/aarch64-unknown-linux-gnu/` in the Rust prebuilt)
- `out-arm64/`, `out/linux_glibc_arm64.ninja` and `out/linux_glibc_arm64/`
