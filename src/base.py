#!/usr/bin/env python3
"""Package a CFDE C2M2 Frictionless datapackage as a FAIRSCAPE RO-Crate.

This is the base mapper: the generic, datapackage-driven structure that needs no per-table
configuration. ``convert.py`` subclasses it to add declarative row explosion. It is
self-contained — only ``fairscape_models`` (the pydantic models) plus the standard library.

C2M2 ships each DCC submission as a Frictionless Data Package: a ``C2M2_datapackage.json``
descriptor, one TSV per relational table, and a prebuilt ``C2M2_datapackage.sqlite`` index.
The authoritative copy normally lives in a DERIVA/ERMrest catalog; packaging the datapackage
as a self-describing RO-Crate lets the metadata survive without it. The TSVs, datapackage.json,
and SQLite are preserved verbatim, with schema.org/EVI provenance and file-level links back to
the source data overlaid on top.

For each populated table it emits one ``Dataset`` + ``evi:Schema`` (the relational contract
travels as ``cfde:primaryKey`` / ``cfde:foreignKeys``), explodes the ``file`` table into
source-linked file entities, preserves datapackage.json + SQLite as file entities, and adds a
``Computation`` / ``Software`` provenance pair.

Usage:
    python3 base.py <datapackage-dir> [--output-path DIR] [options]
"""
import argparse
import csv
import json
import pathlib
import re
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

# fairscape_models is a locally installed (editable) package -- import it directly.
from fairscape_models.rocrate import ROCrateMetadataElem, ROCrateV1_2
from fairscape_models.dataset import Dataset, _count_csv
from fairscape_models.schema import Schema
from fairscape_models.computation import Computation
from fairscape_models.software import Software

# Crate @context. `cfde:` is a placeholder term namespace — register a real CFDE
# term URI before publishing.
CONTEXT = {
    "@vocab": "https://schema.org/",
    "evi": "https://w3id.org/EVI#",
    "prov": "http://www.w3.org/ns/prov#",
    "cfde": "https://w3id.org/cfde/terms#",
}

DEFAULT_NAAN = "59853"
DEFAULT_LICENSE = "https://creativecommons.org/licenses/by/4.0/"

# Frictionless field types that map straight onto the Property.type enum
# ({integer, number, string, array, boolean, object}). Everything else
# (datetime/date/time/year/duration/geopoint/...) degrades to "string".
_PASSTHROUGH_TYPES = {"integer", "number", "string", "array", "boolean", "object"}

DATAPACKAGE_NAME = "C2M2_datapackage.json"
SQLITE_NAME = "C2M2_datapackage.sqlite"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-").lower()


def _prop_type(frictionless_type: Optional[str]) -> str:
    return frictionless_type if frictionless_type in _PASSTHROUGH_TYPES else "string"


def _as_list(value: Any) -> List[Any]:
    """C2M2 datapackage primaryKey / foreignKey fields are string-or-array; normalize."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _value_url_for_field(field: Dict[str, Any]) -> Optional[str]:
    """Infer an ontology base for a controlled-vocabulary column from its description."""
    upper = (field.get("description") or "").upper()
    if "EDAM" in upper:
        return "http://edamontology.org/"
    if any(tok in upper for tok in ("UBERON", "OBI ", "OBI.", "OBI CV", "OBO", "OBOLIBRARY")):
        return "http://purl.obolibrary.org/obo/"
    return None


def _edam_encoding_format(file_format: Optional[str]) -> Optional[str]:
    """Expand an EDAM compact id (e.g. ``format:3475``) to its ontology IRI."""
    if not file_format:
        return None
    value = file_format.strip()
    if value.startswith("format:"):
        return "http://edamontology.org/format_" + value.split(":", 1)[1]
    return None


def _prune_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _prune_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_prune_none(v) for v in value if v is not None]
    return value


def _dump(model) -> Dict[str, Any]:
    """Serialize a fairscape_models element with JSON-LD aliases, dropping None."""
    return _prune_none(model.model_dump(by_alias=True))


class C2M2ToROCrateMapper:
    """Map one C2M2 Frictionless datapackage directory to an RO-Crate."""

    def __init__(self, datapackage_dir: Union[str, pathlib.Path]):
        self.dir = pathlib.Path(datapackage_dir)
        if not self.dir.is_dir():
            raise FileNotFoundError(f"C2M2 datapackage path is not a directory: {self.dir}")

        self.datapackage_path = self.dir / DATAPACKAGE_NAME
        if not self.datapackage_path.exists():
            raise FileNotFoundError(
                f"No {DATAPACKAGE_NAME} found in {self.dir}; not a C2M2 datapackage."
            )

        with self.datapackage_path.open("r", encoding="utf-8") as f:
            self.datapackage = json.load(f)
        self.resources: List[Dict[str, Any]] = self.datapackage.get("resources", [])
        if not self.resources:
            raise ValueError(f"{self.datapackage_path} lists no resources.")

        self.sqlite_path = self.dir / SQLITE_NAME
        self._resource_by_name = {r["name"]: r for r in self.resources}

    # -- datapackage helpers -------------------------------------------------

    def _table_path(self, table: str) -> pathlib.Path:
        resource = self._resource_by_name.get(table, {})
        return self.dir / (resource.get("path") or f"{table}.tsv")

    def _read_tsv(self, table: str) -> List[Dict[str, str]]:
        path = self._table_path(table)
        if not path.exists():
            return []
        with path.open("r", newline="", encoding="utf-8", errors="replace") as f:
            return list(csv.DictReader(f, delimiter="\t"))

    def _populated_tables(self) -> List[str]:
        """Tables with a present TSV holding >= 1 data row (drive off real content)."""
        populated = []
        for resource in self.resources:
            table = resource["name"]
            path = self._table_path(table)
            if not path.exists():
                continue
            rows, _ = _count_csv(path, "\t")
            if rows >= 1:
                populated.append(table)
        return populated

    @staticmethod
    def _is_association(resource: Dict[str, Any]) -> bool:
        """Association (link) table: composite PK whose columns are all FK columns."""
        schema = resource.get("schema", {})
        pk = _as_list(schema.get("primaryKey"))
        if len(pk) < 2:
            return False
        fk_columns = set()
        for fk in schema.get("foreignKeys", []):
            fk_columns.update(_as_list(fk.get("fields")))
        return set(pk).issubset(fk_columns)

    # -- element builders ----------------------------------------------------

    def _schema_element(self, resource: Dict[str, Any], meta: Dict[str, Any]) -> Schema:
        table = resource["name"]
        schema = resource["schema"]
        fields = schema.get("fields", [])

        properties: Dict[str, Dict[str, Any]] = {}
        required: List[str] = []
        for index, field in enumerate(fields):
            prop = {
                "description": field.get("description") or field["name"],
                "index": index,
                "type": _prop_type(field.get("type")),
            }
            value_url = _value_url_for_field(field)
            if value_url:
                prop["value-url"] = value_url
            properties[field["name"]] = prop
            if (field.get("constraints") or {}).get("required"):
                required.append(field["name"])

        primary_key = _as_list(schema.get("primaryKey"))
        for column in primary_key:
            if column not in required:
                required.append(column)

        foreign_keys = [
            {
                "fields": _as_list(fk["fields"]),
                "reference": {
                    "resource": fk["reference"]["resource"],
                    "fields": _as_list(fk["reference"]["fields"]),
                },
            }
            for fk in schema.get("foreignKeys", [])
        ]

        element = {
            "@id": f"{meta['prefix']}-schema-{table}",
            "@type": "evi:Schema",
            "conformsTo": {"@id": "https://json-schema.org/draft/2020-12/schema"},
            "name": f"C2M2 {table} table schema",
            "description": (
                f"Column schema for the C2M2 '{table}' table, derived from "
                f"{DATAPACKAGE_NAME}. Primary key and foreign keys are preserved in the "
                f"cfde:* extension fields (fairscape's evi:Schema has no native "
                f"relational key fields)."
            ),
            "keywords": ["C2M2", "schema", table],
            "type": "object",
            "separator": "\t",
            "header": True,
            "additionalProperties": False,
            "required": required,
            "properties": properties,
            "cfde:primaryKey": primary_key,
            "cfde:foreignKeys": foreign_keys,
        }
        return Schema.model_validate(element)

    def _table_dataset(self, resource: Dict[str, Any], meta: Dict[str, Any]) -> Dataset:
        table = resource["name"]
        path = self._table_path(table)
        rows, cols = _count_csv(path, "\t")
        size = path.stat().st_size

        description = resource.get("description") or (
            f"The C2M2 '{table}' table for the {meta['dcc_label']} instance, preserved "
            f"verbatim as a TSV ({rows} data rows, {cols} columns). Its column schema "
            f"and foreign keys are described by the linked evi:Schema."
        )

        element = {
            "@id": f"{meta['prefix']}-table-{table}",
            "@type": ["prov:Entity", "https://w3id.org/EVI#Dataset"],
            "additionalType": "Dataset",
            "name": f"C2M2 {table} table ({meta['dcc_label']})",
            "description": description,
            "author": meta["author"],
            "datePublished": meta["date"],
            "keywords": ["C2M2", "table", table],
            "format": "text/tab-separated-values",
            "contentUrl": f"file:///{table}.tsv",
            "evi:Schema": {"@id": f"{meta['prefix']}-schema-{table}"},
            "rowCount": rows,
            "columnCount": cols,
            "contentSize": str(size),
            "generatedBy": {"@id": meta["conversion_guid"]},
            "isPartOf": [{"@id": meta["crate_guid"]}],
            "cfde:c2m2Table": table,
        }

        if self._is_association(resource):
            referenced = []
            seen = set()
            for fk in resource["schema"].get("foreignKeys", []):
                ref_table = fk["reference"]["resource"]
                if ref_table in meta["populated"] and ref_table not in seen:
                    seen.add(ref_table)
                    referenced.append({"@id": f"{meta['prefix']}-table-{ref_table}"})
            if referenced:
                element["cfde:referencesTable"] = referenced

        return Dataset.model_validate(element)

    def _file_entities(self, meta: Dict[str, Any]) -> List[Dataset]:
        """Explode the (small) file table into source-linked file entities."""
        entities = []
        file_table_guid = f"{meta['prefix']}-table-file"
        for row in self._read_tsv("file"):
            local_id = (row.get("local_id") or row.get("filename") or "").strip()
            if not local_id:
                continue
            persistent_id = (row.get("persistent_id") or "").strip()
            access_url = (row.get("access_url") or "").strip()
            filename = (row.get("filename") or local_id).strip()

            guid = persistent_id if persistent_id else f"{meta['prefix']}-file/{local_id}"
            content_url = access_url or persistent_id or None

            element = {
                "@id": guid,
                "@type": ["prov:Entity", "https://w3id.org/EVI#Dataset"],
                "additionalType": "File",
                "name": filename,
                "description": (
                    f"Source data file '{filename}', an individual record exploded from "
                    f"the C2M2 file table (id_namespace={row.get('id_namespace')}, "
                    f"local_id={local_id}). Its contentUrl links back to the source data. "
                    f"This is the source-data bridge from C2M2 metadata to actual data."
                ),
                "author": meta["author"],
                "datePublished": meta["date"],
                "keywords": ["C2M2", "file", "source-data"],
                "format": (row.get("mime_type") or "").strip() or "application/octet-stream",
                "isPartOf": [{"@id": meta["crate_guid"]}],
                "cfde:c2m2Entity": "file",
                "cfde:idNamespace": row.get("id_namespace"),
                "cfde:localId": local_id,
                "cfde:describedByC2M2Table": {"@id": file_table_guid},
            }
            if content_url:
                element["contentUrl"] = content_url
            if (row.get("sha256") or "").strip():
                element["sha256"] = row["sha256"].strip()
            if (row.get("md5") or "").strip():
                element["md5"] = row["md5"].strip()
            if (row.get("size_in_bytes") or "").strip():
                element["contentSize"] = str(row["size_in_bytes"]).strip()
            encoding_format = _edam_encoding_format(row.get("file_format"))
            if encoding_format:
                element["encodingFormat"] = encoding_format

            # NOT generatedBy the conversion: the file pre-exists; the conversion
            # produced this metadata node, not the file content.
            entities.append(Dataset.model_validate(element))
        return entities

    def _preserved_file(self, path: pathlib.Path, guid: str, role: str,
                        fmt: str, meta: Dict[str, Any], description: str) -> Dataset:
        element = {
            "@id": guid,
            "@type": ["prov:Entity", "https://w3id.org/EVI#Dataset"],
            "additionalType": "File",
            "name": path.name,
            "description": description,
            "author": meta["author"],
            "datePublished": meta["date"],
            "keywords": ["C2M2", "preservation", role],
            "format": fmt,
            "contentUrl": f"file:///{path.name}",
            "contentSize": str(path.stat().st_size),
            "isPartOf": [{"@id": meta["crate_guid"]}],
            "cfde:role": role,
        }
        return Dataset.model_validate(element)

    def _software(self, meta: Dict[str, Any]) -> Software:
        element = {
            "@id": meta["software_guid"],
            "@type": ["prov:Entity", "https://w3id.org/EVI#Software"],
            "additionalType": "Software",
            "name": "CFDE C2M2 to RO-Crate Converter",
            "description": (
                "Reference converter that reads a C2M2 Frictionless datapackage "
                "(datapackage.json + TSVs + SQLite) and emits a FAIRSCAPE RO-Crate "
                "following the C2M2-to-ROCrate design spec."
            ),
            "author": "CFDE / FAIRSCAPE",
            "version": "0.1.0",
            "format": "text/x-python",
            "url": "https://github.com/fairscape",
            "isPartOf": [{"@id": meta["crate_guid"]}],
        }
        return Software.model_validate(element)

    def _computation(self, meta: Dict[str, Any], generated_guids: List[str],
                     used_dataset_guids: List[str]) -> Computation:
        element = {
            "@id": meta["conversion_guid"],
            "@type": ["prov:Activity", "https://w3id.org/EVI#Computation"],
            "additionalType": "Computation",
            "name": f"C2M2 to RO-Crate conversion ({meta['dcc_label']})",
            "description": (
                f"Conversion of the {meta['dcc_label']} C2M2 Frictionless datapackage "
                f"into this RO-Crate: one Dataset + evi:Schema per populated table, the "
                f"file table additionally exploded into file-level source-data entities, "
                f"and the datapackage.json + SQLite preserved verbatim."
            ),
            "runBy": meta["author"],
            "dateCreated": meta["date"],
            "command": f"python3 convert.py {self.dir} --output-path {meta['output_path']}",
            "usedSoftware": [{"@id": meta["software_guid"]}],
            "usedDataset": [{"@id": g} for g in used_dataset_guids],
            "generated": [{"@id": g} for g in generated_guids],
            "isPartOf": [{"@id": meta["crate_guid"]}],
        }
        return Computation.model_validate(element)

    # -- orchestration -------------------------------------------------------

    def _resolve_metadata(self, author, publisher, date_published, naan):
        dcc_rows = self._read_tsv("dcc")
        dcc_row = dcc_rows[0] if dcc_rows else {}
        namespaces = [r.get("id") for r in self._read_tsv("id_namespace") if r.get("id")]

        # Top-level project: DCC-level metadata that seeds the crate name/description/identifier
        # when the caller supplies no explicit override. Take the first row (per the dcc pattern);
        # in the CFDE samples the first project row is the root of the project_in_project hierarchy.
        project_rows = self._read_tsv("project")
        project_row = project_rows[0] if project_rows else {}

        def _clean(value):
            return (value or "").strip() or None

        slug = _slug(dcc_row.get("dcc_abbreviation") or self.dir.name)
        prefix = f"ark:{naan}/{slug}-c2m2"

        dcc_label = dcc_row.get("dcc_name") or self.dir.name
        resolved_author = author or f"CFDE / {dcc_label} DCC"
        resolved_date = date_published or datetime.now(timezone.utc).date().isoformat()

        is_part_of = [{"@id": "https://cfde.cloud"}]
        for ns in namespaces:
            is_part_of.append({"@id": f"ark:{naan}/{ns}"})

        return {
            "crate_guid": prefix,
            "prefix": prefix,
            "conversion_guid": f"{prefix}-conversion",
            "software_guid": f"{prefix}-converter-software",
            "dcc_label": dcc_label,
            "author": resolved_author,
            "publisher": publisher or "NIH Common Fund Data Ecosystem",
            "date": resolved_date,
            "isPartOf": is_part_of,
            "project_name": _clean(project_row.get("name")),
            "project_description": _clean(project_row.get("description")),
            "project_identifier": _clean(project_row.get("persistent_id")),
        }

    def create_rocrate(
        self,
        output_path: Optional[Union[str, pathlib.Path]] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        author: Optional[str] = None,
        publisher: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        license: Optional[str] = None,
        version: str = "1.0",
        date_published: Optional[str] = None,
        identifier: Optional[str] = None,
        naan: str = DEFAULT_NAAN,
        explode_file: bool = True,
        include_sqlite: bool = True,
    ) -> str:
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

        # Build every element, in @graph order: per table (schema, dataset), exploded
        # file entities, preserved datapackage + sqlite, then Computation + Software.
        elements = []
        generated_guids: List[str] = []
        for resource in self.resources:
            table = resource["name"]
            if table not in meta["populated"]:
                continue
            schema = self._schema_element(resource, meta)
            dataset = self._table_dataset(resource, meta)
            elements.extend([schema, dataset])
            generated_guids.extend([schema.guid, dataset.guid])

        if explode_file and "file" in meta["populated"]:
            for entity in self._file_entities(meta):
                elements.append(entity)
                generated_guids.append(entity.guid)

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

        software = self._software(meta)
        computation = self._computation(meta, generated_guids, used_dataset_guids)
        elements.extend([computation, software])

        # Root data entity + descriptor.
        root = self._root_element(
            meta, name, description, keywords, license, version, identifier,
            populated, total_tables, [e.guid for e in elements],
        )
        descriptor = root.generateFileElem()

        graph = [_dump(descriptor), _dump(root)] + [_dump(e) for e in elements]
        crate = {"@context": CONTEXT, "@graph": graph}

        # Validate the whole graph (raises on any invalid element).
        ROCrateV1_2.model_validate(json.loads(json.dumps(crate)))

        metadata_path = output_path / "ro-crate-metadata.json"
        with metadata_path.open("w", encoding="utf-8") as f:
            json.dump(crate, f, indent=2)

        # Copy the datapackage files into the crate so the file:/// contentUrls resolve.
        # Skip when the crate directory is the source directory.
        self._copy_preservation_files(output_path, populated, preserve_sqlite)

        return meta["crate_guid"]

    def _root_element(self, meta, name, description, keywords, license, version,
                      identifier, populated, total_tables, element_guids):
        crate_name = name or f"{meta['dcc_label']} — C2M2 Metadata Instance (RO-Crate)"
        crate_description = description or (
            f"RO-Crate packaging of the CFDE C2M2 Frictionless datapackage for the "
            f"{meta['dcc_label']} dataset. It preserves the C2M2 relational tables (TSV), "
            f"the datapackage schema, and the SQLite relational index verbatim, and "
            f"overlays schema.org/EVI provenance plus file-level links back to the source "
            f"data. The relational contract (primary/foreign keys) travels as cfde:* "
            f"extension fields so the C2M2 relational model can be reconstituted."
        )
        crate_keywords = keywords or [
            "CFDE", "C2M2", meta["dcc_label"], "metadata", "RO-Crate",
            "Frictionless", "provenance",
        ]
        empty_note = (
            f"{total_tables - len(populated)} of the {total_tables} C2M2 tables are empty "
            f"for this instance and are omitted from @graph; their full schema "
            f"(fields/PK/FK) is preserved in {DATAPACKAGE_NAME}. Populated tables: "
            f"{', '.join(sorted(populated))}."
        )

        element = {
            "@id": meta["crate_guid"],
            "@type": ["Dataset", "https://w3id.org/EVI#ROCrate"],
            "name": crate_name,
            "description": crate_description,
            "keywords": crate_keywords,
            "version": version,
            "author": meta["author"],
            "publisher": meta["publisher"],
            "datePublished": meta["date"],
            "license": license or DEFAULT_LICENSE,
            "isPartOf": meta["isPartOf"],
            "hasPart": [{"@id": g} for g in element_guids],
            "cfde:c2m2Level": "1",
            "cfde:tablesTotal": total_tables,
            "cfde:tablesPopulated": len(populated),
            "cfde:emptyTablesNote": empty_note,
        }
        if identifier:
            element["identifier"] = identifier
        return ROCrateMetadataElem.model_validate(element)

    def _copy_preservation_files(self, output_path, populated, preserve_sqlite):
        to_copy = [self._table_path(t) for t in populated]
        to_copy.append(self.datapackage_path)
        if preserve_sqlite:
            to_copy.append(self.sqlite_path)
        for source in to_copy:
            destination = output_path / source.name
            if source.resolve() == destination.resolve():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, destination)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert a CFDE C2M2 Frictionless datapackage into a FAIRSCAPE RO-Crate.",
    )
    parser.add_argument("datapackage_dir",
                        help="Directory holding C2M2_datapackage.json + per-table TSVs (+ .sqlite).")
    parser.add_argument("--output-path",
                        help="Output crate directory. Default: write ro-crate-metadata.json in place, "
                             "in the datapackage dir. Pass a path to emit a self-contained copy elsewhere.")
    parser.add_argument("--name", help="Override the RO-Crate name.")
    parser.add_argument("--description", help="Override the RO-Crate description.")
    parser.add_argument("--author", help='Author (default: "CFDE / <DCC> DCC").')
    parser.add_argument("--publisher", help="Publisher (default: NIH Common Fund Data Ecosystem).")
    parser.add_argument("--keywords", nargs="*", help="Keywords (space-separated).")
    parser.add_argument("--license", help="License URL.")
    parser.add_argument("--version", default="1.0", help="Version string (default: 1.0).")
    parser.add_argument("--date-published", help="Publication date (ISO 8601; default: today).")
    parser.add_argument("--identifier", help="DOI/persistent id (e.g. after Dataverse deposit).")
    parser.add_argument("--naan", default=DEFAULT_NAAN, help=f"ARK NAAN (default: {DEFAULT_NAAN}).")
    parser.add_argument("--no-explode-file", action="store_true",
                        help="Do not explode the file table into source-linked entities.")
    parser.add_argument("--exclude-sqlite", action="store_true",
                        help="Do not preserve C2M2_datapackage.sqlite in the crate.")
    args = parser.parse_args(argv)

    mapper = C2M2ToROCrateMapper(args.datapackage_dir)
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
        explode_file=not args.no_explode_file,
        include_sqlite=not args.exclude_sqlite,
    )
    print(crate_guid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
