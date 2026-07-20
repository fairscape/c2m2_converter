#!/usr/bin/env python3
"""Ontology term resolution + schema.org PropertyValue helpers for the C2M2 -> RO-Crate converter.

Maps a C2M2 controlled-vocabulary value to a real, resolvable ontology IRI (+ CURIE + ontology name)
and builds the small set of PropertyValue / IdentifierValue stubs the parsers and mappings emit. This
module is pure data + functions -- no ``fairscape_models`` and no converter imports -- so a CV node's
``@id`` and every edge pointing at it always resolve through exactly the same code.
"""
import re
from typing import Any, Dict, List, Optional, Set

# --------------------------------------------------------------------------------------------
# Ontology term resolution: C2M2 CV value  ->  real, resolvable IRI + CURIE + ontology name.
# --------------------------------------------------------------------------------------------
OBO = "http://purl.obolibrary.org/obo/"
IDORG = "https://identifiers.org/"

# CURIE prefix *as it literally appears in the TSV value*  ->  (IRI base, ontology name).
_CURIE_BASES: Dict[str, tuple] = {
    "DOID": (OBO + "DOID_", "Disease Ontology"),
    "MONDO": (OBO + "MONDO_", "Mondo Disease Ontology"),
    "UBERON": (OBO + "UBERON_", "Uberon"),
    "HP": (OBO + "HP_", "Human Phenotype Ontology"),
    "OBI": (OBO + "OBI_", "Ontology for Biomedical Investigations"),
    "GO": (OBO + "GO_", "Gene Ontology"),
    "ILX": ("http://uri.interlex.org/base/ilx_", "InterLex"),
    "format": ("http://edamontology.org/format_", "EDAM"),
    "data": ("http://edamontology.org/data_", "EDAM"),
    # SNOMED CT's official URI scheme: http://snomed.info/id/<SCTID> (the SCTID follows the prefix
    # verbatim, no zero-padding). C2M2 CV tables write these as 'SNOMED:<SCTID>'.
    "SNOMED": ("http://snomed.info/id/", "SNOMED CT"),
    "SNOMEDCT": ("http://snomed.info/id/", "SNOMED CT"),
    # FBbi (Biological Imaging Methods Ontology) is an OBO ontology; the local id follows FBbi_.
    "FBBI": (OBO + "FBbi_", "Biological Imaging Methods Ontology"),
    # LOINC term pages live under the loinc.org namespace; the code (incl. MTHU part codes) is
    # appended verbatim, e.g. https://loinc.org/MTHU017278.
    "LOINC": ("https://loinc.org/", "LOINC"),
    # MedDRA has no free OBO PURL; identifiers.org resolves the numeric code.
    "MEDDRA": (IDORG + "meddra:", "MedDRA"),
}

# Tables whose ``id`` column is a *bare* accession (no CURIE prefix) -> (IRI base, name).
_BARE_TABLE_BASES: Dict[str, tuple] = {
    "gene": (IDORG + "ensembl:", "Ensembl"),
    "protein": ("http://purl.uniprot.org/uniprot/", "UniProtKB"),
    "substance": (IDORG + "pubchem.substance:", "PubChem Substance"),
    "compound": (IDORG + "pubchem.compound:", "PubChem Compound"),
}
_BARE_CURIE_PREFIX = {
    "gene": "ensembl",
    "protein": "uniprot",
    "substance": "pubchem.substance",
    "compound": "pubchem.compound",
}

# CFDE subject_sex CV term  ->  schema.org gender string. The CFDE sex enumeration
# (cfde_subject_sex:0/1/2) order is not asserted here to avoid mislabeling; unmapped terms
# leave Patient.gender = None and preserve the raw CV term in additionalProperty. Fill this in
# once the CFDE sex CV -> schema.org gender mapping is confirmed.
SEX_MAP: Dict[str, str] = {}


def resolve_term(value: Optional[str], source_table: Optional[str] = None):
    """Resolve a C2M2 CV value to (iri, curie, ontology_name).

    Returns (None, value, None) when the value cannot be bound to a known ontology (the caller
    then mints a crate-local id). ``source_table`` disambiguates *bare* accession tables
    (gene/protein/substance/compound) whose values carry no CURIE prefix.
    """
    if not value:
        return None, value, None
    value = value.strip()
    if not value:
        return None, value, None

    # NCBI Taxonomy is written with a literal 'NCBI:txid' prefix (regex-enforced in the schema).
    if value.startswith("NCBI:txid"):
        return OBO + "NCBITaxon_" + value[len("NCBI:txid"):], value, "NCBI Taxonomy"

    # Prefixed CURIE (DOID:, UBERON:, HP:, OBI:, format:, data:, ILX:, MONDO:, GO:, SNOMED:, ...).
    if ":" in value:
        prefix, local = value.split(":", 1)
        base = _CURIE_BASES.get(prefix)
        if base:
            return base[0] + local, value, base[1]

    # Bare accession from a known source table.
    if source_table in _BARE_TABLE_BASES:
        # compound values are either a PubChem CID (numeric) or a GlyTouCan accession ('G' + alnum).
        if source_table == "compound" and re.match(r"^G[A-Z0-9]{6,}$", value):
            return IDORG + "glytoucan:" + value, "glytoucan:" + value, "GlyTouCan"
        base = _BARE_TABLE_BASES[source_table]
        return base[0] + value, _BARE_CURIE_PREFIX[source_table] + ":" + value, base[1]

    return None, value, None


def _key(ns: Optional[str], lid: Optional[str]) -> tuple:
    return (ns or "").strip(), (lid or "").strip()


def _ident_refs(ids) -> List[Dict[str, str]]:
    """Unique list of {"@id": ...} IdentifierValue stubs, order-preserving."""
    seen: Set[str] = set()
    out: List[Dict[str, str]] = []
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append({"@id": i})
    return out


def _pv(name: str, raw: Any, iri: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """A schema.org PropertyValue for a raw C2M2 column value, or None when empty.

    When the value resolves to an ontology term, the literal value is kept in ``value`` and the
    resolved IRI is attached as ``valueReference`` (schema.org's mechanism for a value that is
    itself an entity) -- no invented predicate.
    """
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    pv = {"@type": "PropertyValue", "propertyID": name, "name": name, "value": raw}
    if iri:
        pv["valueReference"] = {"@id": iri}
    return pv


def _pv_term(name: str, raw: Any, source_table: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """PropertyValue for a CV column, resolving the value to an ontology IRI when possible."""
    if raw is None or not str(raw).strip():
        return None
    iri, _, _ = resolve_term(str(raw), source_table)
    return _pv(name, raw, iri)
