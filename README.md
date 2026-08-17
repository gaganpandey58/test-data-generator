# Test Data Generator

Generate deterministic, test healthcare JSON Lines (JSONL) data for providers, members, professional claims, and institutional claims. The output uses the field names, JSON structure, and record relationships represented by the supplied source samples, without copying real data.

The generator creates deterministic new records and can now derive update fixtures from those records. Update scenarios are driven by the checked-in `Member_Provider_ClaimsKeysAndSurvivorship(5).docx` rule catalog, with optional field-level selection for targeted tests.

## What it writes

| Entity | Output file | Description |
| --- | --- | --- |
| Provider | `providers.jsonl` | Provider identity, address, and network data. |
| Member | `members.jsonl` | Member, address, enrollment, and coordination-of-benefits data. |
| Professional claim | `professional-claims.jsonl` | Professional claim headers, details, and embedded payment fields. |
| Institutional claim | `institutional-claims.jsonl` | Institutional claim headers, details, and embedded payment fields. |

Professional and institutional claims are distinct streams and are always written to separate files. Payment data remains part of each claim object; the generator does not create a separate payment file.

## Design

The project separates the fields that *can* be generated from the fields that are *currently emitted*. This avoids losing GDF coverage while keeping output identical in shape to the selected source layouts.

```mermaid
flowchart LR
    A["schema/gdf workbook"] --> B["Complete GDF field catalog<br/>all available attributes"]
    B --> C["JSON Schemas<br/>validate all available fields"]
    D["Simple client profile file<br/>headers and client values"] --> F["Entity builder"]
    E["Layout profile<br/>selected headers, root fields, groups"] --> G["Layout projection"]
    F --> G
    G --> H["Generic nested-field deduplication<br/>keep only declared relationship references"]
    H --> I["Schema validation"]
    I --> J["Separate JSONL outputs"]
    K["generator.config.json<br/>client + happy path + counts"] --> F
```

### GDF fields and schemas

The Excel workbook in [`schema/gdf/`](schema/gdf/) is the complete field
catalog. Replace it with an updated workbook, or add a newer `.xlsx` file; the
next generation run automatically detects the newest workbook and refreshes
the JSON Schemas under [`schema/json/`](schema/json/). This keeps every
available provider, member, and claim field—even fields not emitted today.

From the repository root, refresh schema properties with:

```sh
uv run python schema/tools/extract-gdf-catalogs.py schema/gdf/GDF\ Request\ File\ Layouts\ Standard.xlsx
```

Or verify that no GDF field is missing without writing files:

```sh
uv run python schema/tools/extract-gdf-catalogs.py schema/gdf/GDF\ Request\ File\ Layouts\ Standard.xlsx --verify
```

The schemas are the extension point for future layouts. A field is not removed
merely because a current source sample does not use it.

### Layouts control output

The JSON files under [src/test_data_generator/layouts/](src/test_data_generator/layouts/) are the output contract. A layout explicitly declares:

- transport/header fields;
- root fields;
- nested groups and their child fields; and
- parent references that must stay in a nested group.

Only fields selected by a layout are emitted. This is why a complete GDF catalog does not cause extra attributes to appear in generated JSONL. For example, the member layout explicitly selects the `CM_MEMBER_COB` nested group.

Nested projection is generic. If a nested object repeats a value already held by its parent, it is removed unless that group declares the field as a required parent reference. The layout—not entity-specific code—decides which structural links such as member or provider identifiers remain duplicated.

### Sample patterns and client headers

[`src/test_data_generator/samples/sample_shapes.json`](src/test_data_generator/samples/sample_shapes.json)
contains type-only patterns extracted from the supplied provider, member,
professional/institutional claim, and professional/institutional payment
samples. It makes the origin of optional defaults explicit without copying any
source values. Entity builders provide the realistic test values; the
pattern file fills remaining sample fields with type-compatible blanks.

`client` selects an entry in
[`src/test_data_generator/configuration/client_profiles.json`](src/test_data_generator/configuration/client_profiles.json).
That single file owns client-specific headers and values (payer, platforms,
product, source-system values, and similar envelope metadata). Entity builders
do not contain client-specific header literals.

To support a new client, add a complete top-level client entry with `headers` and `values` for each supported entity, then select that key through `client`. No separate generator implementation is needed. Layouts declare the headers they recognize, so profile values are emitted only when the selected entity layout includes that header.

## Configuration

[generator.config.json](generator.config.json) is the only run-time file you normally edit. It has four concepts:

- `client` — the checked-in client profile to use;
- `seed` — integer used to reproduce a deterministic run; and
- entity `count` values — the exact number of objects to write; and
- optional entity `layout` — a compatible checked-in output layout (the current
  layout is used when omitted).

`schema`, `module`, and output filenames are internal defaults. Omit an entity
to skip its output. A supplied layout must be valid for the selected data type:
for example, `provider` can use only `"provider"`, while professional claims
can use only `"claim-professional"` today.

```json
{
  "client": "chc",
  "seed": 20260805,
  "output_directory": "./output",
  "provider": {"count": 10},
  "member": {"count": 10},
  "claims": {
    "professional": {"count": 10},
    "institutional": {"count": 10}
  }
}
```

`count` is exact: `{"member": {"count": 10}}` writes exactly ten member objects. Scenario quantities and scenario maps are not accepted. Claims may run alone: their linked member and provider IDs are generated deterministically. When member/provider streams are selected too, the claim IDs link to the corresponding generated records.

`seed` is not a business date or source-layout version. Reusing the same configuration and seed produces the same test records; changing the seed produces a different deterministic set.

## Generate data

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/), then install the project dependencies:

```sh
uv sync --extra dev
```

Generate with the checked-in configuration:

```sh
uv run generate-data
```

The default configuration generates both creation and update fixtures. New data
is written under `output/new-test-data/`; update data is written under
`output/update-test-data/`. Each update is derived
from the matching creation record using the same seed and row index.

Run only one side of the workflow when needed:

```sh
uv run python -m test_data_generator generate --config generator.config.json --mode creation
uv run python -m test_data_generator generate --config generator.config.json --mode updates
```

Supported update scenarios include `UPDATE_SINGLE_FIELD`,
`UPDATE_REQUIRED_FIELDS`, `UPDATE_OPTIONAL_FIELDS`, `MISSING_REQUIRED_FIELD`,
`MISSING_MULTIPLE_FIELDS`, `MISSING_SELECTED_FIELDS`, `INVALID_KEY`,
`CHANGE_WEIGHT_BELOW_LIMIT`, `CHANGE_WEIGHT_AT_LIMIT`, and
`POST_MATCH_WEIGHT_LIMIT_EXCEEDED`. A per-entity `updates` block can specify
`fields`, `include`, `exclude`, `matching_method`, and `threshold`; explicit
fields take precedence over include/exclude selection.

### Update scenario field reference

The field names below are the currently supported runtime fields from the
normalized rule catalog. The catalog remains the source of truth:
[`member-provider-claims-key-survivorship.json`](src/test_data_generator/configuration/member-provider-claims-key-survivorship.json).

| Entity | Matching keys (`INVALID_KEY`) | Baseline required update fields | Baseline optional update fields |
| --- | --- | --- | --- |
| Member | `CM_MEMBER_CLIENT_ID` | `CM_PAYER_SHORT`, `CM_MEMBER_FIRST_NAME`, `CM_MEMBER_LAST_NAME`, `CM_MEMBER_BIRTH_DATE`, `CM_MEMBER_GENDER`, `CM_MEMBER_SSN` | `CM_MEMBER_STATE`, `CM_MEMBER_ZIP` |
| Provider | `CP_PROVIDER_NPI`, `CP_PROVIDER_FEDERAL_TAX_ID`, `CP_PROVIDER_CLIENT_ID` | `CP_PROVIDER_FIRST_NAME`, `CP_PROVIDER_LAST_NAME`, `CP_PROVIDER_TAXONOMY_CODE` | `CP_PROVIDER_BILLING_GROUP_NAME`, `CP_PROVIDER_ZIP` |
| Professional Claim | `CH_CLIENT_CLAIM_ID`, `CH_PATIENT_CLIENT_ID`, `CH_BILLING_PROVIDER_NPI` | `CH_CLAIM_SERVICE_FROM_DATE`, `CH_CLAIM_SERVICE_TO_DATE`, `CH_CLAIM_FREQUENCY_CODE`, `CH_PATIENT_ACCOUNT_CONTROL_NUMBER` | `CH_PATIENT_FIRST_NAME`, `CH_CHARGE_AMOUNT`, `CH_PAID_AMOUNT` |
| Institutional Claim | `CH_CLIENT_CLAIM_ID`, `CH_PATIENT_CLIENT_ID`, `CH_BILLING_PROVIDER_NPI` | `CH_TYPE_OF_BILL_CODE`, `CH_CLAIM_SERVICE_FROM_DATE`, `CH_CLAIM_SERVICE_TO_DATE`, `CH_PATIENT_ACCOUNT_CONTROL_NUMBER` | `CH_PATIENT_FIRST_NAME`, `CH_CHARGE_AMOUNT`, `CH_PAID_AMOUNT` |

Scenario selection rules:

Required and optional classification is context-aware. A field can be
required for one matching method or update scenario and optional for another;
the catalog uses `required_in` and `optional_in` for those overrides, while
the legacy `required` value remains the fallback. For example,
`CM_MEMBER_FIRST_NAME` is eligible in both `UPDATE_REQUIRED_FIELDS` and
`UPDATE_OPTIONAL_FIELDS` according to the active context.

| Scenario | Supported fields |
| --- | --- |
| `UPDATE_SINGLE_FIELD` | Exactly one required or optional non-key field from the table above. |
| `UPDATE_REQUIRED_FIELDS` | One or more fields from the **required-in-context** set. If `fields` is omitted, all eligible required non-key fields are selected. |
| `UPDATE_OPTIONAL_FIELDS` | One or more fields from the **optional-in-context** set. |
| `MISSING_REQUIRED_FIELD` | Exactly one field from the **required-in-context** set. |
| `MISSING_MULTIPLE_FIELDS` | Two or more fields from the **required-in-context** set. |
| `MISSING_SELECTED_FIELDS` | Any explicitly selected required or optional non-key field. |
| `INVALID_KEY` | One or more fields from the **Matching keys** column. |
| `CHANGE_WEIGHT_BELOW_LIMIT` | Any eligible non-key fields whose catalog weights total below the selected matching-method threshold. |
| `CHANGE_WEIGHT_AT_LIMIT` | Any eligible non-key fields whose catalog weights total exactly equal the selected matching-method threshold. |
| `POST_MATCH_WEIGHT_LIMIT_EXCEEDED` | Any eligible non-key fields whose catalog weights exceed the selected matching-method threshold; matching keys remain unchanged. |

Example for a targeted Member update:

```json
{
  "member": {
    "count": 1,
    "updates": {
      "scenario": "UPDATE_SINGLE_FIELD",
      "fields": ["CM_MEMBER_SSN"]
    }
  }
}
```

The normalized catalog at
`src/test_data_generator/configuration/member-provider-claims-key-survivorship.json`
records source document revision `0.9`, entity keys, matching methods, field
classification, elasticity, weights, and survivorship behavior. The DOCX is
the business source; the JSON catalog is the runtime contract and must be
regenerated/reviewed when the source document changes.

### Header ordering

JSON object order does not affect parsing, but the output order can be
configured for readability or systems that compare serialized fixtures. Set
the global order in `generation.output_order.headers`:

```json
{
  "generation": {
    "output_order": {"headers": "last"}
  }
}
```

Supported values are `source`, `first`, and `last`. An entity can override the
global value with `output_order.headers` inside its `member`, `provider`, or
`claims` configuration block. Ordering is applied after layout projection for
both creation and update JSONL files.

To audit the DOCX revision and preserve its tables as source evidence:

```sh
make extract-source
```

`generate-data` automatically refreshes schemas from `schema/gdf/` first. To
use another configuration file:

```sh
uv run python -m test_data_generator generate --config path/to/config.json
```

For the checked-in configuration, generated files appear in `./output`:

```text
output/
├── new-test-data/
│   ├── providers.jsonl
│   ├── members.jsonl
│   ├── professional-claims.jsonl
│   └── institutional-claims.jsonl
└── update-test-data/
    ├── providers.update.jsonl
    └── ...
```

Each line is a complete JSON object. Records are validated against their JSON Schema before publication. Files are written atomically, so a failed entity run does not replace that entity's prior output. If an entity is omitted from a later successful run, only its known generated output is removed; unrelated output-directory files are not touched.

## Verify the generator

Run the source lint, format, and type checks:

```sh
make verify
```

## Project layout

```text
generator.config.json                         # One simple generation request
schema/
├── gdf/                                      # Replaceable GDF Excel source
├── json/                                     # Complete GDF-aware JSON Schemas
└── tools/                                    # GDF schema refresh utility
src/test_data_generator/
├── configuration/                            # Client profiles and config loading
├── core/                                     # Generation, validation, identifiers
├── entities/                                 # Provider, member, and claim builders
├── layouts/                                  # Current JSON output-selection contracts
├── samples/                                  # Sample type patterns and source references
└── cli.py                                    # Command-line entry point
```

## Current scope

- JSONL is the only output format.
- Data is test and intended for development, integration, and processing exercises; the generator produces matching/survivorship fixtures but is not a production matching or adjudication engine.
- Provider, member, professional claim, and institutional claim are the currently supported entity streams.
- Update generation is layered after creation generation; scenario rules are reusable across supported entity streams.
