"""Native Python AST analyzer with explicit syntax and inference boundaries."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import ast
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .code_analyzer_common import (
    SourceMap,
    comparison_fingerprints,
    manifest_evidence,
    searchable_chunks,
)
from .code_contracts import (
    AnalysisStatus,
    CodeAnalysis,
    CodeChunk,
    CodeFileInput,
    CodeRouteConfig,
    DependencyRecord,
    DiagnosticRecord,
    DiagnosticSeverity,
    MetricRecord,
    ReferenceRecord,
    SourceRange,
    SymbolRecord,
)


# region [01] AST formatting and complexity


def _expression_name(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_name(node.value)
        return node.attr if prefix is None else f"{prefix}.{node.attr}"
    if isinstance(node, ast.Call):
        return _expression_name(node.func)
    if isinstance(node, ast.Subscript):
        return _expression_name(node.value)
    try:
        return ast.unparse(node)
    except (ValueError, TypeError, RecursionError):
        return None


def _annotation(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except (ValueError, TypeError, RecursionError):
        return _expression_name(node)


def _function_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    arguments: list[str] = []
    positional = [*node.args.posonlyargs, *node.args.args]
    default_offset = len(positional) - len(node.args.defaults)
    for index, argument in enumerate(positional):
        value = argument.arg
        annotation = _annotation(argument.annotation)
        if annotation:
            value += f": {annotation}"
        if index >= default_offset:
            default_text = _annotation(node.args.defaults[index - default_offset])
            value += " = " + (default_text or "...")
        arguments.append(value)
        if node.args.posonlyargs and index + 1 == len(node.args.posonlyargs):
            arguments.append("/")
    if node.args.vararg is not None:
        value = "*" + node.args.vararg.arg
        annotation = _annotation(node.args.vararg.annotation)
        if annotation:
            value += f": {annotation}"
        arguments.append(value)
    elif node.args.kwonlyargs:
        arguments.append("*")
    for argument, default in zip(
        node.args.kwonlyargs, node.args.kw_defaults, strict=True
    ):
        value = argument.arg
        annotation = _annotation(argument.annotation)
        if annotation:
            value += f": {annotation}"
        if default is not None:
            value += " = " + (_annotation(default) or "...")
        arguments.append(value)
    if node.args.kwarg is not None:
        value = "**" + node.args.kwarg.arg
        annotation = _annotation(node.args.kwarg.annotation)
        if annotation:
            value += f": {annotation}"
        arguments.append(value)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = _annotation(node.returns)
    suffix = "" if returns is None else f" -> {returns}"
    return f"{prefix} {node.name}({', '.join(arguments)}){suffix}"


class _ComplexityVisitor(ast.NodeVisitor):
    """Small McCabe-compatible counter that excludes nested definitions."""

    def __init__(self, root: ast.AST):
        self.root = root
        self.value = 1

    def _branch(self, node: ast.AST) -> None:
        self.value += 1
        self.generic_visit(node)

    visit_If = _branch
    visit_For = _branch
    visit_AsyncFor = _branch
    visit_While = _branch
    visit_IfExp = _branch
    visit_comprehension = _branch

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.value += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.value += len(node.handlers) + bool(node.orelse) + bool(node.finalbody)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.value += len(node.cases)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        if node is self.root:
            self.generic_visit(node)


def _complexity(node: ast.AST) -> int:
    visitor = _ComplexityVisitor(node)
    visitor.visit(node)
    return visitor.value


# endregion [01]


# region [02] Python AST extraction


@dataclass(frozen=True, slots=True)
class _ImportBinding:
    """One lexical import name that can qualify a call without guessing types."""

    root: str
    source_prefix: str
    target_prefix: str
    line: int


@dataclass(slots=True)
class _ScopeFacts:
    """Bindings relevant to conservative import-call qualification."""

    imports: dict[str, list[_ImportBinding]]
    other_bindings: set[str]
    global_names: set[str]
    nonlocal_names: set[str]


class _ScopeBindingCollector(ast.NodeVisitor):
    """Collect one Python lexical scope without entering child scopes."""

    def __init__(self) -> None:
        self.facts = _ScopeFacts({}, set(), set(), set())

    def _import(self, binding: _ImportBinding) -> None:
        self.facts.imports.setdefault(binding.root, []).append(binding)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.facts.other_bindings.add(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        self.facts.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.facts.nonlocal_names.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.asname or alias.name.split(".", 1)[0]
            source_prefix = alias.asname or alias.name
            target_prefix = alias.name.rsplit(".", 1)[-1]
            self._import(
                _ImportBinding(
                    root=root,
                    source_prefix=source_prefix,
                    target_prefix=target_prefix,
                    line=int(getattr(node, "lineno", 1)),
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        module_stem = module.rsplit(".", 1)[-1] if module else ""
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            target_prefix = f"{module_stem}.{alias.name}" if module_stem else alias.name
            self._import(
                _ImportBinding(
                    root=local_name,
                    source_prefix=local_name,
                    target_prefix=target_prefix,
                    line=int(getattr(node, "lineno", 1)),
                )
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.facts.other_bindings.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.facts.other_bindings.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.facts.other_bindings.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        del node

    def visit_ListComp(self, node: ast.ListComp) -> None:
        del node

    def visit_SetComp(self, node: ast.SetComp) -> None:
        del node

    def visit_DictComp(self, node: ast.DictComp) -> None:
        del node

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        del node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.facts.other_bindings.add(node.name)
        self.generic_visit(node)


def _argument_names(arguments: ast.arguments) -> set[str]:
    values = {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    }
    if arguments.vararg is not None:
        values.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        values.add(arguments.kwarg.arg)
    return values


def _scope_facts(
    node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> _ScopeFacts:
    collector = _ScopeBindingCollector()
    if isinstance(node, ast.Module):
        statements: Iterable[ast.AST] = node.body
    elif isinstance(node, ast.Lambda):
        collector.facts.other_bindings.update(_argument_names(node.args))
        statements = (node.body,)
    else:
        collector.facts.other_bindings.update(_argument_names(node.args))
        statements = node.body
    for statement in statements:
        collector.visit(statement)
    excluded = collector.facts.global_names | collector.facts.nonlocal_names
    collector.facts.other_bindings.difference_update(excluded)
    for name in excluded:
        collector.facts.imports.pop(name, None)
    return collector.facts


class _ImportCallHintVisitor(ast.NodeVisitor):
    """Qualify call hints only when lexical import evidence is unambiguous."""

    def __init__(self) -> None:
        self.hints: dict[int, str] = {}
        self.scopes: list[tuple[str, _ScopeFacts | None]] = []

    def _binding_hint(self, expression: str, node: ast.Call) -> str | None:
        root = expression.split(".", 1)[0]
        top_kind = self.scopes[-1][0] if self.scopes else None
        if top_kind in {"class", "comprehension"}:
            return None
        for kind, facts in reversed(self.scopes):
            if kind == "comprehension":
                return None
            if kind == "class" or facts is None:
                continue
            if root in facts.global_names or root in facts.nonlocal_names:
                return None
            if root in facts.other_bindings:
                return None
            bindings = facts.imports.get(root, ())
            matches: set[str] = set()
            for binding in bindings:
                if (
                    kind == "module"
                    and top_kind == "module"
                    and binding.line > int(getattr(node, "lineno", 1))
                ):
                    continue
                if expression == binding.source_prefix:
                    matches.add(binding.target_prefix)
                elif expression.startswith(binding.source_prefix + "."):
                    matches.add(
                        binding.target_prefix + expression[len(binding.source_prefix) :]
                    )
            if len(matches) == 1:
                return matches.pop()
            if bindings:
                return None
        return None

    def _visit_function_header(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self.visit(node.args.vararg.annotation)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self.visit(node.args.kwarg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_Module(self, node: ast.Module) -> None:
        self.scopes.append(("module", _scope_facts(node)))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._visit_function_header(node)
        self.scopes.append(("function", _scope_facts(node)))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        self.scopes.append(("function", _scope_facts(node)))
        self.visit(node.body)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for value in (*node.decorator_list, *node.bases):
            self.visit(value)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.scopes.append(("class", None))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _visit_comprehension(self, node: ast.AST) -> None:
        self.scopes.append(("comprehension", None))
        self.generic_visit(node)
        self.scopes.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    def visit_Call(self, node: ast.Call) -> None:
        expression = _expression_name(node.func)
        if expression:
            hint = self._binding_hint(expression, node)
            if hint is not None and hint != expression:
                self.hints[id(node)] = hint
        self.generic_visit(node)


def _import_bound_call_hints(tree: ast.Module) -> dict[int, str]:
    visitor = _ImportCallHintVisitor()
    visitor.visit(tree)
    return visitor.hints


class _PythonVisitor(ast.NodeVisitor):
    def __init__(
        self,
        source: CodeFileInput,
        source_map: SourceMap,
        config: CodeRouteConfig,
        call_target_hints: dict[int, str],
    ):
        self.source = source
        self.source_map = source_map
        self.config = config
        self.symbols: list[SymbolRecord] = []
        self.references: list[ReferenceRecord] = []
        self.dependencies: list[DependencyRecord] = []
        self.diagnostics: list[DiagnosticRecord] = []
        self.metrics: list[MetricRecord] = []
        self.parents: list[tuple[str, str]] = []
        self.module_name = Path(source.snapshot.path).stem
        self.call_target_hints = call_target_hints

    def _range(self, node: ast.AST) -> SourceRange:
        start_line = int(getattr(node, "lineno", 1))
        start_column = int(getattr(node, "col_offset", 0))
        end_line = int(getattr(node, "end_lineno", start_line) or start_line)
        end_column = int(getattr(node, "end_col_offset", start_column) or start_column)
        return self.source_map.source_range(
            start_line,
            start_column,
            end_line,
            end_column,
            utf8_columns=True,
        )

    def _qualified(self, name: str) -> str:
        values = [self.module_name, *(parent[0] for parent in self.parents), name]
        return ".".join(value for value in values if value)

    def _current_qualified(self) -> str | None:
        if not self.parents:
            return self.module_name
        return ".".join([self.module_name, *(parent[0] for parent in self.parents)])

    def _visibility(self, name: str) -> str:
        if name.startswith("__") and name.endswith("__"):
            return "special"
        return "private" if name.startswith("_") else "public"

    def _add_reference(
        self,
        kind: str,
        name: str | None,
        node: ast.AST,
        *,
        confirmed: bool,
        confidence: float,
        evidence: str,
        target_hint: str | None = None,
    ) -> None:
        if not name:
            return
        self.references.append(
            ReferenceRecord(
                kind=kind,
                name=name,
                source_range=self._range(node),
                source_qualified_name=self._current_qualified(),
                target_hint=target_hint,
                confirmed=confirmed,
                confidence=confidence,
                evidence=evidence,
            )
        )

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self.parents[-1][1] if self.parents else None
        kind = "method" if parent == "class" else "function"
        if parent in {"function", "method"}:
            kind = "nested_function"
        qualified = self._qualified(node.name)
        source_range = self._range(node)
        complexity = _complexity(node)
        decorators = tuple(
            value
            for value in (_expression_name(item) for item in node.decorator_list)
            if value
        )
        self.symbols.append(
            SymbolRecord(
                kind=kind,
                name=node.name,
                qualified_name=qualified,
                signature=_function_signature(node),
                source_range=source_range,
                parent_qualified_name=self._current_qualified(),
                visibility=self._visibility(node.name),
                docstring=ast.get_docstring(node, clean=False),
                complexity=complexity,
                metadata={
                    "async": isinstance(node, ast.AsyncFunctionDef),
                    "decorators": decorators,
                    "return_annotation": _annotation(node.returns),
                },
            )
        )
        self.metrics.append(
            MetricRecord(
                "cyclomatic_complexity", complexity, qualified, True, "python-ast"
            )
        )
        line_count = source_range.end_line - source_range.start_line + 1
        self.metrics.append(
            MetricRecord("function_lines", line_count, qualified, True, "python-ast")
        )
        if complexity >= self.config.complexity_warning:
            self.diagnostics.append(
                DiagnosticRecord(
                    source="neocortex-python-ast",
                    code="high_complexity",
                    severity=DiagnosticSeverity.WARNING,
                    message=f"{qualified} has cyclomatic complexity {complexity}",
                    source_range=source_range,
                    tool_name="python-ast",
                    tool_version=sys.version.split()[0],
                    confirmed=True,
                    metadata={
                        "value": complexity,
                        "threshold": self.config.complexity_warning,
                    },
                )
            )
        if line_count >= self.config.function_lines_warning:
            self.diagnostics.append(
                DiagnosticRecord(
                    source="neocortex-python-ast",
                    code="long_function",
                    severity=DiagnosticSeverity.WARNING,
                    message=f"{qualified} spans {line_count} lines",
                    source_range=source_range,
                    tool_name="python-ast",
                    tool_version=sys.version.split()[0],
                    confirmed=True,
                    metadata={
                        "value": line_count,
                        "threshold": self.config.function_lines_warning,
                    },
                )
            )
        for decorator in node.decorator_list:
            self._add_reference(
                "decorator",
                _expression_name(decorator),
                decorator,
                confirmed=True,
                confidence=1.0,
                evidence="python-ast:decorator",
            )
        self.parents.append((node.name, kind))
        self.generic_visit(node)
        self.parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualified(node.name)
        bases = tuple(
            value for value in (_expression_name(item) for item in node.bases) if value
        )
        decorators = tuple(
            value
            for value in (_expression_name(item) for item in node.decorator_list)
            if value
        )
        self.symbols.append(
            SymbolRecord(
                kind="class",
                name=node.name,
                qualified_name=qualified,
                signature=f"class {node.name}({', '.join(bases)})"
                if bases
                else f"class {node.name}",
                source_range=self._range(node),
                parent_qualified_name=self._current_qualified(),
                visibility=self._visibility(node.name),
                docstring=ast.get_docstring(node, clean=False),
                metadata={"bases": bases, "decorators": decorators},
            )
        )
        for base in node.bases:
            self._add_reference(
                "inherits",
                _expression_name(base),
                base,
                confirmed=True,
                confidence=1.0,
                evidence="python-ast:class-base",
            )
        for decorator in node.decorator_list:
            self._add_reference(
                "decorator",
                _expression_name(decorator),
                decorator,
                confirmed=True,
                confidence=1.0,
                evidence="python-ast:class-decorator",
            )
        self.parents.append((node.name, "class"))
        self.generic_visit(node)
        self.parents.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.dependencies.append(
                DependencyRecord(
                    alias.name.split(".", 1)[0],
                    "python_import",
                    source_range=self._range(node),
                    evidence="python-ast:import",
                )
            )
            self._add_reference(
                "import",
                alias.name,
                node,
                confirmed=True,
                confidence=1.0,
                evidence="python-ast:import",
                target_hint=alias.name,
            )
            self._add_reference(
                "import_binding",
                alias.asname or alias.name.split(".", 1)[0],
                node,
                confirmed=True,
                confidence=1.0,
                evidence="python-ast:import-binding",
                target_hint=alias.name.rsplit(".", 1)[-1],
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        dependency_names: tuple[str, ...]
        if node.level:
            if node.module:
                dependency_names = (module,)
            else:
                imported_modules = tuple(
                    f"{module}{alias.name}" for alias in node.names if alias.name != "*"
                )
                dependency_names = imported_modules or (module,)
        else:
            dependency_names = ((node.module or "").split(".", 1)[0],)
        self.dependencies.extend(
            DependencyRecord(
                name,
                "python_relative_import" if node.level else "python_import",
                source_range=self._range(node),
                evidence="python-ast:import-from",
            )
            for name in dependency_names
            if name
        )
        for alias in node.names:
            target = f"{module}.{alias.name}".strip(".") if module else alias.name
            self._add_reference(
                "import",
                target,
                node,
                confirmed=True,
                confidence=1.0,
                evidence="python-ast:import-from",
                target_hint=module or None,
            )
            if alias.name != "*":
                module_stem = (node.module or "").rsplit(".", 1)[-1]
                canonical_target = (
                    f"{module_stem}.{alias.name}" if module_stem else alias.name
                )
                self._add_reference(
                    "import_binding",
                    alias.asname or alias.name,
                    node,
                    confirmed=True,
                    confidence=1.0,
                    evidence="python-ast:import-binding",
                    target_hint=canonical_target,
                )

    def visit_Call(self, node: ast.Call) -> None:
        expression = _expression_name(node.func)
        target_hint = self.call_target_hints.get(id(node), expression)
        import_bound = target_hint != expression
        self._add_reference(
            "call",
            expression,
            node.func,
            confirmed=True,
            confidence=0.9,
            evidence=(
                "python-ast:call-expression-import-bound"
                if import_bound
                else "python-ast:call-expression"
            ),
            target_hint=target_hint,
        )
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self._add_reference(
            "raises",
            _expression_name(node.exc),
            node,
            confirmed=True,
            confidence=1.0,
            evidence="python-ast:raise",
        )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._add_reference(
            "catches",
            _expression_name(node.type),
            node,
            confirmed=True,
            confidence=1.0,
            evidence="python-ast:except-handler",
        )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assignment_symbols((node.target,), node.annotation, node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assignment_symbols(tuple(node.targets), None, node)
        self.generic_visit(node)

    def _assignment_symbols(
        self,
        targets: Iterable[ast.expr],
        annotation: ast.AST | None,
        node: ast.AST,
    ) -> None:
        parent_kind = self.parents[-1][1] if self.parents else "module"
        if parent_kind not in {"module", "class"}:
            return
        seen_names: set[str] = set()
        for target in targets:
            for name in _bound_target_names(target):
                if name in seen_names:
                    continue
                seen_names.add(name)
                self.symbols.append(
                    SymbolRecord(
                        kind="class_variable"
                        if parent_kind == "class"
                        else "module_variable",
                        name=name,
                        qualified_name=self._qualified(name),
                        signature=None
                        if annotation is None
                        else f"{name}: {_annotation(annotation)}",
                        source_range=self._range(node),
                        parent_qualified_name=self._current_qualified(),
                        visibility=self._visibility(name),
                        metadata={"annotation": _annotation(annotation)},
                    )
                )


def _bound_target_names(target: ast.expr) -> tuple[str, ...]:
    """Return only identifiers bound by an assignment target."""
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, ast.Starred):
        return _bound_target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return tuple(
            name for element in target.elts for name in _bound_target_names(element)
        )
    return ()


def _main_guard(tree: ast.Module) -> ast.If | None:
    for node in tree.body:
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        left = node.test.left
        comparators = node.test.comparators
        if (
            isinstance(left, ast.Name)
            and left.id == "__name__"
            and len(comparators) == 1
            and isinstance(comparators[0], ast.Constant)
            and comparators[0].value == "__main__"
        ):
            return node
    return None


def _annotated_chunks(
    chunks: tuple[CodeChunk, ...], symbols: tuple[SymbolRecord, ...]
) -> tuple[CodeChunk, ...]:
    annotated: list[CodeChunk] = []
    for chunk in chunks:
        containing = [
            symbol
            for symbol in symbols
            if symbol.source_range.start_line
            <= chunk.source_range.start_line
            <= symbol.source_range.end_line
        ]
        owner = min(
            containing,
            key=lambda item: item.source_range.end_line - item.source_range.start_line,
            default=None,
        )
        annotated.append(
            replace(
                chunk,
                symbol_qualified_name=(None if owner is None else owner.qualified_name),
                kind="python_source",
            )
        )
    return tuple(annotated)


# endregion [02]


# region [03] Public analyzer


class PythonAnalyzer:
    """Analyze valid Python with ``ast`` and degrade to searchable text on failure."""

    analyzer_id = "neocortex-python-ast"
    analyzer_version = f"5|python-{sys.version_info.major}.{sys.version_info.minor}"
    languages = frozenset({"python"})

    def analyze(self, source: CodeFileInput, config: CodeRouteConfig) -> CodeAnalysis:
        source_map = SourceMap.build(source.text)
        hints, manifest_dependencies, manifest_diagnostics = manifest_evidence(
            Path(source.snapshot.path), source.text
        )
        try:
            tree = ast.parse(
                source.text,
                filename=source.snapshot.path,
                type_comments=True,
            )
        except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
            diagnostics = [*manifest_diagnostics]
            if isinstance(exc, SyntaxError):
                line = int(exc.lineno or 1)
                column = max(0, int(exc.offset or 1) - 1)
                end_line = int(exc.end_lineno or line)
                end_column = max(column, int(exc.end_offset or column + 1) - 1)
                source_range = source_map.source_range(
                    line, column, end_line, end_column
                )
                message = exc.msg
                context = None if exc.text is None else exc.text.rstrip("\r\n")[:1024]
            else:
                source_range = None
                message = str(exc) or type(exc).__name__
                context = None
            diagnostics.append(
                DiagnosticRecord(
                    source="python-ast",
                    code="python_parse_error",
                    severity=DiagnosticSeverity.ERROR,
                    message=message[:4096],
                    source_range=source_range,
                    tool_name="ast",
                    tool_version=self.analyzer_version,
                    confirmed=True,
                    metadata={"context": context, "exception_type": type(exc).__name__},
                )
            )
            fingerprints = comparison_fingerprints(
                source.raw_bytes,
                source.text,
                "python",
                structure=None,
            )
            return CodeAnalysis(
                input=source,
                status=AnalysisStatus.PARTIAL,
                analyzer_id=self.analyzer_id,
                analyzer_version=self.analyzer_version,
                parser_kind="python-ast-failed",
                symbols=(),
                references=(),
                dependencies=manifest_dependencies,
                diagnostics=tuple(diagnostics),
                metrics=(
                    MetricRecord(
                        "line_count", len(source_map.lines), provenance="text"
                    ),
                ),
                chunks=searchable_chunks(source.text, config.chunk_chars),
                project_hints=hints,
                provenance={"parser": "ast", "syntax_confirmed": False},
                **fingerprints,
            )

        visitor = _PythonVisitor(
            source,
            source_map,
            config,
            _import_bound_call_hints(tree),
        )
        visitor.visit(tree)
        module_range = source_map.offset_range(0, len(source.text))
        module_symbol = SymbolRecord(
            kind="module",
            name=visitor.module_name,
            qualified_name=visitor.module_name,
            signature=None,
            source_range=module_range,
            visibility="public",
            docstring=ast.get_docstring(tree, clean=False),
            metadata={"parser": "python-ast"},
        )
        symbols = [module_symbol, *visitor.symbols]
        guard = _main_guard(tree)
        if guard is not None:
            symbols.append(
                SymbolRecord(
                    kind="entrypoint",
                    name="__main__",
                    qualified_name=f"{visitor.module_name}.__main__",
                    signature="if __name__ == '__main__'",
                    source_range=visitor._range(guard),
                    parent_qualified_name=visitor.module_name,
                    visibility="public",
                    metadata={"confirmed_by": "python-ast-main-guard"},
                )
            )
        structure = "\n".join(
            f"{item.kind}\0{item.qualified_name}\0{item.signature or ''}"
            for item in symbols
        )
        fingerprints = comparison_fingerprints(
            source.raw_bytes,
            source.text,
            "python",
            structure=structure,
        )
        all_dependencies = (*visitor.dependencies, *manifest_dependencies)
        all_diagnostics = (*visitor.diagnostics, *manifest_diagnostics)
        all_symbols = tuple(symbols)
        complexities = [
            item.complexity for item in all_symbols if item.complexity is not None
        ]
        metrics = [
            *visitor.metrics,
            MetricRecord("line_count", len(source_map.lines), provenance="python-ast"),
            MetricRecord("symbol_count", len(all_symbols), provenance="python-ast"),
            MetricRecord(
                "reference_count", len(visitor.references), provenance="python-ast"
            ),
            MetricRecord(
                "maximum_complexity",
                max(complexities, default=0),
                provenance="python-ast",
            ),
        ]
        chunks = _annotated_chunks(
            searchable_chunks(source.text, config.chunk_chars), all_symbols
        )
        return CodeAnalysis(
            input=source,
            status=AnalysisStatus.COMPLETE,
            analyzer_id=self.analyzer_id,
            analyzer_version=self.analyzer_version,
            parser_kind="python-ast",
            symbols=all_symbols,
            references=tuple(visitor.references),
            dependencies=all_dependencies,
            diagnostics=all_diagnostics,
            metrics=tuple(metrics),
            chunks=chunks,
            project_hints=hints,
            provenance={
                "parser": "ast",
                "parser_version": self.analyzer_version,
                "syntax_confirmed": True,
                "stdlib_modules_version": sys.version.split()[0],
            },
            **fingerprints,
        )


# endregion [03]


__all__ = ["PythonAnalyzer"]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_python")
