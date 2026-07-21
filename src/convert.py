#!/usr/bin/env python3
"""Mapping-driven C2M2 -> FAIRSCAPE RO-Crate converter.

This is the main entry point. It reads row-explosion rules from editable per-table JSON files
under ``mappings/``:

    mappings/manifest.json   ordered list of mappings to load (+ @graph order / scope)
    mappings/root.json       how the crate root/identity fields are filled
    mappings/<table>.json    how one C2M2 table's TSV rows become typed @graph nodes

A single generic engine interprets those mappings, so adding a C2M2 table is a new JSON file, not
new Python. It subclasses ``C2M2ToROCrateMapper`` (``base.py``) to reuse the datapackage plumbing,
the generic per-table ``Dataset``/``evi:Schema`` reflection, the preservation layer, and the
``Computation``/``Software`` provenance pair. Only row explosion and root construction are
mapping-driven.

Usage:
    python3 convert.py <datapackage-dir> [--output-path DIR] [--mappings-dir DIR] \
        [--name ...] [--description ...] [--author ...] [--publisher ...] \
        [--keywords ...] [--license ...] [--version ...] [--date-published ...] \
        [--identifier ...] [--naan ...] [--exclude-sqlite]
"""
import argparse
import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from base import C2M2ToROCrateMapper, DATAPACKAGE_NAME  # noqa: E402
from fairscape_models.rocrate import ROCrateMetadataElem, ROCrateV1_2  # noqa: E402
from fairscape_models.dataset import Dataset  # noqa: E402
from fairscape_models.sample import Sample  # noqa: E402
from fairscape_models.patient import Patient  # noqa: E402
from fairscape_models.medical_condition import MedicalCondition  # noqa: E402
from fairscape_models.biochem_entity import BioChemEntity  # noqa: E402
from fairscape_models.defined_term import DefinedTerm  # noqa: E402

from parsers import (  # noqa: E402
    PARSERS,
    CONTEXT,
    DEFAULT_NAAN,
    DEFAULT_LICENSE,
    _dump,
    _key,
    interp,
    interp_value,
    meta_tokens,
    build_vars,
    mint_id,
    match_key,
    resolve_value,
    wrap_value,
    passes_filter,
    dedup_ident_refs,
)

ENTITY_MODELS = {
    "Patient": Patient,
    "Sample": Sample,
    "MedicalCondition": MedicalCondition,
    "BioChemEntity": BioChemEntity,
    "DefinedTerm": DefinedTerm,
    "Dataset": Dataset,
}

_ADDITIONAL_PROPERTY = "additionalProperty"


class MappingC2M2Converter(C2M2ToROCrateMapper):
    """Generic table/schema/preservation from the base mapper + declarative row explosion & root."""

    def __init__(self, datapackage_dir, mappings_dir: Optional[str] = None):
        super().__init__(datapackage_dir)
        self.mappings_dir = pathlib.Path(mappings_dir) if mappings_dir else pathlib.Path(_HERE) / "mappings"

    # -- mapping loading -----------------------------------------------------

    def _load_mappings(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        manifest_path = self.mappings_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"No manifest.json in {self.mappings_dir}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        root_cfg = json.loads((self.mappings_dir / manifest["root"]).read_text(encoding="utf-8"))

        entity_cfgs: List[Dict[str, Any]] = []
        for filename in manifest.get("entities", []):
            cfg = json.loads((self.mappings_dir / filename).read_text(encoding="utf-8"))
            etype = cfg.get("entity_type")
            if etype not in ENTITY_MODELS:
                raise ValueError(
                    f"{filename}: entity_type {etype!r} is not one of {sorted(ENTITY_MODELS)}; "
                    f"an unknown @type would silently degrade to GenericMetadataElem."
                )
            entity_cfgs.append(cfg)
        entity_cfgs.sort(key=lambda c: c.get("order", 0))
        return root_cfg, entity_cfgs

    # -- guid maps + association index ---------------------------------------

    def _build_guid_maps(self, meta, entity_cfgs) -> Dict[str, Dict]:
        """(id_namespace, local_id) -> node @id, for every configured entity-strategy table."""
        guid_maps: Dict[str, Dict] = {}
        for cfg in entity_cfgs:
            table = cfg["source_table"]
            if table not in meta["populated"]:
                continue
            if cfg["id"]["strategy"] not in ("persistent_or_mint", "persistent_ark_or_mint", "mint", "column"):
                continue
            gm: Dict[Any, str] = {}
            for row in self._read_tsv(table):
                vars = build_vars(cfg, row, meta)
                gid = mint_id(cfg["id"], row, vars, meta)
                if gid:
                    gm[_key(row.get("id_namespace"), row.get("local_id"))] = gid
            guid_maps[table] = gm
        return guid_maps

    def _build_assoc_index(self, meta, guid_maps, entity_cfgs) -> Dict[str, List[Tuple[str, Dict]]]:
        """source_table -> ordered list of (target, {match_key -> [wrapped values]}).

        Built once from every mapping's ``associations`` rules; empty/missing assoc tables no-op.
        Kept in declaration order so additionalProperty edges (race, ruled-out) attach predictably.
        """
        index: Dict[str, List[Tuple[str, Dict]]] = {}
        for cfg in entity_cfgs:
            entries: List[Tuple[str, Dict]] = []
            for rule in cfg.get("associations", []):
                targets = rule["targets"]
                per_target = {t: {} for t in targets}
                for row in self._read_tsv(rule["assoc_table"]):
                    if not passes_filter(rule.get("filter"), row):
                        continue
                    mkey = match_key(rule["match"], row, meta, guid_maps)
                    vid = resolve_value(rule["value"], row, meta, guid_maps)
                    if mkey is None or vid is None:
                        continue
                    for target in targets:
                        wrapped = wrap_value(rule["wrap"], vid, rule, target)
                        if wrapped is None:
                            continue
                        per_target[target].setdefault(mkey, []).append(wrapped)
                for target in targets:
                    entries.append((target, per_target[target]))
            index[cfg["source_table"]] = entries
        return index

    # -- one row -> one node dict --------------------------------------------

    def _build_node(self, cfg, row, meta, assoc_index) -> Optional[Dict[str, Any]]:
        vars = build_vars(cfg, row, meta)
        guid = mint_id(cfg["id"], row, vars, meta)
        if not guid:
            return None
        vars["guid"] = guid
        ctx = {"row": row, "vars": vars, "meta": meta}

        element: Dict[str, Any] = {"@id": guid}

        for prop, value in cfg.get("constants", {}).items():
            element[prop] = interp_value(value, vars)

        for field in cfg.get("fields", []):
            raw = row.get(field["source"]) if field.get("source") else None
            result = PARSERS[field["parser"]](raw, field, ctx)
            if result is not None:
                element[field["target"]] = result

        additional: List[Dict[str, Any]] = []
        for ap in cfg.get("additional_property", []):
            raw = row.get(ap["source"]) if ap.get("source") else None
            pv = PARSERS[ap["parser"]](raw, ap, ctx)
            if pv is not None:
                additional.append(pv)

        # Attach declared association edges. A node is looked up by BOTH its entity key (tuple) and
        # its @id (string); the two key spaces are disjoint, so a mapping never has to restate which
        # key its own associations use.
        entity_key = None
        if "id_namespace" in row and "local_id" in row:
            entity_key = _key(row.get("id_namespace"), row.get("local_id"))
        for target, keymap in assoc_index.get(cfg["source_table"], []):
            values: List[Dict[str, Any]] = []
            if entity_key is not None:
                values += keymap.get(entity_key, [])
            values += keymap.get(guid, [])
            if not values:
                continue
            if target == _ADDITIONAL_PROPERTY:
                additional.extend(values)
            else:
                element[target] = dedup_ident_refs(element.get(target, []) + values)

        if additional:
            element["additionalProperty"] = additional
        if cfg.get("extra_type"):
            element.update(interp_value(cfg["extra_type"], vars))
        return element

    # -- root ----------------------------------------------------------------

    def _build_root(self, root_cfg, meta, populated, total_tables, element_guids,
                    overrides) -> ROCrateMetadataElem:
        vars = dict(meta_tokens(meta))
        vars.update({
            "populated_list": ", ".join(sorted(populated)),
            "total_tables": str(total_tables),
            "tables_populated": str(len(populated)),
            "empty_count": str(total_tables - len(populated)),
        })
        computed = {
            "cfde_and_namespaces": meta["isPartOf"],
            "all_element_guids": [{"@id": g} for g in element_guids],
            "tables_total": total_tables,
            "tables_populated": len(populated),
            "today_or_arg": meta["date"],
            "version_arg": overrides.get("version") or "1.0",
            "license_arg": overrides.get("license") or DEFAULT_LICENSE,
            "identifier_arg": overrides.get("identifier"),
        }
        element: Dict[str, Any] = {}
        for prop, spec in root_cfg["fields"].items():
            value = self._resolve_root_field(spec, vars, computed, overrides, meta)
            if value is not None:
                element[prop] = value
        return ROCrateMetadataElem.model_validate(element)

    @staticmethod
    def _resolve_root_field(spec, vars, computed, overrides, meta):
        # Source branches are tried in precedence order (override > meta > const > template >
        # computed), falling through on a None result so a field can declare a fallback chain --
        # e.g. name = override("$name_arg") -> meta("project_name") -> template(dcc default).
        override = spec.get("override")
        if override:
            key = override[1:].replace("_arg", "") if override.startswith("$") else override
            if overrides.get(key) is not None:
                return overrides[key]
        if "meta" in spec:
            value = meta.get(spec["meta"])
            if value is not None:
                return value
        if "const" in spec:
            value = interp_value(spec["const"], vars)
            if value is not None:
                return value
        if "template" in spec:
            value = interp(spec["template"], vars)
            if value is not None:
                return value
        if "computed" in spec:
            value = computed.get(spec["computed"])
            return value if value is not None else spec.get("default")
        return None

    # -- orchestration -------------------------------------------------------

    def create_rocrate(self, output_path=None, name=None, description=None, author=None,
                       publisher=None, keywords=None, license=None, version="1.0",
                       date_published=None, identifier=None, naan=None,
                       include_sqlite=True) -> str:
        root_cfg, entity_cfgs = self._load_mappings()
        if naan is None:
            naan = str(root_cfg.get("identity", {}).get("naan", DEFAULT_NAAN))
        meta = self._resolve_metadata(author, publisher, date_published, naan)

        if output_path is None:
            # Default: write the crate in place, alongside the datapackage's own files. The
            # preservation copy below no-ops here (source == destination). Pass --output-path to
            # emit a self-contained copy into a separate directory instead.
            output_path = self.dir
        output_path = pathlib.Path(output_path)
        output_path.mkdir(parents=True, exist_ok=True)
        meta["output_path"] = str(output_path)

        populated = self._populated_tables()
        meta["populated"] = set(populated)

        elements: List[Any] = []
        generated_guids: List[str] = []

        # (A) generic per-table Dataset + evi:Schema (datapackage-driven, from the base mapper).
        for resource in self.resources:
            table = resource["name"]
            if table not in meta["populated"]:
                continue
            schema = self._schema_element(resource, meta)
            dataset = self._table_dataset(resource, meta)
            elements.extend([schema, dataset])
            generated_guids.extend([schema.guid, dataset.guid])

        # (B) guid maps + (C) association index.
        guid_maps = self._build_guid_maps(meta, entity_cfgs)
        assoc_index = self._build_assoc_index(meta, guid_maps, entity_cfgs)

        # (D) declarative row explosion.
        seen = {e.guid for e in elements}
        for cfg in entity_cfgs:
            table = cfg["source_table"]
            if table not in meta["populated"]:
                continue
            model = ENTITY_MODELS[cfg["entity_type"]]
            for row in self._read_tsv(table):
                node = self._build_node(cfg, row, meta, assoc_index)
                if node is None or node["@id"] in seen:
                    continue
                seen.add(node["@id"])
                obj = model.model_validate(node)
                elements.append(obj)
                if cfg.get("generated"):
                    generated_guids.append(obj.guid)

        # (E) preservation layer (from the base mapper).
        total_tables = len(self.resources)
        used_dataset_guids: List[str] = []
        datapackage_element = self._preserved_file(
            self.datapackage_path, f"{meta['prefix']}-datapackage-json",
            "frictionless-datapackage-descriptor", "application/json", meta,
            description=(
                f"The Frictionless Data Package descriptor (JSON Table Schema) defining "
                f"all {total_tables} C2M2 table fields, primary keys, and foreign-key "
                f"relationships for this instance — the authoritative relational contract, "
                f"preserved verbatim so the full model survives even for tables omitted "
                f"from @graph."
            ),
        )
        elements.append(datapackage_element)
        used_dataset_guids.append(datapackage_element.guid)

        preserve_sqlite = include_sqlite and self.sqlite_path.exists()
        if preserve_sqlite:
            sqlite_element = self._preserved_file(
                self.sqlite_path, f"{meta['prefix']}-datapackage-sqlite",
                "relational-index", "application/vnd.sqlite3", meta,
                description=(
                    "A prebuilt SQLite database containing every C2M2 table for this "
                    "instance with primary/foreign keys enforced — a ready-to-query "
                    "relational index. Preserved verbatim so a future system can rebuild "
                    "the CFDE index from the crate alone, without the live DERIVA/ERMrest "
                    "catalog."
                ),
            )
            elements.append(sqlite_element)
            used_dataset_guids.append(sqlite_element.guid)

        # (provenance)
        software = self._software(meta)
        computation = self._computation(meta, generated_guids, used_dataset_guids)
        elements.extend([computation, software])

        # (F) declarative root + descriptor.
        overrides = {
            "name": name, "description": description, "keywords": keywords,
            "version": version, "license": license, "identifier": identifier,
        }
        root = self._build_root(
            root_cfg, meta, populated, total_tables, [e.guid for e in elements], overrides,
        )
        descriptor = root.generateFileElem()

        graph = [_dump(descriptor), _dump(root)] + [_dump(e) for e in elements]
        crate = {"@context": CONTEXT, "@graph": graph}

        ROCrateV1_2.model_validate(json.loads(json.dumps(crate)))

        metadata_path = output_path / "ro-crate-metadata.json"
        with metadata_path.open("w", encoding="utf-8") as f:
            json.dump(crate, f, indent=2)

        self._copy_preservation_files(output_path, populated, preserve_sqlite)
        return meta["crate_guid"]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert a CFDE C2M2 Frictionless datapackage into a FAIRSCAPE RO-Crate "
                    "using editable per-table JSON mappings.",
    )
    parser.add_argument("datapackage_dir",
                        help="Directory holding C2M2_datapackage.json + per-table TSVs (+ .sqlite).")
    parser.add_argument("--output-path",
                        help="Output crate directory. Default: write ro-crate-metadata.json in place, "
                             "in the datapackage dir. Pass a path to emit a self-contained copy elsewhere.")
    parser.add_argument("--mappings-dir", help="Directory of mapping JSON files (default: ./mappings).")
    parser.add_argument("--name", help="Override the RO-Crate name.")
    parser.add_argument("--description", help="Override the RO-Crate description.")
    parser.add_argument("--author", help='Author (default: "CFDE / <DCC> DCC").')
    parser.add_argument("--publisher", help="Publisher (default: NIH Common Fund Data Ecosystem).")
    parser.add_argument("--keywords", nargs="*", help="Keywords (space-separated).")
    parser.add_argument("--license", help="License URL.")
    parser.add_argument("--version", default="1.0", help="Version string (default: 1.0).")
    parser.add_argument("--date-published", help="Publication date (ISO 8601; default: today).")
    parser.add_argument("--identifier", help="DOI/persistent id (e.g. after Dataverse deposit).")
    parser.add_argument("--naan", help="ARK NAAN (default: from mappings/root.json identity.naan).")
    parser.add_argument("--exclude-sqlite", action="store_true",
                        help="Do not preserve C2M2_datapackage.sqlite in the crate.")
    args = parser.parse_args(argv)

    mapper = MappingC2M2Converter(args.datapackage_dir, mappings_dir=args.mappings_dir)
    crate_guid = mapper.create_rocrate(
        output_path=args.output_path,
        name=args.name,
        description=args.description,
        author=args.author,
        publisher=args.publisher,
        keywords=args.keywords,
        license=args.license,
        version=args.version,
        date_published=args.date_published,
        identifier=args.identifier,
        naan=args.naan,
        include_sqlite=not args.exclude_sqlite,
    )
    print(crate_guid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
