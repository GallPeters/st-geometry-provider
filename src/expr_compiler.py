# -*- coding: utf-8 -*-
"""
Conservative QGIS expression -> Oracle SQL compiler.

Only top-level AND-ed parts that can be translated exactly are pushed to Oracle.
QGIS core re-evaluates the full expression on every returned feature, so a part
that is NOT compiled only costs speed, never correctness.
"""

from qgis.core import (QgsExpression, QgsExpressionNodeBinaryOperator, QgsExpressionNodeUnaryOperator,
                       QgsExpressionNodeColumnRef, QgsExpressionNodeLiteral, QgsExpressionNodeInOperator)


def _enum(cls, group, name):
    g = getattr(cls, group, None)
    v = getattr(g, name, None) if g is not None else None
    if v is None:
        v = getattr(cls, name, None)
    return v


_B = QgsExpressionNodeBinaryOperator
_U = QgsExpressionNodeUnaryOperator
BIN = {n: _enum(_B, "BinaryOperator", n) for n in (
    "boOr", "boAnd", "boEQ", "boNE", "boLE", "boGE", "boLT", "boGT",
    "boLike", "boNotLike", "boILike", "boNotILike", "boIs", "boIsNot",
    "boPlus", "boMinus", "boMul")}
UN = {n: _enum(_U, "UnaryOperator", n) for n in ("uoNot", "uoMinus")}

_CMP = {"boEQ": "=", "boNE": "<>", "boLE": "<=", "boGE": ">=", "boLT": "<", "boGT": ">"}
_ARITH = {"boPlus": "+", "boMinus": "-", "boMul": "*"}
NUM = ("int", "int64", "double")


def _is(op, name):
    v = BIN.get(name)
    return v is not None and op == v


class _Compiler(object):
    def __init__(self, cols):
        self.cols = cols  # UPPER(name) -> column object with .sql and .kind
        self.binds = {}

    def bind(self, v):
        n = "q%d" % len(self.binds)
        self.binds[n] = v
        return ":" + n

    def node(self, n):
        if isinstance(n, QgsExpressionNodeColumnRef):
            c = self.cols.get(n.name().upper())
            if c is None:
                return None
            if c.kind in NUM:
                return (c.sql, "num")
            if c.kind == "string":
                return (c.sql, "str")
            return None

        if isinstance(n, QgsExpressionNodeLiteral):
            v = n.value()
            if v is None:
                return ("NULL", "null")
            if isinstance(v, bool):
                return None
            if isinstance(v, (int, float)):
                return (self.bind(v), "num")
            if isinstance(v, str):
                return (self.bind(v), "str")
            return None

        if isinstance(n, QgsExpressionNodeUnaryOperator):
            r = self.node(n.operand())
            if r is None:
                return None
            op = n.op()
            if UN.get("uoNot") is not None and op == UN["uoNot"] and r[1] == "bool":
                return ("(NOT %s)" % r[0], "bool")
            if UN.get("uoMinus") is not None and op == UN["uoMinus"] and r[1] == "num":
                return ("(-%s)" % r[0], "num")
            return None

        if isinstance(n, QgsExpressionNodeInOperator):
            left = self.node(n.node())
            if left is None or left[1] not in ("num", "str"):
                return None
            items = []
            for it in n.list().list():
                r = self.node(it)
                if r is None or r[1] != left[1]:
                    return None
                items.append(r[0])
            if not items or len(items) > 1000:
                return None
            return ("(%s %s (%s))" % (left[0], "NOT IN" if n.isNotIn() else "IN", ", ".join(items)), "bool")

        if isinstance(n, QgsExpressionNodeBinaryOperator):
            op = n.op()
            left = self.node(n.opLeft())
            if left is None:
                return None
            right = self.node(n.opRight())
            if right is None:
                return None
            if _is(op, "boAnd") or _is(op, "boOr"):
                if left[1] == "bool" and right[1] == "bool":
                    return ("(%s %s %s)" % (left[0], "AND" if _is(op, "boAnd") else "OR", right[0]), "bool")
                return None
            for name, sqlop in _CMP.items():
                if _is(op, name):
                    if left[1] == right[1] and left[1] in ("num", "str"):
                        return ("(%s %s %s)" % (left[0], sqlop, right[0]), "bool")
                    return None
            if _is(op, "boIs") or _is(op, "boIsNot"):
                if right[1] == "null" and left[1] in ("num", "str"):
                    return ("(%s IS %sNULL)" % (left[0], "" if _is(op, "boIs") else "NOT "), "bool")
                return None
            if _is(op, "boLike") or _is(op, "boNotLike"):
                if left[1] == "str" and right[1] == "str":
                    neg = "NOT " if _is(op, "boNotLike") else ""
                    return ("(%s %sLIKE %s ESCAPE '\\')" % (left[0], neg, right[0]), "bool")
                return None
            if _is(op, "boILike") or _is(op, "boNotILike"):
                if left[1] == "str" and right[1] == "str":
                    neg = "NOT " if _is(op, "boNotILike") else ""
                    return ("(UPPER(%s) %sLIKE UPPER(%s) ESCAPE '\\')" % (left[0], neg, right[0]), "bool")
                return None
            for name, sqlop in _ARITH.items():
                if _is(op, name):
                    if left[1] == "num" and right[1] == "num":
                        return ("(%s %s %s)" % (left[0], sqlop, right[0]), "num")
                    return None
            return None
        return None


def _split_and(n):
    if isinstance(n, QgsExpressionNodeBinaryOperator) and _is(n.op(), "boAnd"):
        return _split_and(n.opLeft()) + _split_and(n.opRight())
    return [n]


def compile_filter(text, cols):
    """Return (sql, binds, complete). complete=True means sql is exactly equivalent to the expression."""
    if not text:
        return ("", {}, True)
    e = QgsExpression(text)
    if e.hasParserError():
        return ("", {}, False)
    root = e.rootNode()
    if root is None:
        return ("", {}, False)
    comp = _Compiler(cols)
    parts = []
    complete = True
    for node in _split_and(root):
        snapshot = dict(comp.binds)
        try:
            r = comp.node(node)
        except Exception:
            r = None
        if r is None or r[1] != "bool":
            comp.binds = snapshot
            complete = False
            continue
        parts.append(r[0])
    return (" AND ".join(parts), comp.binds, complete)
