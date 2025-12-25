import yaml
import numpy as np
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
import soundfile as sf

from src.phonemizer.phonemizer import Phonemizer
from src.acoustic.model import LinguisticModel, DurationModel, PitchModel, VarianceModel, AcousticModel
from src.vocoder.model import Vocoder
from src.musicxml.parser import parse_musicxml, ScoreData, NoteEvent, TempoEvent

@dataclass
class PipelineConfig:
    sample_rate: int = 44100
    hop_size: int = 512
    frame_ms: float = 512 / 44100 * 1000
    use_lang_id: bool = False
    use_variable_depth: bool = False
    max_depth: float = 1.0
    steps: int = 10

class TimeAxis:
    def __init__(self, tempos: List[TempoEvent]):
        self.tempos = sorted(tempos, key=lambda t: t.offset_beats)
        # Precompute ms offsets for each tempo change
        self.ms_offsets = [0.0]
        current_ms = 0.0
        for i in range(len(self.tempos) - 1):
            beats = self.tempos[i+1].offset_beats - self.tempos[i].offset_beats
            duration_ms = beats * 60000.0 / self.tempos[i].bpm
            current_ms += duration_ms
            self.ms_offsets.append(current_ms)

    def get_ms_at_beat(self, beat: float) -> float:
        # Find active tempo
        # Since list is short, linear scan is fine
        idx = 0
        for i, t in enumerate(self.tempos):
            if beat >= t.offset_beats:
                idx = i
            else:
                break
        
        tempo = self.tempos[idx]
        ms_offset = self.ms_offsets[idx]
        beat_offset = beat - tempo.offset_beats
        return ms_offset + (beat_offset * 60000.0 / tempo.bpm)

class Pipeline:
    HEAD_FRAMES = 8
    TAIL_FRAMES = 8
    PADDING_MS = 500.0

    def __init__(self, voicebank_path: Path, device: str = "cpu"):
        self.root = voicebank_path
        self.device = device
        self.logger = logging.getLogger(__name__)
        self.variance_predict_energy = False
        self.variance_predict_breathiness = False
        self.variance_predict_voicing = False
        self.variance_predict_tension = False
        
        self.config = self._load_pipeline_config()
        self.phonemizer = self._init_phonemizer()
        self.spk_embed = self._load_speaker_embed()
        self.linguistic = self._init_linguistic() 
        self.duration = self._init_duration()
        self.pitch = self._init_pitch()
        self.pitch_linguistic = self._init_pitch_linguistic()
        self.variance = self._init_variance()
        self.variance_linguistic = self._init_variance_linguistic()
        self.acoustic = self._init_acoustic()
        self.vocoder = self._init_vocoder()

    def _load_pipeline_config(self) -> PipelineConfig:
        config_path = self.root / "dsconfig.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"Root dsconfig.yaml not found at {config_path}")
        
        data = yaml.safe_load(config_path.read_text())
        return PipelineConfig(
            sample_rate=data.get("sample_rate", 44100),
            hop_size=data.get("hop_size", 512),
            frame_ms=data.get("hop_size", 512) / data.get("sample_rate", 44100) * 1000,
            use_lang_id=data.get("use_lang_id", False),
            use_variable_depth=data.get("use_variable_depth", False),
            max_depth=float(data.get("max_depth", 1.0)),
            steps=int(data.get("steps", 10)),
        )

    def _init_phonemizer(self) -> Phonemizer:
        conf = yaml.safe_load((self.root / "dsconfig.yaml").read_text())
        phonemes_path = (self.root / conf["phonemes"]).resolve()
        languages_path = None
        if "languages" in conf:
            languages_path = (self.root / conf["languages"]).resolve()
        dictionary_path = self._resolve_dictionary_path()
        return Phonemizer(
            phonemes_path=phonemes_path,
            dictionary_path=dictionary_path,
            languages_path=languages_path,
            allow_g2p=True,
        )

    def _init_linguistic(self) -> LinguisticModel:
        # Try dsdur first
        dsdur_path = self.root / "dsdur"
        if dsdur_path.exists():
             conf = yaml.safe_load((dsdur_path / "dsconfig.yaml").read_text())
             ling_path = dsdur_path / conf["linguistic"]
             return LinguisticModel(ling_path.resolve(), self.device)
        # Fallback to dsmain
        dsmain_path = self.root / "dsmain"
        if dsmain_path.exists():
             conf = yaml.safe_load((dsmain_path / "dsconfig.yaml").read_text())
             if "linguistic" in conf:
                ling_path = dsmain_path / conf["linguistic"]
                return LinguisticModel(ling_path.resolve(), self.device)
        raise FileNotFoundError("Could not find Linguistic Model.")

    def _init_duration(self) -> DurationModel:
        dsdur_path = self.root / "dsdur"
        if dsdur_path.exists():
             conf = yaml.safe_load((dsdur_path / "dsconfig.yaml").read_text())
             dur_path = dsdur_path / conf["dur"]
             return DurationModel(dur_path.resolve(), self.device)
        raise FileNotFoundError("Could not find Duration Model.")

    def _init_pitch(self) -> Optional[PitchModel]:
        dspitch_path = self.root / "dspitch"
        if dspitch_path.exists():
             conf = yaml.safe_load((dspitch_path / "dsconfig.yaml").read_text())
             pitch_path = dspitch_path / conf["pitch"]
             return PitchModel(pitch_path.resolve(), self.device)
        self.logger.info("Pitch model not found. Will use naive fallback.")
        return None

    def _init_pitch_linguistic(self) -> Optional[LinguisticModel]:
        dspitch_path = self.root / "dspitch"
        if dspitch_path.exists():
            conf = yaml.safe_load((dspitch_path / "dsconfig.yaml").read_text())
            ling_path = dspitch_path / conf["linguistic"]
            return LinguisticModel(ling_path.resolve(), self.device)
        return None

    def _init_variance(self) -> Optional[VarianceModel]:
        dsvariance_path = self.root / "dsvariance"
        if dsvariance_path.exists():
             conf = yaml.safe_load((dsvariance_path / "dsconfig.yaml").read_text())
             if "variance" in conf:
                 variance_path = dsvariance_path / conf["variance"]
                 self.variance_predict_energy = bool(conf.get("predict_energy", False))
                 self.variance_predict_breathiness = bool(conf.get("predict_breathiness", False))
                 self.variance_predict_voicing = bool(conf.get("predict_voicing", False))
                 self.variance_predict_tension = bool(conf.get("predict_tension", False))
                 return VarianceModel(variance_path.resolve(), self.device)
        self.logger.info("Variance model not found. Will use zero variances.")
        return None

    def _init_variance_linguistic(self) -> Optional[LinguisticModel]:
        dsvariance_path = self.root / "dsvariance"
        if dsvariance_path.exists():
            conf = yaml.safe_load((dsvariance_path / "dsconfig.yaml").read_text())
            ling_path = dsvariance_path / conf["linguistic"]
            return LinguisticModel(ling_path.resolve(), self.device)
        return None

    def _init_acoustic(self) -> AcousticModel:
        conf = yaml.safe_load((self.root / "dsconfig.yaml").read_text())
        if "acoustic" in conf:
             return AcousticModel((self.root / conf["acoustic"]).resolve(), self.device)
        dsmain = self.root / "dsmain"
        if dsmain.exists():
             conf = yaml.safe_load((dsmain / "dsconfig.yaml").read_text())
             return AcousticModel((dsmain / conf["acoustic"]).resolve(), self.device)
        raise FileNotFoundError("Could not find Acoustic Model.")

    def _init_vocoder(self) -> Vocoder:
        conf = yaml.safe_load((self.root / "dsconfig.yaml").read_text())
        if "vocoder" in conf:
             vocoder_path = (self.root / conf["vocoder"]).resolve()
             if vocoder_path.is_dir():
                 vocoder_yaml = vocoder_path / "vocoder.yaml"
                 if vocoder_yaml.exists():
                     vocoder_conf = yaml.safe_load(vocoder_yaml.read_text())
                     model_name = vocoder_conf.get("model")
                     if not model_name:
                         raise FileNotFoundError(
                             f"Missing 'model' in {vocoder_yaml}"
                         )
                     return Vocoder((vocoder_path / model_name).resolve(), self.device)
             return Vocoder(vocoder_path, self.device)
        dsvocoder = self.root / "dsvocoder"
        if dsvocoder.exists():
             vocoder_yaml = dsvocoder / "vocoder.yaml"
             if vocoder_yaml.exists():
                 vocoder_conf = yaml.safe_load(vocoder_yaml.read_text())
                 model_name = vocoder_conf.get("model")
                 if model_name:
                     return Vocoder((dsvocoder / model_name).resolve(), self.device)
             return Vocoder(dsvocoder / "vocoder.onnx", self.device)
        raise FileNotFoundError("Could not find Vocoder.")

    def _resolve_dictionary_path(self) -> Path:
        candidates = [
            self.root / "dsvariance" / "dsdict.yaml",
            self.root / "dsdur" / "dsdict.yaml",
            self.root / "dsdur" / "dsdict-en.yaml",
            self.root / "dsdict.yaml",
        ]
        for path in candidates:
            if path.exists():
                return path.resolve()
        raise FileNotFoundError(
            "Could not locate a phoneme dictionary. Expected one of: "
            "dsvariance/dsdict.yaml, dsdur/dsdict.yaml, dsdur/dsdict-en.yaml."
        )

    def _load_speaker_embed(self, name: Optional[str] = None) -> np.ndarray:
        conf = yaml.safe_load((self.root / "dsconfig.yaml").read_text())
        speakers = conf.get("speakers", [])
        if not speakers:
            raise FileNotFoundError("No speaker embeddings listed in dsconfig.yaml.")
        chosen = speakers[0]
        if name is not None:
            for entry in speakers:
                if name in entry:
                    chosen = entry
                    break
        embed_path = (self.root / chosen)
        if not embed_path.exists():
            embed_path = embed_path.with_suffix(".emb")
        if not embed_path.exists():
            raise FileNotFoundError(
                f"Speaker embedding not found at {embed_path}. "
                "Check the 'speakers' list in dsconfig.yaml."
            )
        data = np.frombuffer(embed_path.read_bytes(), dtype=np.float32)
        return data

    @staticmethod
    def _repeat_embed(embed: np.ndarray, length: int) -> np.ndarray:
        if embed.ndim != 1:
            raise ValueError("Speaker embedding must be a 1D vector.")
        return np.repeat(embed[None, None, :], length, axis=1)

    @staticmethod
    def _build_ph2word(word_div: List[int]) -> np.ndarray:
        ph2word: List[int] = []
        for idx, count in enumerate(word_div, start=1):
            ph2word.extend([idx] * count)
        return np.array(ph2word, dtype=np.int64)

    @staticmethod
    def _align_durations(
        ph_dur_pred: np.ndarray,
        word_div: List[int],
        word_dur: List[int],
    ) -> np.ndarray:
        ph2word = Pipeline._build_ph2word(word_div)
        if ph2word.shape[0] != ph_dur_pred.shape[0]:
            raise ValueError("phoneme durations and word_div are misaligned.")
        ph_dur_pred = np.maximum(ph_dur_pred, 0.0)
        word_dur_in = np.zeros(len(word_div), dtype=np.float32)
        for ph_idx, word_idx in enumerate(ph2word):
            word_dur_in[word_idx - 1] += ph_dur_pred[ph_idx]
        alpha = np.ones_like(word_dur_in)
        for idx, total in enumerate(word_dur_in):
            if total > 0:
                alpha[idx] = word_dur[idx] / total
        ph_dur = np.round(ph_dur_pred * alpha[ph2word - 1]).astype(np.int64)
        # Ensure each phoneme has at least 1 frame.
        ph_dur = np.maximum(ph_dur, 1)
        # Adjust rounding drift per word to match word_dur exactly.
        offset = 0
        for word_idx, count in enumerate(word_div):
            target = word_dur[word_idx]
            span = ph_dur[offset : offset + count]
            diff = target - int(span.sum())
            if diff != 0 and count > 0:
                span[-1] = max(1, span[-1] + diff)
                ph_dur[offset : offset + count] = span
            offset += count
        return ph_dur

    @staticmethod
    def _expand_by_duration(values: List[float], durations: np.ndarray) -> np.ndarray:
        expanded: List[float] = []
        for value, dur in zip(values, durations):
            expanded.extend([value] * int(dur))
        return np.array(expanded, dtype=np.float32)

    @staticmethod
    def _fill_rest_midi(note_midi: np.ndarray, note_rest: np.ndarray) -> np.ndarray:
        if note_midi.size == 0:
            return note_midi
        if note_rest.all():
            return np.full_like(note_midi, 60.0, dtype=np.float32)
        idx = np.where(~note_rest)[0]
        values = note_midi[idx]
        interpolated = np.interp(np.arange(len(note_midi)), idx, values)
        return interpolated.astype(np.float32)

    @staticmethod
    def _dump_debug(debug_dir: Optional[Path], name: str, array: np.ndarray) -> None:
        if debug_dir is None:
            return
        output_dir = debug_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        np.save(output_dir / f"{name}.npy", array)

    @staticmethod
    def _resolve_debug_dir(
        debug_dir: Optional[Path],
        output_path: Optional[Path],
    ) -> Optional[Path]:
        if debug_dir is not None:
            return debug_dir
        if output_path is not None:
            return Path(output_path).parent
        return None

    def infer(
        self,
        score_path: Path,
        output_path: Optional[Path] = None,
        *,
        debug_dir: Optional[Path] = None,
        voice_id: Optional[str] = None,
        stop_after: Optional[str] = None,
    ):
        score = parse_musicxml(score_path, keep_rests=True)
        time_axis = TimeAxis(score.tempos)
        debug_root = self._resolve_debug_dir(debug_dir, output_path)

        notes = score.parts[0].notes
        if not notes:
            raise ValueError("No notes found in the selected MusicXML part.")

        voice_pitches: Dict[str, List[float]] = {}
        for note in notes:
            if note.is_rest or note.voice is None or note.pitch_midi is None:
                continue
            voice_pitches.setdefault(note.voice, []).append(note.pitch_midi)
        selected_voice: Optional[str] = None
        if voice_id is not None:
            if voice_id not in voice_pitches:
                raise ValueError(f"voice_id '{voice_id}' not found in the selected part.")
            selected_voice = voice_id
        elif "1" in voice_pitches:
            selected_voice = "1"
        elif len(voice_pitches) > 1:
            selected_voice = max(
                voice_pitches.items(),
                key=lambda item: sum(item[1]) / len(item[1]),
            )[0]
        elif len(voice_pitches) == 1:
            selected_voice = next(iter(voice_pitches.keys()))

        if selected_voice is not None:
            notes = [note for note in notes if note.voice == selected_voice]
        elif voice_pitches:
            # Fallback to notes with any explicit voice labels.
            notes = [note for note in notes if note.voice is not None]

        if not notes:
            raise ValueError("No notes left after applying voice selection.")

        note_start_frames: List[int] = []
        note_end_frames: List[int] = []
        note_midi_raw: List[float] = []
        note_rest_flags: List[bool] = []

        for note in notes:
            start_ms = time_axis.get_ms_at_beat(note.offset_beats)
            end_ms = time_axis.get_ms_at_beat(note.offset_beats + note.duration_beats)
            start_frame = int(round(start_ms / self.config.frame_ms))
            end_frame = int(round(end_ms / self.config.frame_ms))
            if end_frame <= start_frame:
                end_frame = start_frame + 1
            note_start_frames.append(start_frame)
            note_end_frames.append(end_frame)

            if note.is_rest:
                note_rest_flags.append(True)
                note_midi_raw.append(-1.0)
            else:
                note_rest_flags.append(False)
                note_midi_raw.append(float(note.pitch_midi or 0.0))

        note_midi = self._fill_rest_midi(
            np.array(note_midi_raw, dtype=np.float32),
            np.array(note_rest_flags, dtype=bool),
        )

        word_groups: List[Dict[str, Any]] = []
        current_group: Optional[Dict[str, Any]] = None

        for idx, note in enumerate(notes):
            if note.is_rest:
                current_group = None
                word_groups.append(
                    {
                        "notes": [note],
                        "note_indices": [idx],
                        "is_rest": True,
                    }
                )
                continue

            is_continuation = note.lyric_is_extended or note.tie_type in ("stop", "continue")
            if current_group is None or not is_continuation:
                current_group = {
                    "notes": [note],
                    "note_indices": [idx],
                    "is_rest": False,
                }
                word_groups.append(current_group)
            else:
                current_group["notes"].append(note)
                current_group["note_indices"].append(idx)

        padding_frames = int(round(self.PADDING_MS / self.config.frame_ms))
        sp_id = self.phonemizer._phoneme_to_id["SP"]

        phrase_groups: List[Dict[str, Any]] = [
            {
                "position": note_start_frames[0] - padding_frames,
                "phonemes": ["SP"],
                "ids": [sp_id],
                "lang_ids": [0],
                "tone": float(note_midi[0]),
                "note_idx": None,
            }
        ]
        note_phonemes: Dict[int, List[str]] = {}

        for group in word_groups:
            notes_in_group: List[NoteEvent] = group["notes"]
            note_indices: List[int] = group["note_indices"]
            if group["is_rest"]:
                note_idx = note_indices[0]
                phrase_groups.append(
                    {
                        "position": note_start_frames[note_idx],
                        "phonemes": ["SP"],
                        "ids": [sp_id],
                        "lang_ids": [0],
                        "tone": float(note_midi[note_idx]),
                        "note_idx": note_idx,
                    }
                )
                note_phonemes[note_idx] = ["SP"]
                continue

            lyric = notes_in_group[0].lyric or ""
            ph_res = self.phonemizer.phonemize_tokens([lyric])
            phonemes = list(ph_res.phonemes) or ["SP"]
            if not phonemes:
                phonemes = ["SP"]

            is_vowel = [self.phonemizer.is_vowel(p) for p in phonemes]
            is_glide = [self.phonemizer.is_glide(p) for p in phonemes]
            is_start = [False] * len(phonemes)
            if not any(is_vowel):
                is_start[0] = True
            for i in range(len(phonemes)):
                if is_vowel[i]:
                    if i >= 2 and is_glide[i - 1] and not is_vowel[i - 2]:
                        is_start[i - 1] = True
                    else:
                        is_start[i] = True

            non_extension_indices = [note_indices[0]]
            word_entries: List[Dict[str, Any]] = [
                {
                    "position": None,
                    "phonemes": [],
                    "note_idx": None,
                }
            ]
            note_index = 0
            for idx, phoneme in enumerate(phonemes):
                if is_start[idx] and note_index < len(non_extension_indices):
                    note_idx = non_extension_indices[note_index]
                    note_index += 1
                    word_entries.append(
                        {
                            "position": note_start_frames[note_idx],
                            "phonemes": [],
                            "note_idx": note_idx,
                        }
                    )
                word_entries[-1]["phonemes"].append(phoneme)

            if word_entries[0]["phonemes"]:
                ids = [self.phonemizer._phoneme_to_id[p] for p in word_entries[0]["phonemes"]]
                lang_ids = [
                    self.phonemizer._language_map.get(p.split("/")[0] if "/" in p else "", 0)
                    for p in word_entries[0]["phonemes"]
                ]
                phrase_groups[-1]["phonemes"].extend(word_entries[0]["phonemes"])
                phrase_groups[-1]["ids"].extend(ids)
                phrase_groups[-1]["lang_ids"].extend(lang_ids)
                prev_note_idx = phrase_groups[-1].get("note_idx")
                if prev_note_idx is not None:
                    note_phonemes.setdefault(prev_note_idx, []).extend(word_entries[0]["phonemes"])

            for entry in word_entries[1:]:
                entry_phonemes = entry["phonemes"]
                if not entry_phonemes:
                    continue
                ids = [self.phonemizer._phoneme_to_id[p] for p in entry_phonemes]
                lang_ids = [
                    self.phonemizer._language_map.get(p.split("/")[0] if "/" in p else "", 0)
                    for p in entry_phonemes
                ]
                note_idx = entry["note_idx"]
                phrase_groups.append(
                    {
                        "position": entry["position"],
                        "phonemes": entry_phonemes,
                        "ids": ids,
                        "lang_ids": lang_ids,
                        "tone": float(note_midi[note_idx]),
                        "note_idx": note_idx,
                    }
                )
                if note_idx is not None:
                    note_phonemes.setdefault(note_idx, []).extend(entry_phonemes)

        phrase_groups.append(
            {
                "position": note_end_frames[-1],
                "phonemes": [],
                "ids": [],
                "lang_ids": [],
                "tone": float(note_midi[-1]),
                "note_idx": None,
            }
        )

        input_tokens = [pid for group in phrase_groups[:-1] for pid in group["ids"]]
        input_languages = [lid for group in phrase_groups[:-1] for lid in group["lang_ids"]]
        input_word_div = [len(group["ids"]) for group in phrase_groups[:-1]]
        positions = [group["position"] for group in phrase_groups]
        input_word_dur = [
            max(1, positions[i + 1] - positions[i])
            for i in range(len(positions) - 1)
        ]

        tokens_tensor = np.array(input_tokens, dtype=np.int64)[None, :]
        languages_tensor = np.array(input_languages, dtype=np.int64)[None, :]
        word_div_tensor = np.array(input_word_div, dtype=np.int64)[None, :]
        word_dur_tensor = np.array(input_word_dur, dtype=np.int64)[None, :]

        ling_inputs = {
            "tokens": tokens_tensor,
            "languages": languages_tensor if self.config.use_lang_id else np.zeros_like(tokens_tensor),
            "word_div": word_div_tensor,
            "word_dur": word_dur_tensor,
        }
        ling_out = self.linguistic.run(ling_inputs)
        encoder_out = ling_out[0]
        x_masks = ling_out[1]
        self._dump_debug(debug_root, "tokens", tokens_tensor)
        self._dump_debug(debug_root, "languages", languages_tensor)
        self._dump_debug(debug_root, "word_div", word_div_tensor)
        self._dump_debug(debug_root, "word_dur", word_dur_tensor)
        self._dump_debug(debug_root, "encoder_out", encoder_out)
        self._dump_debug(debug_root, "x_masks", x_masks.astype(np.int8))
        if stop_after == "linguistic":
            return

        ph_midi: List[int] = []
        for group in phrase_groups[:-1]:
            tone = int(round(group["tone"]))
            ph_midi.extend([tone] * len(group["ids"]))
        ph_midi_tensor = np.array(ph_midi, dtype=np.int64)[None, :]

        spk_embed_tokens = self._repeat_embed(self.spk_embed, tokens_tensor.shape[1]).astype(np.float32)
        duration_out = self.duration.forward(encoder_out, x_masks, ph_midi_tensor, spk_embed_tokens)
        ph_dur_pred = duration_out[0]
        ph_durations = self._align_durations(ph_dur_pred, input_word_div, input_word_dur)
        self._dump_debug(debug_root, "ph_midi", ph_midi_tensor)
        self._dump_debug(debug_root, "ph_dur_pred", ph_dur_pred.astype(np.float32))
        self._dump_debug(debug_root, "ph_durations", ph_durations.astype(np.int64))
        if stop_after == "duration":
            return

        if ph_durations.shape[0] <= 1:
            raise ValueError("Phoneme durations are too short to build pitch inputs.")
        ph_durations_core = ph_durations[1:]
        ph_durations_pitch = np.concatenate(
            (
                np.array([self.HEAD_FRAMES], dtype=np.int64),
                ph_durations_core.astype(np.int64),
                np.array([self.TAIL_FRAMES], dtype=np.int64),
            )
        )
        n_frames = int(ph_durations_pitch.sum())
        if n_frames <= 0:
            raise ValueError("Predicted durations produced zero frames.")

        pitch_tokens_tensor = np.array(input_tokens + [sp_id], dtype=np.int64)[None, :]
        pitch_languages_tensor = np.array(input_languages + [0], dtype=np.int64)[None, :]

        start_frame = note_start_frames[0] - self.HEAD_FRAMES
        note_dur = [max(1, note_start_frames[0] - start_frame)]
        for idx in range(len(note_start_frames) - 1):
            note_dur.append(max(1, note_start_frames[idx + 1] - note_start_frames[idx]))
        note_dur.append(0)
        note_dur[-1] = max(1, n_frames - sum(note_dur[:-1]))
        note_midi_pitch = np.concatenate(([note_midi[0]], note_midi)).astype(np.float32)
        computed_note_rest: List[bool] = []
        prev_rest = True
        for idx, note in enumerate(notes):
            is_extension = note.lyric_is_extended or note.tie_type in ("stop", "continue")
            if is_extension and idx > 0:
                computed_note_rest.append(prev_rest)
                continue
            phs = note_phonemes.get(idx, [])
            is_rest = (not phs) or all(
                ph == "AP" or ph == "SP" or not self.phonemizer.is_vowel(ph)
                for ph in phs
            )
            computed_note_rest.append(is_rest)
            prev_rest = is_rest
        note_rest_pitch = np.concatenate(([True], np.array(computed_note_rest, dtype=bool)))

        base_midi = self._expand_by_duration(note_midi_pitch.tolist(), np.array(note_dur))
        base_midi = base_midi[:n_frames]
        if base_midi.shape[0] < n_frames:
            base_midi = np.pad(base_midi, (0, n_frames - base_midi.shape[0]), mode="edge")

        spk_embed_frames = self._repeat_embed(self.spk_embed, n_frames).astype(np.float32)
        expr = np.ones((1, n_frames), dtype=np.float32)
        retake = np.ones((1, n_frames), dtype=bool)

        if self.pitch:
            pitch_encoder_out = encoder_out
            if self.pitch_linguistic is not None:
                pitch_ling_inputs = {
                    "tokens": pitch_tokens_tensor,
                    "languages": pitch_languages_tensor if self.config.use_lang_id else np.zeros_like(pitch_tokens_tensor),
                    "ph_dur": ph_durations_pitch[None, :].astype(np.int64),
                }
                pitch_encoder_out = self.pitch_linguistic.run(pitch_ling_inputs)[0]
            pitch_inputs = {
                "encoder_out": pitch_encoder_out,
                "ph_dur": ph_durations_pitch[None, :].astype(np.int64),
                "note_midi": note_midi_pitch[None, :].astype(np.float32),
                "note_rest": note_rest_pitch[None, :],
                "note_dur": np.array(note_dur, dtype=np.int64)[None, :],
                "pitch": np.full((1, n_frames), 60.0, dtype=np.float32),
                "expr": expr,
                "retake": retake,
                "spk_embed": spk_embed_frames,
                "steps": np.array(self.config.steps, dtype=np.int64),
            }
            pitch_pred = self.pitch.run(pitch_inputs)[0]
            pitch_midi = pitch_pred.astype(np.float32)
            f0 = 440.0 * (2.0 ** ((pitch_midi - 69.0) / 12.0))
        else:
            ph_midi_pitch = [float(ph_midi[0])] + [float(m) for m in ph_midi[1:]] + [float(ph_midi[-1])]
            pitch_midi = self._expand_by_duration(ph_midi_pitch, ph_durations_pitch)[None, :]
            f0 = 440.0 * (2.0 ** ((pitch_midi - 69.0) / 12.0))
        pitch_semitone = pitch_midi.astype(np.float32)
        self._dump_debug(debug_root, "note_midi", note_midi_pitch)
        self._dump_debug(debug_root, "note_rest", note_rest_pitch)
        self._dump_debug(debug_root, "note_dur", np.array(note_dur, dtype=np.int64))
        self._dump_debug(debug_root, "base_midi", base_midi)
        if self.pitch:
            self._dump_debug(debug_root, "pitch_pred", pitch_pred.astype(np.float32))
        self._dump_debug(debug_root, "f0", f0.astype(np.float32))
        if stop_after == "pitch":
            return

        if self.variance:
            variance_encoder_out = encoder_out
            if self.variance_linguistic is not None:
                variance_ling_inputs = {
                    "tokens": pitch_tokens_tensor,
                    "languages": pitch_languages_tensor if self.config.use_lang_id else np.zeros_like(pitch_tokens_tensor),
                    "ph_dur": ph_durations_pitch[None, :].astype(np.int64),
                }
                variance_encoder_out = self.variance_linguistic.run(variance_ling_inputs)[0]
            num_variances = sum(
                [
                    int(self.variance_predict_energy),
                    int(self.variance_predict_breathiness),
                    int(self.variance_predict_voicing),
                    int(self.variance_predict_tension),
                ]
            )
            if num_variances <= 0:
                num_variances = 3
            variance_inputs = {
                "encoder_out": variance_encoder_out,
                "ph_dur": ph_durations_pitch[None, :].astype(np.int64),
                "pitch": pitch_semitone,
                "breathiness": np.zeros((1, n_frames), dtype=np.float32),
                "voicing": np.zeros((1, n_frames), dtype=np.float32),
                "tension": np.zeros((1, n_frames), dtype=np.float32),
                "retake": np.ones((1, n_frames, num_variances), dtype=bool),
                "spk_embed": spk_embed_frames,
                "steps": np.array(self.config.steps, dtype=np.int64),
            }
            if self.variance_predict_energy:
                variance_inputs["energy"] = np.zeros((1, n_frames), dtype=np.float32)
            variance_out = self.variance.run(variance_inputs)
            breathiness, voicing, tension = variance_out
        else:
            breathiness = np.zeros((1, n_frames), dtype=np.float32)
            voicing = np.zeros((1, n_frames), dtype=np.float32)
            tension = np.zeros((1, n_frames), dtype=np.float32)
        self._dump_debug(debug_root, "breathiness", breathiness.astype(np.float32))
        self._dump_debug(debug_root, "voicing", voicing.astype(np.float32))
        self._dump_debug(debug_root, "tension", tension.astype(np.float32))
        if stop_after == "variance":
            return

        depth = self.config.max_depth if self.config.use_variable_depth else 1.0
        acoustic_inputs = {
            "tokens": pitch_tokens_tensor,
            "languages": pitch_languages_tensor if self.config.use_lang_id else np.zeros_like(pitch_tokens_tensor),
            "durations": ph_durations_pitch[None, :].astype(np.int64),
            "f0": f0.astype(np.float32),
            "breathiness": breathiness.astype(np.float32),
            "voicing": voicing.astype(np.float32),
            "tension": tension.astype(np.float32),
            "gender": np.zeros((1, n_frames), dtype=np.float32),
            "velocity": np.ones((1, n_frames), dtype=np.float32),
            "spk_embed": spk_embed_frames,
            "depth": np.array(depth, dtype=np.float32),
            "steps": np.array(self.config.steps, dtype=np.int64),
        }

        mel = self.acoustic.run(acoustic_inputs)[0]
        self._dump_debug(debug_root, "mel", mel.astype(np.float32))
        if stop_after == "acoustic":
            return
        wav = self.vocoder.forward(mel, f0.astype(np.float32))
        self._dump_debug(debug_root, "wav", wav.astype(np.float32))
        if output_path is None:
            raise ValueError("output_path is required when stop_after is not set.")
        sf.write(output_path, wav.flatten(), self.config.sample_rate)
        
    def _naive_pitch(self, ph_midi: List[int], ph_durations: np.ndarray) -> np.ndarray:
        # Construct F0 curve from MIDI values
        f0 = []
        for midi, dur in zip(ph_midi, ph_durations):
            freq = 440.0 * (2.0 ** ((midi - 69.0) / 12.0)) if midi > 0 else 0.0
            f0.extend([freq] * dur)
        return np.array(f0)
