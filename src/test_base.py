#!/usr/bin/env python3
"""Tests for the base mapper (``base.py``).

``convert.py`` subclasses ``base.C2M2ToROCrateMapper`` and reuses its generic per-table
Dataset/``evi:Schema`` reflection, FK normalization, the preservation layer, and the
``Computation``/``Software`` provenance pair, so this file guards that shared base by exercising
its standalone ``create_rocrate`` path directly.

Run with:  python3 -m pytest test_base.py
(or simply  python3 test_base.py  to run the checks without pytest).
"""
import json
import os
import pathlib
import tempfile

import base as c2m2

HERE = pathlib.Path(__file__).parent
FIXTURE = HERE / "test-data" / "c2m2-mini"


def _convert(tmp, **kwargs):
    out = pathlib.Path(tmp) / "crate"
    mapper = c2m2.C2M2ToROCrateMapper(FIXTURE)
    guid = mapper.create_rocrate(output_path=out, **kwargs)
    metadata = json.loads((out / "ro-crate-metadata.json").read_text())
    return out, metadata, guid


def test_validates_and_omits_empty_tables():
    from fairscape_models.rocrate import ROCrateV1_2

    with tempfile.TemporaryDirectory() as tmp:
        out, metadata, guid = _convert(tmp)
        assert guid.startswith("ark:")

        # validates end-to-end (fresh copy: model_validate mutates input)
        crate = ROCrateV1_2.model_validate(json.loads(json.dumps(metadata)))
        assert len(crate.getSchemas()) == 6  # subject (empty) omitted
        tables = [e for e in metadata["@graph"] if e.get("cfde:c2m2Table")]
        assert len(tables) == 6
        assert not any(e.get("cfde:c2m2Table") == "subject" for e in metadata["@graph"])

        root = metadata["@graph"][1]
        assert root["cfde:tablesTotal"] == 7
        assert root["cfde:tablesPopulated"] == 6
        assert "cfde" in metadata["@context"]


def test_foreign_keys_preserved_and_normalized():
    with tempfile.TemporaryDirectory() as tmp:
        _, metadata, _ = _convert(tmp)
        file_schema = next(e for e in metadata["@graph"] if e["@id"].endswith("-schema-file"))

        # string-form PK/FK fields are normalized to lists
        assert file_schema["cfde:primaryKey"] == ["id_namespace", "local_id"]
        fk_set = {
            (tuple(fk["fields"]), fk["reference"]["resource"])
            for fk in file_schema["cfde:foreignKeys"]
        }
        assert fk_set == {
            (("id_namespace",), "id_namespace"),
            (("project_id_namespace", "project_local_id"), "project"),
            (("file_format",), "file_format"),
        }

        # vocab table: string primaryKey normalized to a list, no foreign keys
        ff_schema = next(e for e in metadata["@graph"] if e["@id"].endswith("-schema-file_format"))
        assert ff_schema["cfde:primaryKey"] == ["id"]
        assert ff_schema["cfde:foreignKeys"] == []
        # ontology value URL inferred from the EDAM description
        assert ff_schema["properties"]["id"].get("valueURL") == "http://edamontology.org/"


def test_association_table_references_parents():
    with tempfile.TemporaryDirectory() as tmp:
        _, metadata, _ = _convert(tmp)
        assoc = next(
            e for e in metadata["@graph"]
            if e.get("cfde:c2m2Table") == "file_describes_biosample"
        )
        refs = {r["@id"] for r in assoc["cfde:referencesTable"]}
        assert any(r.endswith("-table-file") for r in refs)
        assert any(r.endswith("-table-biosample") for r in refs)


def test_file_table_exploded_with_source_links():
    with tempfile.TemporaryDirectory() as tmp:
        _, metadata, _ = _convert(tmp)
        files = [e for e in metadata["@graph"] if e.get("cfde:c2m2Entity") == "file"]
        assert len(files) == 2
        by_local = {e["cfde:localId"]: e for e in files}

        # row with access_url + sha256 -> contentUrl bridges to source, minted ARK @id
        bridged = by_local["sample.tsv"]
        assert bridged["contentUrl"] == "https://example.org/files/sample.tsv?download"
        assert bridged["sha256"].startswith("e3b0c442")
        assert bridged["encodingFormat"] == "http://edamontology.org/format_3475"
        assert bridged["@id"].endswith("-file/sample.tsv")

        # row with only persistent_id -> @id and contentUrl fall back to it
        pid = by_local["data.bin"]
        assert pid["@id"] == "ark:99999/data-bin"
        assert pid["contentUrl"] == "ark:99999/data-bin"

        # exploded files are NOT generatedBy the conversion (pre-existing external data)
        conversion = next(e for e in metadata["@graph"] if e.get("additionalType") == "Computation")
        for f in files:
            gb = f.get("generatedBy") or []
            gb = [gb] if isinstance(gb, dict) else gb
            assert conversion["@id"] not in {r.get("@id") for r in gb}


def test_preservation_layer_copied_into_crate():
    with tempfile.TemporaryDirectory() as tmp:
        out, metadata, _ = _convert(tmp)
        dp = next(e for e in metadata["@graph"] if e["@id"].endswith("-datapackage-json"))
        assert dp["additionalType"] == "File"
        assert (out / "C2M2_datapackage.json").exists()
        for table in ("file", "biosample", "file_describes_biosample", "file_format"):
            assert (out / f"{table}.tsv").exists()
        assert not (out / "subject.tsv").exists()  # empty table not copied


def test_no_explode_file_flag():
    with tempfile.TemporaryDirectory() as tmp:
        _, metadata, _ = _convert(tmp, explode_file=False)
        assert not any(e.get("cfde:c2m2Entity") == "file" for e in metadata["@graph"])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
