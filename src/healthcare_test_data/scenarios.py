"""Plan deterministic baseline and variation positions for one entity output."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    """Describe the internal variation applied to one generated record."""

    name: str
    baseline_index: int | None


@dataclass(frozen=True)
class ScenarioPlan:
    """Expose baseline positions and their configured variations."""

    baseline_indexes: tuple[int, ...]
    _variations: Mapping[int, Scenario]

    def variation_for(self, index: int) -> Scenario | None:
        """Return the configured variation for one stable output position."""
        return self._variations.get(index)


def plan(count: int, scenarios: Mapping[str, int], seed: int) -> ScenarioPlan:
    """Assign exact configured scenario quantities to stable output indexes."""
    if count < 0:
        raise ValueError("count must be non-negative")
    if any(quantity < 0 for quantity in scenarios.values()):
        raise ValueError("scenario quantities must be non-negative")
    variation_count = sum(scenarios.values())
    if variation_count > count:
        raise ValueError("scenario quantities must not exceed count")

    baseline_indexes = tuple(range(count - variation_count))
    variations: dict[int, Scenario] = {}
    output_index = len(baseline_indexes)
    baseline_cursor = 0
    for name, quantity in scenarios.items():
        for _ in range(quantity):
            baseline_index, baseline_cursor = _baseline_for(name, baseline_indexes, baseline_cursor)
            variations[output_index] = Scenario(name=name, baseline_index=baseline_index)
            output_index += 1
    return ScenarioPlan(baseline_indexes=baseline_indexes, _variations=variations)


def _baseline_for(
    name: str, baseline_indexes: tuple[int, ...], baseline_cursor: int
) -> tuple[int | None, int]:
    """Select a deterministic baseline for a variation when one is required."""
    if name == "new":
        return None, baseline_cursor
    if not baseline_indexes:
        raise ValueError(f"scenario {name!r} requires at least one baseline record")
    baseline_index = baseline_indexes[baseline_cursor % len(baseline_indexes)]
    return baseline_index, baseline_cursor + 1
