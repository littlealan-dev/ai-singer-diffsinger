from src.mcp.tools import list_tools


def _tool_schema_map():
    return {tool["name"]: tool for tool in list_tools()}


def test_preprocess_output_schema_exposes_detailed_fields() -> None:
    schema = _tool_schema_map()["preprocess_voice_parts"]["outputSchema"]
    properties = schema["properties"]

    assert properties["status"]["description"]
    assert properties["validation"]["description"]
    assert properties["targets"]["description"]
    assert properties["targets"]["items"]["properties"]["quality_class"]["description"]
    assert properties["failed_validation_rules"]["items"]["properties"]["rule_name"]["description"]
    assert properties["review_materialization"]["properties"]["transformed_part"]["description"]


def test_synthesize_schema_requires_exactly_one_selector_and_describes_output() -> None:
    tool = _tool_schema_map()["synthesize"]
    input_schema = tool["inputSchema"]
    output_schema = tool["outputSchema"]

    assert input_schema["oneOf"] == [{"required": ["part_id"]}, {"required": ["part_index"]}]
    assert input_schema["properties"]["score"]["description"]
    assert output_schema["description"]
    assert len(output_schema["oneOf"]) == 2
    assert output_schema["oneOf"][0]["description"]
    assert output_schema["oneOf"][1]["description"]


def test_parse_score_schema_describes_per_part_lyric_verse_samples() -> None:
    output_schema = _tool_schema_map()["parse_score"]["outputSchema"]
    summary_schema = output_schema["properties"]["score_summary"]
    part_schema = summary_schema["properties"]["parts"]["items"]
    verse_schema = part_schema["properties"]["lyric_verses"]["items"]

    assert verse_schema["required"] == ["verse_number", "sample"]
    assert verse_schema["properties"]["sample"]["maxItems"] == 20
    assert "First 20" in verse_schema["properties"]["sample"]["description"]


def test_solfege_tools_expose_add_and_modify_contracts() -> None:
    schemas = _tool_schema_map()
    add_schema = schemas["add_solfege_lyric_verse"]["inputSchema"]
    modify_schema = schemas["modify_solfege_settings"]["inputSchema"]

    assert len(add_schema["oneOf"]) == 2
    assert add_schema["oneOf"][0]["properties"]["part_id"]["type"] == "string"
    assert "score_summary.parts[].part_id" in add_schema["properties"]["part_id"]["description"]
    assert "not part_name" in add_schema["oneOf"][0]["properties"]["part_id"]["description"]
    assert "instead of guessing" in add_schema["properties"]["part_index"]["description"]
    assert "exactly one selected clean part" in schemas["add_solfege_lyric_verse"]["description"]
    assert "one successful invocation per part" in schemas["add_solfege_lyric_verse"]["description"]
    assert modify_schema["properties"]["system"]["enum"] == [
        "movable_do",
        "fixed_do",
        None,
    ]
    assert len(modify_schema["anyOf"]) == 2


def test_metadata_tools_have_field_descriptions() -> None:
    schema_map = _tool_schema_map()

    list_voicebanks = schema_map["list_voicebanks"]
    assert list_voicebanks["inputSchema"]["properties"]["search_path"]["description"]
    assert list_voicebanks["outputSchema"]["items"]["description"]

    voicebank_info = schema_map["get_voicebank_info"]["outputSchema"]
    assert voicebank_info["description"]
    assert voicebank_info["properties"]["speakers"]["items"]["description"]

    assert "estimate_credits" not in schema_map
