#!/usr/bin/env python3
"""Index selected project text and build a self-contained offline study viewer."""
import argparse
import ast
import copy
import datetime
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sys
import tempfile

MARKER = "__CODE_STUDY_DATA__"
SKIP_DIRS = {".git", ".code-study", "node_modules", ".venv", "venv", ".godot", "__pycache__", "dist", "build"}
LANGUAGES = {"py": "python", "pyi": "python", "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript", "ts": "typescript", "tsx": "typescript", "gd": "gdscript", "cs": "csharp", "c": "c", "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "h": "c", "hpp": "cpp", "java": "java", "go": "go", "rs": "rust", "lua": "lua", "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml", "ini": "ini", "cfg": "config", "godot": "godot", "tscn": "godot", "tres": "godot", "md": "markdown", "rst": "text", "txt": "text", "sql": "sql", "html": "html", "htm": "html", "css": "css", "scss": "css", "sh": "shell", "bash": "shell", "ps1": "powershell", "xml": "xml", "csv": "csv", "vue": "vue", "svelte": "svelte", "bat": "batch"}
NODE_KINDS = {"file", "class", "function", "method", "external", "unknown"}
EDGE_KINDS = {"import", "call", "event", "data", "config", "lifecycle"}
EDGE_STATUS = {"source_confirmed", "inferred", "unresolved", "runtime_observed"}
VIEWS = {"atlas", "guided", "reader"}
SHA = re.compile(r"[0-9a-fA-F]{64}\Z")


class StudyError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise StudyError(message)


def emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def root_path(value):
    root = Path(value).resolve(strict=True)
    require(root.is_dir(), "project root must be a directory")
    return root


def relative_path(value):
    require(isinstance(value, str) and bool(value) and "\x00" not in value, "invalid relative path")
    normalized = value.replace("\\", "/")
    require(not PurePosixPath(normalized).is_absolute() and not PureWindowsPath(value).drive,
            "absolute source paths are not allowed")
    parts = PurePosixPath(normalized).parts
    require(bool(parts) and ".." not in parts, "source path escapes project root")
    return PurePosixPath(*parts).as_posix()


def source_path(root, relative):
    relative = relative_path(relative)
    path = root / relative
    resolved = path.resolve()
    require(resolved.is_relative_to(root), "source path resolves outside project root: " + relative)
    return path


def linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def matches(path, patterns):
    for pattern in patterns:
        pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern):
            return True
        while pattern.startswith("**/"):
            pattern = pattern[3:]
            if fnmatch.fnmatchcase(path, pattern):
                return True
    return False


def language(path):
    name = PurePosixPath(path).name
    if name.casefold() in {".env.example", ".env.sample"}:
        return "config"
    if name in {"Dockerfile", "Makefile", "LICENSE", "README", ".gitignore", ".gitattributes", ".editorconfig"}:
        return "text"
    return LANGUAGES.get(PurePosixPath(path).suffix.lower().lstrip("."))


def enumerate_files(root, options):
    """Enumerate without opening content or traversing any link/reparse directory."""
    excluded, candidates = [], []
    count = 0
    def reject(path, reason, kind="file"):
        excluded.append({"path": path, "reason": reason, "kind": kind})
    def walk(directory):
        nonlocal count
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(root).as_posix()
            if linked(path):
                reject(relative, "link_or_reparse_point", "link")
                continue
            if path.is_dir():
                if path.name.casefold() in SKIP_DIRS:
                    reject(relative, "default_directory", "directory")
                elif matches(relative, options["exclude"]):
                    reject(relative, "exclude_glob", "directory")
                else:
                    walk(path)
                continue
            count += 1
            if not path.is_file():
                reject(relative, "not_regular_file")
            elif (path.name.casefold() == ".env" or path.name.casefold().startswith(".env.")) and path.name.casefold() not in {".env.example", ".env.sample"}:
                reject(relative, "credential_file")
            elif matches(relative, options["exclude"]):
                reject(relative, "exclude_glob")
            elif options["include"] and not matches(relative, options["include"]):
                reject(relative, "outside_include_globs")
            elif language(relative) is None:
                reject(relative, "unsupported_text_extension")
            elif path.stat().st_size > options["max_file_bytes"]:
                reject(relative, "max_file_bytes")
            elif len(candidates) >= options["max_files"]:
                reject(relative, "max_files")
            else:
                candidates.append((path, relative))
    walk(root)
    return candidates, excluded, count


def line_count(source):
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    return max(1, normalized.count("\n") + (not normalized.endswith("\n")))


def node(node_id, kind, name, file_id=None, parent_id=None, start=None, end=None):
    return {"id": node_id, "kind": kind, "name": name, "file_id": file_id,
            "parent_id": parent_id, "start_line": start, "end_line": end, "role": "", "explanation": ""}


class PythonIndex:
    """Resolve lexical definitions/imports; retain uncertainty for dynamic targets."""
    def __init__(self, files, nodes, edges):
        self.files, self.nodes, self.edges = files, nodes, edges
        self.trees, self.symbols, self.definitions, self.scopes = {}, {}, {}, {}
        self.modules = {}
        self.external = {}
        self.attribute_writes = []
        self.invalidated_names = {}
        self.invalidated_symbols = set()
        for file in files:
            if file["language"] != "python":
                continue
            path = file["path"]
            stem = path.rsplit(".", 1)[0].replace("/", ".")
            module = stem[:-9] if stem.endswith(".__init__") else stem
            if module == "__init__":
                module = ""
            for name in {module, module[4:] if module.startswith("src.") else module}:
                self.modules.setdefault(name, []).append(file["id"])
            try:
                tree = ast.parse(file["source"].removeprefix("\ufeff"), filename=path)
            except (SyntaxError, ValueError, RecursionError):
                file["analysis_status"] = "parse_error"
                continue
            file["analysis_status"] = "parsed"
            self.trees[file["id"]] = tree
            self.symbols[file["id"]] = {}
            self.scopes[file["id"]] = {"parent": None, "kind": "file", "defs": {}, "imports": {}, "bound": set(), "types": {}, "file_id": file["id"]}
            self.collect(tree, file, file["id"], "")
        for file_id, tree in self.trees.items():
            self.collect_bindings(tree, file_id)
        for scope_id, name in self.attribute_writes:
            file_id = self.scopes[scope_id]["file_id"]
            self.invalidated_names.setdefault(file_id, set()).add(name)
            target = self._resolve(name, scope_id)[0]
            if target in self.definitions:
                self.invalidated_symbols.add(target)
        for file_id, tree in self.trees.items():
            self.visit(tree, file_id, file_id)

    def collect(self, tree, file, scope_id, prefix):
        for child in ast.iter_child_nodes(tree):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = prefix + child.name
                base = "symbol:" + file["path"] + ":" + qualified
                identifier = base if base not in self.definitions else base + "@" + str(child.lineno)
                kind = "class" if isinstance(child, ast.ClassDef) else ("method" if self.scopes[scope_id]["kind"] == "class" else "function")
                entry = node(identifier, kind, child.name, file["id"], scope_id, child.lineno, getattr(child, "end_lineno", child.lineno))
                self.nodes.append(entry)
                self.definitions[identifier] = entry
                self.symbols[file["id"]].setdefault(qualified, []).append(identifier)
                self.scopes[scope_id]["defs"].setdefault(child.name, []).append(identifier)
                self.scopes[identifier] = {"parent": scope_id, "kind": kind, "defs": {}, "imports": {}, "bound": set(), "types": {}, "file_id": file["id"]}
                child._study_id = identifier
                self.collect(child, file, identifier, qualified + ".")
            else:
                self.collect(child, file, scope_id, prefix)

    def module_file(self, name):
        candidates = self.modules.get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    def import_module(self, statement, file_id):
        module = statement.module or ""
        if not statement.level:
            return module
        path = next(file["path"] for file in self.files if file["id"] == file_id)
        package = path.rsplit("/", 1)[0].replace("/", ".") if "/" in path else ""
        parts = package.split(".") if package else []
        if statement.level - 1 > len(parts):
            return None
        base = parts[:len(parts) - statement.level + 1]
        return ".".join(base + ([module] if module else []))

    def collect_bindings(self, tree, scope_id):
        scope = self.scopes[scope_id]
        def bind_import(name, binding):
            if name in scope["imports"]:
                scope.setdefault("ambiguous_imports", set()).add(name)
            else:
                scope["imports"][name] = binding

        def bind_type(name, annotation):
            # Repeated hints have no uniquely established receiver type.
            scope["types"][name] = None if name in scope["types"] else annotation

        if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = tree.args.posonlyargs + tree.args.args + tree.args.kwonlyargs
            args += [arg for arg in (tree.args.vararg, tree.args.kwarg) if arg is not None]
            for argument in args:
                scope["bound"].add(argument.arg)
                if argument.annotation:
                    bind_type(argument.arg, self.expression_name(argument.annotation))
            parent = self.scopes[scope["parent"]]
            if parent["kind"] == "class" and args and args[0].arg in {"self", "cls"}:
                bind_type(args[0].arg, ("class_id", scope["parent"]))
        def walk(current):
            for child in ast.iter_child_nodes(current):
                if hasattr(child, "_study_id"):
                    self.collect_bindings(child, child._study_id)
                    continue
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        bind_import(alias.asname or alias.name.split(".")[0], ("module", alias.name if alias.asname else alias.name.split(".")[0]))
                elif isinstance(child, ast.ImportFrom):
                    module = self.import_module(child, scope["file_id"])
                    for alias in child.names:
                        if alias.name != "*":
                            bind_import(alias.asname or alias.name, ("from", module, alias.name))
                        else:
                            scope["wildcard_import"] = True
                if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    scope["bound"].add(child.id)
                if isinstance(child, ast.Attribute) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    name = self.expression_name(child)
                    if name:
                        self.attribute_writes.append((scope_id, name))
                if isinstance(child, ast.ExceptHandler) and child.name:
                    scope["bound"].add(child.name)
                if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
                    scope["bound"].add(child.name)
                if isinstance(child, ast.MatchMapping) and child.rest:
                    scope["bound"].add(child.rest)
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    bind_type(child.target.id, self.expression_name(child.annotation))
                walk(child)
        walk(tree)

    @staticmethod
    def expression_name(expression):
        if isinstance(expression, ast.Name):
            return expression.id
        if isinstance(expression, ast.Attribute):
            prefix = PythonIndex.expression_name(expression.value)
            return prefix + "." + expression.attr if prefix else None
        return None

    def symbol(self, file_id, name):
        # Nested function definitions are not attributes on their enclosing function.
        parts = name.split(".")
        for length in range(1, len(parts)):
            parents = self.symbols.get(file_id, {}).get(".".join(parts[:length]), [])
            if len(parents) != 1 or self.definitions[parents[0]]["kind"] != "class":
                return None
        targets = self.symbols.get(file_id, {}).get(name, [])
        return targets[0] if len(targets) == 1 else None

    def scope_chain(self, scope_id):
        while scope_id:
            scope = self.scopes[scope_id]
            yield scope_id, scope
            scope_id = scope["parent"]
            # A class namespace is not a lexical closure for method bodies.
            if scope_id and scope["kind"] in {"function", "method"} and self.scopes[scope_id]["kind"] == "class":
                scope_id = self.scopes[scope_id]["parent"]

    def imported_target(self, binding, suffix):
        if binding[0] == "module":
            module = binding[1]
            pieces = (module + ("." + suffix if suffix else "")).split(".")
            for split in range(len(pieces), 0, -1):
                module_name = ".".join(pieces[:split])
                if module_name in self.modules and len(self.modules[module_name]) != 1:
                    return None, "unresolved", ".".join(pieces)
                file_id = self.module_file(module_name)
                if file_id:
                    target = self.symbol(file_id, ".".join(pieces[split:])) if split < len(pieces) else file_id
                    return target, "source_confirmed" if target else "unresolved", ".".join(pieces)
            return None, "external", ".".join(pieces)
        module, name = binding[1:]
        if module is None:
            return None, "unresolved", name
        full_name = name + ("." + suffix if suffix else "")
        if module in self.modules and len(self.modules[module]) != 1:
            return None, "unresolved", (module + "." if module else "") + full_name
        file_id = self.module_file(module)
        if file_id:
            target = self.symbol(file_id, full_name)
            if target:
                return target, "source_confirmed", module + "." + full_name
            child_file = self.module_file((module + "." if module else "") + name)
            if child_file:
                target = self.symbol(child_file, suffix) if suffix else child_file
                return target, "source_confirmed" if target else "unresolved", module + "." + full_name
            return None, "unresolved", module + "." + full_name
        child_file = self.module_file((module + "." if module else "") + name)
        if child_file:
            target = self.symbol(child_file, suffix) if suffix else child_file
            return target, "source_confirmed" if target else "unresolved", module + "." + full_name
        return None, "external", (module + "." if module else "") + full_name

    def resolve(self, name, scope_id, permit_types=True):
        if name:
            invalidated = self.invalidated_names.get(self.scopes[scope_id]["file_id"], set())
            if any(name == path or name.startswith(path + ".") for path in invalidated):
                return None, "unresolved", name
        target, status, label = self._resolve(name, scope_id, permit_types)
        parent = target
        while parent in self.definitions:
            if parent in self.invalidated_symbols:
                return None, "unresolved", label
            parent = self.definitions[parent]["parent_id"]
        return target, status, label

    def _resolve(self, name, scope_id, permit_types=True):
        if not name:
            return None, "unresolved", "dynamic expression"
        first, _, suffix = name.partition(".")
        for current_id, scope in self.scope_chain(scope_id):
            if scope.get("wildcard_import"):
                return None, "unresolved", name
            if first in scope.get("ambiguous_imports", set()) or (first in scope["defs"] and first in scope["imports"]):
                return None, "unresolved", name
            if first in scope["bound"]:
                annotation = scope["types"].get(first)
                if suffix and annotation and permit_types:
                    if isinstance(annotation, tuple):
                        class_id = annotation[1]
                    else:
                        class_id = self.resolve(annotation, current_id, False)[0]
                    if class_id and class_id in self.definitions and self.definitions[class_id]["kind"] == "class":
                        method = self.scopes[class_id]["defs"].get(suffix, [])
                        if len(method) == 1:
                            return method[0], "inferred", name
                return None, "unresolved", name
            if first in scope["defs"]:
                targets = scope["defs"][first]
                if len(targets) != 1:
                    return None, "unresolved", name
                target = targets[0]
                if suffix:
                    for attribute in suffix.split("."):
                        # Only class namespaces expose nested definitions as attributes.
                        if target is None or self.definitions[target]["kind"] != "class":
                            target = None
                            break
                        nested = self.scopes[target]["defs"].get(attribute, [])
                        target = nested[0] if len(nested) == 1 else None
                return target, "source_confirmed" if target else "unresolved", name
            if first in scope["imports"]:
                return self.imported_target(scope["imports"][first], suffix)
        builtins = {"print", "len", "range", "str", "int", "float", "dict", "list", "set", "tuple", "open", "isinstance", "super", "getattr", "setattr", "hasattr", "enumerate", "zip", "sum", "min", "max", "sorted", "map", "filter", "bool", "type", "object"}
        return None, "external" if first in builtins else "unresolved", name

    def synthetic(self, name, status, file_id, line):
        kind = "external" if status == "external" else "unknown"
        identifier = kind + ":" + (name if kind == "external" else file_id + ":" + str(line) + ":" + name)
        if identifier not in self.external:
            self.external[identifier] = node(identifier, kind, name)
            self.nodes.append(self.external[identifier])
        return identifier

    def edge(self, origin, target, kind, status, label, file_id, statement):
        if status == "external":
            status = "source_confirmed"
        self.edges.append({"id": "edge:" + str(len(self.edges) + 1), "from": origin, "to": target,
                           "kind": kind, "status": status, "label": label,
                           "evidence": [{"file_id": file_id, "start_line": statement.lineno, "end_line": getattr(statement, "end_lineno", statement.lineno)}], "note": ""})

    def visit(self, current, scope_id, file_id):
        for child in ast.iter_child_nodes(current):
            if hasattr(child, "_study_id"):
                # Defaults/decorators execute in the defining namespace, bodies in their own scope.
                for decorator in child.decorator_list:
                    self.visit_expression(decorator, scope_id, file_id)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for default in child.args.defaults + [item for item in child.args.kw_defaults if item is not None]:
                        self.visit_expression(default, scope_id, file_id)
                elif isinstance(child, ast.ClassDef):
                    for base in child.bases:
                        self.visit_expression(base, scope_id, file_id)
                    for keyword in child.keywords:
                        self.visit_expression(keyword.value, scope_id, file_id)
                for statement in child.body:
                    self.visit_expression(statement, child._study_id, file_id)
            else:
                self.visit_expression(child, scope_id, file_id)

    def visit_expression(self, expression, scope_id, file_id):
        if hasattr(expression, "_study_id"):
            # Delegate a nested definition through a temporary AST wrapper.
            self.visit(ast.Module(body=[expression], type_ignores=[]), scope_id, file_id)
            return
        if isinstance(expression, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # Anonymous/comprehension bindings must not resolve to same-named globals.
            temporary_scope = "scope:" + file_id + ":" + str(expression.lineno) + ":" + str(id(expression))
            bound = set()
            if isinstance(expression, ast.Lambda):
                args = expression.args.posonlyargs + expression.args.args + expression.args.kwonlyargs
                args += [arg for arg in (expression.args.vararg, expression.args.kwarg) if arg is not None]
                bound.update(argument.arg for argument in args)
            else:
                for generator in expression.generators:
                    bound.update(child.id for child in ast.walk(generator.target) if isinstance(child, ast.Name))
            self.scopes[temporary_scope] = {"parent": scope_id, "kind": "function", "defs": {}, "imports": {}, "bound": bound, "types": {}, "file_id": file_id, "origin": self.scopes[scope_id].get("origin", scope_id)}
            for child in ast.iter_child_nodes(expression):
                self.visit_expression(child, temporary_scope, file_id)
            return
        if isinstance(expression, ast.Call):
            name = self.expression_name(expression.func)
            target, status, label = self.resolve(name, scope_id)
            if target is not None and target.startswith("file:"):
                target, status = None, "unresolved"
            if target is None:
                target = self.synthetic(label, status, file_id, expression.lineno)
            self.edge(self.scopes[scope_id].get("origin", scope_id), target, "call", status, label, file_id, expression)
        elif isinstance(expression, ast.Import):
            for alias in expression.names:
                target = self.module_file(alias.name)
                status = "unresolved" if alias.name in self.modules and target is None else "source_confirmed"
                if target is None:
                    target = self.synthetic(alias.name, "unresolved" if status == "unresolved" else "external", file_id, expression.lineno)
                self.edge(scope_id, target, "import", status, alias.name, file_id, expression)
        elif isinstance(expression, ast.ImportFrom):
            module = self.import_module(expression, file_id)
            for alias in expression.names:
                target, status, label = self.imported_target(("from", module, alias.name), "")
                if alias.name == "*":
                    status, target = "unresolved", None
                if target is None:
                    target = self.synthetic(label, status, file_id, expression.lineno)
                self.edge(scope_id, target, "import", status, label, file_id, expression)
        for child in ast.iter_child_nodes(expression):
            self.visit_expression(child, scope_id, file_id)


def scan_project(root, options, title):
    candidates, excluded, scanned = enumerate_files(root, options)
    files, nodes, edges = [], [], []
    for path, relative in candidates:
        # Recheck before opening, including ancestors, in case a link was encountered later.
        resolved = source_path(root, relative)
        ancestors = [resolved] + list(resolved.parents)[:len(PurePosixPath(relative).parts) - 1]
        if any(linked(item) for item in ancestors):
            excluded.append({"path": relative, "reason": "link_or_reparse_point", "kind": "link"})
            continue
        with path.open("rb") as stream:
            raw = stream.read(options["max_file_bytes"] + 1)
        if len(raw) > options["max_file_bytes"]:
            excluded.append({"path": relative, "reason": "max_file_bytes", "kind": "file"})
            continue
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            excluded.append({"path": relative, "reason": "not_utf8", "kind": "file"})
            continue
        file_id = "file:" + relative
        files.append({"id": file_id, "path": relative, "language": language(relative), "source": source,
                      "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw), "analysis_status": "indexed"})
        nodes.append(node(file_id, "file", PurePosixPath(relative).name, file_id, None, 1, line_count(source)))
    PythonIndex(files, nodes, edges)
    entries = [file["id"] for file in files if PurePosixPath(file["path"]).name in {"main.py", "__main__.py", "app.py", "main.js", "main.ts", "index.js", "index.ts", "Program.cs", "main.go", "main.rs", "project.godot"}]
    parsed = sum(file["analysis_status"] == "parsed" for file in files)
    return {"schema_version": 1,
            "project": {"root": str(root), "title": title, "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "default_view": "atlas", "entry_node_ids": entries},
            "scan_options": options,
            "coverage": {"scanned_files": scanned, "included_files": len(files), "parsed_files": parsed, "indexed_only_files": len(files) - parsed,
                         "excluded": excluded, "analysis_notes": ["Python relations use lexical AST evidence; type-hint method targets are inferred. Dynamic targets remain unresolved.", "Other languages are indexed in full without automatic relation analysis.", "Entry filenames suggest candidates; they do not confirm runtime entrypoints."]},
            "files": files, "nodes": nodes, "edges": edges, "lessons": []}


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def unique(items, category):
    require(isinstance(items, list), category + " must be a list")
    result = {}
    for item in items:
        require(isinstance(item, dict) and isinstance(item.get("id"), str) and bool(item["id"]), "invalid " + category + " ID")
        require(item["id"] not in result, "duplicate " + category + " ID: " + item["id"])
        result[item["id"]] = item
    return result


def valid_lines(start, end, file):
    require(type(start) is int and type(end) is int and 1 <= start <= end <= line_count(file["source"]), "invalid source line range")


def validate_node(entry, files, nodes):
    require(entry.get("kind") in NODE_KINDS, "invalid node kind")
    require(all(isinstance(entry.get(field), str) for field in ("name", "role", "explanation")), "invalid node text fields")
    file_id = entry.get("file_id")
    require(file_id is None or (isinstance(file_id, str) and file_id in files), "unknown node file reference")
    parent = entry.get("parent_id")
    require(parent is None or (isinstance(parent, str) and parent in nodes and parent != entry["id"]), "unknown or self node parent reference")
    if file_id is not None:
        valid_lines(entry.get("start_line"), entry.get("end_line"), files[file_id])
        require(parent is None or nodes[parent].get("file_id") == file_id, "node parent must belong to the same source file")
    else:
        require(entry["kind"] in {"external", "unknown"} and entry.get("start_line") is None and entry.get("end_line") is None, "defined node requires a real file and lines")


def validate_edge(entry, files, nodes):
    require(entry.get("kind") in EDGE_KINDS and entry.get("status") in EDGE_STATUS, "invalid edge kind or status")
    require(entry.get("from") in nodes and entry.get("to") in nodes, "unknown edge node reference")
    require(isinstance(entry.get("label"), str) and isinstance(entry.get("note"), str), "invalid edge text")
    require(isinstance(entry.get("evidence"), list), "edge evidence must be a list")
    for evidence in entry["evidence"]:
        require(isinstance(evidence, dict) and evidence.get("file_id") in files, "unknown evidence file reference")
        valid_lines(evidence.get("start_line"), evidence.get("end_line"), files[evidence["file_id"]])
    require(entry["status"] != "source_confirmed" or bool(entry["evidence"]), "source-confirmed edge requires evidence")


def validate_lessons(lessons, files, nodes):
    unique(lessons, "lesson")
    for lesson in lessons:
        require(all(isinstance(lesson.get(field), str) for field in ("title", "goal")), "invalid lesson text")
        require(isinstance(lesson.get("steps"), list), "lesson steps must be a list")
        for step in lesson["steps"]:
            require(isinstance(step, dict) and step.get("node_id") in nodes, "unknown lesson node reference")
            require(all(isinstance(step.get(field), str) for field in ("title", "explanation", "question")), "invalid lesson step text")
            require(isinstance(step.get("focus_lines"), list), "invalid focus lines")
            file_id = nodes[step["node_id"]]["file_id"]
            require(not step["focus_lines"] or file_id in files, "focus lines require source file")
            for line in step["focus_lines"]:
                valid_lines(line, line, files[file_id])


def validate_data(data):
    require(isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1, "invalid data schema version")
    project = data.get("project")
    require(isinstance(project, dict) and isinstance(project.get("root"), str) and Path(project["root"]).is_absolute(), "invalid project root")
    require(isinstance(project.get("title"), str) and isinstance(project.get("generated_at_utc"), str) and project.get("default_view") in VIEWS, "invalid project fields")
    try:
        timestamp = datetime.datetime.fromisoformat(project["generated_at_utc"].replace("Z", "+00:00"))
        require(timestamp.utcoffset() == datetime.timedelta(0), "generated timestamp must be UTC")
    except ValueError:
        raise StudyError("invalid generated timestamp") from None
    options = data.get("scan_options")
    require(isinstance(options, dict), "invalid scan options")
    for field in ("include", "exclude"):
        require(isinstance(options.get(field), list) and all(isinstance(value, str) and bool(value) for value in options[field]), "invalid scan glob list")
    for field in ("max_file_bytes", "max_files"):
        require(type(options.get(field)) is int and options[field] > 0, "invalid scan limit")
    coverage = data.get("coverage")
    require(isinstance(coverage, dict), "invalid coverage")
    for field in ("scanned_files", "included_files", "parsed_files", "indexed_only_files"):
        require(type(coverage.get(field)) is int and coverage[field] >= 0, "invalid coverage count")
    require(isinstance(coverage.get("excluded"), list) and isinstance(coverage.get("analysis_notes"), list) and all(isinstance(note, str) for note in coverage["analysis_notes"]), "invalid coverage details")
    for excluded in coverage["excluded"]:
        require(isinstance(excluded, dict) and isinstance(excluded.get("reason"), str) and excluded.get("kind") in {"file", "directory", "link"}, "invalid exclusion record")
        relative_path(excluded.get("path"))
    files = unique(data.get("files"), "file")
    paths = set()
    for file in files.values():
        path = relative_path(file.get("path"))
        require(path == file["path"] and path not in paths and file["id"] == "file:" + path, "invalid or duplicate file path")
        paths.add(path)
        require(isinstance(file.get("source"), str) and isinstance(file.get("language"), str) and file.get("analysis_status") in {"parsed", "indexed", "parse_error"}, "invalid file fields")
        raw = file["source"].encode("utf-8")
        require(isinstance(file.get("sha256"), str) and SHA.fullmatch(file["sha256"]) and hashlib.sha256(raw).hexdigest() == file["sha256"].lower(), "source text hash mismatch")
        require(type(file.get("size_bytes")) is int and file["size_bytes"] == len(raw), "source size mismatch")
    require(coverage["included_files"] == len(files) and coverage["parsed_files"] == sum(file["analysis_status"] == "parsed" for file in files.values()) and coverage["indexed_only_files"] == len(files) - coverage["parsed_files"], "coverage counts disagree with files")
    nodes = unique(data.get("nodes"), "node")
    for entry in nodes.values():
        validate_node(entry, files, nodes)
    for file_id in files:
        require(file_id in nodes and nodes[file_id]["kind"] == "file" and nodes[file_id]["file_id"] == file_id, "missing file node")
    # Parent cycles would make hierarchical viewers unsafe to traverse.
    for identifier in nodes:
        seen = set()
        current = identifier
        while current is not None:
            require(current not in seen, "node parent cycle")
            seen.add(current)
            current = nodes[current]["parent_id"]
    edges = unique(data.get("edges"), "edge")
    for entry in edges.values():
        validate_edge(entry, files, nodes)
    require(isinstance(project.get("entry_node_ids"), list) and all(identifier in nodes for identifier in project["entry_node_ids"]), "invalid entry node references")
    validate_lessons(data.get("lessons"), files, nodes)
    return files, nodes


def check_data(data, root_override=None):
    files, _ = validate_data(data)
    root = root_path(root_override or data["project"]["root"])
    candidates, current_excluded, _ = enumerate_files(root, data["scan_options"])
    result = {"status": "current", "project_root_changed": str(root) != data["project"]["root"],
              "changed": [], "missing": [], "new": [], "unchanged": []}
    expected_paths = {file["path"] for file in files.values()}
    budget_reasons = {"max_file_bytes", "max_files"}
    previous_budget = {entry["path"] for entry in data["coverage"]["excluded"] if entry["reason"] in budget_reasons}
    current_budget = {entry["path"] for entry in current_excluded if entry["reason"] in budget_reasons}
    previous_scope = expected_paths | previous_budget
    current_scope = {relative for _, relative in candidates} | current_budget
    # Budget exclusions are part of the coverage contract even though their bytes were not read.
    result["new"].extend(current_budget - previous_scope)
    result["missing"].extend(previous_budget - current_scope)
    for file in files.values():
        path = source_path(root, file["path"])
        if not path.exists():
            result["missing"].append(file["path"])
            continue
        ancestors = [path] + list(path.parents)[:len(PurePosixPath(file["path"]).parts) - 1]
        require(not any(linked(item) for item in ancestors), "tracked source is now a link: " + file["path"])
        require(path.is_file(), "tracked source is no longer a regular file: " + file["path"])
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(block)
        result["unchanged" if sha.hexdigest() == file["sha256"].lower() else "changed"].append(file["path"])
    # Decode new candidates to match scan's actual inclusion rather than counting binary text.
    for path, relative in candidates:
        if relative not in expected_paths:
            with path.open("rb") as stream:
                raw = stream.read(data["scan_options"]["max_file_bytes"] + 1)
            if len(raw) <= data["scan_options"]["max_file_bytes"]:
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                result["new"].append(relative)
    for field in ("changed", "missing", "new", "unchanged"):
        result[field] = sorted(set(result[field]))
    if result["project_root_changed"] or result["changed"] or result["missing"] or result["new"]:
        result["status"] = "changed"
    return result


def apply_annotations(data, annotations):
    require(isinstance(annotations, dict) and type(annotations.get("schema_version")) is int and annotations["schema_version"] == 1, "invalid annotations schema version")
    result = copy.deepcopy(data)
    files, nodes = validate_data(result)
    hashes = annotations.get("source_hashes")
    require(isinstance(hashes, dict), "annotations source_hashes must be an object")
    for file_id, digest in hashes.items():
        require(file_id in files and isinstance(digest, str) and SHA.fullmatch(digest), "invalid annotation source hash")
        require(digest.lower() == files[file_id]["sha256"].lower(), "annotation source changed; refresh annotations: " + file_id)
    touched = set()
    additions = unique(annotations.get("nodes", []), "annotation node")
    for identifier, entry in additions.items():
        if identifier in nodes:
            require(set(entry) <= {"id", "role", "explanation"}, "existing annotation nodes may only override role/explanation")
            for field in ("role", "explanation"):
                if field in entry:
                    require(isinstance(entry[field], str), "invalid annotation node text")
                    nodes[identifier][field] = entry[field]
        else:
            require(set(entry) >= {"id", "kind", "name", "file_id", "parent_id", "start_line", "end_line", "role", "explanation"}, "new annotation node requires complete fields")
            require(entry.get("file_id") in files and entry.get("kind") in {"class", "function", "method", "unknown"}, "new annotation symbol requires a real source file")
            nodes[identifier] = copy.deepcopy(entry)
            result["nodes"].append(nodes[identifier])
        if nodes[identifier]["file_id"]:
            touched.add(nodes[identifier]["file_id"])
    extra_edges = unique(annotations.get("edges", []), "annotation edge")
    existing_edges = {entry["id"] for entry in result["edges"]}
    for identifier, entry in extra_edges.items():
        require(identifier not in existing_edges, "annotation edge ID already exists")
        validate_edge(entry, files, nodes)
        for endpoint in (entry["from"], entry["to"]):
            if nodes[endpoint]["file_id"]:
                touched.add(nodes[endpoint]["file_id"])
        touched.update(evidence["file_id"] for evidence in entry["evidence"])
        result["edges"].append(copy.deepcopy(entry))
    if "lessons" in annotations:
        validate_lessons(annotations["lessons"], files, nodes)
        result["lessons"] = copy.deepcopy(annotations["lessons"])
        for lesson in result["lessons"]:
            for step in lesson["steps"]:
                file_id = nodes[step["node_id"]]["file_id"]
                if file_id:
                    touched.add(file_id)
    if "project" in annotations:
        project = annotations["project"]
        require(isinstance(project, dict) and set(project) <= {"title", "default_view", "entry_node_ids"}, "invalid annotation project override")
        result["project"].update(copy.deepcopy(project))
        for identifier in project.get("entry_node_ids", []):
            require(identifier in nodes, "unknown annotation entry node")
            if nodes[identifier]["file_id"]:
                touched.add(nodes[identifier]["file_id"])
    require(touched <= hashes.keys(), "annotations need matching source_hashes for every referenced source file")
    validate_data(result)
    return result


def protect_output(output, inputs):
    output = Path(output).resolve()
    for path in inputs:
        path = Path(path).resolve()
        require(output != path and not (output.exists() and path.exists() and os.path.samefile(output, path)), "output cannot overwrite an input or source file")
    return output


def project_inputs(root, options):
    """Protect every encountered file, without opening exclusions or entering skipped dirs."""
    candidates, excluded, _ = enumerate_files(root, options)
    inputs = [path for path, _ in candidates]
    inputs.extend(root / entry["path"] for entry in excluded if entry["kind"] in {"file", "link"})
    return inputs


def atomic_write(output, text, force):
    require(force or not output.exists(), "output exists; use --force to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, output)
        else:
            os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def scan(args):
    root = root_path(args.root)
    require(args.max_file_bytes > 0 and args.max_files > 0, "scan limits must be positive")
    options = {"include": args.include, "exclude": args.exclude, "max_file_bytes": args.max_file_bytes, "max_files": args.max_files}
    data = scan_project(root, options, args.title or root.name)
    validate_data(data)
    output = protect_output(args.output, project_inputs(root, options))
    atomic_write(output, json.dumps(data, ensure_ascii=True, indent=2) + "\n", args.force)
    emit({"status": "scanned", "output": str(output), "coverage": data["coverage"]})
    return 0


def build(args):
    data = read_json(args.data)
    files, _ = validate_data(data)
    template_path = Path(args.template) if args.template else Path(__file__).resolve().parent.parent / "assets" / "study-viewer.html"
    inputs = [args.data, template_path] + ([args.annotations] if args.annotations else [])
    inputs += [Path(data["project"]["root"]) / file["path"] for file in files.values()]
    inputs += project_inputs(root_path(data["project"]["root"]), data["scan_options"])
    output = protect_output(args.output, inputs)
    checked = check_data(data)
    require(checked["status"] == "current", "source data is stale; rescan before build (changed/missing/new source)")
    if args.annotations:
        data = apply_annotations(data, read_json(args.annotations))
    if args.default_view:
        data["project"]["default_view"] = args.default_view
    template = template_path.read_bytes().decode("utf-8")
    require(template.count(MARKER) == 1, "template must contain exactly one data marker")
    embedded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for raw, escaped in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        embedded = embedded.replace(raw, escaped)
    atomic_write(output, template.replace(MARKER, embedded), args.force)
    emit({"status": "built", "output": str(output), "files": len(data["files"]), "nodes": len(data["nodes"]), "edges": len(data["edges"]), "lessons": len(data["lessons"])})
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise StudyError(message)


def main():
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    scanning = sub.add_parser("scan")
    scanning.add_argument("--root", required=True)
    scanning.add_argument("--output", required=True)
    scanning.add_argument("--title")
    scanning.add_argument("--include", action="append", default=[])
    scanning.add_argument("--exclude", action="append", default=[])
    scanning.add_argument("--max-file-bytes", type=int, default=1024 * 1024)
    scanning.add_argument("--max-files", type=int, default=500)
    scanning.add_argument("--force", action="store_true")
    building = sub.add_parser("build")
    building.add_argument("--data", required=True)
    building.add_argument("--annotations")
    building.add_argument("--output", required=True)
    building.add_argument("--default-view", choices=sorted(VIEWS))
    building.add_argument("--template")
    building.add_argument("--force", action="store_true")
    checking = sub.add_parser("check")
    checking.add_argument("--data", required=True)
    checking.add_argument("--root")
    try:
        args = parser.parse_args()
        if args.command == "scan":
            return scan(args)
        if args.command == "build":
            return build(args)
        result = check_data(read_json(args.data), args.root)
        emit(result)
        return 0 if result["status"] == "current" else 1
    except (StudyError, OSError, ValueError, TypeError, KeyError, RecursionError, RuntimeError) as error:
        emit({"status": "unavailable", "error": str(error)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
