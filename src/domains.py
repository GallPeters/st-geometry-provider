# -*- coding: utf-8 -*-
"""
Esri coded-value domains and subtypes -> description fields.

Read once per layer from the geodatabase system table SDE.GDB_ITEMS (standard Esri XML):
  * the feature class item (DEFeatureClassInfo): field -> default domain (GPFieldInfoEx/DomainName),
    subtype field (SubtypeFieldName) and, per subtype (Subtype/SubtypeCode, SubtypeName), the domain
    of each field (SubtypeFieldInfo/FieldName + DomainName)
  * the domain items (GPCodedValueDomain2): CodedValue/Code -> CodedValue/Name

Per feature:  (subtype code, field) -> domain -> code -> description.
If a subtype defines no domain for a field, the field's default domain applies (as in ArcGIS).
"""

import xml.etree.ElementTree as ET

NUMERIC = ("int", "int64", "double")
_CACHE = {}


def _ln(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el:
        if _ln(c.tag) == name:
            return (c.text or "").strip()
    return ""


def _iter(el, name):
    return (e for e in el.iter() if _ln(e.tag) == name)


def _xml(text):
    if text is None:
        return None
    if hasattr(text, "read"):
        text = text.read()
    if isinstance(text, str):
        text = text.encode("utf-8")       # ElementTree refuses str with an encoding declaration
    return ET.fromstring(text)


def norm(v, numeric):
    """Comparable key for a code: numbers as int (if integral) or float, text as str."""
    if v is None:
        return None
    if numeric:
        if v.__class__ is int:
            return v
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v).strip()
        return int(f) if f.is_integer() else f
    return str(v)


def parse_feature_class(xml_text):
    root = _xml(xml_text)
    field_dom = {}
    for fi in _iter(root, "GPFieldInfoEx"):
        n, d = _text(fi, "Name"), _text(fi, "DomainName")
        if n and d:
            field_dom[n.upper()] = d
    sub_field = ""
    for e in _iter(root, "SubtypeFieldName"):
        sub_field = (e.text or "").strip().upper()
        break
    subtypes, sub_dom = {}, {}
    for st in _iter(root, "Subtype"):
        code = _text(st, "SubtypeCode")
        if code == "":
            continue
        subtypes[code] = _text(st, "SubtypeName")
        for sfi in _iter(st, "SubtypeFieldInfo"):
            fn, dn = _text(sfi, "FieldName"), _text(sfi, "DomainName")
            if fn and dn:
                sub_dom[(code, fn.upper())] = dn
    return field_dom, sub_field, subtypes, sub_dom


def parse_domain(xml_text):
    """{code text: description} of a coded-value domain (None for other domain types)."""
    root = _xml(xml_text)
    if "CodedValue" not in _ln(root.tag):
        return None
    return {_text(cv, "Code"): _text(cv, "Name") for cv in _iter(root, "CodedValue")}


class DescField(object):
    """One output field '<FIELD>_DESC' computed from a raw column (+ the subtype column)."""
    __slots__ = ("name", "src", "sub", "fn", "values", "valuemap")

    def __init__(self, name, src, sub, fn, values, valuemap):
        self.name, self.src, self.sub, self.fn = name, src, sub, fn
        self.values = values          # every description this field can show (for unique values)
        self.valuemap = valuemap      # [(description, code)] when unambiguous (same for all subtypes), else None


def build(cols, field_dom, sub_field, subtypes, sub_dom, domains):
    """cols: [(NAME, kind)] of the layer's real columns. Returns [DescField]."""
    names = [c[0].upper() for c in cols]
    kinds = {c[0].upper(): c[1] for c in cols}
    out = []
    sub_idx = names.index(sub_field) if sub_field in names else None
    sub_numeric = kinds.get(sub_field) in NUMERIC
    taken = set(names)

    def out_name(base):
        n = base + "_DESC"
        k = 2
        while n.upper() in taken:
            n = "%s_DESC%d" % (base, k)
            k += 1
        taken.add(n.upper())
        return n

    def typed_map(codes, numeric):
        return {norm(c, numeric): d for c, d in codes.items()}

    # subtype names
    if sub_idx is not None and subtypes:
        smap = {norm(c, sub_numeric): n for c, n in subtypes.items()}
        sm_get = smap.get

        def sfn(v, s, g=sm_get, nm=sub_numeric):
            return g(v if v.__class__ is int else norm(v, nm))

        vm = [(n, norm(c, sub_numeric)) for c, n in subtypes.items()]
        out.append(DescField(out_name(cols[sub_idx][0]), sub_idx, None, sfn, sorted(set(smap.values())), vm))

    for i, (name, kind) in enumerate(cols):
        F = name.upper()
        if i == sub_idx:
            continue
        numeric = kind in NUMERIC
        default_name = field_dom.get(F)
        default = typed_map(domains[default_name], numeric) if default_name in domains else {}
        per_sub = {}
        per_sub_names = set()
        for (code, f), dn in sub_dom.items():
            if f == F and dn in domains:
                per_sub[norm(code, sub_numeric)] = typed_map(domains[dn], numeric)
                per_sub_names.add(dn)
        if not default and not per_sub:
            continue
        values = set(default.values())
        for m in per_sub.values():
            values.update(m.values())
        varying = bool(per_sub) and (len(per_sub_names) > 1 or (default_name and per_sub_names != {default_name}))
        if per_sub and sub_idx is not None:
            def fn(v, s, ps=per_sub, d=default, nm=numeric, snm=sub_numeric):
                if v is None:
                    return None
                m = ps.get(s if s.__class__ is int else norm(s, snm), d)
                return m.get(v if v.__class__ is int else norm(v, nm))
            sub = sub_idx
        else:
            dget = default.get

            def fn(v, s, g=dget, nm=numeric):
                return None if v is None else g(v if v.__class__ is int else norm(v, nm))
            sub = None
        if varying:
            vm = None                                   # differs per subtype: only the _DESC field is exact
        else:
            src_map = default or next(iter(per_sub.values()))
            vm = [(d, c) for c, d in src_map.items()]
        out.append(DescField(out_name(name), i, sub, fn, sorted(values), vm))
    return out


def load(conn, owner, table, cols, cache_key=None, log=None):
    """Read and build the description fields of OWNER.TABLE. [] when the table has none."""
    if cache_key is not None and cache_key in _CACHE:
        return _CACHE[cache_key]
    pn = ("%s.%s" % (owner, table)).upper()
    fc_xml = None
    for sql in ("SELECT XMLSERIALIZE(CONTENT i.definition AS CLOB) FROM sde.gdb_items i WHERE UPPER(i.physicalname) = :pn",
                "SELECT i.definition FROM sde.gdb_items i WHERE UPPER(i.physicalname) = :pn"):
        try:
            r = conn.fetchone(sql, {"pn": pn})
            fc_xml = r[0] if r else None
            break
        except Exception as e:
            err = e
    else:
        raise RuntimeError("SDE.GDB_ITEMS not readable: %s" % err)
    if not fc_xml:
        result = []
    else:
        field_dom, sub_field, subtypes, sub_dom = parse_feature_class(fc_xml)
        wanted = sorted(set(field_dom.values()) | set(sub_dom.values()))
        domains = {}
        for k in range(0, len(wanted), 500):
            chunk = wanted[k:k + 500]
            binds = {"d%d" % j: n for j, n in enumerate(chunk)}
            inlist = ", ".join(":d%d" % j for j in range(len(chunk)))
            rows = None
            for sql in ("SELECT i.name, XMLSERIALIZE(CONTENT i.definition AS CLOB) FROM sde.gdb_items i WHERE i.name IN (%s)",
                        "SELECT i.name, i.definition FROM sde.gdb_items i WHERE i.name IN (%s)"):
                try:
                    rows = conn.fetchall(sql % inlist, binds)
                    break
                except Exception:
                    continue
            for name, xml_text in rows or []:
                try:
                    codes = parse_domain(xml_text)
                except Exception:
                    codes = None
                if codes is not None:
                    domains[name] = codes
        result = build(cols, field_dom, sub_field, subtypes, sub_dom, domains)
        if log:
            log("%s.%s: %d description field(s) from %d coded-value domain(s); subtype field %s (%d subtypes)"
                % (owner, table, len(result), len(domains), sub_field or "-", len(subtypes)))
    if cache_key is not None:
        _CACHE[cache_key] = result
    return result
