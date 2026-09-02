"""Build deterministic existing/incoming Claim recency fixtures."""

import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import orjson

from test_data_generator.configuration.config import ClaimFixtureConfig
from test_data_generator.core.identifiers import deterministic_uuid4
from test_data_generator.entities.claim import generate_record as generate_claim
from test_data_generator.entities.payment import derive_payment_from_claim
from test_data_generator.layouts import project_record

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_FIXTURE_EPOCH = datetime(2026, 8, 20)


def write_claim_fixtures(
    output_directory: Path,
    fixtures: Sequence[ClaimFixtureConfig],
    seed: int,
    entity_counts: Mapping[str, int],
) -> dict[str, Path]:
    """Write deterministic paired Claim fixtures under one output directory."""
    paths: dict[str, Path] = {}
    for index, fixture in enumerate(fixtures):
        for suffix, record in build_claim_fixture(fixture, seed, index, entity_counts).items():
            path = output_directory / "claim-fixtures" / f"{fixture.name}.{suffix}.jsonl"
            _write_jsonl(path, (record,))
            paths[f"{fixture.name}.{suffix}"] = path
    return paths


def build_claim_fixture(
    fixture: ClaimFixtureConfig,
    seed: int,
    fixture_index: int,
    entity_counts: Mapping[str, int],
) -> dict[str, dict[str, object]]:
    """Generate existing records and an incoming Claim using existing Claim logic."""
    profile = "claim-professional" if fixture.claim_type == "P" else "claim-institutional"
    existing_index = fixture_index * 10
    existing_lifecycle = _lifecycle(fixture.claim_frequency, existing_index)
    existing_claim = generate_claim(
        seed,
        existing_index,
        entity_counts,
        {},
        {},
        profile,
        existing_lifecycle,
    )
    if fixture.match:
        incoming_claim = deepcopy(existing_claim)
    else:
        incoming_index = existing_index + 2
        incoming_claim = generate_claim(
            seed,
            incoming_index,
            entity_counts,
            {},
            {},
            profile,
            _lifecycle(fixture.claim_frequency, existing_index + 1),
        )

    incoming_timestamp, existing_timestamps = _fixture_timestamps(fixture, fixture_index)
    records: dict[str, dict[str, object]] = {}
    for record_type, timestamp in existing_timestamps.items():
        if record_type == "835":
            payment_profile = (
                "payment-professional" if fixture.claim_type == "P" else "payment-institutional"
            )
            record = derive_payment_from_claim(existing_claim, payment_profile, seed, fixture_index)
            _set_fixture_transport(record, timestamp, seed, fixture, f"existing-{record_type}")
        else:
            record = project_record(deepcopy(existing_claim), profile)
            _set_fixture_transport(record, timestamp, seed, fixture, f"existing-{record_type}")
            if record_type == "ch":
                record["CH_RECORD_TAG"] = "CH Verified"
                record["CH_RECORD_STATUS"] = "Active"
            else:
                record["CH_RECORD_TAG"] = "837 Provisional"
                record["CH_RECORD_STATUS"] = "New"
        records[f"existing_{record_type}"] = record

    incoming = project_record(incoming_claim, profile)
    _set_fixture_transport(incoming, incoming_timestamp, seed, fixture, "incoming")
    incoming["CH_RECORD_TAG"] = "837 Provisional"
    incoming["CH_RECORD_STATUS"] = "New"
    records["incoming"] = incoming
    return records


def _lifecycle(frequency: str, original_index: int) -> tuple[str, int | None]:
    """Use the Claim generator's existing lifecycle argument convention."""
    return (frequency, original_index)


def _fixture_timestamps(
    fixture: ClaimFixtureConfig, fixture_index: int
) -> tuple[datetime, dict[str, datetime]]:
    """Resolve actual deterministic timestamps and validate requested recency."""
    parsed_existing = {
        kind: _parse_timestamp(timestamp, f"existing {kind}")
        for kind, timestamp in fixture.existing_timestamps.items()
        if timestamp is not None
    }
    incoming = (
        _parse_timestamp(fixture.incoming_timestamp, "incoming")
        if fixture.incoming_timestamp is not None
        else _resolve_incoming_timestamp(parsed_existing, fixture.recency, fixture_index)
    )
    timestamps: dict[str, datetime] = {}
    for kind, relation in fixture.recency.items():
        timestamp = parsed_existing.get(kind)
        if timestamp is None:
            timestamp = _timestamp_for_relation(incoming, relation)
        if not _matches_relation(incoming, timestamp, relation):
            raise ValueError(
                f"Claim fixture {fixture.name!r} incoming timestamp does not satisfy "
                f"{relation} relative to existing {kind}"
            )
        timestamps[kind] = timestamp
    return incoming, timestamps


def _resolve_incoming_timestamp(
    existing: Mapping[str, datetime], recency: Mapping[str, str], fixture_index: int
) -> datetime:
    """Find a fixed timestamp satisfying all explicitly supplied comparisons."""
    default = _FIXTURE_EPOCH + timedelta(days=fixture_index)
    same_values = {
        existing[kind]
        for kind, relation in recency.items()
        if relation == "SAME" and kind in existing
    }
    if len(same_values) > 1:
        raise ValueError("SAME recency references must use the same existing timestamp")
    if same_values:
        incoming = same_values.pop()
    else:
        lower_bounds = [
            existing[kind] + timedelta(seconds=1)
            for kind, relation in recency.items()
            if relation == "NEWER" and kind in existing
        ]
        upper_bounds = [
            existing[kind] - timedelta(seconds=1)
            for kind, relation in recency.items()
            if relation == "OLDER" and kind in existing
        ]
        lower = max(lower_bounds, default=None)
        upper = min(upper_bounds, default=None)
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("Claim fixture recency constraints have no valid incoming timestamp")
        if lower is None and upper is None:
            incoming = default
        elif lower is None:
            assert upper is not None
            incoming = min(default, upper)
        elif upper is None:
            incoming = max(default, lower)
        else:
            incoming = min(max(default, lower), upper)
    return incoming


def _timestamp_for_relation(incoming: datetime, relation: str) -> datetime:
    """Create an existing timestamp one second around the incoming event."""
    if relation == "NEWER":
        return incoming - timedelta(seconds=1)
    if relation == "SAME":
        return incoming
    return incoming + timedelta(seconds=1)


def _matches_relation(incoming: datetime, existing: datetime, relation: str) -> bool:
    """Return whether two real timestamps satisfy the requested ordering."""
    return {
        "NEWER": incoming > existing,
        "SAME": incoming == existing,
        "OLDER": incoming < existing,
    }[relation]


def _parse_timestamp(value: str, label: str) -> datetime:
    """Parse the public fixture timestamp format without using wall-clock time."""
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError as error:
        raise ValueError(f"Claim fixture {label} timestamp must be YYYY-MM-DDTHH:MM:SSZ") from error


def _set_fixture_transport(
    record: dict[str, object],
    timestamp: datetime,
    seed: int,
    fixture: ClaimFixtureConfig,
    record_role: str,
) -> None:
    """Keep matching identity stable while assigning each fixture event unique transport IDs."""
    token = f"claim-fixture:{fixture.name}:{record_role}"
    record["cotiviti.produced_at"] = timestamp.strftime(_TIMESTAMP_FORMAT)
    record["ROWID"] = deterministic_uuid4(seed, f"{token}:row")
    record["cotiviti.message_id"] = deterministic_uuid4(seed, f"{token}:message")
    record["cotiviti.correlation_id"] = deterministic_uuid4(seed, f"{token}:correlation")
    record["cotiviti.batch_id"] = f"claim-fixture-{fixture.name}-{record_role}"


def _write_jsonl(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    """Publish one small fixture stream atomically without sidecar files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as output_file:
            temporary_path = Path(output_file.name)
            for record in records:
                output_file.write(orjson.dumps(record))
                output_file.write(b"\n")
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
