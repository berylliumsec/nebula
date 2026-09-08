"""Isolated, bounded Z3 analysis of recorded state. No network or action tools."""

from __future__ import annotations

import asyncio
import json
import sys
from .domain import Formula, Value


def solve(payload: dict) -> dict:
    import z3

    solver = z3.Solver()
    solver.set(timeout=payload.get("timeout_ms", 5000))
    fields = {}
    values = {}
    for name, raw in payload["fields"].items():
        value = Value.model_validate(raw)
        if value.kind == "conditional":
            raise ValueError(
                "Conditional values require an explicitly selected formula in V1"
            )
        values[name] = value
        constructor = {"integer": z3.Int, "boolean": z3.Bool}.get(value.type, z3.String)
        term = constructor(name)
        fields[name] = term
        if value.kind == "concrete":
            solver.assert_and_track(
                term == value.value,
                z3.Bool(payload.get("field_assertions", {}).get(name, "fact:" + name)),
            )
        if value.domain:
            solver.assert_and_track(
                z3.Or(*[term == x for x in value.domain]), z3.Bool("domain:" + name)
            )

    symbols = {}
    for name, value in values.items():
        reference = value.reference if value.kind == "alias" else None
        if value.kind == "symbolic":
            reference = symbols.setdefault(value.symbol, name)
        if reference is not None:
            if reference not in fields or values[reference].type != value.type:
                raise ValueError(
                    "Alias or shared symbol requires an existing field of the same type"
                )
            solver.assert_and_track(
                fields[name] == fields[reference], z3.Bool("alias:" + name)
            )

    node_count = 0

    def compile_formula(node: Formula, depth=0):
        nonlocal node_count
        node_count += 1
        if depth > 20 or node_count > 500:
            raise ValueError("Formula exceeds the supported complexity budget")
        if node.op == "field":
            if node.field not in fields:
                raise ValueError("Unknown field; select a declared state property")
            return fields[node.field]
        if node.op == "literal":
            if type(node.value) is bool:
                return z3.BoolVal(node.value)
            if type(node.value) is int:
                return z3.IntVal(node.value)
            return z3.StringVal(node.value)
        args = [compile_formula(a, depth + 1) for a in node.args]
        if node.op in {"and", "or", "not"}:
            if any(not z3.is_bool(a) for a in args):
                raise ValueError("Boolean operator requires Boolean operands")
            return {"and": z3.And, "or": z3.Or, "not": z3.Not}[node.op](*args)
        if node.op == "in":
            literals = [
                compile_formula(Formula(op="literal", value=v), depth + 1)
                for v in node.values
            ]
            if any(v.sort() != args[0].sort() for v in literals):
                raise ValueError("Membership values must have the field type")
            return z3.Or(*[args[0] == v for v in literals])
        a, b = args
        if a.sort() != b.sort():
            raise ValueError("Comparison operands must have the same type")
        if node.op in {"lt", "le", "gt", "ge"} and not (z3.is_int(a) and z3.is_int(b)):
            raise ValueError("Ordering is supported only for integers")
        return {
            "eq": lambda: a == b,
            "ne": lambda: a != b,
            "lt": lambda: a < b,
            "le": lambda: a <= b,
            "gt": lambda: a > b,
            "ge": lambda: a >= b,
        }[node.op]()

    for assertion in payload.get("assumptions", []):
        compiled = compile_formula(Formula.model_validate(assertion["formula"]))
        if not z3.is_bool(compiled):
            raise ValueError("Assumption must be Boolean")
        solver.assert_and_track(compiled, z3.Bool(assertion["id"]))
    condition = compile_formula(Formula.model_validate(payload["formula"]))
    if not z3.is_bool(condition):
        raise ValueError("Query must be Boolean")
    base = str(solver.check()).upper()
    result = {
        "base_result": base,
        "engine_version": z3.get_version_string(),
        "assignments": {},
        "assignment_types": {},
        "unsat_core": [],
    }
    if base != "SAT":
        result.update(
            result=base,
            unsat_core=[str(x) for x in solver.unsat_core()] if base == "UNSAT" else [],
        )
        return result
    solver.add(condition)
    status = str(solver.check()).upper()
    result["result"] = status
    if status == "SAT":
        model = solver.model()
        for name, term in fields.items():
            value = model.eval(term, model_completion=False)
            if z3.is_true(value) or z3.is_false(value):
                result["assignments"][name] = z3.is_true(value)
            elif z3.is_int_value(value):
                result["assignments"][name] = value.as_long()
            elif z3.is_string_value(value):
                result["assignments"][name] = value.as_string()
            if name in result["assignments"]:
                result["assignment_types"][name] = values[name].type
    elif status == "UNSAT":
        result["unsat_core"] = [str(x) for x in solver.unsat_core()]
    return result


async def isolated_solve(payload: dict) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nebula.v3.application_model.solver",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()),
            payload.get("timeout_ms", 5000) / 1000 + 1,
        )
        if process.returncode:
            raise RuntimeError(
                "Constraint worker failed; recorded state is still available"
            )
        return json.loads(stdout)
    except asyncio.TimeoutError:
        return {
            "result": "UNKNOWN",
            "base_result": "UNKNOWN",
            "error": "Constraint worker time budget exceeded",
        }
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


if __name__ == "__main__":
    try:
        result = solve(json.loads(sys.stdin.buffer.read(1_000_001)))
    except (ValueError, TypeError) as exc:
        result = {"status": "invalid", "error": str(exc)}
    print(json.dumps(result))
