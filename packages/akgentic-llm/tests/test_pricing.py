"""Tests for pricing table, aggregation models, and aggregate_usage()."""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import BaseModel

from akgentic.llm.event import LlmUsageEvent
from akgentic.llm.pricing import (
    PRICING,
    AgentUsageSummary,
    ModelUsage,
    RunUsageSummary,
    _resolve_pricing,
    aggregate_usage,
)

# The OpenAI row the inclusive-cached-turn test prices against: the model the measured
# over-billing was found on. Held in one place so the row can be re-pointed in a single
# line when a later table refresh moves the deployment on again.
_OPENAI_MODEL = "gpt-5.4"

# The Anthropic row, whose cache_write rate (3.75) differs from its input rate (3.0).
# On a row where cache_write == input the two cache_write contributions cancel and the
# cache_write half of the subtraction is unobservable by construction.
_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


def _expected_cost(
    model_key: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Derive the expected cost from the PRICING row, independently of pricing.py.

    Deliberately a transcription rather than a call into ``_compute_model_cost`` or
    ``_resolve_pricing``: a helper that delegates to the code under test agrees with
    whatever that code does, including a defect, and so asserts nothing.

    ``input_tokens`` is inclusive of both cache figures, so the input rate applies to
    the uncached remainder only.
    """
    rates = PRICING[model_key]
    uncached = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
    return (
        uncached * rates["input"]
        + output_tokens * rates["output"]
        + cache_read_tokens * rates["cache_read"]
        + cache_write_tokens * rates["cache_write"]
    ) / 1_000_000


def _make_event(
    run_id: str = "run-1",
    model_name: str = "claude-sonnet-4-20250514",
    provider_name: str = "anthropic",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    requests: int = 1,
) -> LlmUsageEvent:
    return LlmUsageEvent(
        run_id=run_id,
        model_name=model_name,
        provider_name=provider_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        requests=requests,
    )


class TestModelUsage:
    """AC-2: ModelUsage is a frozen dataclass with correct fields."""

    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(ModelUsage)
        usage = ModelUsage(
            model_name="m",
            provider_name="p",
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=3,
            cache_write_tokens=4,
            requests=5,
            estimated_cost_usd=0.1,
        )
        assert dataclasses.fields(usage) is not None
        # Frozen check
        try:
            usage.input_tokens = 999  # type: ignore[misc]
            raise AssertionError("Should be frozen")
        except dataclasses.FrozenInstanceError:
            pass

    def test_has_correct_fields(self) -> None:
        field_names = {f.name for f in dataclasses.fields(ModelUsage)}
        expected = {
            "model_name",
            "provider_name",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "requests",
            "estimated_cost_usd",
        }
        assert field_names == expected


class TestRunUsageSummary:
    """AC-3: RunUsageSummary is a frozen dataclass with correct fields."""

    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(RunUsageSummary)
        summary = RunUsageSummary(
            run_id="r",
            models=[],
            total_input_tokens=0,
            total_output_tokens=0,
            total_cost_usd=0.0,
        )
        try:
            summary.run_id = "x"  # type: ignore[misc]
            raise AssertionError("Should be frozen")
        except dataclasses.FrozenInstanceError:
            pass

    def test_has_correct_fields(self) -> None:
        field_names = {f.name for f in dataclasses.fields(RunUsageSummary)}
        expected = {
            "run_id",
            "models",
            "total_input_tokens",
            "total_output_tokens",
            "total_cost_usd",
        }
        assert field_names == expected


class TestAgentUsageSummary:
    """AC-4: AgentUsageSummary is a Pydantic BaseModel."""

    def test_is_pydantic_basemodel(self) -> None:
        assert issubclass(AgentUsageSummary, BaseModel)

    def test_defaults(self) -> None:
        s = AgentUsageSummary()
        assert s.by_model == {}
        assert s.runs == []
        assert s.total_input_tokens == 0
        assert s.total_output_tokens == 0
        assert s.total_cache_read_tokens == 0
        assert s.total_cache_write_tokens == 0
        assert s.total_requests == 0
        assert s.total_cost_usd == 0.0

    def test_serialization_roundtrip(self) -> None:
        events = [
            _make_event(input_tokens=1000, output_tokens=500, cache_read_tokens=100),
        ]
        summary = aggregate_usage(events)
        data = summary.model_dump()
        restored = AgentUsageSummary.model_validate(data)
        assert restored.total_cost_usd == summary.total_cost_usd
        assert restored.total_input_tokens == summary.total_input_tokens
        assert restored.by_model == summary.by_model


class TestPricingTable:
    """AC-1: PRICING contains required model entries."""

    def test_required_models_present(self) -> None:
        required = [
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "gpt-4o",
            "gpt-5.2",
            "gpt-5.4",
            "gpt-5.6-terra",
        ]
        for model in required:
            assert model in PRICING, f"{model} missing from PRICING"

    def test_required_keys(self) -> None:
        required_keys = {"input", "output", "cache_read", "cache_write"}
        for model, rates in PRICING.items():
            assert set(rates.keys()) == required_keys, (
                f"{model} has wrong keys: {set(rates.keys())}"
            )

    def test_values_are_floats(self) -> None:
        for model, rates in PRICING.items():
            for key, val in rates.items():
                assert isinstance(val, (int, float)), f"{model}.{key} is not numeric"


class TestPricingResolution:
    """A model name resolves to its own row, never to a pricier relative's.

    ``_resolve_pricing`` scans keys longest-first and tests substring containment, so a
    flagship row present without its cheaper siblings captures them and overcharges them
    silently — a row *was* found, so nothing signals an unknown model.

    Every assertion here is an identity check against the very dict stored in ``PRICING``.
    Equality cannot do this job: the table carries four groups of value-identical rows,
    including a three-way collision between ``gpt-5-mini``, ``gpt-5.1-mini`` and
    ``gpt-5.1-codex-mini``, so ``==`` would pass against a genuine mis-resolution.
    """

    def test_flagship_resolves_to_its_own_row(self) -> None:
        """The current flagship no longer falls through to the gpt-5 row by substring."""
        assert _resolve_pricing("gpt-5.4") is PRICING["gpt-5.4"]
        assert _resolve_pricing("gpt-5.4") is not PRICING["gpt-5"]

    def test_mini_sibling_does_not_capture_its_flagship_row(self) -> None:
        """gpt-5.4-mini contains gpt-5.4, so a missing sibling row bills it at 3.3x."""
        assert _resolve_pricing("gpt-5.4-mini") is PRICING["gpt-5.4-mini"]
        assert _resolve_pricing("gpt-5.4-mini") is not PRICING["gpt-5.4"]

    def test_dotted_mini_sibling_does_not_capture_its_flagship_row(self) -> None:
        """The sharper case: gpt-5.1-mini contains no other -mini key.

        The ".1" breaks the "gpt-5-mini" substring, so without a row of its own this model
        matches the gpt-5.1 flagship and bills at 5.00x on input, output and cache_read
        alike. The row it must resolve to is value-identical to two others, so only an
        identity assertion pins the resolution to the right key.
        """
        assert _resolve_pricing("gpt-5.1-mini") is PRICING["gpt-5.1-mini"]
        assert _resolve_pricing("gpt-5.1-mini") is not PRICING["gpt-5.1"]
        assert _resolve_pricing("gpt-5.1-mini") is not PRICING["gpt-5-mini"]

    def test_versioned_deployment_name_resolves_to_its_base_row(self) -> None:
        """Azure deployment names carry a date suffix and must still match."""
        assert _resolve_pricing("gpt-5.4-2026-03-05") is PRICING["gpt-5.4"]


class TestAggregateUsageEmpty:
    """AC-8: Empty event list returns zeroed summary."""

    def test_empty_list(self) -> None:
        result = aggregate_usage([])
        assert result.total_input_tokens == 0
        assert result.total_output_tokens == 0
        assert result.total_cache_read_tokens == 0
        assert result.total_cache_write_tokens == 0
        assert result.total_requests == 0
        assert result.total_cost_usd == 0.0
        assert result.by_model == {}
        assert result.runs == []


class TestAggregateUsageSingleModel:
    """AC-5: Single-model aggregation with correct totals and cost."""

    def test_single_event(self) -> None:
        events = [
            _make_event(
                input_tokens=1_000_000,
                output_tokens=500_000,
            ),
        ]
        result = aggregate_usage(events)
        assert result.total_input_tokens == 1_000_000
        assert result.total_output_tokens == 500_000
        assert result.total_requests == 1
        assert len(result.by_model) == 1
        model = result.by_model["claude-sonnet-4-20250514"]
        assert model.input_tokens == 1_000_000
        assert model.output_tokens == 500_000
        expected_cost = (1_000_000 * 3.0 + 500_000 * 15.0) / 1_000_000
        assert model.estimated_cost_usd == expected_cost
        assert result.runs == []

    def test_multiple_events_same_model(self) -> None:
        events = [
            _make_event(input_tokens=100, output_tokens=50, requests=1),
            _make_event(input_tokens=200, output_tokens=100, requests=1),
        ]
        result = aggregate_usage(events)
        assert result.total_input_tokens == 300
        assert result.total_output_tokens == 150
        assert result.total_requests == 2
        assert len(result.by_model) == 1


class TestAggregateUsageMultiModel:
    """AC-5: Multi-model aggregation with by_model containing one entry per model."""

    def test_two_models(self) -> None:
        events = [
            _make_event(
                model_name="claude-sonnet-4-20250514",
                provider_name="anthropic",
                input_tokens=1000,
                output_tokens=500,
            ),
            _make_event(
                model_name="gpt-4o",
                provider_name="openai",
                input_tokens=2000,
                output_tokens=1000,
            ),
        ]
        result = aggregate_usage(events)
        assert len(result.by_model) == 2
        assert "claude-sonnet-4-20250514" in result.by_model
        assert "gpt-4o" in result.by_model
        assert result.total_input_tokens == 3000
        assert result.total_output_tokens == 1500

        sonnet = result.by_model["claude-sonnet-4-20250514"]
        assert sonnet.provider_name == "anthropic"
        assert sonnet.input_tokens == 1000

        gpt = result.by_model["gpt-4o"]
        assert gpt.provider_name == "openai"
        assert gpt.input_tokens == 2000


class TestAggregateUsageByRun:
    """AC-6: by_run=True produces RunUsageSummary per run_id."""

    def test_by_run(self) -> None:
        events = [
            _make_event(run_id="run-1", input_tokens=100, output_tokens=50),
            _make_event(run_id="run-1", input_tokens=200, output_tokens=100),
            _make_event(run_id="run-2", input_tokens=300, output_tokens=150),
        ]
        result = aggregate_usage(events, by_run=True)
        assert len(result.runs) == 2
        run_ids = {r.run_id for r in result.runs}
        assert run_ids == {"run-1", "run-2"}

        run1 = next(r for r in result.runs if r.run_id == "run-1")
        assert run1.total_input_tokens == 300
        assert run1.total_output_tokens == 150
        assert len(run1.models) == 1

        run2 = next(r for r in result.runs if r.run_id == "run-2")
        assert run2.total_input_tokens == 300
        assert run2.total_output_tokens == 150

    def test_by_run_cost_consistency(self) -> None:
        """RunUsageSummary.total_cost_usd must equal sum of its models' costs."""
        events = [
            _make_event(
                run_id="run-1",
                model_name="claude-sonnet-4-20250514",
                input_tokens=1000,
                output_tokens=500,
            ),
            _make_event(
                run_id="run-1",
                model_name="gpt-4o",
                provider_name="openai",
                input_tokens=2000,
                output_tokens=1000,
            ),
        ]
        result = aggregate_usage(events, by_run=True)
        assert len(result.runs) == 1
        run = result.runs[0]
        expected_cost = sum(m.estimated_cost_usd for m in run.models)
        assert run.total_cost_usd == expected_cost

    def test_by_run_false_no_runs(self) -> None:
        events = [_make_event()]
        result = aggregate_usage(events, by_run=False)
        assert result.runs == []

    def test_by_run_multi_model_per_run(self) -> None:
        events = [
            _make_event(run_id="run-1", model_name="claude-sonnet-4-20250514", input_tokens=100),
            _make_event(run_id="run-1", model_name="gpt-4o", provider_name="openai",
                        input_tokens=200),
        ]
        result = aggregate_usage(events, by_run=True)
        assert len(result.runs) == 1
        run1 = result.runs[0]
        assert len(run1.models) == 2
        assert run1.total_input_tokens == 300


class TestUnknownModel:
    """AC-7: Unknown model produces estimated_cost_usd == 0.0."""

    def test_unknown_model_cost_zero(self) -> None:
        events = [
            _make_event(
                model_name="unknown-model-xyz",
                input_tokens=5000,
                output_tokens=2000,
            ),
        ]
        result = aggregate_usage(events)
        model = result.by_model["unknown-model-xyz"]
        assert model.estimated_cost_usd == 0.0
        assert model.input_tokens == 5000
        assert model.output_tokens == 2000

    def test_mixed_known_and_unknown(self) -> None:
        events = [
            _make_event(model_name="claude-sonnet-4-20250514", input_tokens=1000),
            _make_event(model_name="unknown-model", input_tokens=500),
        ]
        result = aggregate_usage(events)
        assert result.by_model["unknown-model"].estimated_cost_usd == 0.0
        assert result.by_model["claude-sonnet-4-20250514"].estimated_cost_usd > 0.0


class TestCacheTokenPricing:
    """The input rate applies to the uncached remainder, not to every input token.

    Providers report ``input_tokens`` inclusive of the cached figures, so charging the
    full input count at the input rate bills every cached token a second time.
    """

    def test_inclusive_cached_turn_prices_uncached_remainder(self) -> None:
        """A realistic cached turn: 50k input of which 43k was served from cache."""
        events = [
            _make_event(
                model_name=_OPENAI_MODEL,
                provider_name="openai",
                input_tokens=50_000,
                output_tokens=800,
                cache_read_tokens=43_000,
                cache_write_tokens=0,
            ),
        ]
        result = aggregate_usage(events)
        model = result.by_model[_OPENAI_MODEL]
        assert model.estimated_cost_usd == pytest.approx(
            _expected_cost(
                _OPENAI_MODEL,
                input_tokens=50_000,
                output_tokens=800,
                cache_read_tokens=43_000,
            )
        )

    def test_cache_write_is_subtracted_as_well_as_cache_read(self) -> None:
        """Both cache figures come out of the input term, not just cache_read.

        Only observable on a row where cache_write != input, hence the Anthropic row.
        Asserting inequality with the cache-read-only decomposition catches a partial
        fix as well as no fix at all.
        """
        events = [
            _make_event(
                model_name=_ANTHROPIC_MODEL,
                input_tokens=100_000,
                output_tokens=2_000,
                cache_read_tokens=40_000,
                cache_write_tokens=25_000,
            ),
        ]
        result = aggregate_usage(events)
        model = result.by_model[_ANTHROPIC_MODEL]

        both_subtracted = _expected_cost(
            _ANTHROPIC_MODEL,
            input_tokens=100_000,
            output_tokens=2_000,
            cache_read_tokens=40_000,
            cache_write_tokens=25_000,
        )
        assert model.estimated_cost_usd == pytest.approx(both_subtracted)

        # Same tokens, but with only cache_read taken out of the input term: the
        # cache_write tokens are still priced, they are just not removed from input.
        rates = PRICING[_ANTHROPIC_MODEL]
        cache_read_only = (
            (100_000 - 40_000) * rates["input"]
            + 2_000 * rates["output"]
            + 40_000 * rates["cache_read"]
            + 25_000 * rates["cache_write"]
        ) / 1_000_000
        neither_subtracted = (
            100_000 * rates["input"]
            + 2_000 * rates["output"]
            + 40_000 * rates["cache_read"]
            + 25_000 * rates["cache_write"]
        ) / 1_000_000

        assert model.estimated_cost_usd != pytest.approx(cache_read_only)
        assert model.estimated_cost_usd != pytest.approx(neither_subtracted)

    def test_cache_tokens_exceeding_input_clamp_to_non_negative(self) -> None:
        """A malformed row under-reports rather than producing a negative cost."""
        events = [
            _make_event(
                model_name=_ANTHROPIC_MODEL,
                input_tokens=1_000,
                output_tokens=100,
                cache_read_tokens=5_000,
                cache_write_tokens=2_000,
            ),
        ]
        result = aggregate_usage(events)
        model = result.by_model[_ANTHROPIC_MODEL]
        assert model.estimated_cost_usd >= 0.0
        assert model.estimated_cost_usd == pytest.approx(
            _expected_cost(
                _ANTHROPIC_MODEL,
                input_tokens=1_000,
                output_tokens=100,
                cache_read_tokens=5_000,
                cache_write_tokens=2_000,
            )
        )
        assert result.total_input_tokens == 1_000
        assert result.total_cache_read_tokens == 5_000
        assert result.total_cache_write_tokens == 2_000


class TestTotalRequestsAccumulation:
    """total_requests accumulates correctly across multiple events."""

    def test_total_requests_multi_event(self) -> None:
        events = [
            _make_event(requests=3),
            _make_event(requests=2),
            _make_event(model_name="gpt-4o", provider_name="openai", requests=5),
        ]
        result = aggregate_usage(events)
        assert result.total_requests == 10
        assert result.by_model["claude-sonnet-4-20250514"].requests == 5
        assert result.by_model["gpt-4o"].requests == 5


class TestTotalCostConsistency:
    """total_cost_usd equals sum of all model costs."""

    def test_total_equals_sum(self) -> None:
        events = [
            _make_event(
                model_name="claude-sonnet-4-20250514",
                input_tokens=1000,
                output_tokens=500,
            ),
            _make_event(
                model_name="gpt-4o",
                provider_name="openai",
                input_tokens=2000,
                output_tokens=1000,
            ),
            _make_event(
                model_name="unknown-model",
                input_tokens=500,
                output_tokens=250,
            ),
        ]
        result = aggregate_usage(events)
        expected_total = sum(m.estimated_cost_usd for m in result.by_model.values())
        assert result.total_cost_usd == expected_total


class TestPublicApiExport:
    """AC-9: All five exports importable from akgentic.llm and present in __all__."""

    def test_all_exports_importable(self) -> None:
        import akgentic.llm

        assert hasattr(akgentic.llm, "PRICING")
        assert hasattr(akgentic.llm, "AgentUsageSummary")
        assert hasattr(akgentic.llm, "ModelUsage")
        assert hasattr(akgentic.llm, "RunUsageSummary")
        assert hasattr(akgentic.llm, "aggregate_usage")

    def test_all_in_dunder_all(self) -> None:
        import akgentic.llm

        names = ["PRICING", "AgentUsageSummary", "ModelUsage", "RunUsageSummary", "aggregate_usage"]
        for name in names:
            assert name in akgentic.llm.__all__, f"{name} missing from __all__"
