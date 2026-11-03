"""Derives a Linux aarch64 build of a host library from Soong's Linux x86_64 build.

Soong has no linux_glibc arm64 target, so the aarch64 build is derived with three rules:

1. Copy the architecture-specific statements of the library's linux_glibc_x86_64 closure into
   a new linux_glibc_arm64 variant. Compiling (C/C++, Rust, bindgen) and reading an x86_64
   toolchain prebuilt are architecture-specific, and so is every statement that reads an
   architecture-specific output (archives, links, copies). Code generators that read only
   sources are shared with x86_64, and so are executables and Rust proc macros (compiler
   plugins): they run on the build machine.
2. Give each module its arm64 difference: what its android_arm64 device build has that its
   android_x86_64 one does not (sources, flags), and the reverse. Both are Android builds, so
   the comparison isolates the architecture. Differences most modules share come from the
   device toolchains and are ignored. A flag difference only applies if the host build has
   its x86_64 side: a host build that leaves out the device's x86_64 specifics (zlib's host
   build turns SIMD off) leaves out the arm64 ones too.
3. Retarget the toolchain: every Linux* toolchain variable the copied statements use gets an
   aarch64 copy, and x86_64 triples, sysroot paths and runtime names are replaced, in those
   variables and in the statements themselves.

The output is a ninja file that includes Soong's main ninja file and adds the new statements.
"""

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import ninja_graph

HOST, ARM_HOST = "linux_glibc_x86_64", "linux_glibc_arm64"
DEVICE_X86, DEVICE_ARM = "android_x86_64", "android_arm64_armv8-a"
ARCHES = f"{HOST}|{DEVICE_X86}|{DEVICE_ARM}"
COMPILERS = {"g.cc.cc", "g.rust.rustc", "g.rust.bindgen"}  # rule 1's roots
X86_ONLY = {"g.cc.yasm"}  # x86 assembly: dropped, arm64 sources come from rule 2
SOURCE_RULES = {"g.cc.cc", "g.cc.yasm"}
FLAG_VARIABLES = ("cFlags", "rustcFlags")
SYSROOT = "prebuilts/gcc/linux-x86/host/aarch64-linux-glibc2.17-4.8"
PREBUILT = re.compile(r"prebuilts/[^\s=:,$]+")
INTERMEDIATE = re.compile(r"out/soong/\.intermediates/[^\s:=,]+")
TOOLCHAIN_VAR = re.compile(r"\$\{(g\.android\.soong\.(?:cc|rust)\.config\.(Linux\w*))\}")
VARIANT = re.compile(rf"/({ARCHES})([A-Za-z0-9_-]*)(?=/)")
VARIANT_HASH = re.compile(r"(?<=/VARIANT)([A-Za-z0-9_-]*)/[0-9a-f]{32}/")
MODULE_VAR = re.compile(rf"^(m\..+?_(?:{ARCHES})[A-Za-z0-9_-]*)\.(\w+)$")


def log(message):
    print(message, flush=True)


def module_of(path):
    """(directory/name, variant suffix) for a path in a module variant's directory."""
    match = re.search(r"\.intermediates/(.+?)" + VARIANT.pattern, path)
    return (match.group(1), match.group(3)) if match else None


def normalize(token):
    """Makes flags comparable across the device builds (out dirs, variant names)."""
    token = re.sub(r"(^|[=I@])out(-arm64)?/", r"\1out/", token)
    token = VARIANT.sub(r"/VARIANT\2", token)
    token = VARIANT_HASH.sub(r"\1/HASH/", token)  # newer versions: a directory per variant
    return re.sub(rf"(\$\{{m\.[^}}]+?)_({DEVICE_X86}|{DEVICE_ARM})", r"\1_VARIANT", token)


def soong_ninja(aosp, out_dir, product):
    """Soong's main ninja file: build.<product>.ninja in newer versions, build.ninja before."""
    per_product = aosp / out_dir / f"soong/build.{product}.ninja"
    return per_product if per_product.exists() else aosp / out_dir / "soong/build.ninja"


def expand(value, variables):
    return re.sub(r"\$\{([^}]+)\}",
                  lambda m: expand(variables[m.group(1)], variables) if m.group(1) in variables
                  else m.group(0), value)


# --- Rule 2: each module's arm64 difference -------------------------------------------------------


class Delta:
    def __init__(self):
        self.added_sources, self.removed_sources = {}, set()
        self.variables = {}  # module variable -> (added, removed)
        self.flags = {}  # statement flag variable -> (added, removed)


def by_module(builds):
    modules = defaultdict(list)
    for build in builds:
        key = module_of(build.outputs[0]) if build.outputs else None
        if key:
            modules[key].append(build)
    return modules


def var_prefix(builds):
    """The module variable prefix of a module variant, e.g. m.libz_linux_glibc_x86_64_static."""
    for build in builds:
        match = re.search(rf"\$\{{(m\.[^}}]+?_(?:{ARCHES})[A-Za-z0-9_-]*)\.moduleDesc\}}",
                          build.variables.get("description", ""))
        if match:
            return match.group(1)
    return None


def common_flags(builds, variable):
    """The literal flags every compile statement of a module has in `variable`."""
    sets = [set(map(normalize, b.variables[variable].split()))
            for b in builds if b.rule in COMPILERS and variable in b.variables]
    if not sets:
        return set()
    common = set.intersection(*sets)
    return {t for t in common if not t.startswith("${") and not device_toolchain(t)}


def diff(arm, x86):
    a, x = Counter(map(normalize, arm)), Counter(map(normalize, x86))
    return list((a - x).elements()), set((x - a).elements())


def deltas(host_modules, device_x86, device_arm):
    x86_builds, arm_builds = by_module(device_x86[0]), by_module(device_arm[0])
    x86_vars, arm_vars = device_x86[1], device_arm[1]

    def find(builds, path, suffix):  # a host static library can be shared on the device
        for candidate in (suffix, "_static", "_shared", ""):
            if (path, candidate) in builds:
                return builds[(path, candidate)]
        return next((b for (p, s), b in builds.items() if p == path and s.startswith(suffix)),
                    None)

    def module_variables(variables, prefix):
        return {k[len(prefix) + 1:]: v for k, v in variables.items()
                if prefix and k.startswith(prefix + ".")}

    result, flag_diffs, variable_diffs = {}, defaultdict(dict), defaultdict(dict)
    having = defaultdict(set)  # kind of flag variable -> modules that have it on both devices
    for key in host_modules:
        delta = result[key] = Delta()
        x86, arm = find(x86_builds, *key), find(arm_builds, *key)
        if x86 is None or arm is None:
            continue
        sources = lambda builds: {normalize(b.inputs[0]): b for b in builds
                                  if b.rule in SOURCE_RULES and b.inputs}
        x86_sources, arm_sources = sources(x86), sources(arm)
        delta.added_sources = {s: b for s, b in arm_sources.items() if s not in x86_sources}
        delta.removed_sources = set(x86_sources) - set(arm_sources)
        xv = module_variables(x86_vars, var_prefix(x86))
        av = module_variables(arm_vars, var_prefix(arm))
        for name in (set(xv) | set(av)) - {"moduleDesc", "moduleDescSuffix"}:
            if name in xv and name in av:
                having[("variables", re.sub(r"\d+$", "", name))].add(key)
            added, removed = diff(av.get(name, "").split(), xv.get(name, "").split())
            added = [t for t in added if not device_toolchain(t)]
            removed = {t for t in removed if not device_toolchain(t)}
            if added or removed:
                variable_diffs[re.sub(r"\d+$", "", name)][(key, name)] = (added, removed)
        for variable in FLAG_VARIABLES:
            x86_flags, arm_flags = common_flags(x86, variable), common_flags(arm, variable)
            if x86_flags and arm_flags:
                having[("flags", variable)].add(key)
            flag_diffs[variable][(key, variable)] = (sorted(arm_flags - x86_flags),
                                                     x86_flags - arm_flags)

    # Differences that more than half of the modules with that kind of variable share come
    # from the device toolchains, not the modules.
    def shared(sets, compared):
        counts = Counter(t for s in sets for t in set(s))
        return {t for t, c in counts.items() if c * 2 > compared}

    for diffs, target in ((variable_diffs, "variables"), (flag_diffs, "flags")):
        for kind, per_name in diffs.items():
            compared = len(having[(target, kind)])
            toolchain_added = shared((a for a, _ in per_name.values()), compared)
            toolchain_removed = shared((r for _, r in per_name.values()), compared)
            for (key, name), (added, removed) in per_name.items():
                added = [t for t in added if t not in toolchain_added]
                removed = removed - toolchain_removed
                if added or removed:
                    getattr(result[key], target)[name] = (added, removed)
    return result


def device_toolchain(token):
    """Flags that only a device build has: device toolchain variables and target triples."""
    return token.startswith("${g.") or "-linux-android" in token


def apply(value, added, removed):
    tokens = value.split()
    if not removed <= {normalize(t) for t in tokens}:
        added = []  # the host build does not have the x86_64 side of this difference
    return " ".join([t for t in tokens if normalize(t) not in removed] + added)


# --- Rule 3: the aarch64 toolchain ----------------------------------------------------------------


class Toolchain:
    def __init__(self, aosp, host_vars, gcc_version):
        x86_gcc = expand("${g.android.soong.cc.config.LinuxGccTriple}/"
                         "${g.android.soong.cc.config.LinuxGccVersion}", host_vars)
        self.literals = [  # x86_64 strings in toolchain values and statements, in this order
            (expand("${g.android.soong.cc.config.LinuxGccRoot}", host_vars), SYSROOT),
            (f"/lib/gcc/{x86_gcc}", f"/lib/gcc/aarch64-redhat-linux/{gcc_version}"),
            ("/x86_64-linux/lib", "/aarch64-redhat-linux/lib"),
            ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"),
            ("x86_64-linux-gnu", "aarch64-linux-gnu"),
            ("-x86_64.a", "-aarch64.a"),  # compiler-rt names in older clang versions
        ]
        self.aosp = aosp
        self.host_vars = host_vars
        self.gcc_version = gcc_version
        self.used = {}
        self.prebuilts = {}  # aarch64 prebuilt path -> the x86_64 one it replaces

    def __call__(self, value):
        def variable(match):
            name, short = match.group(1), match.group(2)
            if short == "LinuxGccTriple":
                self.used[short] = "aarch64-redhat-linux"
            elif short == "LinuxGccVersion":
                self.used[short] = self.gcc_version
            elif short not in self.used:
                self.used[short] = self(expand(self.host_vars.get(name, ""), self.host_vars))
            return "${r." + short + "}"

        tokens = []
        for token in TOOLCHAIN_VAR.sub(variable, value).split(" "):
            new = self.retarget(token)
            if new != token:
                # Prebuilt paths can sit behind other toolchain variables (older versions name
                # compiler-rt as ${...ClangAsanLibDir}/libclang_rt.builtins-x86_64.a), so they
                # are found in the token with those variables expanded.
                expanded = expand(token, self.host_vars)
                for x86, arm in zip(PREBUILT.findall(expanded),
                                    PREBUILT.findall(self.retarget(expanded))):
                    if arm != x86 and not arm.startswith(SYSROOT):
                        self.prebuilts[arm] = x86
            tokens.append("-march=armv8-a" if new == "-m64" else new)
        return " ".join(tokens)

    def retarget(self, token):
        for x86, arm in self.literals:
            token = token.replace(x86, arm)
        return self.rust_rlib(token)

    def retargets(self, path):
        """Whether `path` is an x86_64 toolchain prebuilt with an aarch64 counterpart."""
        return path.startswith("prebuilts/") and any(x86 in path for x86, _ in self.literals)

    def rust_rlib(self, token):
        """Rust standard library file names carry a build hash: find the aarch64 one by crate."""
        match = re.search(r"(prebuilts/\S*/rustlib/aarch64-unknown-linux-gnu/lib/)"
                          r"(lib\w+?)-[0-9a-f]+\.rlib", token)
        if match:
            found = sorted((self.aosp / match.group(1)).glob(f"{match.group(2)}-*.rlib"))
            if len(found) == 1:
                return token.replace(match.group(0), match.group(1) + found[0].name)
        return token

    def missing(self):
        """aarch64 prebuilts the rewritten build refers to that do not exist (yet)."""
        return sorted(arm for arm, x86 in self.prebuilts.items()
                      if not (self.aosp / arm).exists() and (self.aosp / x86).exists())

    def definitions(self):
        return "".join(f"r.{name} = {value}\n" for name, value in sorted(self.used.items()))


# --- Writing -------------------------------------------------------------------------------------


def is_tool(build):
    """Executables and Rust proc macros (compiler plugins) run on the build machine."""
    rust = build.variables.get("rustcFlags", "")
    if "--crate-type=proc-macro" in rust or "--crate-type=bin" in rust:
        return True
    return build.rule == "g.cc.ld" and "-shared" not in build.variables.get("ldFlags", "")


def write(path, include, target, closure, host_vars, delta_of, toolchain):
    # Rule 1. Only outputs in module variant directories carry the architecture: installed host
    # tools (aidl, bindgen, ...) are x86_64 programs that run during the build.
    producer = {o: b for b in closure for o in b.all_outputs() if f"/{HOST}" in o}
    specific = {}

    def is_specific(build):
        if id(build) not in specific:
            specific[id(build)] = False  # ninja graphs are acyclic; this only guards recursion
            if is_tool(build):
                return False  # runs on the build machine
            specific[id(build)] = build.rule in COMPILERS | X86_ONLY or any(
                toolchain.retargets(i) or (i in producer and is_specific(producer[i]))
                for i in build.all_inputs())
        return specific[id(build)]

    # What the target needs, through specific statements: their inputs, and the paths their
    # commands name (Soong leaves some, like a crate's --extern dependencies, undeclared).
    copied, stack, seen = [], [target], set()
    while stack:
        build = producer.get(stack.pop())
        if build is None or id(build) in seen or not is_specific(build):
            continue
        seen.add(id(build))
        if build.rule not in X86_ONLY:
            copied.append(build)
        stack.extend(build.all_inputs())
        stack.extend(p for v in build.variables.values() for p in INTERMEDIATE.findall(v))
    renamed = {o for b in copied for o in b.all_outputs()}
    outputs = set(renamed)  # files copied statements produce
    # Directories holding copied outputs are renamed too (Rust passes -L dependency=<dir>).
    # Shared files in them are named by their full path, which stays; directories with only
    # shared outputs (a module's gen/ from its code generators) stay too.
    renamed |= {str(d) for p in outputs for d in Path(p).parents if f"/{HOST}" in str(d)}
    arm = lambda p: VARIANT.sub(lambda m: f"/{ARM_HOST}{m.group(2)}" if m.group(1) == HOST
                                else m.group(0), p, count=1)
    modules = by_module(closure)

    removed_objects, object_module = set(), {}
    for build in closure:
        if build.rule in SOURCE_RULES:
            key = module_of(build.outputs[0])
            object_module[arm(build.outputs[0])] = key
            if build.rule in X86_ONLY or normalize(build.inputs[0]) in delta_of[key].removed_sources:
                removed_objects |= {build.outputs[0], arm(build.outputs[0])}  # either form

    def added_object(key, device_build):
        return re.sub(r"^out(-arm64)?/", "out/", VARIANT.sub(
            lambda m: f"/{ARM_HOST}{key[1]}", device_build.outputs[0], count=1))

    def rename(token):  # paths, also inside flags such as -L dependency=<dir> or -I<dir>
        return INTERMEDIATE.sub(lambda m: arm(m.group(0))
                      if m.group(0).rstrip("/") in renamed else m.group(0), token)

    def text(value):
        value = re.sub(rf"(\$\{{m\.[^}}]+?)_{HOST}", rf"\1_{ARM_HOST}", value)
        return toolchain(" ".join(rename(t) for t in value.split(" ")))

    def copy(build, key, inputs=None):
        delta = delta_of.get(key, Delta())
        variables = {k: text(apply(v, *delta.flags[k]) if k in delta.flags else v)
                     for k, v in build.variables.items()}
        paths = lambda values: [toolchain(arm(p) if p in renamed else p) for p in values]
        implicit = paths(build.implicit)
        # Outputs of copied statements that the command names (a link's libFlags naming a
        # library) are dependencies too: Soong only orders some of them through .toc files.
        named = {arm(p) for v in build.variables.values() for p in
                 INTERMEDIATE.findall(v) if p in outputs}
        implicit += sorted(named - set(implicit) - set(inputs or paths(build.inputs)))
        return ninja_graph.Build(paths(build.outputs), paths(build.implicit_outputs), build.rule,
                                 inputs if inputs is not None else paths(build.inputs),
                                 implicit, paths(build.order_only), variables)

    statements = []
    # Soong copies lists of paths into module variants (a crate's .soong_deps, the sources its
    # depfile may name). The lists of copied variants name the copied paths, like commands do.
    aosp = path.parent.parent
    raw_dir = path.parent / ARM_HOST / "raw"
    lists = [b for b in closure if b.rule == "g.android.rawFileCopy" and id(b) not in seen
             and str(Path(b.outputs[0]).parent) in renamed and (aosp / b.inputs[0]).exists()]
    for build in lists:  # first all of them, as the lists name each other
        renamed.add(build.outputs[0])
        outputs.add(arm(build.outputs[0]))
    for build in lists:
        raw = raw_dir / Path(build.inputs[0]).name
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw.write_text("\n".join(" ".join(rename(t) for t in line.split(" "))
                                  for line in (aosp / build.inputs[0]).read_text().split("\n")))
        new = copy(build, module_of(build.outputs[0]))
        new.outputs, new.inputs = [arm(build.outputs[0])], [str(raw.relative_to(aosp))]
        statements.append(new)
    for build in copied:
        key = module_of(build.outputs[0])
        if build.rule in SOURCE_RULES and arm(build.outputs[0]) in removed_objects:
            continue
        inputs = None
        if build.rule.startswith(("g.cc.ar", "g.cc.ld")):  # objects can be in other modules'
            inputs = [arm(p) if p in renamed else p for p in build.inputs]  # archives too
            used = {object_module[p] for p in inputs if p in object_module}
            inputs = [p for p in inputs if p not in removed_objects]
            inputs += [added_object(m, b) for m in sorted(used, key=str)
                       for b in delta_of[m].added_sources.values()]
        statements.append(copy(build, key, inputs))
    for key, delta in delta_of.items():  # arm64-only sources compile like the module's others
        compiles = [b for b in modules[key] if b.rule == "g.cc.cc"]
        for source, device_build in delta.added_sources.items():
            ext = "." + source.rsplit(".", 1)[-1]
            new = copy(next((b for b in compiles if b.inputs[0].endswith(ext)), compiles[0]), key)
            new.outputs = [added_object(key, device_build)]
            new.inputs = [re.sub(r"^out(-arm64)?/", "out/", device_build.inputs[0])]
            statements.append(new)

    # The module variables the copied statements use, with rule 2 applied.
    key_of = {var_prefix(builds): key for key, builds in modules.items()}
    used = {m for b in copied for v in b.variables.values() for m in
            re.findall(rf"\$\{{(m\.[^}}]+?_{HOST}[A-Za-z0-9_-]*)\.\w+\}}", v)}
    prefixes = {prefix: key_of.get(prefix) for prefix in used}
    variables = []
    for name, value in sorted(host_vars.items()):
        match = MODULE_VAR.match(name)
        if match and match.group(1) in prefixes:
            delta = delta_of.get(prefixes[match.group(1)], Delta())
            added, removed = delta.variables.get(match.group(2), ([], set()))
            if any("VARIANT" in t for t in added):
                raise SystemExit(f"{name}: arm64 adds a variant-specific path: {added}")
            variables.append(f"{name.replace(HOST, ARM_HOST, 1)} = "
                             f"{text(apply(value, added, removed))}\n")

    body = "".join(variables) + "".join(s.format() for s in statements)
    # Soong's ninja file uses pools that the combined ninja file of the product defines.
    pools = "".join(sorted({m for combined in path.parent.glob("combined-*.ninja") for m in
                            re.findall(r"^pool .+\n(?:[ \t]+.+\n)*", combined.read_text(), re.M)}))
    path.write_text(f"# Generated by aosp-linux-aarch64. Do not edit.\n{pools}"
                    f"include {include}\nbuilddir = out/{ARM_HOST}\n"
                    f"{toolchain.definitions()}{body}")
    return len(statements), len(variables)


def rewrite(aosp, target, host_ninja, device_arm_ninja, device_arm_out, out, gcc_version):
    """Writes `out` (relative to `aosp`).

    Returns the aarch64 counterpart of `target` and the aarch64 prebuilts it needs that do
    not exist yet.
    """
    log(f"Reading the {HOST} build")
    host_builds, host_vars = ninja_graph.read(
        host_ninja, lambda first, line: f"/{HOST}" in first,
        lambda name: (name.startswith("m.") and f"_{HOST}" in name)
        or name.startswith(("g.android.soong.cc.config.", "g.android.soong.rust.config.")), aosp)
    producer = {o: b for b in host_builds for o in b.all_outputs()}
    closure, stack = {}, [target]
    while stack:  # inputs, and paths commands name without declaring them
        build = producer.get(stack.pop())
        if build is not None and id(build) not in closure:
            closure[id(build)] = build
            stack.extend(build.all_inputs())
            stack.extend(p for v in build.variables.values() for p in INTERMEDIATE.findall(v))
    if not closure:
        raise SystemExit(f"{target} is not in the {HOST} build; build it once first")
    closure = list(closure.values())
    host_modules = sorted({k for k in (module_of(b.outputs[0]) for b in closure if b.outputs)
                           if k}, key=str)
    log(f"  {len(closure)} statements in {len(host_modules)} module variants: "
        f"{dict(Counter(b.rule for b in closure).most_common())}")

    def device(ninja, out_prefix, variant):
        prefixes = tuple(f"{out_prefix}/soong/.intermediates/{k[0]}/" for k in host_modules)
        return ninja_graph.read(ninja, lambda first, line: first.startswith(prefixes) and
                                f"/{variant}" in first, lambda name: name.startswith("m.") and
                                f"_{variant}" in name, aosp)

    log(f"Comparing the {DEVICE_ARM} and {DEVICE_X86} builds of those modules")
    delta_of = deltas(host_modules, device(host_ninja, "out", DEVICE_X86),
                      device(device_arm_ninja, device_arm_out, DEVICE_ARM))
    for key, delta in sorted(delta_of.items(), key=lambda kv: str(kv[0])):
        changes = [f"sources +{len(delta.added_sources)} -{len(delta.removed_sources)}"] if (
            delta.added_sources or delta.removed_sources) else []
        changes += [f"{n} +{a} -{sorted(r)}" for n, (a, r) in sorted(delta.variables.items())]
        changes += [f"{n} +{a} -{sorted(r)}" for n, (a, r) in sorted(delta.flags.items())]
        if changes:
            log(f"  {key[0]}{key[1]}: " + "; ".join(changes))

    toolchain = Toolchain(aosp, host_vars, gcc_version)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10 * len(closure)))
    statements, variables = write(aosp / out, host_ninja.relative_to(aosp), target, closure,
                                  host_vars, delta_of, toolchain)
    log(f"Wrote {out}: {statements} statements, {variables} module variables")
    arm_target = VARIANT.sub(lambda m: f"/{ARM_HOST}{m.group(2)}", target, count=1)
    return arm_target, toolchain.missing()
