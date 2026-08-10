"""Plan which generated rows are independent baselines or scenario variants.

Scenario planning is intentionally internal: it lets entity generators create
real source-shaped records without adding scenario markers to JSONL output.
The planner reserves the leading rows as baselines, assigns the configured
scenario quantities to the remaining rows, and points each non-``new`` row at
a deterministic baseline that can be copied and varied.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    """Describe one internal mutation applied to a generated output row.

    Attributes:
        name: Configured scenario name, such as ``changed`` or ``duplicate``.
        baseline_index: Output position of the baseline to copy.  ``None`` is
            used for ``new`` records because they are independently generated.
    """

    name: str
    baseline_index: int | None


@dataclass(frozen=True)
class ScenarioPlan:
    """Expose deterministic baseline positions and their row variations.

    Attributes:
        baseline_indexes: Output positions generated without a scenario
            mutation and available as sources for related variations.
        _variations: Internal mapping from output position to its scenario.
    """

    baseline_indexes: tuple[int, ...]
    _variations: Mapping[int, Scenario]

    def variation_for(self, index: int) -> Scenario | None:
        """Return the scenario assigned to one stable output position.

        Args:
            index: Zero-based output row position within the entity file.

        Returns:
            The internal scenario for ``index``, or ``None`` when the row is
            an independently generated baseline.
        """
        return self._variations.get(index)


def plan(count: int, scenarios: Mapping[str, int], seed: int) -> ScenarioPlan:
    """Assign exact configured scenario quantities to stable output indexes.

    ``seed`` is accepted to keep the public planning contract aligned with the
    generators.  Current allocation is stable by configured mapping order and
    therefore does not need randomization; keeping the parameter allows a
    future deterministic seed-based allocation without changing callers.

    Args:
        count: Exact number of rows requested for the entity output.
        scenarios: Scenario names mapped to their requested row quantities.
        seed: Shared deterministic generation seed reserved for allocation.

    Returns:
        A plan with baseline positions and a variation for every scenario row.

    Raises:
        ValueError: If ``count`` or a quantity is negative, scenario totals
            exceed ``count``, or a non-new scenario has no baseline to copy.
    """
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
    """Select a deterministic baseline for one requested scenario row.

    Args:
        name: Scenario name being allocated.
        baseline_indexes: Available independent output positions.
        baseline_cursor: Next position in the round-robin baseline cycle.

    Returns:
        The baseline position (or ``None`` for ``new``) and the cursor to use
        for the next allocation.

    Raises:
        ValueError: If a scenario that needs a baseline is configured when no
            independent baseline row exists.
    """
    if name == "new":
        return None, baseline_cursor
    if not baseline_indexes:
        raise ValueError(f"scenario {name!r} requires at least one baseline record")
    baseline_index = baseline_indexes[baseline_cursor % len(baseline_indexes)]
    return baseline_index, baseline_cursor + 1
