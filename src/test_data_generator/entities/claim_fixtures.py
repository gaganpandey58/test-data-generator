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
from test_data_generator.layouts import project_record

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_FIXTURE_EPOCH = datetime(2026, 8, 20)


def write_claim_fixtures(
    output_directory: Path,
    fixtures: Sequence[ClaimFixtureConfig],
    seed: int,
    entity_counts: Mapping[str, int],
) -> dict[str, Path]:
    """Write deterministic existing and incoming Claim records as JSONL fixtures."""
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
    """Build matching existing 837/CH records and an incoming 837 Claim."""
    profile = "claim-professional" if fixture.claim_type == "P" else "claim-institutional"
    claim = generate_claim(
        seed,
        fixture_index,
        entity_counts,
        {},
        {},
        profile,
        (fixture.claim_frequency, fixture_index),
    )
    incoming_timestamp, existing_timestamps = _fixture_timestamps(fixture, fixture_index)
    records: dict[str, dict[str, object]] = {}
    for record_type, timestamp in existing_timestamps.items():
        record = project_record(deepcopy(claim), profile)
        if record_type == "ch":
            record["FILE_TYPE"] = "CH"
        _set_fixture_transport(record, timestamp, seed, fixture, f"existing-{record_type}")
        records[f"existing_{record_type}"] = record

    incoming = project_record(claim, profile)
    _set_fixture_transport(incoming, incoming_timestamp, seed, fixture, "incoming")
    records["incoming"] = incoming
    return records


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
        timestamp = parsed_existing.get(kind) or _timestamp_for_relation(incoming, relation)
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
    """Find a fixed timestamp satisfying every explicit existing-record comparison."""
    default = _FIXTURE_EPOCH + timedelta(days=fixture_index)
    same_values = {
        existing[kind]
        for kind, relation in recency.items()
        if relation == "SAME" and kind in existing
    }
    if len(same_values) > 1:
        raise ValueError("SAME recency references must use the same existing timestamp")
    if same_values:
        return same_values.pop()
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
    if lower is None:
        return min(default, upper) if upper is not None else default
    return max(default, lower) if upper is None else min(max(default, lower), upper)


def _timestamp_for_relation(incoming: datetime, relation: str) -> datetime:
    """Create an existing timestamp one second around the incoming event."""
    if relation == "NEWER":
        return incoming - timedelta(seconds=1)
    if relation == "SAME":
        return incoming
    return incoming + timedelta(seconds=1)


def _matches_relation(incoming: datetime, existing: datetime, relation: str) -> bool:
    """Return whether the supplied timestamps satisfy the requested relation."""
    return {
        "NEWER": incoming > existing,
        "SAME": incoming == existing,
        "OLDER": incoming < existing,
    }[relation]


def _parse_timestamp(value: str, label: str) -> datetime:
    """Parse the public fixture timestamp format without consulting the system clock."""
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
    """Assign deterministic event metadata without changing Claim identity or lineage."""
    token = f"claim-fixture:{fixture.name}:{record_role}"
    record["cotiviti.produced_at"] = timestamp.strftime(_TIMESTAMP_FORMAT)
    record["ROWID"] = deterministic_uuid4(seed, f"{token}:row")
    record["cotiviti.message_id"] = deterministic_uuid4(seed, f"{token}:message")
    record["cotiviti.correlation_id"] = deterministic_uuid4(seed, f"{token}:correlation")
    record["cotiviti.batch_id"] = f"claim-fixture-{fixture.name}-{record_role}"


def _write_jsonl(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    """Atomically write one small fixture stream without sidecar files."""
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
