# C2M2 → RO-Crate mapping schema

The converter (`convert.py`) turns each C2M2 table's TSV rows into typed RO-Crate `@graph`
nodes using **editable JSON mappings** under `mappings/`, interpreted by one generic engine + a fixed
parser registry (`parsers.py`). Adding a C2M2 table is a new JSON file, not new Python.

```
python3 convert.py <datapackage-dir> [--output-path DIR] [--mappings-dir DIR] [root overrides…]
```

## How it maps

A C2M2 datapackage is a pile of relational tables of three kinds, and each kind becomes a different
part of the RO-Crate `@graph`:

**Entity tables** hold the records being described. Each row becomes one typed FAIRSCAPE node:

| C2M2 entity table | FAIRSCAPE class |
|---|---|
| `subject` | `Patient` |
| `biosample` | `Sample` |
| `collection` | `Sample` |
| `file` | `Dataset` |

**CV (controlled-vocabulary) tables** — the 14 term tables (`disease`, `anatomy`, `assay_type`, …).
Each row becomes a schema.org **`DefinedTerm`** whose `@id` is the term resolved to a real ontology
IRI (DOID, UBERON, …).

**Association tables** are the link tables (`subject_disease`, `biosample_from_subject`, …). They add
no nodes of their own; they supply the **edges** that wire the graph together. Each association row
points an entity node at a term or at another entity — `subject_disease` becomes a `Patient` →
`diagnosis`/`healthCondition` edge onto a disease `DefinedTerm`; `biosample_from_subject` becomes a
`Sample` → `prov:wasDerivedFrom` edge onto its `Patient`.

So the crate is assembled from all three: entity tables give the typed nodes, CV tables give the
`DefinedTerm` nodes they refer to, and association tables provide the pointers between them. A patient
with a diagnosis is a `Patient` node whose `diagnosis` (from `subject_disease`) points at a disease
`DefinedTerm` (from the `disease` CV table). Because the term node's `@id` and the edge pointing at it
resolve through the same code, the pointer always lands on the node.

## The engine

The engine reuses the base mapper (`C2M2ToROCrateMapper`) for the generic per-table
`Dataset`+`evi:Schema` reflection, the preservation layer (datapackage.json + sqlite), and the
`Computation`/`Software` provenance pair. Only **row explosion** and the **root** are mapping-driven.

## Files

| File | Purpose |
|---|---|
| `mappings/manifest.json` | Load order. `root` names the root mapping; `entities` lists the per-table mappings to apply (in each file's `order`); `follow_on` is a parking lot for not-yet-enabled tables. |
| `mappings/root.json` | How the crate root (`ROCrateMetadataElem`) is filled. |
| `mappings/<table>.json` | How one C2M2 table's rows become typed nodes. |
| `mappings/cv/<table>.json` | The 14 C2M2 controlled-vocabulary (CV term) tables — a class of their own, each → a `DefinedTerm`. Referenced from the manifest as `cv/<table>.json`. |

### The CV term tables (`mappings/cv/`)

The CFDE C2M2 spec marks 14 tables as **"CV term table"** in the
[C2M2 Table Summary](https://github.com/nih-cfde/published-documentation/wiki/C2M2-Table-Summary):
`analysis_type`, `anatomy`, `assay_type`, `biofluid`, `compound`, `data_type`, `disease`,
`file_format`, `gene`, `ncbi_taxonomy`, `phenotype`, `protein`, `sample_prep_method`, `substance`.
They all share the same TSV shape (`id`, `name`, `description`, `synonyms`, plus a couple of
table-specific extras) and all resolve their `id` to a real ontology IRI, so they map uniformly to
schema.org **`DefinedTerm`** and live together under `mappings/cv/`:

| CV column | `DefinedTerm` field | via |
|---|---|---|
| `id` (resolved) | `@id` | `id.strategy = ontology_term` (IRI, else minted `{prefix}-cv/…` fallback) |
| `name` | `name` | `name_or_curie` (falls back to the CURIE) |
| `id` | `termCode` | `scalar` (the raw CURIE) |
| `id` (resolved) | `identifier` | `ontology_term` (the external ontology IRI) |
| `description` | `description` | `description_or_template` |
| `synonyms` | `synonym` | `json_string_list` (parses the JSON-array cell → `List[str]`) |

Each CV node also carries a `c2m2CvTable` `additionalProperty` naming its source table, and a
`usedBy` back-reference to the biosamples / subjects / files that cite the term (via the same
`associations` machinery below). Because the `@id` is the resolved IRI, a CV node and every edge
pointing at it (e.g. a biosample's `anatomicalStructure`) always share one identity, and two CV
rows that resolve to the same IRI collapse to a single node.

Any key starting with `_` (e.g. `_doc`, `_note`) is documentation and ignored by the engine.

## Per-table mapping

```jsonc
{
  "source_table": "subject",        // C2M2 resource name (TSV resolved via the datapackage)
  "entity_type": "Patient",         // Patient | Sample | MedicalCondition | BioChemEntity | Dataset
  "order": 10,                       // @graph emission order (deterministic)
  "generated": false,                // true => nodes join the conversion Computation.generated
  "extra_type": {"additionalType": "File"},   // Dataset rows only
  "cv_source": {"column": "id", "source_table": "disease"},  // exposes {curie}/{onto}/{onto_suffix}/{iri}
  "computed": { … },                 // derived per-row template tokens (see below)
  "id": { … },                       // how to mint @id
  "constants": { "prop": <literal|template> },   // fixed props (interpolated)
  "fields": [ { "source", "target", "parser", … } ],           // column → native property
  "additional_property": [ { "name", "source"|"const", "parser", … } ],  // → schema.org additionalProperty[]
  "associations": [ … ]              // declarative edges from link tables (see below)
}
```

`entity_type` picks the `fairscape_models` class **and** its default `@type`. Only the six types
`Patient | Sample | MedicalCondition | BioChemEntity | DefinedTerm | Dataset` are allowed —
anything else would deserialize to `GenericMetadataElem` (guarded at load).

### `id` — minting the `@id`
```jsonc
"id": { "strategy": "persistent_or_mint | persistent_ark_or_mint | ontology_term | mint | column",
        "column": "persistent_id",           // column read by persistent_*_or_mint / ontology_term / column
        "source_table": "disease",            // ontology_term: passed to resolve_term
        "mint": "{prefix}-subject/{local_id}" }// template when the column is empty / not an ARK
```
- `persistent_or_mint` → `row[column]` if set, else the `mint` template.
- `persistent_ark_or_mint` → `row[column]` **only if it is (or embeds) a real ARK** (e.g. an `n2t.net/ark:…`
  resolver URL → the bare `ark:…`), else the `mint` template. External PIDs (DOI, Cellosaurus, …) do
  **not** land in the `@id` slot (the fairscape models require `@id` to match the ARK pattern); pair
  with an `external_pid` field to keep the PID as `identifier`. Used by file/subject/biosample/collection.
- `ontology_term` → resolved ontology IRI (`resolve_term`), else a minted `{prefix}-cv/{table}/{slug}`.
  **The same resolver the associations use, so a CV node's `@id` always equals the edge `@id` pointing at it.**
- `mint` → template only. `column` → verbatim value.

Whichever strategy an entity table uses, `_build_guid_maps` re-derives each node's `@id` with the
**same** `mint_id` call, so `entity_guid` association joins (e.g. biosample→subject `wasDerivedFrom`)
always match the node they point at — even when the `@id` was minted rather than taken from the PID.

### Template tokens
Any `{column}` from the row, plus `{prefix}`, `{crate_guid}`, `{dcc_label}`, `{author}`,
`{publisher}`, `{date}`, `{slug:column}`, the `cv_source` tokens `{curie}` / `{onto}` /
`{onto_suffix}` (` (Ontology)` or empty) / `{iri}`, and any `computed` token.

### `computed` — derived per-row tokens
```jsonc
"computed": {
  "eff_local_id": {"first_nonempty": ["local_id", "filename"]},           // first non-empty column
  "anatomy_clause": {"template": ", anatomy {anatomy}", "when": "anatomy"} // template, or "" if 'when' col empty
}
```

### `fields` / `additional_property` — parsers
`fields` set native model properties; `additional_property` build the `additionalProperty[]` list
(order preserved, empty values dropped). `parser` is one of:

| parser | result |
|---|---|
| `scalar` / `scalar_default` (`default`) / `first_nonempty` (`fallbacks`) / `int` | trimmed value variants |
| `sex_map` | CFDE sex CV → gender (currently unmapped ⇒ `null`) |
| `ontology_term` (`source_table`) | resolved ontology IRI |
| `edam_encoding` | EDAM `format:NNNN` → IRI |
| `template` (`template`) / `context` (`value`) | interpolated string / a context var (`$publisher`, …) |
| `name_or_curie` / `description_or_template` (`template`) | value else CURIE / value else template |
| `curie_identifier` | `[{"@type":"PropertyValue","value":<curie>,"name":<ontology>}]` |
| `property_value` / `property_value_const` (`const`) / `property_value_term` (`source_table`) | a schema.org `PropertyValue` |
| `json_string_list` | parse a JSON-array cell (e.g. CV `synonyms`) → `List[str]`; `None` when empty |
| `external_pid` | a `persistent_id` **only when it is not an ARK** (DOI/Cellosaurus/…) → kept as `identifier`; `None` when empty or an ARK (already the `@id`). Pairs with `persistent_ark_or_mint`. |
| `skip` | drop |

### `associations` — declarative edges from link tables
One uniform rule expresses both forward edges (attach on this table's node) and back-references
(this node is the value another table points at):

```jsonc
{ "assoc_table": "subject_disease",
  "match":  { "columns": ["subject_id_namespace","subject_local_id"], "resolver": "entity_key" },
  "value":  { "columns": ["disease"], "resolver": "ontology_term", "source_table": "disease" },
  "targets": ["diagnosis","healthCondition"],
  "wrap":   "ident_ref",                                         // ident_ref | ruled_out_pv | property_value (pv_name)
  "filter": { "column": "association_type", "endswith": ":1" } } // optional gate on a CV code
```
- **`match.resolver`** — `entity_key` (`columns` → this table's `(id_namespace, local_id)` key) or
  `ontology_term` (`columns` → this CV node's resolved `@id`).
- **`value.resolver`** — `ontology_term` (+`source_table`), `entity_guid` (+`entity` = another
  configured table's node), or `raw`.
- A node is looked up by **both** its `(id_namespace, local_id)` key and its `@id`, so a mapping
  never restates which key its own associations use.

> **association_type is a trap.** Filter on the CV *code* suffix (`:1` = observed, `:0` = ruled-out),
> never the label — the datapackage field description lists the two example labels in reverse order,
> which previously inverted the converter. Ruled-out edges use `wrap: "ruled_out_pv"` and never assert
> a `healthCondition`/`diagnosis`. See the regression test in `test_convert.py`.

## `root.json`

`identity` documents what the engine derives from the `dcc` + `id_namespace` tables (and supplies the
ARK `naan`); `context_vars` documents the author/publisher/date defaults. `fields` is what actually
gets written onto the root, each entry one of `{meta:<key>}`, `{const:…}`, `{template:…}`, or
`{computed:<primitive>}`. A CLI flag named by `override` (e.g. `$name_arg` = `--name`) wins when given.

Computed primitives: `today_or_arg`, `version_arg`, `license_arg`, `identifier_arg`,
`cfde_and_namespaces`, `all_element_guids`, `tables_total`, `tables_populated`.

## Extending

All 14 CV term tables ship in `mappings/cv/` (→ `DefinedTerm`). To add a *non-CV* entity table, add
a `mappings/<table>.json` and list it in `manifest.json → entities`; the only engine-level list is
`ENTITY_MODELS` in `convert.py`, which already covers the six allowed `entity_type`s — a brand
new `entity_type` (a new `fairscape_models` class) is the one change that touches Python.

## Verification

`python3 -m pytest -q` runs the full suite. `test_convert.py` covers the AI-READI conversion
(node-type counts, the disease `DefinedTerm` shape, subject/biosample edges, and the root seeded
from the top-level project row), the voice disease `usedBy` back-reference and `synonym` JSON-array
parse, c2m2-mini file-node parity against the base mapper plus empty-table safety, and the `:1`/`:0`
association-type inversion regression. `test_base.py` guards the base mapper directly.
