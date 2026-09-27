import pytest

from src.backend.synthesis_pricing import (
    INSTRUMENTAL_CREDIT_DURATION_SECONDS,
    VOCAL_CREDIT_DURATION_SECONDS,
    calculate_synthesis_credit_breakdown,
    estimate_synthesis_credits,
    score_has_instrumental_parts,
    selected_score_duration,
)


@pytest.mark.parametrize(
    ("duration", "vocal", "instrumental", "total"),
    [
        (1.0, 1, 1, 2),
        (30.0, 1, 1, 2),
        (30.001, 2, 1, 3),
        (120.0, 4, 1, 5),
        (120.001, 5, 2, 7),
        (300.0, 10, 3, 13),
    ],
)
def test_combined_credit_breakdown_rounds_each_component(duration, vocal, instrumental, total):
    result = calculate_synthesis_credit_breakdown(
        duration_seconds=duration,
        include_instrumentals=True,
    )
    assert result.vocal_part_credits == vocal
    assert result.instrumental_credits == instrumental
    assert result.total_credits == total


def test_vocal_only_breakdown_never_charges_instrumentals():
    result = calculate_synthesis_credit_breakdown(
        duration_seconds=121.0,
        include_instrumentals=False,
    )
    assert result.vocal_part_credits == 5
    assert result.instrumental_credits == 0
    assert result.total_credits == 5


def test_estimate_serializes_itemized_contract_and_components():
    result = estimate_synthesis_credits(
        vocal_duration_seconds=121.0,
        vocal_part_id="P1",
        expand_repeats=True,
        has_instrumental_parts=True,
        instrumental_charge_required=True,
        instrumental_charge_scope="u/s/score",
    ).to_dict()
    assert result["vocal_part"] == {
        "pricing_unit_seconds": VOCAL_CREDIT_DURATION_SECONDS,
        "estimated_credits": 5,
    }
    assert result["instrumentals"]["pricing_unit_seconds"] == INSTRUMENTAL_CREDIT_DURATION_SECONDS
    assert result["instrumentals"]["estimated_credits"] == 2
    assert result["instrumentals"]["charged_once_for_all_tracks"] is True
    assert result["billing_components"] == ["vocal", "instrumental"]
    assert result["total_estimated_credits"] == 7


def test_paid_instrumentals_produce_vocal_only_estimate():
    result = estimate_synthesis_credits(
        vocal_duration_seconds=121.0,
        vocal_part_id="P1",
        expand_repeats=False,
        has_instrumental_parts=True,
        instrumental_charge_required=False,
        instrumental_charge_scope="u/s/score",
    ).to_dict()
    assert result["instrumentals"]["has_instrumental_parts"] is True
    assert result["instrumentals"]["charge_required"] is False
    assert result["instrumentals"]["estimated_credits"] == 0
    assert result["billing_components"] == ["vocal"]
    assert result["total_estimated_credits"] == 5


def test_instrumental_eligibility_uses_exporter_routes_not_part_count():
    assert score_has_instrumental_parts(
        {
            "instrument_program_resolution": {
                "instrumental_score_instrument_ids": ["P2-I1"]
            }
        }
    )
    assert not score_has_instrumental_parts(
        {"instrument_program_resolution": {"instrumental_score_instrument_ids": []}}
    )


def test_selected_duration_honors_repeat_mode():
    summary = {"duration_seconds": 60.0, "expanded_duration_seconds": 150.0}
    assert selected_score_duration(summary, expand_repeats=False) == 60.0
    assert selected_score_duration(summary, expand_repeats=True) == 150.0
