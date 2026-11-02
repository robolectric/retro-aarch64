"""A streaming reader for the parts of Soong's build.ninja that the aarch64 rewrite needs.

Soong's build.ninja is several gigabytes, so it is read once, line by line, and only build
statements whose outputs match a filter, plus top-level variables whose names match a filter,
are kept. Values are kept in their escaped ninja form so that they can be written back as is.
"""

import re

_TOKEN = re.compile(r"(?:\$.|[^ $])+")


class Build:
    """One build statement. Paths are kept escaped, as they appear in the file."""

    __slots__ = ("outputs", "implicit_outputs", "rule", "inputs", "implicit", "order_only",
                 "variables")  # validations (|@) are dropped: they check, they do not build

    def __init__(self, outputs, implicit_outputs, rule, inputs, implicit, order_only, variables):
        self.outputs = outputs
        self.implicit_outputs = implicit_outputs
        self.rule = rule
        self.inputs = inputs
        self.implicit = implicit
        self.order_only = order_only
        self.variables = variables

    def all_outputs(self):
        return self.outputs + self.implicit_outputs

    def all_inputs(self):
        return self.inputs + self.implicit + self.order_only

    def format(self):
        def paths(values):
            return " ".join(values)

        line = paths(self.outputs)
        if self.implicit_outputs:
            line += " | " + paths(self.implicit_outputs)
        line += f": {self.rule}"
        if self.inputs:
            line += " " + paths(self.inputs)
        if self.implicit:
            line += " | " + paths(self.implicit)
        if self.order_only:
            line += " || " + paths(self.order_only)
        body = "".join(f"    {key} = {value}\n" for key, value in self.variables.items())
        return f"build {line}\n{body}"


def _split(text):
    return _TOKEN.findall(text)


def _parse_build(line, body):
    """Parses `build outs [| implicit outs]: rule ins [| implicit] [|| order-only]`."""
    # The first unescaped colon ends the outputs.
    match = re.search(r"(?<!\$):", line)
    outputs_text, rest = line[: match.start()], line[match.end() :]
    output_tokens = _split(outputs_text)
    if "|" in output_tokens:
        split = output_tokens.index("|")
        outputs, implicit_outputs = output_tokens[:split], output_tokens[split + 1 :]
    else:
        outputs, implicit_outputs = output_tokens, []
    tokens = _split(rest)
    rule, tokens = tokens[0], tokens[1:]
    groups = {"": [], "|": [], "||": [], "|@": []}  # |@: validations, not dependencies
    current = ""
    for token in tokens:
        if token in groups:
            current = token
            continue
        groups[current].append(token)
    variables = {}
    for entry in body:
        key, _, value = entry.strip().partition(" = ")
        if not _:
            key, value = entry.strip().rstrip("=").strip(), ""
        variables[key] = value
    return Build(
        outputs,
        implicit_outputs,
        rule,
        groups[""],
        groups["|"],
        groups["||"],
        variables,
    )


def _logical_lines(path):
    """Yields (indented, text) for each logical line, joining `$`-continued physical lines."""
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        pending = None
        indented = False
        for raw in f:
            line = raw.rstrip("\n")
            continued = line.endswith("$") and not line.endswith("$$")
            if continued:
                line = line[:-1]
            if pending is None:
                indented = line.startswith((" ", "\t"))
                pending = line.strip() if indented else line
            else:
                pending += line.lstrip()
            if not continued:
                yield indented, pending
                pending = None


def read(path, want_build, want_variable, root=None):
    """Reads `path` and the files it includes; returns (builds, variables) the filters accept.

    want_build(first_output, line) and want_variable(name) are called with escaped text. Other
    build statements and their bodies are skipped without being parsed. `include` and
    `subninja` paths are relative to `root`, the directory ninja runs in (newer Soong versions
    split build.ninja into shards that the main file includes with subninja).
    """
    from pathlib import Path

    root = Path(root) if root is not None else Path(path).parent.parent.parent
    builds = []
    variables = {}
    current = None  # (line, body) of a build statement being collected
    for indented, text in _logical_lines(path):
        if not indented and text.startswith(("include ", "subninja ")):
            included = root / text.split(" ", 1)[1].strip()
            more_builds, more_variables = read(included, want_build, want_variable, root)
            builds += more_builds
            variables.update(more_variables)
            continue
        if indented:
            if current is not None:
                current[1].append(text)
            continue
        if current is not None:
            builds.append(_parse_build(*current))
            current = None
        if text.startswith("build "):
            line = text[6:]
            first = line.lstrip().split(" ", 1)[0].split(":", 1)[0]
            if want_build(first, line):
                current = (line, [])
        elif " = " in text and not text.startswith(("rule ", "pool ", "#")):
            name, value = text.split(" = ", 1)
            if want_variable(name):
                variables[name] = value
    if current is not None:
        builds.append(_parse_build(*current))
    return builds, variables
