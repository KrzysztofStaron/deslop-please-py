"""Reads a Python project and writes its model as JSON to stdout.

usage: python3 extract.py <root> [--all-files]

The model is what the Rust side of deslop-please lints: a tree of packages and
modules, the files, the items they define (classes, functions, methods,
fields, module constants), every reference from code to an item, and the
functions that only wrap others. Names are resolved from the code alone: its
imports, annotations, constructor calls and `self`. A name that cannot be
resolved to something the project defines is no reference.
"""

import ast
import json
import os
import subprocess
import sys

SKIP_DIRS = {
    "__pycache__", "node_modules", "build", "dist", "site-packages",
    "venv", "env", "target", "docs",
}
PLAIN_TYPES = {
    "int", "float", "str", "bool", "bytes", "complex", "Path", "PurePath",
    "Decimal", "datetime", "date", "time", "timedelta", "UUID", "Enum",
}
MUTABLE_TYPES = {
    "list", "dict", "set", "bytearray", "List", "Dict", "Set", "deque",
    "defaultdict", "OrderedDict", "Counter", "ndarray", "array", "Tensor",
    "MutableMapping", "MutableSequence", "MutableSet", "Queue",
}
UNKNOWN = "unknown"


def is_test_name(name):
    return name in ("tests", "test", "conftest") or name.startswith("test_") or name.endswith("_tests")


def is_test_path(path):
    parts = path.split("/")
    base = parts[-1]
    return (
        any(part in ("tests", "test") for part in parts[:-1])
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base == "conftest.py"
    )


def find_files(root):
    names = None
    try:
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "--", "*.py"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        names = [line for line in out.splitlines() if line]
        names = [n for n in names if os.path.isfile(os.path.join(root, n))]
    except (OSError, subprocess.CalledProcessError):
        pass
    if names is None:
        names = []
        for here, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for file in files:
                if file.endswith(".py"):
                    names.append(os.path.relpath(os.path.join(here, file), root).replace(os.sep, "/"))
    keep = []
    for name in names:
        parts = name.split("/")
        if any(p.startswith(".") or p in SKIP_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
            continue
        keep.append(name)
    return sorted(set(keep))


class Item:
    def __init__(self, idx, name, owner, kind, parent, file, **flags):
        self.idx = idx
        self.name = name
        self.owner = owner
        self.kind = kind
        self.parent = parent
        self.file = file
        self.trait_impl = flags.get("trait_impl", False)
        self.takes_self = flags.get("takes_self", False)
        self.ast = flags.get("ast")
        self.mod = flags.get("mod")
        self.annotation = flags.get("annotation")
        self.value = flags.get("value")
        self.static = flags.get("static", False)
        self.classmethod = flags.get("classmethod", False)
        self.members = {}
        self.bases = []
        self.base_exprs = []
        self.is_enum = False


class Mod:
    def __init__(self, name, path, node, test, tree, file, is_package):
        self.name = name
        self.path = path
        self.node = node
        self.test = test
        self.tree = tree
        self.file = file
        self.is_package = is_package
        self.defs = {}
        self.imports = {}


class Extractor:
    def __init__(self, root):
        self.root = root
        self.nodes = []
        self.node_index = {}
        self.files = []
        self.items = []
        self.refs = []
        self.mods = {}
        self.field_type = {}
        self.field_kind = {}
        self.ret_cache = {}
        self.wrappers = []

    def node_for(self, parts, test_file):
        """The tree node of a dotted module, made with its ancestors."""
        parent = -1
        for depth in range(len(parts)):
            key = tuple(parts[: depth + 1])
            if key not in self.node_index:
                self.node_index[key] = len(self.nodes)
                self.nodes.append({
                    "name": parts[depth],
                    "kind": "package" if depth == 0 else "module",
                    "parent": parent,
                    "test": is_test_name(parts[depth]),
                })
            parent = self.node_index[key]
        return parent

    def module_name(self, path, source_roots):
        for prefix in source_roots:
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        stem = path[:-3]
        parts = stem.split("/")
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        return parts, is_package

    def load(self, paths):
        source_roots = []
        for candidate in ("src/",):
            if os.path.isdir(os.path.join(self.root, candidate)) and not os.path.exists(
                os.path.join(self.root, candidate, "__init__.py")
            ) and any(p.startswith(candidate) for p in paths):
                source_roots.append(candidate)
        for path in paths:
            try:
                with open(os.path.join(self.root, path), encoding="utf-8") as handle:
                    text = handle.read()
                tree = ast.parse(text, filename=path)
            except (SyntaxError, UnicodeDecodeError, ValueError) as error:
                print(f"deslop-please: skipping {path}: {error}", file=sys.stderr)
                continue
            parts, is_package = self.module_name(path, source_roots)
            if not parts:
                continue
            name = ".".join(parts)
            if name in self.mods:
                continue
            node = self.node_for(parts, False)
            test = is_test_path(path)
            file = len(self.files)
            self.files.append({"path": path, "module": node, "test": test, "lines": len(text.splitlines())})
            self.mods[name] = Mod(name, path, node, test, tree, file, is_package)

    def add_item(self, mod, name, kind, parent=None, **flags):
        item = Item(len(self.items), name, mod.node, kind, parent, mod.file, mod=mod, **flags)
        self.items.append(item)
        return item

    # ---- definitions -----------------------------------------------------

    def define_all(self):
        for mod in self.mods.values():
            self.define_module(mod)
        for mod in self.mods.values():
            for cls in [i for i in self.items if i.kind == "type" and i.mod is mod]:
                for expr in cls.base_exprs:
                    base = self.entity_static(mod, expr)
                    if base and base[0] == "item" and self.items[base[1]].kind == "type":
                        cls.bases.append(base[1])
                    name = expr_name(expr)
                    if name and name.endswith("Enum"):
                        cls.is_enum = True

    def statements(self, body):
        """Top-level statements, looking through if/try/with blocks."""
        for stmt in body:
            if isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody"):
                    yield from self.statements(getattr(stmt, field, []))
                for handler in getattr(stmt, "handlers", []):
                    yield from self.statements(handler.body)
            else:
                yield stmt

    def define_module(self, mod):
        package = mod.name if mod.is_package else mod.name.rpartition(".")[0]
        for stmt in self.statements(mod.tree.body):
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.asname:
                        mod.imports[alias.asname] = ("module", alias.name)
                    else:
                        top = alias.name.split(".")[0]
                        mod.imports[top] = ("module", top)
            elif isinstance(stmt, ast.ImportFrom):
                base = stmt.module or ""
                if stmt.level:
                    parts = package.split(".") if package else []
                    parts = parts[: max(len(parts) - (stmt.level - 1), 0)]
                    base = ".".join(parts + ([stmt.module] if stmt.module else []))
                for alias in stmt.names:
                    if alias.name != "*":
                        mod.imports[alias.asname or alias.name] = ("from", base, alias.name)
            elif isinstance(stmt, ast.ClassDef):
                self.define_class(mod, stmt)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                item = self.add_item(mod, stmt.name, "function", ast=stmt)
                mod.defs[stmt.name] = item.idx
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in ("__all__",):
                        item = self.add_item(
                            mod, target.id, "constant", ast=stmt,
                            annotation=getattr(stmt, "annotation", None), value=stmt.value,
                        )
                        mod.defs[target.id] = item.idx

    def define_class(self, mod, node):
        cls = self.add_item(mod, node.name, "type", ast=node)
        cls.base_exprs = list(node.bases)
        mod.defs[node.name] = cls.idx
        for stmt in self.statements(node.body):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = {expr_name(d) for d in stmt.decorator_list}
                static = "staticmethod" in decorators
                klass = "classmethod" in decorators
                takes_self = bool(stmt.args.args or stmt.args.posonlyargs) and not static and not klass
                dunder = stmt.name.startswith("__") and stmt.name.endswith("__") and stmt.name != "__init__"
                method = self.add_item(
                    mod, f"{node.name}.{stmt.name}", "function", cls.idx, ast=stmt,
                    takes_self=takes_self, trait_impl=dunder, static=static, classmethod=klass,
                )
                cls.members.setdefault(stmt.name, method.idx)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in cls.members:
                        field = self.add_item(
                            mod, f"{node.name}.{target.id}", "field", cls.idx, ast=stmt,
                            annotation=getattr(stmt, "annotation", None), value=stmt.value,
                        )
                        cls.members[target.id] = field.idx
        for name, idx in list(cls.members.items()):
            method = self.items[idx]
            if method.kind != "function" or not method.takes_self:
                continue
            args = method.ast.args
            first = (args.posonlyargs + args.args)[0].arg
            for sub in ast.walk(method.ast):
                targets = []
                if isinstance(sub, ast.Assign):
                    targets = sub.targets
                elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
                    targets = [sub.target]
                for target in flatten_targets(targets):
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == first
                        and target.attr not in cls.members
                    ):
                        field = self.add_item(
                            mod, f"{node.name}.{target.attr}", "field", cls.idx, ast=sub,
                            annotation=getattr(sub, "annotation", None), value=getattr(sub, "value", None),
                        )
                        cls.members[target.attr] = field.idx

    # ---- names -----------------------------------------------------------

    def lookup(self, mod, name, depth=0):
        if name in mod.defs:
            return ("item", mod.defs[name])
        imported = mod.imports.get(name)
        if imported is None or depth > 8:
            return None
        if imported[0] == "module":
            return ("module", imported[1]) if imported[1] in self.mods else None
        _, base, attr = imported
        return self.in_module(base, attr, depth + 1)

    def in_module(self, modname, attr, depth=0):
        target = self.mods.get(modname)
        if target is not None:
            found = self.lookup(target, attr, depth)
            if found:
                return found
        sub = f"{modname}.{attr}" if modname else attr
        if sub in self.mods:
            return ("module", sub)
        return None

    def entity_static(self, mod, expr):
        """A name or dotted name, without any code around it."""
        if isinstance(expr, ast.Name):
            return self.lookup(mod, expr.id)
        if isinstance(expr, ast.Attribute):
            base = self.entity_static(mod, expr.value)
            if base and base[0] == "module":
                return self.in_module(base[1], expr.attr)
            if base and base[0] == "item" and self.items[base[1]].kind == "type":
                member = self.member(base[1], expr.attr)
                return ("item", member) if member is not None else None
        return None

    def member(self, cls, name, seen=None):
        seen = seen or set()
        if cls in seen:
            return None
        seen.add(cls)
        item = self.items[cls]
        if name in item.members:
            return item.members[name]
        for base in item.bases:
            found = self.member(base, name, seen)
            if found is not None:
                return found
        return None

    def ann_class(self, mod, expr):
        """The project class an annotation names, through Optional and unions."""
        if expr is None:
            return None
        if isinstance(expr, ast.Constant):
            if isinstance(expr.value, str):
                try:
                    return self.ann_class(mod, ast.parse(expr.value, mode="eval").body)
                except SyntaxError:
                    return None
            return None
        if isinstance(expr, (ast.Name, ast.Attribute)):
            found = self.entity_static(mod, expr)
            if found and found[0] == "item" and self.items[found[1]].kind == "type":
                return found[1]
            return None
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return self.ann_class(mod, expr.left) or self.ann_class(mod, expr.right)
        if isinstance(expr, ast.Subscript):
            head = expr_name(expr.value)
            if head in ("Optional", "Annotated", "Final", "ClassVar"):
                inner = expr.slice
                if isinstance(inner, ast.Tuple):
                    inner = inner.elts[0]
                return self.ann_class(mod, inner)
            if head == "Union":
                parts = expr.slice.elts if isinstance(expr.slice, ast.Tuple) else [expr.slice]
                for part in parts:
                    found = self.ann_class(mod, part)
                    if found is not None:
                        return found
        return None

    def kind_of_annotation(self, mod, expr):
        """`plain`, `mutable`, or `unknown`, by what an annotation names."""
        if expr is None:
            return UNKNOWN
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                return self.kind_of_annotation(mod, ast.parse(expr.value, mode="eval").body)
            except SyntaxError:
                return UNKNOWN
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            kinds = [self.kind_of_annotation(mod, expr.left), self.kind_of_annotation(mod, expr.right)]
            kinds = [k for k in kinds if k != "none"]
            return kinds[0] if len(kinds) == 1 else ("mutable" if "mutable" in kinds else UNKNOWN)
        if isinstance(expr, ast.Constant) and expr.value is None:
            return "none"
        if isinstance(expr, ast.Subscript):
            head = expr_name(expr.value)
            if head in ("Optional", "Final", "ClassVar", "Annotated"):
                inner = expr.slice.elts[0] if isinstance(expr.slice, ast.Tuple) else expr.slice
                return self.kind_of_annotation(mod, inner)
            if head in ("tuple", "Tuple", "frozenset", "FrozenSet"):
                return "plain"
            return "mutable" if head in MUTABLE_TYPES else UNKNOWN
        name = expr_name(expr)
        cls = self.ann_class(mod, expr)
        if cls is not None:
            return "plain" if self.items[cls].is_enum else "mutable"
        if name in PLAIN_TYPES:
            return "plain"
        if name in MUTABLE_TYPES:
            return "mutable"
        return UNKNOWN

    def kind_of_value(self, mod, value):
        if value is None:
            return UNKNOWN
        if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
            return "mutable"
        if isinstance(value, (ast.Constant, ast.JoinedStr)):
            return "plain"
        if isinstance(value, ast.Tuple):
            return "plain"
        if isinstance(value, ast.Call):
            name = expr_name(value.func)
            if name in MUTABLE_TYPES:
                return "mutable"
            found = self.entity_static(mod, value.func)
            if found and found[0] == "item" and self.items[found[1]].kind == "type":
                return "plain" if self.items[found[1]].is_enum else "mutable"
        return UNKNOWN

    def field_is_mutable(self, idx):
        item = self.items[idx]
        kind = self.field_kind.get(idx)
        if kind is None:
            kind = self.kind_of_annotation(item.mod, item.annotation)
            if kind == UNKNOWN:
                kind = self.kind_of_value(item.mod, item.value)
            self.field_kind[idx] = kind
        return kind == "mutable"

    def field_class(self, idx):
        if idx in self.field_type:
            return self.field_type[idx]
        item = self.items[idx]
        found = self.ann_class(item.mod, item.annotation)
        if found is not None:
            self.field_type[idx] = found
        return found

    def return_class(self, idx):
        item = self.items[idx]
        if idx not in self.ret_cache:
            self.ret_cache[idx] = self.ann_class(item.mod, item.ast.returns)
        return self.ret_cache[idx]

    # ---- references ------------------------------------------------------

    def reference_pass(self, emit):
        for mod in self.mods.values():
            walker = Walker(self, mod, emit)
            walker.module(mod.tree)

    def find_wrappers(self):
        wrapper_of = {}
        for item in self.items:
            if item.kind != "function" or not item.takes_self or item.parent is None:
                continue
            wrapper = self.accessor(item)
            if wrapper:
                wrapper_of[item.idx] = wrapper
        for item in self.items:
            if item.kind == "function" and item.idx not in wrapper_of:
                wrapper = self.forwarder(item)
                if wrapper:
                    wrapper_of[item.idx] = wrapper
        for idx, wrapper in list(wrapper_of.items()):
            seen = {idx}
            at = wrapper
            while at and at["kind"] == "forwarder":
                if at["target"] in seen:
                    del wrapper_of[idx]
                    break
                seen.add(at["target"])
                at = wrapper_of.get(at["target"])
        self.wrappers = [dict(item=idx, **w) for idx, w in sorted(wrapper_of.items())]

    def body_of(self, item):
        body = list(item.ast.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        return [s for s in body if not isinstance(s, ast.Pass)]

    def accessor(self, item):
        args = item.ast.args
        params = [a.arg for a in args.posonlyargs + args.args]
        if not params:
            return None
        me = params[0]
        cls = item.parent
        lent, copied, to_read = [], [], True
        body = self.body_of(item)
        if not body:
            return None
        for stmt in body:
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                field = self.own_field(stmt.value, me, cls)
                if field is None:
                    return None
                (lent if self.field_is_mutable(field) else copied).append(field)
            elif (
                isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.value, ast.Name) and stmt.value.id in params[1:]
            ):
                field = self.own_field(stmt.targets[0], me, cls)
                if field is None:
                    return None
                lent.append(field)
                to_read = False
            else:
                return None
        return {"kind": "accessor", "lent": sorted(set(lent)), "copied": sorted(set(copied)), "to_read": to_read}

    def own_field(self, expr, me, cls):
        if (
            isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == me
        ):
            idx = self.member(cls, expr.attr)
            if idx is not None and self.items[idx].kind == "field":
                return idx
        return None

    def forwarder(self, item):
        body = self.body_of(item)
        if len(body) != 1:
            return None
        stmt = body[0]
        call = stmt.value if isinstance(stmt, (ast.Return, ast.Expr)) else None
        if not isinstance(call, ast.Call):
            return None
        args = item.ast.args
        params = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        me = params[0] if item.takes_self and params else None
        names = {a.id for a in call.args if isinstance(a, ast.Name)}
        names |= {k.value.id for k in call.keywords if isinstance(k.value, ast.Name)}
        operands = len(call.args) + len(call.keywords)
        if len(names) != operands or not names <= set(params) or me in names:
            return None
        fields = []
        func = call.func
        mod = item.mod
        if isinstance(func, ast.Attribute):
            receiver = func.value
            chain = []
            while isinstance(receiver, ast.Attribute):
                chain.append(receiver.attr)
                receiver = receiver.value
            if not isinstance(receiver, ast.Name) or receiver.id not in params:
                return None
            if receiver.id == me:
                if not chain:
                    method = self.member(item.parent, func.attr)
                    return {"kind": "forwarder", "target": method, "fields": []} if method is not None and method != item.idx and self.items[method].kind == "function" else None
                cls = item.parent
                for attr in reversed(chain):
                    field = self.member(cls, attr)
                    if field is None or self.items[field].kind != "field":
                        return None
                    fields.append(field)
                    cls = self.field_class(field)
                    if cls is None:
                        return None
                target = self.member(cls, func.attr)
            else:
                return None
        elif isinstance(func, ast.Name):
            found = self.lookup(mod, func.id)
            if not found or found[0] != "item":
                return None
            target = found[1]
        else:
            return None
        if target is None or target == item.idx or self.items[target].kind != "function":
            return None
        return {"kind": "forwarder", "target": target, "fields": fields}

    def output(self):
        items = []
        for item in self.items:
            items.append({
                "name": item.name, "owner": item.owner, "kind": item.kind,
                "parent": item.parent, "file": item.file,
                "trait_impl": item.trait_impl, "takes_self": item.takes_self,
            })
        return {
            "nodes": self.nodes, "files": self.files, "items": items,
            "references": self.refs, "wrappers": self.wrappers,
        }


class Walker:
    """Walks one module's code, resolving names as it goes."""

    def __init__(self, ex, mod, emit):
        self.ex = ex
        self.mod = mod
        self.emit = emit
        self.holder = None
        self.env = {}
        self.cls = None
        self.me = None
        self.memo = {}

    # -- entities: ("item", idx) | ("module", name) | ("instance", cls) | None

    def entity(self, expr):
        key = id(expr)
        if key in self.memo:
            return self.memo[key]
        found = self.compute(expr)
        self.memo[key] = found
        return found

    def of_item(self, idx):
        item = self.ex.items[idx]
        if item.kind == "field":
            cls = self.ex.field_class(idx)
            return ("instance", cls) if cls is not None else ("item", idx)
        if item.kind == "constant":
            cls = self.ex.field_type.get(idx)
            if cls is None:
                cls = self.ex.ann_class(item.mod, item.annotation)
            if cls is None and isinstance(item.value, ast.Call):
                called = self.ex.entity_static(item.mod, item.value.func)
                if called and called[0] == "item" and self.ex.items[called[1]].kind == "type":
                    cls = called[1]
            return ("instance", cls) if cls is not None else ("item", idx)
        return ("item", idx)

    def compute(self, expr):
        ex = self.ex
        if isinstance(expr, ast.Name):
            if expr.id in self.env:
                value = self.env[expr.id]
                return None if value == UNKNOWN else value
            found = ex.lookup(self.mod, expr.id)
            if found and found[0] == "item":
                return self.of_item(found[1])
            return found
        if isinstance(expr, ast.Attribute):
            base = self.entity(expr.value)
            if base is None:
                return None
            if base[0] == "module":
                found = ex.in_module(base[1], expr.attr)
                if found and found[0] == "item":
                    return self.of_item(found[1])
                return found
            cls = base[1] if base[0] == "instance" else None
            if base[0] == "item" and ex.items[base[1]].kind == "type":
                cls = base[1]
            if cls is not None:
                member = ex.member(cls, expr.attr)
                if member is not None:
                    return self.of_item(member)
            return None
        if isinstance(expr, ast.Call):
            func = self.entity(expr.func)
            if func is None:
                return None
            if func[0] == "item":
                item = ex.items[func[1]]
                if item.kind == "type":
                    return ("instance", func[1])
                if item.kind == "function":
                    cls = ex.return_class(func[1])
                    return ("instance", cls) if cls is not None else None
            return None
        if isinstance(expr, ast.Await):
            return self.entity(expr.value)
        return None

    def item_of(self, expr):
        """The project item a name or attribute names, if any."""
        found = self.entity(expr)
        if found is None:
            return None
        if found[0] == "item":
            return found[1]
        if found[0] == "instance" and isinstance(expr, (ast.Name, ast.Attribute)):
            # A field or constant whose type is known: the item itself.
            return self.item_named(expr)
        return None

    def item_named(self, expr):
        ex = self.ex
        if isinstance(expr, ast.Name):
            if expr.id in self.env:
                return None
            found = ex.lookup(self.mod, expr.id)
            return found[1] if found and found[0] == "item" else None
        base = self.entity(expr.value)
        if base is None:
            return None
        if base[0] == "module":
            found = ex.in_module(base[1], expr.attr)
            return found[1] if found and found[0] == "item" else None
        cls = base[1] if base[0] == "instance" else (base[1] if base[0] == "item" and ex.items[base[1]].kind == "type" else None)
        return ex.member(cls, expr.attr) if cls is not None else None

    # -- emitting

    def refer(self, node, idx, copies=False):
        if not self.emit:
            return
        self.ex.refs.append({
            "file": self.mod.file, "line": node.lineno, "from": self.mod.node,
            "holder": self.holder, "item": idx, "copies": copies,
        })

    # -- walking

    def module(self, tree):
        for stmt in tree.body:
            self.stmt(stmt)

    def stmt(self, node):
        method = getattr(self, "stmt_" + type(node).__name__, None)
        if method:
            method(node)
        else:
            self.generic(node)

    def generic(self, node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self.stmt(child)
            elif isinstance(child, ast.expr):
                self.expr(child)
            elif isinstance(child, (ast.ExceptHandler, ast.match_case, ast.withitem, ast.comprehension, ast.arguments, ast.keyword)):
                self.generic(child)

    def bind(self, target, value):
        if isinstance(target, ast.Name):
            self.env[target.id] = value if value else UNKNOWN
        elif isinstance(target, (ast.Tuple, ast.List)):
            for sub in target.elts:
                self.bind(sub, None)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, None)
        else:
            self.expr(target)

    def unbind_all(self, node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                self.env[sub.id] = UNKNOWN

    def stmt_Import(self, node):
        pass

    stmt_ImportFrom = stmt_Import

    def stmt_Assign(self, node):
        self.expr(node.value)
        value = self.entity(node.value)
        if value is not None and value[0] not in ("instance",) and not (value[0] == "item" and self.ex.items[value[1]].kind == "type"):
            value = None
        for target in node.targets:
            self.assign_target(target, value)

    def stmt_AnnAssign(self, node):
        self.annotation(node.annotation)
        if node.value is not None:
            self.expr(node.value)
        cls = self.ex.ann_class(self.mod, node.annotation)
        value = ("instance", cls) if cls is not None else (self.entity(node.value) if node.value is not None else None)
        if value is not None and value[0] not in ("instance",):
            value = None
        self.assign_target(node.target, value)

    def assign_target(self, target, value):
        if isinstance(target, ast.Attribute) and self.emit is False and value and value[0] == "instance":
            base = self.entity(target.value)
            if base and base[0] == "instance":
                field = self.ex.member(base[1], target.attr)
                if field is not None and self.ex.items[field].kind == "field":
                    self.ex.field_type.setdefault(field, value[1])
        self.bind(target, value)

    def stmt_AugAssign(self, node):
        self.expr(node.value)
        self.expr(node.target)
        if isinstance(node.target, ast.Name):
            self.env[node.target.id] = UNKNOWN

    def stmt_For(self, node):
        self.expr(node.iter)
        self.bind(node.target, None)
        for stmt in node.body + node.orelse:
            self.stmt(stmt)

    stmt_AsyncFor = stmt_For

    def stmt_With(self, node):
        for item in node.items:
            self.expr(item.context_expr)
            if item.optional_vars is not None:
                self.bind(item.optional_vars, None)
        for stmt in node.body:
            self.stmt(stmt)

    stmt_AsyncWith = stmt_With

    def stmt_Try(self, node):
        for stmt in node.body:
            self.stmt(stmt)
        for handler in node.handlers:
            if handler.type is not None:
                self.expr(handler.type)
            if handler.name:
                self.env[handler.name] = UNKNOWN
            for stmt in handler.body:
                self.stmt(stmt)
        for stmt in node.orelse + node.finalbody:
            self.stmt(stmt)

    stmt_TryStar = stmt_Try

    def stmt_Match(self, node):
        self.expr(node.subject)
        for case in node.cases:
            self.unbind_all(case.pattern)
            for sub in ast.walk(case.pattern):
                if isinstance(sub, (ast.MatchAs, ast.MatchStar)) and sub.name:
                    self.env[sub.name] = UNKNOWN
                if isinstance(sub, ast.MatchClass):
                    self.expr(sub.cls)
                if isinstance(sub, ast.MatchValue):
                    self.expr(sub.value)
            if case.guard is not None:
                self.expr(case.guard)
            for stmt in case.body:
                self.stmt(stmt)

    def annotation(self, expr):
        if expr is None:
            return
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                parsed = ast.parse(expr.value, mode="eval").body
            except SyntaxError:
                return
            for sub in ast.walk(parsed):
                sub.lineno = expr.lineno
            self.expr(parsed)
        else:
            for sub in ast.walk(expr):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub is not expr:
                    self.annotation(sub)
            self.expr(expr)

    def stmt_ClassDef(self, node):
        outer = (self.holder, self.env, self.cls, self.me)
        idx = self.class_item(node)
        for decorator in node.decorator_list:
            self.expr(decorator)
        if idx is not None:
            self.holder = idx
        for base in node.bases:
            self.expr(base)
        for keyword in node.keywords:
            self.expr(keyword.value)
        self.cls = idx
        self.env = {}
        for stmt in node.body:
            self.class_stmt(stmt, idx)
        self.holder, self.env, self.cls, self.me = outer

    def class_item(self, node):
        found = self.ex.mods[self.mod.name].defs.get(node.name)
        if found is not None and self.ex.items[found].ast is node:
            return found
        if self.cls is None:
            return None
        member = self.ex.items[self.cls].members.get(node.name)
        return None

    def class_stmt(self, stmt, cls):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            idx = self.ex.items[cls].members.get(stmt.name) if cls is not None else None
            if idx is not None and self.ex.items[idx].ast is not stmt:
                idx = None
            self.function(stmt, idx, cls)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            field = None
            if cls is not None:
                for target in targets:
                    if isinstance(target, ast.Name):
                        member = self.ex.items[cls].members.get(target.id)
                        if member is not None and self.ex.items[member].ast is stmt:
                            field = member
            outer = self.holder
            if field is not None:
                self.holder = field
            self.stmt(stmt)
            self.holder = outer
        else:
            self.stmt(stmt)

    def stmt_FunctionDef(self, node):
        idx = self.mod.defs.get(node.name) if self.holder is None and self.cls is None else None
        if idx is not None and self.ex.items[idx].ast is not node:
            idx = None
        self.function(node, idx, None)

    stmt_AsyncFunctionDef = stmt_FunctionDef

    def function(self, node, idx, cls):
        outer = (self.holder, self.env, self.me, self.cls)
        for decorator in node.decorator_list:
            self.expr(decorator)
        if idx is not None:
            self.holder = idx
        env = dict(self.env) if cls is None else {}
        args = node.args
        everyone = args.posonlyargs + args.args + args.kwonlyargs
        everyone += [a for a in (args.vararg, args.kwarg) if a is not None]
        item = self.ex.items[idx] if idx is not None else None
        for position, arg in enumerate(everyone):
            self.annotation(arg.annotation)
            env[arg.arg] = UNKNOWN
            found = self.ex.ann_class(self.mod, arg.annotation)
            if found is not None:
                env[arg.arg] = ("instance", found)
            if position == 0 and item is not None and item.parent is not None and not item.static:
                env[arg.arg] = ("item", item.parent) if item.classmethod else ("instance", item.parent)
        for default in args.defaults + [d for d in args.kw_defaults if d is not None]:
            self.expr(default)
        self.annotation(node.returns)
        self.env = env
        self.memo_scope()
        for stmt in node.body:
            self.stmt(stmt)
        self.holder, self.env, self.me, self.cls = outer
        self.memo_scope()

    def memo_scope(self):
        self.memo = {}

    def stmt_Return(self, node):
        if node.value is not None:
            self.expr(node.value)

    def expr(self, node):
        method = getattr(self, "expr_" + type(node).__name__, None)
        if method:
            method(node)
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self.expr(child)
                elif isinstance(child, (ast.comprehension, ast.keyword)):
                    self.generic(child)

    def expr_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id not in self.env:
            found = self.ex.lookup(self.mod, node.id)
            if found and found[0] == "item":
                idx = getattr(node, "_instead", found[1])
                self.refer(node, idx, copies=self.copies(idx, node))

    def copies(self, idx, node):
        if self.ex.items[idx].kind != "field":
            return False
        if not isinstance(node.ctx, ast.Load) or getattr(node, "_reach", False):
            return False
        return not self.ex.field_is_mutable(idx)

    def expr_Attribute(self, node):
        idx = self.item_named(node)
        if idx is not None:
            idx = getattr(node, "_instead", idx)
            self.refer(node, idx, copies=self.copies(idx, node))
        node.value._reach = True
        self.expr(node.value)

    def expr_Subscript(self, node):
        node.value._reach = True
        self.expr(node.value)
        self.expr(node.slice)

    def expr_Call(self, node):
        func = node.func
        func._reach = True
        target = self.entity(func)
        if target and target[0] == "item" and self.ex.items[target[1]].kind == "type":
            init = self.ex.items[target[1]].members.get("__init__")
            if init is not None and self.ex.items[init].kind == "function":
                func._instead = init
        self.expr(func)
        for arg in node.args:
            self.expr(arg)
        for keyword in node.keywords:
            self.expr(keyword.value)

    def expr_Lambda(self, node):
        outer = self.env
        self.env = dict(outer)
        for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            self.env[arg.arg] = UNKNOWN
        self.memo_scope()
        self.expr(node.body)
        self.env = outer
        self.memo_scope()

    def comprehension(self, node, elements):
        outer = self.env
        self.env = dict(outer)
        for generator in node.generators:
            self.expr(generator.iter)
            self.bind(generator.target, None)
            for condition in generator.ifs:
                self.expr(condition)
        self.memo_scope()
        for element in elements:
            self.expr(element)
        self.env = outer
        self.memo_scope()

    def expr_ListComp(self, node):
        self.comprehension(node, [node.elt])

    expr_SetComp = expr_GeneratorExp = expr_ListComp

    def expr_DictComp(self, node):
        self.comprehension(node, [node.key, node.value])

    def expr_NamedExpr(self, node):
        self.expr(node.value)
        self.bind(node.target, self.entity(node.value))


def expr_name(expr):
    while isinstance(expr, ast.Call):
        expr = expr.func
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Subscript):
        return expr_name(expr.value)
    return None


def flatten_targets(targets):
    for target in targets:
        if isinstance(target, (ast.Tuple, ast.List)):
            yield from flatten_targets(target.elts)
        else:
            yield target


def main():
    root = os.path.abspath(sys.argv[1])
    ex = Extractor(root)
    ex.load(find_files(root))
    ex.define_all()
    ex.reference_pass(emit=False)
    ex.reference_pass(emit=False)
    ex.find_wrappers()
    ex.reference_pass(emit=True)
    json.dump(ex.output(), sys.stdout)


if __name__ == "__main__":
    main()
