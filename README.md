# C2M2 → FAIRSCAPE RO-Crate converter

C2M2 datapackage to FAIRSCAPE RO-Crate.

## Install

Python 3.10+ is required. Install the dependencies (a virtualenv is recommended):

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Run from the `src/` directory:

```
cd src
python3 convert.py <datapackage-dir> [--output-path DIR]
```

## How it maps

A C2M2 datapackage has three kinds of tables, each becoming a different part of the `@graph`:

| C2M2 table kind | Becomes | Example |
|---|---|---|
| **Entity** | a typed FAIRSCAPE node | `subject`→`Patient`, `biosample`/`collection`→`Sample`, `file`→`Dataset` |
| **CV** (controlled vocabulary) | a schema.org `DefinedTerm` (`@id` = resolved ontology IRI) | `disease`, `anatomy`, `assay_type`, … (14 tables) |
| **Association** | an edge between the above (no node of its own) | `subject_disease` → `Patient.diagnosis` → disease `DefinedTerm` |

So a patient with a diagnosis is a `Patient` node whose `diagnosis` (from `subject_disease`) points at a
disease `DefinedTerm` (from the `disease` CV table). See [`MAPPING-SCHEMA.md`](MAPPING-SCHEMA.md) for the full mapping reference.

## Module map

| File | Role |
|---|---|
| `convert.py` | **Main entry point.** The mapping-driven engine (`MappingC2M2Converter`): loads the mappings, explodes rows into typed nodes, builds the root + provenance, writes the crate. |
| `base.py` | Base mapper (`C2M2ToROCrateMapper`) that `convert.py` subclasses: datapackage plumbing, generic per-table `Dataset`/`evi:Schema` reflection, the preservation layer (datapackage.json + sqlite), and the `Computation`/`Software` pair. |
| `ontology.py` | Pure CV-value → ontology IRI resolution (`resolve_term`) + the schema.org `PropertyValue`/IdentifierValue helpers. No converter or model imports. |
| `parsers.py` | The fixed parser registry the JSON mappings refer to by name (imports from `base` + `ontology`). |
| `mappings/` | The declarative heart — `manifest.json` (load order), `root.json`, the entity mappings (`file`/`subject`/`biosample`/`collection`), and `cv/` (the 14 controlled-vocabulary tables → `DefinedTerm`). |
| `MAPPING-SCHEMA.md` | Full reference for the mapping JSON format. |
| `test_convert.py` | End-to-end test suite for `convert.py`. |
| `test_base.py` | Base-layer tests guarding `base.py` (the class `convert.py` subclasses). |
| `examples/`, `test-data/` | Datapackage fixtures and reference crates used by the tests. |

Import graph: `convert.py → base.py, parsers.py`; `parsers.py → base.py, ontology.py`. `ontology.py`
and `base.py` have no sibling dependencies.

## Tests

```
python3 -m pytest -q        # runs test_convert.py + test_base.py
```
