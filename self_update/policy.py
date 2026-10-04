"""Trusted edit boundary. The generator cannot modify this policy or tests."""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import re
import textwrap

TARGET = "intelligence/session_strategy_engine.py"
METHODS = {"_num", "_clamp"}
CALLS = {"float", "min", "max"}
ERRORS = {"TypeError", "ValueError", "OverflowError"}
ALLOWED = (
    ast.Return, ast.Assign, ast.AnnAssign, ast.If, ast.Try, ast.ExceptHandler,
    ast.Pass, ast.Expr, ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.Compare,
    ast.Is, ast.IsNot, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp,
    ast.USub, ast.UAdd, ast.Not, ast.IfExp, ast.Call, ast.Attribute, ast.Tuple,
)


class Rejected(ValueError):
    pass


def digest(source):
    return hashlib.sha256(source.encode()).hexdigest()


def load_proposal(raw):
    if not isinstance(raw, str) or len(raw.encode()) > 20000:
        raise Rejected("PROPOSAL_TOO_LARGE")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Rejected("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(Rejected("NONFINITE_JSON")))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise Rejected("INVALID_JSON") from exc


def methods(source):
    tree = ast.parse(source)
    owners = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SessionStrategyEngine"]
    if len(owners) != 1:
        raise Rejected("TARGET_CLASS_UNAVAILABLE")
    selected = {n.name: n for n in owners[0].body if isinstance(n, ast.FunctionDef) and n.name in METHODS}
    if set(selected) != METHODS:
        raise Rejected("TARGET_METHODS_UNAVAILABLE")
    return selected


def validate_body(function):
    statements = function.body
    nodes = [node for statement in statements for node in ast.walk(statement)]
    if len(nodes) > 180:
        raise Rejected("BODY_TOO_COMPLEX")
    parameters = {arg.arg for arg in function.args.args}
    local = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    reserved = CALLS | ERRORS | {"math"}
    if local & reserved:
        raise Rejected("RESERVED_NAME_REBOUND")
    doc = statements[0].value if (statements and isinstance(statements[0], ast.Expr)
                                  and isinstance(statements[0].value, ast.Constant)
                                  and isinstance(statements[0].value.value, str)) else None
    for node in nodes:
        if not isinstance(node, ALLOWED):
            raise Rejected("UNSUPPORTED_SYNTAX")
        if isinstance(node, ast.Name) and ("__" in node.id or node.id not in local | parameters | reserved):
            raise Rejected("UNSUPPORTED_NAME")
        if isinstance(node, ast.Assign) and any(not isinstance(t, ast.Name) for t in node.targets):
            raise Rejected("ONLY_LOCAL_ASSIGNMENTS")
        if isinstance(node, ast.AnnAssign) and not isinstance(node.target, ast.Name):
            raise Rejected("ONLY_LOCAL_ASSIGNMENTS")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if (any(t.id in parameters for t in targets) or not isinstance(value, ast.Call)
                or not isinstance(value.func, ast.Name) or value.func.id != "float"
                or len(value.args) != 1 or not isinstance(value.args[0], ast.Name) or value.args[0].id != "value"):
                raise Rejected("ONLY_LOCAL_FLOAT_CONVERSION")
        if isinstance(node, ast.Compare):
            if not (isinstance(node.left, ast.Name) and node.left.id == "value"
                    and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot))
                    and len(node.comparators) == 1 and isinstance(node.comparators[0], ast.Constant)
                    and node.comparators[0].value is None):
                raise Rejected("NO_VALUE_SPECIFIC_BRANCHES")
        if isinstance(node, ast.Attribute):
            if not (isinstance(node.value, ast.Name) and node.value.id == "math"
                    and node.attr == "isfinite" and isinstance(node.ctx, ast.Load)):
                raise Rejected("UNSUPPORTED_ATTRIBUTE")
        if isinstance(node, ast.Call):
            plain = isinstance(node.func, ast.Name) and node.func.id in CALLS
            finite = (isinstance(node.func, ast.Attribute) and node.func.attr == "isfinite"
                      and isinstance(node.func.value, ast.Name) and node.func.value.id == "math")
            if not (plain or finite) or node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                raise Rejected("UNSUPPORTED_CALL")
        if isinstance(node, ast.Expr) and node.value is not doc:
            raise Rejected("ONLY_DOCSTRING_EXPRESSION")
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, str) and (node is not doc or len(value) > 1000):
                raise Rejected("ONLY_DOCSTRING_TEXT")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(value) or value not in {0, 100}:
                    raise Rejected("NUMERIC_LITERAL_OUTSIDE_BOUND")
            elif value is not None and not isinstance(value, (str, bool)):
                raise Rejected("UNSUPPORTED_LITERAL")


def apply_proposal(source, proposal, base_sha):
    if (not isinstance(proposal, dict) or set(proposal) != {"schema_version", "base_sha", "rationale", "edits"}
        or type(proposal["schema_version"]) is not int or proposal["schema_version"] != 1
        or not re.fullmatch(r"[0-9a-f]{40}", base_sha) or proposal["base_sha"] != base_sha):
        raise Rejected("PROPOSAL_BASE_OR_SCHEMA_MISMATCH")
    rationale = proposal["rationale"]
    if not isinstance(rationale, str) or not 1 <= len(rationale) <= 1000:
        raise Rejected("INVALID_RATIONALE")
    edits = proposal["edits"]
    if not isinstance(edits, list) or len(edits) > 2:
        raise Rejected("INVALID_EDIT_LIST")
    if re.search(r"sk-(?:proj-|[A-Za-z0-9]{20})|gh[pousr]_[A-Za-z0-9]{20}", json.dumps(proposal)):
        raise Rejected("SECRET_LIKE_CONTENT")
    selected = methods(source)
    replacements, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"method", "body"}:
            raise Rejected("EDIT_SCHEMA_MISMATCH")
        name, body = edit["method"], edit["body"]
        if not isinstance(name, str) or name not in METHODS or name in seen:
            raise Rejected("METHOD_NOT_ALLOWED_OR_DUPLICATED")
        seen.add(name)
        if not isinstance(body, str) or not 1 <= len(body.encode()) <= 6000 or len(body.splitlines()) > 80:
            raise Rejected("BODY_SIZE_INVALID")
        body = textwrap.dedent(body).strip()
        fragment = ast.parse("def repair(value, default=0.0):\n" + textwrap.indent(body, "    "))
        validate_body(fragment.body[0])
        old = selected[name]
        replacements.append((old.body[0].lineno - 1, old.body[-1].end_lineno,
                             textwrap.indent(body, "        ") + "\n"))
    lines = source.splitlines(keepends=True)
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]
    candidate = "".join(lines)
    after = methods(candidate)
    for name in seen:
        validate_body(after[name])
    # Header, defaults, annotations and decorators, plus every other AST node,
    # must remain exactly the trusted base. Rendering also preserves raw text
    # outside the selected method bodies.
    before_tree, after_tree = ast.parse(source), ast.parse(candidate)
    for tree in (before_tree, after_tree):
        owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SessionStrategyEngine")
        for node in owner.body:
            if isinstance(node, ast.FunctionDef) and node.name in seen:
                node.body = [ast.Pass()]
    if ast.dump(before_tree, include_attributes=False) != ast.dump(after_tree, include_attributes=False):
        raise Rejected("CHANGE_OUTSIDE_ALLOWED_BODY")
    return candidate


def safe_functions(source):
    """Compile only the validated arithmetic helpers; never import the module."""
    import __future__
    selected = methods(source)
    namespace = {"math": math, "__builtins__": {name: getattr(__import__("builtins"), name)
                 for name in CALLS | ERRORS}}
    for original in selected.values():
        expected = ["value", "default"] if original.name == "_num" else ["value"]
        args = original.args
        defaults_ok = (len(args.defaults) == 1 and isinstance(args.defaults[0], ast.Constant)
                       and type(args.defaults[0].value) in {float, int} and args.defaults[0].value == 0)
        if ([a.arg for a in args.args] != expected or args.posonlyargs or args.kwonlyargs
            or args.vararg or args.kwarg or (not defaults_ok if original.name == "_num" else bool(args.defaults))):
            raise Rejected("UNSAFE_HELPER_SIGNATURE")
        validate_body(original)
        node = copy.deepcopy(original)
        node.decorator_list = []
        tree = ast.Module(body=[node], type_ignores=[])
        exec(compile(tree, "<bounded-numeric-helper>", "exec", flags=__future__.annotations.compiler_flag), namespace)
    return {name: namespace[name] for name in METHODS}
