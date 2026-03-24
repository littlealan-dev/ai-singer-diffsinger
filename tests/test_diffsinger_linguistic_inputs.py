import unittest

from src.api.diffsinger_linguistic_inputs import (
    DiffSingerLinguisticContract,
    DiffSingerLinguisticFeatures,
    build_linguistic_inputs,
    classify_linguistic_contract,
    resolve_default_language_id,
    run_linguistic_model,
)


class _FakeLinguisticModel:
    def __init__(self, input_names):
        self.input_names = list(input_names)
        self.last_inputs = None
        self.model_path = "fake.onnx"

    def run(self, inputs):
        self.last_inputs = inputs
        return ["encoder_out", "x_masks"]


class TestDiffSingerLinguisticInputs(unittest.TestCase):
    def test_classify_tokens_word(self):
        contract = classify_linguistic_contract(["tokens", "word_div", "word_dur"])
        self.assertEqual(contract, DiffSingerLinguisticContract.TOKENS_WORD)

    def test_classify_tokens_word_lang(self):
        contract = classify_linguistic_contract(
            ["languages", "tokens", "word_dur", "word_div"]
        )
        self.assertEqual(contract, DiffSingerLinguisticContract.TOKENS_WORD_LANG)

    def test_classify_tokens_phdur(self):
        contract = classify_linguistic_contract(["tokens", "ph_dur"])
        self.assertEqual(contract, DiffSingerLinguisticContract.TOKENS_PHDUR)

    def test_classify_unsupported_contract_raises(self):
        with self.assertRaisesRegex(ValueError, "Unsupported linguistic input contract"):
            classify_linguistic_contract(["tokens", "foo"])

    def test_resolve_default_language_prefers_other(self):
        resolved = resolve_default_language_id({"other": 1, "en": 2}, active_language="en")
        self.assertEqual(resolved, 1)

    def test_resolve_default_language_uses_active_language(self):
        resolved = resolve_default_language_id({"en": 2, "ja": 7}, active_language="en")
        self.assertEqual(resolved, 2)

    def test_resolve_default_language_falls_back_to_smallest_id(self):
        resolved = resolve_default_language_id({"ja": 7, "en": 2})
        self.assertEqual(resolved, 2)

    def test_build_word_inputs(self):
        features = DiffSingerLinguisticFeatures(
            phoneme_ids=[1, 2, 3, 4],
            word_boundaries=[1, 3],
            word_durations=[5, 9],
        )
        inputs = build_linguistic_inputs(
            DiffSingerLinguisticContract.TOKENS_WORD,
            features,
            use_lang_id=False,
        )
        self.assertEqual(inputs["tokens"].tolist(), [[1, 2, 3, 4]])
        self.assertEqual(inputs["word_div"].tolist(), [[1, 3]])
        self.assertEqual(inputs["word_dur"].tolist(), [[5, 9]])

    def test_build_language_inputs_uses_default_map_when_explicit_ids_absent(self):
        features = DiffSingerLinguisticFeatures(
            phoneme_ids=[1, 2, 3],
            word_boundaries=[3],
            word_durations=[9],
            language_map={"other": 1},
        )
        inputs = build_linguistic_inputs(
            DiffSingerLinguisticContract.TOKENS_WORD_LANG,
            features,
            use_lang_id=True,
        )
        self.assertEqual(inputs["languages"].tolist(), [[1, 1, 1]])

    def test_build_language_inputs_zeroes_when_use_lang_id_disabled(self):
        features = DiffSingerLinguisticFeatures(
            phoneme_ids=[1, 2, 3],
            language_ids=[0, 0, 0],
            word_boundaries=[3],
            word_durations=[9],
            language_map={"other": 1},
        )
        inputs = build_linguistic_inputs(
            DiffSingerLinguisticContract.TOKENS_WORD_LANG,
            features,
            use_lang_id=False,
        )
        self.assertEqual(inputs["languages"].tolist(), [[0, 0, 0]])

    def test_build_language_inputs_ignores_zero_placeholders_when_zero_not_valid(self):
        features = DiffSingerLinguisticFeatures(
            phoneme_ids=[1, 2, 3],
            language_ids=[0, 0, 0],
            word_boundaries=[3],
            word_durations=[9],
            language_map={"other": 1},
        )
        inputs = build_linguistic_inputs(
            DiffSingerLinguisticContract.TOKENS_WORD_LANG,
            features,
            use_lang_id=True,
        )
        self.assertEqual(inputs["languages"].tolist(), [[1, 1, 1]])

    def test_build_language_inputs_raises_when_required_but_unresolvable(self):
        features = DiffSingerLinguisticFeatures(
            phoneme_ids=[1, 2, 3],
            word_boundaries=[3],
            word_durations=[9],
        )
        with self.assertRaisesRegex(ValueError, "requires language IDs"):
            build_linguistic_inputs(
                DiffSingerLinguisticContract.TOKENS_WORD_LANG,
                features,
                use_lang_id=True,
            )

    def test_build_phdur_inputs(self):
        features = DiffSingerLinguisticFeatures(
            phoneme_ids=[1, 2, 3],
            phoneme_durations=[2, 4, 6],
        )
        inputs = build_linguistic_inputs(
            DiffSingerLinguisticContract.TOKENS_PHDUR,
            features,
            use_lang_id=False,
        )
        self.assertEqual(inputs["ph_dur"].tolist(), [[2, 4, 6]])

    def test_rest_group_is_valid_word_group(self):
        features = DiffSingerLinguisticFeatures(
            phoneme_ids=[7],
            word_boundaries=[1],
            word_durations=[3],
        )
        inputs = build_linguistic_inputs(
            DiffSingerLinguisticContract.TOKENS_WORD,
            features,
            use_lang_id=False,
        )
        self.assertEqual(inputs["word_div"].tolist(), [[1]])
        self.assertEqual(inputs["word_dur"].tolist(), [[3]])

    def test_run_linguistic_model_routes_by_contract(self):
        model = _FakeLinguisticModel(["tokens", "word_div", "word_dur"])
        features = DiffSingerLinguisticFeatures(
            phoneme_ids=[1, 2, 3],
            word_boundaries=[1, 2],
            word_durations=[4, 6],
        )
        outputs = run_linguistic_model(model, features, use_lang_id=False)
        self.assertEqual(outputs, ["encoder_out", "x_masks"])
        self.assertEqual(sorted(model.last_inputs.keys()), ["tokens", "word_div", "word_dur"])
        self.assertEqual(model.last_inputs["word_div"].tolist(), [[1, 2]])


if __name__ == "__main__":
    unittest.main()
