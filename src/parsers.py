#!/usr/bin/env python3
"""Parser registry + resolution helpers for the mapping-driven C2M2 -> RO-Crate converter.

This module is the small, fixed vocabulary that the per-table JSON mappings under ``mappings/``
refer to by name. The generic engine in ``convert.py`` reads a mapping, looks each ``parser``
string up in :data:`PARSERS`, and applies it to a TSV column value. Association edges are resolved
with the ``match_key`` / ``resolve_value`` / ``wrap_value`` helpers.

Ontology logic lives in ``ontology.py`` (``resolve_term``, the CURIE/accession bases, ``SEX_MAP``,
the PropertyValue builders) and is imported here so a CV node's ``@id`` and every edge pointing at
it always resolve through the same code.
"""
import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# base-mapper helpers (shared serialization / CURIE / EDAM utilities).
from base import (  # noqa: E402
    _slug,
    _dump,
    _prune_none,
    _edam_encoding_format,
    CONTEXT,
    DEFAULT_NAAN,
    DEFAULT_LICENSE,
    DATAPACKAGE_NAME,
)

# ontology + PropertyValue helpers (single source of truth for CV value -> IRI resolution).
from ontology import (  # noqa: E402
    resolve_term,
    SEX_MAP,
    _pv,
    _pv_term,
    _ident_refs,
    _key,
)

__all__ = [
    "PARSERS", "interp", "interp_value", "meta_tokens", "term_id", "build_vars",
    "match_key", "resolve_value", "wrap_value", "passes_filter", "mint_id", "dedup_ident_refs",
    "resolve_term", "SEX_MAP", "_pv", "_pv_term", "_ident_refs", "_key", "_slug", "_dump",
    "_prune_none", "CONTEXT", "DEFAULT_NAAN", "DEFAULT_LICENSE", "DATAPACKAGE_NAME",
]


# --------------------------------------------------------------------------------------------
# Template interpolation: "{token}" and "{slug:token}" against a flat vars dict.
# --------------------------------------------------------------------------------------------
_TOKEN = re.compile(r"\{([a-zA-Z0-9_:.]+)\}")


def interp(template: str, vars: Dict[str, Any]) -> str:
    def repl(m: "re.Match") -> str:
        key = m.group(1)
        if key.startswith("slug:"):
            return _slug(str(vars.get(key[5:], "")))
        return str(vars.get(key, ""))
    return _TOKEN.sub(repl, template)


def interp_value(value: Any, vars: Dict[str, Any]) -> Any:
    """Recursively interpolate strings inside a literal (for ``constants`` / ``extra_type``)."""
    if isinstance(value, str):
        return interp(value, vars)
    if isinstance(value, list):
        return [interp_value(v, vars) for v in value]
    if isinstance(value, dict):
        return {k: interp_value(v, vars) for k, v in value.items()}
    return value


def meta_tokens(meta: Dict[str, Any]) -> Dict[str, Any]:
    """The subset of the resolved ``meta`` dict exposed to templates as ``{token}``s."""
    return {
        "prefix": meta.get("prefix", ""),
        "crate_guid": meta.get("crate_guid", ""),
        "conversion_guid": meta.get("conversion_guid", ""),
        "software_guid": meta.get("software_guid", ""),
        "dcc_label": meta.get("dcc_label", ""),
        "author": meta.get("author", ""),
        "publisher": meta.get("publisher", ""),
        "date": meta.get("date", ""),
    }


# --------------------------------------------------------------------------------------------
# @id minting.
# --------------------------------------------------------------------------------------------
def term_id(value: Optional[str], source_table: str, prefix: str) -> Optional[str]:
    """@id for a CV term: the resolved ontology IRI, else a minted crate-local id.

    Identical for the exploded CV node and any edge pointing at it, so the two always agree.
    """
    if not value or not value.strip():
        return None
    iri, _, _ = resolve_term(value, source_table)
    return iri if iri else f"{prefix}-cv/{source_table}/{_slug(value.strip())}"


_ARK_RE = re.compile(r"ark:[0-9]{5}/\S+")


def _embedded_ark(value: str) -> Optional[str]:
    """The bare ARK inside a persistent_id (e.g. an n2t.net resolver URL), else None."""
    match = _ARK_RE.search(value or "")
    return match.group() if match else None


def mint_id(idspec: Dict[str, Any], row: Dict[str, str], vars: Dict[str, Any],
            meta: Dict[str, Any]) -> Optional[str]:
    strategy = idspec["strategy"]
    if strategy == "persistent_or_mint":
        pid = (row.get(idspec.get("column", "persistent_id")) or "").strip()
        if pid:
            return pid
        return interp(idspec["mint"], vars) or None
    if strategy == "persistent_ark_or_mint":
        # Reuse the persistent_id as the @id only when it is (or embeds) a real ARK; otherwise the
        # @id is a freshly minted crate ARK and the raw persistent_id is preserved via a separate
        # 'identifier' field mapping. Keeps external PIDs (DOI, Cellosaurus, ...) out of the @id
        # slot, which the fairscape models require to match the ARK pattern.
        pid = (row.get(idspec.get("column", "persistent_id")) or "").strip()
        ark = _embedded_ark(pid)
        if ark:
            return ark
        return interp(idspec["mint"], vars) or None
    if strategy == "ontology_term":
        raw = (row.get(idspec["column"]) or "").strip()
        return term_id(raw, idspec["source_table"], meta["prefix"]) if raw else None
    if strategy == "mint":
        return interp(idspec["mint"], vars) or None
    if strategy == "column":
        return (row.get(idspec["column"]) or "").strip() or None
    raise ValueError(f"Unknown id strategy: {strategy!r}")


def build_vars(cfg: Dict[str, Any], row: Dict[str, str], meta: Dict[str, Any]) -> Dict[str, Any]:
    """Flat token dict for one row: columns + meta + (optional) CV tokens + computed clauses."""
    vars: Dict[str, Any] = {k: (v if v is not None else "") for k, v in row.items()}
    vars.update(meta_tokens(meta))

    cv = cfg.get("cv_source")
    if cv:
        raw = (row.get(cv["column"]) or "").strip()
        iri, curie, onto = resolve_term(raw, cv["source_table"])
        vars["iri"] = iri or ""
        vars["curie"] = curie or ""
        vars["onto"] = onto or ""
        vars["onto_suffix"] = f" ({onto})" if onto else ""

    for name, spec in cfg.get("computed", {}).items():
        if "first_nonempty" in spec:
            chosen = ""
            for col in spec["first_nonempty"]:
                candidate = (row.get(col) or "").strip()
                if candidate:
                    chosen = candidate
                    break
            vars[name] = chosen
        elif "template" in spec:
            when = spec.get("when")
            if when and not (row.get(when) or "").strip():
                vars[name] = ""
            else:
                vars[name] = interp(spec["template"], vars)
    return vars


# --------------------------------------------------------------------------------------------
# Column / field parsers.  Signature: fn(value, spec, ctx) -> Any | None
#   value : the raw column string (row.get(spec["source"])) or None
#   spec  : the mapping rule dict
#   ctx   : {"row": row, "vars": vars, "meta": meta}
# --------------------------------------------------------------------------------------------
def _p_scalar(value, spec, ctx):
    v = (value or "").strip()
    return v or None


def _p_scalar_default(value, spec, ctx):
    v = (value or "").strip()
    return v or spec.get("default")


def _p_first_nonempty(value, spec, ctx):
    row = ctx["row"]
    for col in [spec["source"]] + list(spec.get("fallbacks", [])):
        candidate = (row.get(col) or "").strip()
        if candidate:
            return candidate
    return None


def _p_int(value, spec, ctx):
    v = (value or "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _p_sex_map(value, spec, ctx):
    # SEX_MAP is intentionally empty until the CFDE sex CV -> schema.org gender map is confirmed;
    # unmapped values leave gender = None (the raw value is preserved in additionalProperty).
    return SEX_MAP.get((value or "").strip())


def _p_ontology_term(value, spec, ctx):
    iri, _, _ = resolve_term((value or "").strip() or None, spec.get("source_table"))
    return iri


def _p_identifier_value_term(value, spec, ctx):
    """Resolve a CV column to its ontology IRI and wrap it as an IdentifierValue ({"@id": ...})."""
    iri, _, _ = resolve_term((value or "").strip() or None, spec.get("source_table"))
    return {"@id": iri} if iri else None


def _p_identifier_value_term_list(value, spec, ctx):
    """Like identifier_value_term but returns a single-item list, so association edges can append
    onto it (the engine merges with element.get(target, []) + values)."""
    iri, _, _ = resolve_term((value or "").strip() or None, spec.get("source_table"))
    return [{"@id": iri}] if iri else None


def _p_edam_encoding(value, spec, ctx):
    return _edam_encoding_format(value)


def _p_template(value, spec, ctx):
    return interp(spec["template"], ctx["vars"])


def _p_context(value, spec, ctx):
    return ctx["vars"].get(spec["value"].lstrip("$"))


def _p_name_or_curie(value, spec, ctx):
    v = (value or "").strip()
    return v or (ctx["vars"].get("curie") or None)


def _p_curie_identifier(value, spec, ctx):
    curie = ctx["vars"].get("curie")
    onto = ctx["vars"].get("onto")
    if not curie:
        return None
    return [{"@type": "PropertyValue", "value": curie, "name": onto or "C2M2 CV"}]


def _p_description_or_template(value, spec, ctx):
    v = (value or "").strip()
    return v or interp(spec["template"], ctx["vars"])


def _p_property_value(value, spec, ctx):
    return _pv(spec["name"], value)


def _p_property_value_const(value, spec, ctx):
    return _pv(spec["name"], spec["const"])


def _p_property_value_term(value, spec, ctx):
    return _pv_term(spec["name"], value, spec.get("source_table"))


def _p_json_string_list(value, spec, ctx):
    """Parse a C2M2 CV ``synonyms`` column into a plain list of non-empty strings.

    The column is a JSON array literal (e.g. ``["insulin resistance", "NIDDM"]``); an empty array,
    empty cell, or non-JSON scalar is handled gracefully. Returns ``None`` when nothing remains so
    the target field is dropped rather than set to an empty list.
    """
    v = (value or "").strip()
    if not v:
        return None
    items: Optional[List[str]] = None
    if v.startswith("["):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            items = [s for x in parsed if (s := str(x).strip())]
    if items is None:
        items = [v]  # not a JSON array: treat the whole value as a single synonym
    return items or None


def _p_external_pid(value, spec, ctx):
    """A persistent_id worth keeping as a schema.org ``identifier``: an EXTERNAL PID only.

    Pairs with the ``persistent_ark_or_mint`` @id strategy. When the persistent_id is (or embeds)
    an ARK it already became the node @id, so surfacing it again as ``identifier`` is redundant and
    would diverge from the pre-existing crates -> return None. When it is a DOI / Cellosaurus / other
    non-ARK PID it was displaced from the @id by a minted ARK, so preserve it here.
    """
    v = (value or "").strip()
    if not v or _embedded_ark(v):
        return None
    return v


def _p_skip(value, spec, ctx):
    return None


PARSERS: Dict[str, Callable[[Any, Dict[str, Any], Dict[str, Any]], Any]] = {
    "scalar": _p_scalar,
    "scalar_default": _p_scalar_default,
    "first_nonempty": _p_first_nonempty,
    "int": _p_int,
    "sex_map": _p_sex_map,
    "ontology_term": _p_ontology_term,
    "identifier_value_term": _p_identifier_value_term,
    "identifier_value_term_list": _p_identifier_value_term_list,
    "edam_encoding": _p_edam_encoding,
    "template": _p_template,
    "context": _p_context,
    "name_or_curie": _p_name_or_curie,
    "curie_identifier": _p_curie_identifier,
    "description_or_template": _p_description_or_template,
    "property_value": _p_property_value,
    "property_value_const": _p_property_value_const,
    "property_value_term": _p_property_value_term,
    "json_string_list": _p_json_string_list,
    "external_pid": _p_external_pid,
    "skip": _p_skip,
}


# --------------------------------------------------------------------------------------------
# Association edge resolution (declarative ``associations`` rules).
# --------------------------------------------------------------------------------------------
def passes_filter(flt: Optional[Dict[str, Any]], row: Dict[str, str]) -> bool:
    if not flt:
        return True
    v = (row.get(flt["column"]) or "").strip()
    if "endswith" in flt:
        return v.endswith(flt["endswith"])
    if "equals" in flt:
        return v == flt["equals"]
    return True


def match_key(match: Dict[str, Any], row: Dict[str, str], meta: Dict[str, Any],
              guid_maps: Dict[str, Dict]) -> Any:
    """The key identifying *which node of this table* an association row belongs to."""
    resolver = match["resolver"]
    cols = match["columns"]
    if resolver == "entity_key":
        return _key(row.get(cols[0]), row.get(cols[1]))
    if resolver == "ontology_term":
        return term_id((row.get(cols[0]) or "").strip(), match["source_table"], meta["prefix"])
    raise ValueError(f"Unknown match resolver: {resolver!r}")


def resolve_value(value: Dict[str, Any], row: Dict[str, str], meta: Dict[str, Any],
                  guid_maps: Dict[str, Dict]) -> Optional[str]:
    """The ``@id`` (or raw string) to append onto the matched node."""
    resolver = value["resolver"]
    cols = value["columns"]
    if resolver == "ontology_term":
        return term_id((row.get(cols[0]) or "").strip(), value["source_table"], meta["prefix"])
    if resolver == "entity_guid":
        gm = guid_maps.get(value["entity"], {})
        return gm.get(_key(row.get(cols[0]), row.get(cols[1])))
    if resolver == "raw":
        return (row.get(cols[0]) or "").strip() or None
    raise ValueError(f"Unknown value resolver: {resolver!r}")


def wrap_value(wrap: str, vid: str, rule: Dict[str, Any], target: str) -> Dict[str, Any]:
    if wrap == "ident_ref":
        return {"@id": vid}
    if wrap == "ruled_out_pv":
        # A negative assertion: preserved without ever claiming the subject has the condition.
        return {"@type": "PropertyValue", "propertyID": "ruledOutCondition",
                "name": "ruledOutCondition", "value": vid, "valueReference": {"@id": vid}}
    if wrap == "property_value":
        return _pv(rule.get("pv_name", target), vid)
    raise ValueError(f"Unknown association wrap: {wrap!r}")


def dedup_ident_refs(refs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Order-preserving unique list of {"@id": ...} stubs."""
    seen = set()
    out = []
    for r in refs:
        gid = r.get("@id")
        if gid and gid not in seen:
            seen.add(gid)
            out.append(r)
    return out
