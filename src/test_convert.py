#!/usr/bin/env python3
"""End-to-end test suite for the mapping-driven C2M2 -> RO-Crate converter (``convert.py``).
``test_base.py`` separately guards the base layer this subclasses.

Covers the AI-READI conversion (node-type counts, the disease ``DefinedTerm`` shape, subject and
biosample edges, and the root seeded from the top-level project row), the disease ``usedBy``
back-reference and ``synonym`` JSON-array parse (voice), file-node parity with the base mapper plus
empty-table safety (c2m2-mini), and an association_type :1/:0 inversion regression on a synthetic
fixture.
"""
import collections
import json
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from convert import MappingC2M2Converter  # noqa: E402
import base  # noqa: E402  (base mapper, for file-node parity)

EXAMPLES = os.path.join(HERE, "examples")
AIREADI = os.path.join(EXAMPLES, "ai-readi")
VOICE = os.path.join(EXAMPLES, "voice")
MINI = os.path.join(HERE, "test-data", "c2m2-mini")

DOID_9352 = "http://purl.obolibrary.org/obo/DOID_9352"
IGNORE_FIELDS = {"datePublished", "dateCreated", "dateModified", "fairscapeVersion", "command"}


def norm_type(t):
    if isinstance(t, list):
        t = t[-1] if t else ""
    t = str(t)
    if t.startswith("http://") or t.startswith("https://"):
        t = t.split("#")[-1].split("/")[-1]
    if ":" in t:
        t = t.split(":")[-1]
    return t


def convert(src, out_dir):
    mapper = MappingC2M2Converter(src)
    mapper.create_rocrate(output_path=str(out_dir))
    with open(os.path.join(out_dir, "ro-crate-metadata.json")) as f:
        return json.load(f)["@graph"]


def by_id(graph):
    return {n["@id"]: n for n in graph}


def cv_terms(graph, table=None):
    """DefinedTerm nodes, optionally filtered to a single C2M2 CV table via the c2m2CvTable marker."""
    out = []
    for n in graph:
        if norm_type(n.get("@type")) != "DefinedTerm":
            continue
        if table is None:
            out.append(n)
            continue
        if any(ap.get("value") == table for ap in n.get("additionalProperty", [])
               if ap.get("name") == "c2m2CvTable"):
            out.append(n)
    return out


def root_node(graph):
    return next(n for n in graph if norm_type(n.get("@type")) == "ROCrate")


def normalized(node):
    d = {k: v for k, v in node.items() if k not in IGNORE_FIELDS}
    ap = d.get("additionalProperty")
    if isinstance(ap, list):
        d["additionalProperty"] = sorted(ap, key=lambda p: json.dumps(p, sort_keys=True))
    return json.dumps(d, sort_keys=True)


# --------------------------------------------------------------------------------------------
# AI-READI conversion.
# --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def aireadi_graph(tmp_path_factory):
    return convert(AIREADI, tmp_path_factory.mktemp("aireadi"))


def test_aireadi_type_counts(aireadi_graph):
    # The 14 CV term tables map to DefinedTerm; AI-READI populates two of them, giving 1 disease +
    # 8 assay_type DefinedTerm nodes.
    hist = collections.Counter(norm_type(n.get("@type")) for n in aireadi_graph)
    assert dict(hist) == {
        "Sample": 9603, "Patient": 1067, "Dataset": 23, "Schema": 12,
        "DefinedTerm": 9, "ROCrate": 1, "CreativeWork": 1,
        "Computation": 1, "Software": 1,
    }
    assert len(aireadi_graph) == 10718


def test_aireadi_no_generic_metadata_elem(aireadi_graph):
    # A node whose @type falls outside ROCrateV1_2.type_map silently degrades to GenericMetadataElem.
    assert "GenericMetadataElem" not in {norm_type(n.get("@type")) for n in aireadi_graph}


def test_aireadi_disease_is_defined_term(aireadi_graph):
    # DOID_9352 is a DefinedTerm carrying the CV shape: name, termCode (CURIE), identifier (IRI),
    # description, and a c2m2CvTable marker.
    node = by_id(aireadi_graph)[DOID_9352]
    assert norm_type(node["@type"]) == "DefinedTerm"
    assert node["identifier"] == DOID_9352
    assert node["termCode"] == "DOID:9352"
    assert node["name"]
    assert node["description"]
    assert any(ap.get("value") == "disease" for ap in node.get("additionalProperty", [])
               if ap.get("name") == "c2m2CvTable")


def test_aireadi_root_seeded_from_project(aireadi_graph):
    # With no CLI override, the root name/description default to the top-level project row
    # (project.tsv first row) rather than the generic dcc template. AI-READI's project row carries a
    # name + description but an empty persistent_id, so identifier stays unset.
    import csv
    with open(os.path.join(AIREADI, "project.tsv"), newline="") as f:
        project = next(csv.DictReader(f, delimiter="\t"))
    root = root_node(aireadi_graph)
    assert root["name"] == project["name"].strip()
    assert root["description"] == project["description"].strip()
    assert root["name"] != f"{project['name']} — C2M2 Metadata Instance (RO-Crate)"
    assert not root.get("identifier")  # empty project persistent_id => no identifier


# --------------------------------------------------------------------------------------------
# Edge spot-checks.
# --------------------------------------------------------------------------------------------
def test_aireadi_subject_disease_edges(aireadi_graph):
    patient = by_id(aireadi_graph)["ark:59853/aireadi-c2m2-subject/subject_1"]
    assert [r["@id"] for r in patient["diagnosis"]] == [DOID_9352]
    assert [r["@id"] for r in patient["healthCondition"]] == [DOID_9352]
    assert patient.get("gender") is None  # SEX_MAP intentionally empty


def test_aireadi_biosample_derived_from(aireadi_graph):
    sample = by_id(aireadi_graph)["ark:59853/aireadi-c2m2-biosample/cardiac_ECG_1"]
    derived = [r["@id"] for r in sample["prov:wasDerivedFrom"]]
    assert "ark:59853/aireadi-c2m2-subject/subject_1" in derived


def test_voice_disease_usedby_backreference(tmp_path):
    graph = convert(VOICE, tmp_path)
    diseases = cv_terms(graph, "disease")
    assert diseases, "voice should have disease DefinedTerm nodes"
    # Every disease is referenced by at least one biosample (biosample_disease back-reference).
    assert all(c.get("usedBy") for c in diseases)
    for c in diseases:
        for ref in c["usedBy"]:
            assert ref["@id"].startswith("ark:59853/voice-c2m2-biosample/")


def test_voice_defined_term_shape_and_synonym(tmp_path):
    graph = convert(VOICE, tmp_path)
    diseases = cv_terms(graph, "disease")
    # @id is the resolved ontology IRI, mirrored into identifier; termCode holds the raw CURIE.
    poland = next(c for c in diseases if c["@id"].endswith("DOID_12961"))
    assert poland["identifier"] == poland["@id"]
    assert poland["termCode"] == "DOID:12961"
    # synonyms JSON array -> a plain list[str] under `synonym` (json_string_list parser).
    assert poland["synonym"] == ["Poland's syndactyly"]
    # Populated CV tables beyond disease are all remodeled as DefinedTerm (anatomy, data_type,
    # file_format, sample_prep_method).
    assert {t for c in cv_terms(graph) for ap in c.get("additionalProperty", [])
            if ap.get("name") == "c2m2CvTable" for t in [ap["value"]]} >= {
        "disease", "anatomy", "data_type", "sample_prep_method"}


# --------------------------------------------------------------------------------------------
# c2m2-mini: file-node parity with the base mapper + empty-table safety.
# --------------------------------------------------------------------------------------------
def test_mini_file_nodes_match_base(tmp_path):
    mapped = by_id(convert(MINI, tmp_path / "mapped"))
    base_mapper = base.C2M2ToROCrateMapper(MINI)
    base_mapper.create_rocrate(output_path=str(tmp_path / "base"))
    base_graph = by_id(json.load(open(tmp_path / "base" / "ro-crate-metadata.json"))["@graph"])

    file_ids = [i for i, n in base_graph.items() if n.get("additionalType") == "File"
                and n.get("cfde:c2m2Entity") == "file"]
    assert len(file_ids) == 2
    for fid in file_ids:
        assert fid in mapped
        assert normalized(base_graph[fid]) == normalized(mapped[fid]), \
            f"file node {fid} diverged from the base mapper"


def test_mini_empty_subject_yields_no_patients(tmp_path):
    graph = convert(MINI, tmp_path)
    assert sum(1 for n in graph if norm_type(n.get("@type")) == "Patient") == 0
    # 1 biosample row still explodes to a Sample.
    assert sum(1 for n in graph if norm_type(n.get("@type")) == "Sample") == 1


# --------------------------------------------------------------------------------------------
# association_type :1 observed / :0 ruled-out inversion regression.
# --------------------------------------------------------------------------------------------
def _write_tsv(path, header, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(header) + "\n")
        for row in rows:
            f.write("\t".join(row) + "\n")


@pytest.fixture
def inversion_datapackage(tmp_path):
    """A minimal datapackage: one subject with an observed (:1) and a ruled-out (:0) disease."""
    shutil.copy(os.path.join(AIREADI, "C2M2_datapackage.json"),
                tmp_path / "C2M2_datapackage.json")
    _write_tsv(tmp_path / "dcc.tsv",
               ["id", "dcc_name", "dcc_abbreviation", "dcc_description", "contact_email",
                "contact_name", "dcc_url", "project_id_namespace", "project_local_id"],
               [["dcc1", "Test DCC", "testdcc", "d", "e@x.org", "n", "http://x", "test", "p1"]])
    _write_tsv(tmp_path / "id_namespace.tsv",
               ["id", "abbreviation", "name", "description"],
               [["test", "test", "Test NS", "d"]])
    _write_tsv(tmp_path / "subject.tsv",
               ["id_namespace", "local_id", "project_id_namespace", "project_local_id",
                "persistent_id", "creation_time", "granularity", "sex", "ethnicity",
                "age_at_enrollment"],
               [["test", "subject_A", "test", "p1", "", "", "", "", "", ""]])
    _write_tsv(tmp_path / "disease.tsv",
               ["id", "name", "description", "synonyms"],
               [["DOID:1", "observed disease", "d1", ""],
                ["DOID:2", "ruled-out disease", "d2", ""]])
    _write_tsv(tmp_path / "subject_disease.tsv",
               ["subject_id_namespace", "subject_local_id", "association_type", "disease"],
               [["test", "subject_A", "cfde_disease_association_type:1", "DOID:1"],
                ["test", "subject_A", "cfde_disease_association_type:0", "DOID:2"]])
    return tmp_path


def test_association_type_inversion(inversion_datapackage, tmp_path_factory):
    out = tmp_path_factory.mktemp("inversion_out")
    graph = convert(str(inversion_datapackage), out)
    patient = next(n for n in graph if norm_type(n.get("@type")) == "Patient")

    observed = "http://purl.obolibrary.org/obo/DOID_1"
    ruled_out = "http://purl.obolibrary.org/obo/DOID_2"

    diagnosis = {r["@id"] for r in patient.get("diagnosis", [])}
    health = {r["@id"] for r in patient.get("healthCondition", [])}
    assert diagnosis == {observed}
    assert health == {observed}
    # The ruled-out disease is NEVER asserted as a condition the subject has.
    assert ruled_out not in diagnosis
    assert ruled_out not in health
    # It survives only as a negative-assertion PropertyValue.
    ruled = [p for p in patient.get("additionalProperty", [])
             if p.get("propertyID") == "ruledOutCondition"]
    assert [p["value"] for p in ruled] == [ruled_out]
