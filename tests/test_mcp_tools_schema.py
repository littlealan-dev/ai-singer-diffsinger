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


def test_metadata_tools_have_field_descriptions() -> None:
    schema_map = _tool_schema_map()

    list_voicebanks = schema_map["list_voicebanks"]
    assert list_voicebanks["inputSchema"]["properties"]["search_path"]["description"]
    assert list_voicebanks["outputSchema"]["items"]["description"]

    voicebank_info = schema_map["get_voicebank_info"]["outputSchema"]
    assert voicebank_info["description"]
    assert voicebank_info["properties"]["speakers"]["items"]["description"]

    estimate_credits = schema_map["estimate_credits"]["outputSchema"]
    assert estimate_credits["description"]
    assert estimate_credits["properties"]["estimated_credits"]["description"]
