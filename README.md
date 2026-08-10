# Healthcare Test Data Generator

Generate deterministic, synthetic healthcare test data as JSONL. The generator
creates provider, member, and medical-claim records. It is deliberately small:
one config, one generic engine, and one Python module per entity shape.

## Installation

Install Python 3.12 or newer and [uv](https://docs.astral.sh/uv/), then install
the project dependencies:

```sh
uv sync --extra dev
```

## Generate data

Run the configured generator from the repository root:

```sh
uv run python -m healthcare_test_data generate --config generator.config.json
```

The default configuration writes providers, members, and medical claims to
`output/providers.jsonl`, `output/members.jsonl`, and `output/claims.jsonl`.
Each file contains one JSON object per line. JSONL is the only output format
supported by this release.

## Configure records and scenarios

The shortest configuration names only the entities to generate. Each `count`
is exact and includes both normal and varied records; scenario quantities must
add up to no more than that count. Variations are ordinary records in the
entity JSONL file, never separate scenario or expected-result files.

```json
{
  "member": {
    "count": 10,
    "scenarios": {
      "new": 1,
      "changed": 1,
      "duplicate": 1,
      "stale": 1,
      "incomplete": 1
    }
  },
  "claim": {
    "count": 20,
    "scenarios": {"replacement": 1, "void": 1}
  }
}
```

Omit an entity to skip it. A claim-only run is valid and creates deterministic
linked member/provider values internally. The short form defaults the seed to
`20260805`, writes to `./output`, and uses the checked-in schema, module,
filename, and standard profile for each entity. `generator.config.json` shows
the supported detailed `entities` form when you need to change those defaults
or select the institutional claim profile.

Supported profiles are `provider`, `member`, `claim-professional`, and
`claim-institutional`. Provider and member entities use their matching single
profile. Set a claim's profile to either claim profile to select its
professional or institutional source-shaped fields.

Provider and member support `new`, `changed`, `duplicate`, `stale`, and
`incomplete`. Claims support those five names plus `replacement`, `void`, and
`orphan_payment`. An unknown scenario, negative quantity, or total larger than
the entity count is rejected before output is created.

The root `survivorship_policy` configures documented internal decisions for
`member_verified_action`, `claim_verified_action`, and `void_action`. Each
accepts `update`, `keep_both`, or `ignore`; the checked-in configuration shows
the defaults. It affects validation of expected outcomes only and never adds a
policy or scenario marker to generated JSONL.

`make generate` runs the same command through the project's managed Python
environment.

The configured seed makes output deterministic: running the same entity and
count with the same seed produces the same data. Each entity output is written
to a temporary sibling file and is published only after its full run succeeds;
a failed generation does not replace the prior output.

Configured entity filenames must be relative to `output_directory`, must not
contain `.` or `..` path components, and must resolve inside that directory.
When a later run disables a known entity, its old JSONL file is removed after a
successful generation. Other files in the output directory are not touched.

## Verify the project

Run all quality gates and generated-record smoke checks without private input:

```sh
make verify
```

To additionally validate an external three-record provider JSONL file, pass it
explicitly to the provider smoke:

```sh
uv run python tools/provider_generator_smoke.py --sample /path/to/providers.jsonl
```

## Add a future entity

The provider, member, and claim schemas are based on the supplied GDF layout
and matching/survivorship reference. Claims preserve separate patient and
subscriber identities, link to the generated billing/rendering provider by NPI
and client/master IDs, and include a reconciled service line. Header and line
payment fields satisfy `allowed = member liability + paid`.

When onboarding another entity:

1. Add `schemas/<entity>/<entity>.schema.json`.
2. Add `src/healthcare_test_data/entities/<entity>.py` exposing `generate_record`.
3. Add an entity config entry.
4. Enable it.

The entity module stays intentionally small:

```python
def generate_record(seed: int, index: int) -> dict[str, object]:
    """Generate one schema-valid record for a stable index."""
```

The generic engine imports that function, validates each record against the
configured schema, and writes it as one JSONL line. It does not need an entity
registry, rule language, plugin, or scenario framework.

Member and claim generators additionally accept an optional third
`entity_counts` argument. The CLI supplies it so relational IDs cycle only
through records that are actually configured for output; ordinary future
generators can keep the two-argument function shown above.

## Reference coverage

The implementation uses these supplied references without copying their source
data into the repository:

| Reference | Implemented coverage |
| --- | --- |
| `GDF Request File Layouts Standard - v2.9 - Copy(3).xlsx` | Flat GDF provider, member/address/enrollment/COB, and medical-claim header/line identifiers. |
| `Member_Provider_ClaimsKeysAndSurvivorship(4).docx` | Payer-scoped member/subscriber identity, provider NPI/TIN links, claim original/root/version lineage, source tags, and payment reconciliation amounts. |

Provider remains at the existing core Provider, Network, and Address layout.
The workbook's specialty, client-group, and metric fields are optional
extensions, so they are deliberately not added to this small generator.

## Project layout

- `generator.config.json` — enabled entities, record counts, seed, and output.
- `schemas/<entity>/<entity>.schema.json` — the source-compatible JSON Schema.
- `src/healthcare_test_data/entities/<entity>.py` — readable synthetic data
  construction for one entity.
- `tools/` — manual smoke checks used by `make verify`.

Generated output and IDE metadata are intentionally ignored. No `tests/` or
`samples/` directory is part of this project.
