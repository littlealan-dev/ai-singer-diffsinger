from __future__ import annotations

from src.backend.llm_prompt import build_prompt_bundle, build_system_prompt


def test_build_system_prompt_includes_voice_part_signals() -> None:
    voice_part_signals = {
        "analysis_version": "v2",
        "parts": [
            {
                "part_index": 0,
                "part_id": "Women",
                "voice_part_measure_spans": {
                    "voice part 1": [{"start": 7, "end": 11}, {"start": 13, "end": 18}],
                    "voice part 2": [{"start": 7, "end": 18}],
                },
                "voice_part_id_to_source_voice_id": {
                    "voice part 1": "1",
                    "voice part 2": "2",
                },
            }
        ],
    }
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=["Raine_Rena_2.01"],
        score_summary={"title": "My Tribute"},
        parsed_score_json={"parts": [{"part_index": 0, "notes": [{"measure_number": 7}]}]},
        voice_part_signals=voice_part_signals,
        voicebank_details=None,
    )
    assert "Voice-part planning signals (if available):" in prompt
    assert "Full parsed score JSON (if available):" in prompt
    assert '"measure_number": 7' in prompt
    assert '"score_summary": {' not in prompt
    assert '"voice part 1"' in prompt
    assert '"voice part 2"' in prompt


def test_build_system_prompt_includes_preprocess_mapping_context() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=["Raine_Rena_2.01"],
        score_summary={"title": "My Tribute"},
        parsed_score_json={"parts": []},
        voice_part_signals={"parts": []},
        preprocess_mapping_context={
            "original_parse": {"score_summary": {"title": "My Tribute"}},
            "derived_mapping": {
                "targets": [
                    {
                        "derived_part_index": 4,
                        "derived_part_id": "P_DERIVED_ABC",
                        "target_voice_part_id": "voice part 1",
                    }
                ]
            },
        },
        voicebank_details=None,
    )
    assert "Preprocess-derived mapping context (if available):" in prompt
    assert '"derived_part_id": "P_DERIVED_ABC"' in prompt


def test_build_system_prompt_includes_latest_attempted_preprocess_plan() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=["Raine_Rena_2.01"],
        score_summary={"title": "My Tribute"},
        parsed_score_json={"parts": []},
        voice_part_signals={"parts": []},
        preprocess_mapping_context=None,
        last_preprocess_plan={
            "targets": [
                {
                    "target": {"part_index": 0, "voice_part_id": "voice part 1"},
                    "sections": [
                        {"start_measure": 7, "end_measure": 11, "mode": "derive"},
                    ],
                }
            ]
        },
        voicebank_details=None,
    )
    assert "Latest attempted line-preparation plan (if available):" in prompt
    assert '"voice_part_id": "voice part 1"' in prompt
    assert '"start_measure": 7' in prompt


def test_build_system_prompt_includes_canonical_lint_rules_from_registry() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
        role="preprocess",
    )
    assert "SVS Voice-Part Lint Rules (Canonical Runtime Validation)" in prompt
    assert "- Rule code: same_part_target_completeness" in prompt
    assert "Suggested fix: Include all required same-part sibling targets" in prompt
    assert "SVS Postflight Validation Rules (Canonical Runtime Validation)" in prompt
    assert "- Rule code: structural_validation_failed" in prompt


def test_build_system_prompt_requires_preprocess_progress_message_from_llm() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
    )
    assert (
        "call `start_preprocess_voice_part_workflow` and set final_message to a short singer-friendly confirmation"
        in prompt
    )


def test_build_system_prompt_declares_full_score_credit_capability_contract() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
    )

    assert "Capability contract:" in prompt
    assert "Excerpt, measure-range, and other partial-song rendering are not available" in prompt
    assert "offer exactly these two next actions: add more credits, or upload another shorter song" in prompt


def test_build_system_prompt_requires_explicit_supported_synthesis_language() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=["Qixuan"],
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
    )

    assert "Every `synthesize` tool call must include an explicit `language` code" in prompt
    assert "infer the language only from lyric text visible in the score context" in prompt
    assert "the chosen language must be in that list" in prompt
    assert "ask the user to choose a language or voicebank" in prompt


def test_build_system_prompt_requires_tool_call_for_preprocess_repair_phase() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
        role="preprocess",
    )
    assert '"phase": "preprocess_repair_planning"' in prompt
    assert "must return exactly one `preprocess_voice_parts` tool call" in prompt
    assert '"phase": "preprocess_postflight_repair"' in prompt
    assert "do not retry the submitted plan" in prompt
    assert "update_existing_derived_lane" in prompt


def test_build_system_prompt_requires_complete_staff_scope_coverage_by_default() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
        role="preprocess",
    )

    assert "selective` is allowed only when the user explicitly requests" in prompt
    assert "each staff scope" in prompt
    assert "maximum simultaneous note count" in prompt
    assert "when the user asks to split a source/staff completely" not in prompt


def test_build_system_prompt_includes_message_only_followup_contract() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
    )
    assert "If the Tool list is empty" in prompt
    assert "Message-only payload:" in prompt
    assert "No tools will be executed from this response" in prompt
    assert "Return `tool_calls: []`" in prompt
    assert "message-only `unsupported_lyric_language` action" in prompt
    assert "Do not say that solfege is being added" in prompt


def test_build_system_prompt_tells_llm_to_study_full_parsed_score_json() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary=None,
        parsed_score_json={"parts": [{"part_index": 1}]},
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
        role="preprocess",
    )
    assert "Study the full parsed score JSON, score summary, and voice-part planning signals together" in prompt
    assert "Prefer the full parsed score JSON as the ground truth for note-level planning details" in prompt


def test_build_system_prompt_shows_none_when_parsed_score_json_not_provided() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
    )
    assert "Full parsed score JSON (if available):\nnone" in prompt


def test_build_prompt_bundle_splits_static_and_dynamic_content() -> None:
    bundle = build_prompt_bundle(
        tools=[{"name": "synthesize", "description": "Render audio", "inputSchema": {"type": "object"}}],
        score_available=True,
        voicebank_ids=["Raine_Rena_2.01"],
        score_summary={"title": "My Tribute"},
        parsed_score_json={"parts": [{"part_index": 1}]},
        voice_part_signals={"parts": []},
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
    )
    assert "Tool list (name, description, input schema):" in bundle.static_prompt_text
    assert '"name": "synthesize"' in bundle.static_prompt_text
    assert "<provided in Dynamic Context>" in bundle.static_prompt_text
    assert '"title": "My Tribute"' not in bundle.static_prompt_text
    assert bundle.dynamic_prompt_text.startswith("Dynamic Context:\n")
    assert '"title": "My Tribute"' in bundle.dynamic_prompt_text
    assert "End Dynamic Context." in bundle.dynamic_prompt_text


def test_build_prompt_bundle_supports_legacy_string_containment() -> None:
    bundle = build_prompt_bundle(
        tools=[],
        score_available=True,
        voicebank_ids=None,
        score_summary={"title": "My Tribute"},
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan={"targets": []},
        voicebank_details=None,
    )
    assert "Latest attempted line-preparation plan (if available):" in bundle
    assert '"title": "My Tribute"' in str(bundle)


def test_build_prompt_bundle_includes_voicebank_gender_and_voice_type() -> None:
    bundle = build_prompt_bundle(
        tools=[],
        score_available=True,
        voicebank_ids=["Katyusha_v170"],
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=[
            {
                "id": "Katyusha_v170",
                "name": "Katyusha v170",
                "gender": "female",
                "voice_type": "soprano",
                "languages": ["en", "ja", "zh"],
                "use_lang_id": True,
                "voice_colors": [{"name": "01: standard", "suffix": "embeds/standard"}],
                "default_voice_color": "01: standard",
            }
        ],
    )
    assert "Voicebank metadata (if available):" in bundle.dynamic_prompt_text
    assert '"gender": "female"' in bundle.dynamic_prompt_text
    assert '"voice_type": "soprano"' in bundle.dynamic_prompt_text
    assert '"languages": [' in bundle.dynamic_prompt_text
    assert '"ja"' in bundle.dynamic_prompt_text
    assert '"use_lang_id": true' in bundle.dynamic_prompt_text


def test_build_prompt_bundle_preserves_unicode_voicebank_names() -> None:
    bundle = build_prompt_bundle(
        tools=[],
        score_available=True,
        voicebank_ids=["Qixuan_v2.7.0_DiffSinger_OpenUtau"],
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        selected_voicebank_id="Qixuan_v2.7.0_DiffSinger_OpenUtau",
        voicebank_details=[
            {
                "id": "Qixuan_v2.7.0_DiffSinger_OpenUtau",
                "name": "Qixuan / 绮萱 v2.7.0",
            }
        ],
    )

    assert "Qixuan / 绮萱 v2.7.0" in bundle.dynamic_prompt_text
    assert r"\u7eee\u8431" not in bundle.dynamic_prompt_text


def test_build_prompt_bundle_includes_canonical_solfege_settings() -> None:
    bundle = build_prompt_bundle(
        tools=[],
        score_available=True,
        solfege_settings={
            "system": "fixed_do",
            "mode": "major",
            "revision": 4,
        },
    )

    assert "Canonical current solfege settings (authoritative):" in bundle.dynamic_prompt_text
    assert "supersede any conflicting statements" in bundle.dynamic_prompt_text
    assert '"system": "fixed_do"' in bundle.dynamic_prompt_text
    assert '"revision": 4' in bundle.dynamic_prompt_text


def test_system_prompt_defaults_to_qixuan_unless_clear_male_lower_part() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=["PM-31_Commercial_Indigo", "Qixuan_v2.7.0_DiffSinger_OpenUtau"],
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
    )
    assert "use `Qixuan_v2.7.0_DiffSinger_OpenUtau` as the default voicebank" in prompt
    assert "tenor, bass, baritone" in prompt
    assert "If there is no clear relationship" in prompt


def test_system_prompt_selects_existing_solfege_verse_and_enables_patch() -> None:
    prompt = build_system_prompt(
        tools=[],
        score_available=True,
        voicebank_ids=["Qixuan_v2.7.0_DiffSinger_OpenUtau"],
        score_summary={"available_verses": ["1", "2"], "selected_verse_number": "1"},
        parsed_score_json={"parts": []},
        voice_part_signals={"parts": []},
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        voicebank_details=None,
    )
    assert "Solfege lyric selection" in prompt
    assert "existing clearly solfege selection" in prompt
    assert "copy the exact `score_summary.parts[].part_id` value" in prompt
    assert "Never use part_index, raw_part_id, part_name" in prompt
    assert "One `add_solfege_lyric_verse` invocation modifies exactly one part" in prompt
    assert "successful tool result explicitly identifies that part" in prompt
    assert "`score_summary.parts[].lyric_selections[]`" in prompt
    assert "`id`, `number`, and `name`" in prompt
    assert "solfege_pronunciation_patch=true" in prompt
    assert "require_solfege_lyrics=true" in prompt
    assert "action=solfege_lyrics_required" in prompt
    assert "`number` may be numeric or alphanumeric" in prompt
    assert "exact chosen `lyric_selection`" in prompt
    assert "call `add_solfege_lyric_verse`" in prompt
    assert "call `modify_solfege_settings`" in prompt
    assert "override conflicting statements in conversation history" in prompt


def test_build_prompt_bundle_expands_selected_voicebank_override() -> None:
    bundle = build_prompt_bundle(
        tools=[],
        score_available=True,
        voicebank_ids=["VoiceA", "VoiceB"],
        score_summary=None,
        parsed_score_json=None,
        voice_part_signals=None,
        preprocess_mapping_context=None,
        last_preprocess_plan=None,
        selected_voicebank_id="VoiceB",
        voicebank_details=[
            {
                "id": "VoiceB",
                "name": "Voice B Display",
                "gender": "female",
                "voice_type": "soprano",
                "voice_colors": [],
                "default_voice_color": None,
            }
        ],
    )
    assert "User-selected voicebank override:" in bundle.dynamic_prompt_text
    assert '"id": "VoiceB"' in bundle.dynamic_prompt_text
    assert '"name": "Voice B Display"' in bundle.dynamic_prompt_text
    assert "VOICEBANK OVERRIDE ACTIVE" in bundle.dynamic_prompt_text
    assert "Do not choose, recommend, or mention another voicebank" in bundle.dynamic_prompt_text
