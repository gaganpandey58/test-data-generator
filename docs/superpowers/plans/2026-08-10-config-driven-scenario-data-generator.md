# Config-Driven Scenario Data Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate source-shaped provider, member, claim, and embedded-payment JSONL data with configurable non-happy-path variations inside each entity's exact record count.

**Architecture:** Keep the existing JSONL engine and flat entity folders. Add a checked-in GDF field-profile registry derived from the supplied workbook, a small scenario planner that replaces selected baseline rows with rule-driven variations, and an internal expected-outcome evaluator used only by manual smoke checks. Outputs remain entity JSONL files only.

**Tech Stack:** Python 3.12, JSON Schema Draft 2020-12, Faker, orjson, uv, Ruff, mypy, Make.

## Global Constraints

- Do not create a project `tests/` or `samples/` directory.
- Do not emit CSV, `incoming-events.jsonl`, or `expected-results.jsonl`.
- Preserve one flat schema file per entity under `schemas/<entity>/<entity>.schema.json`.
- Use the GDF workbook for field names, types, sizes, and functional grouping; use EIP files only as value/shape examples.
- `count` is the exact number of output records; `sum(scenarios.values()) <= count`.
- Scenario rows use real source fields only and add no synthetic scenario marker to JSONL output.
- Keep provider/member/claim/payment links valid for the configured entity counts.
- Keep all functions documented with Google/KDoc-style docstrings.
- Do not commit, stage, push, or delete supplied requirement artifacts unless the user asks.

---

### Task 1: Add source-layout profiles and configuration validation

**Files:**
- Create: `src/healthcare_test_data/layouts/member.json`
- Create: `src/healthcare_test_data/layouts/provider.json`
- Create: `src/healthcare_test_data/layouts/claim-professional.json`
- Create: `src/healthcare_test_data/layouts/claim-institutional.json`
- Create: `src/healthcare_test_data/layouts/__init__.py`
- Modify: `src/healthcare_test_data/config.py`
- Modify: `src/healthcare_test_data/run_config.schema.json`
- Modify: `generator.config.json`
- Modify: `tools/simple_config_smoke.py`

**Interfaces:**
- Produces `EntityConfig.scenarios: Mapping[str, int]`, `EntityConfig.profile: str`, and `RunConfig.survivorship_policy: SurvivorshipPolicy`.
- Produces `load_layout(profile: str) -> LayoutProfile` with canonical GDF name, type, max size, and nested-group metadata.

- [ ] **Step 1: Add a failing manual configuration check**

Add checks that the following config loads and that an invalid scenario total is rejected without creating output:

```json
{
  "member": {
    "count": 10,
    "profile": "member",
    "scenarios": {"new": 1, "changed": 1, "duplicate": 1, "stale": 1, "incomplete": 1}
  }
}
```

- [ ] **Step 2: Run the focused check to establish the gap**

Run: `uv run python tools/simple_config_smoke.py`

Expected: failure because `scenarios`, `profile`, and policy validation do not yet exist.

- [ ] **Step 3: Define source-layout profile files**

Create compact JSON registries from the GDF workbook:

```json
{
  "profile": "member",
  "root": [{"name": "CM_MEMBER_CLIENT_ID", "type": "text", "max_length": 35}],
  "groups": {"CM_MEMBER_ADDRESSES": [{"name": "CM_MEMBER_ADDRESS_01", "type": "text", "max_length": 55}]}
}
```

Include all GDF core provider/address/network fields, member/member-address fields, and the professional/institutional Medical Claims fields represented by the corresponding EIP sample profile. Exclude custom and optional extension groups by default, but retain their profile names for later enablement.

- [ ] **Step 4: Implement strict config parsing**

Extend the packaged run schema and config loader so each enabled entity accepts:

```python
@dataclass(frozen=True)
class EntityConfig:
    name: str
    count: int
    scenarios: Mapping[str, int]
    profile: str
    schema: Path
    module: str
    filename: str
```

Reject an unknown profile, unsupported scenario for that entity, a negative scenario quantity, or a total greater than `count`. Default an omitted `scenarios` object to `{}` for backward-compatible normal generation.

- [ ] **Step 5: Prove the focused check passes**

Run: `uv run python tools/simple_config_smoke.py`

Expected: valid scenario config loads; invalid totals and unsupported combinations fail safely.

### Task 2: Build the reusable scenario planner and internal outcome evaluator

**Files:**
- Create: `src/healthcare_test_data/scenarios.py`
- Create: `src/healthcare_test_data/survivorship.py`
- Modify: `src/healthcare_test_data/engine.py`
- Create: `tools/scenario_survivorship_smoke.py`
- Modify: `Makefile`

**Interfaces:**
- Produces `ScenarioPlan.baseline_indexes: tuple[int, ...]` and `ScenarioPlan.variation_for(index: int) -> Scenario | None`.
- Produces `evaluate(existing: Mapping[str, object], incoming: Mapping[str, object], scenario: Scenario, policy: SurvivorshipPolicy) -> ExpectedDecision`.
- `run_entity(...)` asks the planner for the row's variation, then publishes only the generated record.

- [ ] **Step 1: Add failing scenario assertions**

Create a manual smoke that plans `count=10` with five member scenarios and asserts 10 rows total, five baseline rows, five varied rows, and no output-only scenario field.

- [ ] **Step 2: Run it to verify the current gap**

Run: `uv run python tools/scenario_survivorship_smoke.py`

Expected: failure because no planner or evaluator exists.

- [ ] **Step 3: Implement deterministic planning**

Implement:

```python
def plan(count: int, scenarios: Mapping[str, int], seed: int) -> ScenarioPlan:
    """Assign exact configured scenario quantities to stable output indexes."""
```

The first `count - sum(scenarios.values())` rows are baseline records. Each later variation references a deterministic baseline index; `new` receives no baseline. Reuse baseline records cyclically when necessary.

- [ ] **Step 4: Implement rule-driven outcome evaluation**

Encode source-document actions internally:

```python
class ExpectedAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    KEEP_BOTH = "KEEP_BOTH"
    IGNORE = "IGNORE"
    LINK_PAYMENT = "LINK_PAYMENT"
```

Apply the match tier then the recency gate. Configure the documented 834/837 verified-record branch and voided-837 conflict through `survivorship_policy`; do not hard-code an undocumented resolution.

- [ ] **Step 5: Prove planner and evaluator behavior**

Run: `uv run python tools/scenario_survivorship_smoke.py`

Expected: exact counts and actions for new, changed, duplicate, stale, incomplete, replacement, void, and orphan-payment scenarios.

### Task 3: Make provider and member generation GDF-profile and scenario aware

**Files:**
- Modify: `schemas/provider/provider.schema.json`
- Modify: `schemas/member/member.schema.json`
- Modify: `src/healthcare_test_data/entities/provider.py`
- Modify: `src/healthcare_test_data/entities/member.py`
- Modify: `tools/provider_generator_smoke.py`
- Modify: `tools/member_claim_generator_smoke.py`

**Interfaces:**
- Entity generators accept `GenerationContext(seed, index, profile, scenario, entity_counts)` while maintaining the existing two-argument fallback for ordinary future generators.
- Member scenario transformations preserve payer/member/subscriber separation; provider transformations preserve client/master/NPI/TIN identity.

- [ ] **Step 1: Add failing source-shape assertions**

Add manual checks for GDF max lengths/types, nested address arrays, and variations:

```python
assert len(record["CM_MEMBER_CLIENT_ID"]) <= 35
assert scenario_record["CM_MEMBER_SOURCE_UPDATED_AT"] > baseline["CM_MEMBER_SOURCE_UPDATED_AT"]
assert duplicate == baseline
assert stale["CM_MEMBER_SOURCE_UPDATED_AT"] < baseline["CM_MEMBER_SOURCE_UPDATED_AT"]
```

- [ ] **Step 2: Implement field-profile population and mutation**

Populate required GDF core fields with realistic values matching EIP shapes. For `incomplete`, remove only fields optional in the active profile; for `changed`, retain the selected match key and update permitted demographic/address/source fields; for `duplicate` retain all identity and source values; for `stale` reduce source/effective dates.

- [ ] **Step 3: Add matching-key variations**

Implement member tiers: payer+member ID, member ID+DOB+gender, exact name+DOB+gender, and configurable SSN/address/newborn fallback variants. Implement provider identifier and composite variants: provider ID, NPI/name/address, org+TIN, and taxonomy/name/address.

- [ ] **Step 4: Run entity checks**

Run: `uv run python tools/provider_generator_smoke.py && uv run python tools/member_claim_generator_smoke.py`

Expected: source-shaped records, exact scenario quantities, and internally valid create/update/keep-both/ignore decisions.

### Task 4: Replace the claim contract with EIP/GDF source-shaped claim and payment records

**Files:**
- Modify: `schemas/claim/claim.schema.json`
- Modify: `src/healthcare_test_data/entities/claim.py`
- Modify: `tools/member_claim_generator_smoke.py`
- Modify: `tools/simple_cli_smoke.py`

**Interfaces:**
- Claim output uses `CLAIM_DETAIL` rather than `CD_CLAIM_LINES`.
- Professional and institutional profiles share a common header/line identity model and use profile-specific optional GDF fields.

- [ ] **Step 1: Add failing source-shape checks**

Assert generated records expose source names used by the EIP fixtures:

```python
assert "CLAIM_DETAIL" in claim
assert claim["CH_CLIENT_CLAIM_UNIQUE_ID"]
assert claim["CLAIM_DETAIL"][0]["CD_CLAIM_LINE_NUMBER"] >= 1
```

- [ ] **Step 2: Implement the source-shaped claim envelope**

Populate GDF header identity, root/original/version lineage, member/subscriber/provider links, claim dates/type/POS-or-TOB, diagnosis, payment dates/status/check, claim financial amounts, and source metadata. Populate `CLAIM_DETAIL` with GDF line IDs, service dates, procedure/revenue/modifier values, units, line payment amounts, and line adjustments only for applicable scenarios.

- [ ] **Step 3: Implement claim/payment scenarios**

Generate `replacement` with a stable root ID, linked original ID, increased version/adjustment count, and frequency `7`. Generate `void` with frequency `8` and policy-controlled internal action. Generate `orphan_payment` as a valid claim/payment envelope whose payment composite cannot match a configured claim. Preserve the 835 payment composite fields and recency paid/check date.

- [ ] **Step 4: Prove source relationships and totals**

Run: `uv run python tools/member_claim_generator_smoke.py`

Expected: every non-orphan claim links to a configured member/provider, lines reconcile to the header under the configured profile, and replacement lineage is consistent.

### Task 5: Wire config-driven selection, documentation, and full verification

**Files:**
- Modify: `generator.config.json`
- Modify: `src/healthcare_test_data/cli.py`
- Modify: `README.md`
- Modify: `Makefile`
- Modify: `tools/simple_cli_smoke.py`

**Interfaces:**
- `python -m healthcare_test_data generate --config generator.config.json` generates any enabled subset or all entities.
- No command produces a scenario-only or expected-result file.

- [ ] **Step 1: Define the checked-in example config**

Use the user-approved pattern for each enabled entity:

```json
"member": {
  "enabled": true,
  "count": 10,
  "profile": "member",
  "scenarios": {"new": 1, "changed": 1, "duplicate": 1, "stale": 1, "incomplete": 1}
}
```

Add claim profile and policy examples, including only supported scenario names.

- [ ] **Step 2: Document plain-language use**

Update README with the exact config shape, explain that `count` includes scenario rows, list entity-specific scenario names, describe profile selection, and state that all variations appear in the ordinary entity JSONL files.

- [ ] **Step 3: Expand CLI smoke coverage**

Verify one selected entity, a selected set, and all entities. Assert exact JSONL line counts, no extra output files, deterministic repeated output, and safe config errors.

- [ ] **Step 4: Run final verification**

Run:

```sh
make verify
make generate
git diff --check
```

Expected: all quality gates pass; only configured `providers.jsonl`, `members.jsonl`, and `claims.jsonl` are written; generated output is removed after inspection because it is ignored local output.
