# Config-Driven Healthcare Test Data Design

## Goal

Generate source-shaped provider, member, claim, and payment data from one
configuration file. The generator must use the GDF layout for field names,
data types, and sizes; use EIP samples for realistic populated shapes; and use
the keys and survivorship document to generate non-happy-path variations.

## Output Contract

Generation creates only entity data files. There are no incoming-event files
and no expected-result files.

- `providers.jsonl` contains provider rows.
- `members.jsonl` contains member rows.
- `claims.jsonl` contains medical claim rows with claim detail and payment
  fields in the source-shaped claim envelope.

The internal match-scenario, survivorship/version, and expected-result logic is
used to create and validate variations. It does not create extra output files.

## Configuration

Each selected entity uses this shape:

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
  }
}
```

`count` is the exact number of records written for that entity. The sum of
scenario quantities must not exceed `count`. The generator writes
`count - sum(scenarios.values())` normal baseline rows and then writes exactly
the configured scenario rows. A scenario that needs an existing record selects
a deterministic baseline row; it may reuse one when there are fewer baseline
rows than matching scenarios. A `new` scenario creates an independent row.

Entity selection is controlled by top-level enabled entries. The user can run
one entity, a selected set, or all configured entities in one command.

## Source Layout Profiles

The GDF workbook is the authority for field names, data types, max sizes, and
functional groups. EIP examples are shape and value references only; they are
not copied into generated output.

- Provider: Provider, Provider Address, and Provider Network core layout.
- Member: Member core layout, nested Member Address, and optional COB and
  Enrollment profiles.
- Claim: Medical Claims layout, including `CLAIM_DETAIL` line records and
  embedded payment fields. Professional and institutional profiles are
  selectable in configuration.

Optional custom, specialty, client-group, metric, pharmacy, and laboratory
layouts remain disabled unless explicitly enabled through a feature profile.

## Scenario Behavior

Scenarios alter only real source-shaped fields. They do not add a synthetic
scenario marker to output.

- `new`: different matching keys and a new source/version identity.
- `changed`: matching keys retained, newer source/effective date, changed
  permissible attributes, and an incremented source version where applicable.
- `duplicate`: matching keys and source date retained.
- `stale`: matching keys retained with an older source/effective date.
- `incomplete`: one or more configured matching attributes omitted while the
  record remains valid for its selected output profile.
- `replacement`: claim root identity retained, original claim ID linked,
  version and adjustment count incremented, and replacement frequency applied.
- `void`: claim identity retained and void frequency applied; behavior follows
  the configured void policy because the source document contains conflicting
  prose and decision-tree guidance.
- `orphan_payment`: payment fields are generated without a matching claim
  identity; the record remains a source-shaped claim/payment envelope.

Member, provider, claim, and payment matching uses the source document's
priority tiers. A recency gate produces the survivorship action internally:
create, update, keep both, ignore, or link payment. The configured policy
controls the documented add-or-replace branches for 834/837 records matched to
verified records and the voided-837 ambiguity.

## Relationships and Validation

Member/client/master/subscriber values, provider/client/master/NPI/TIN values,
and claim patient/subscriber/provider values remain referentially consistent
within the configured output counts. Claim-line financial totals reconcile to
claim-header values. Every generated value satisfies the active profile's GDF
type and maximum size.

Manual smoke checks validate profile schema conformance, configured record
counts, scenario quantities, relationship integrity, date/version behavior,
and expected internal survivorship decisions.
