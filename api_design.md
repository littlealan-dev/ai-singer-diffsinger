# API Design

This document reflects the **current** MCP tool surface and internal pipeline APIs.
MCP tools appear first; internal APIs are listed last.

---

## MCP Tools (Public)

These are the tools exposed via `tools/list` in the MCP server.

### `parse_score`
Parse MusicXML into a score JSON.

**Input parameters**
| name | type | required | default | description | example |
| --- | --- | --- | --- | --- | --- |
| `file_path` | string | yes | n/a | Local path to a MusicXML/MXL file. | `assets/test_data/amazing-grace.mxl` |
| `part_id` | string\|null | no | `null` | Part ID to extract (overrides `part_index` when provided). | `P1` |
| `part_index` | integer\|null | no | `null` | Zero-based part index to extract when `part_id` is not set. | `0` |
| `verse_number` | integer\|string\|null | no | `null` | Verse number to select; `null` keeps all verses. | `1` |
| `expand_repeats` | boolean | no | `false` | Whether to expand repeats/voltas in the parsed score. | `true` |

**Output structure**
| field | type | description | example |
| --- | --- | --- | --- |
| `title` | string\|null | Score title. | `Amazing Grace` |
| `tempos` | array[object] | Tempo change list with `offset_beats`, `bpm`. | `[{"offset_beats":0,"bpm":90}]` |
| `parts` | array[object] | Parsed parts with notes and lyrics. | `[{"part_id":"P1",...}]` |
| `structure` | object | Repeat/endings/jump structure. | `{"repeats":[],"endings":[],"jumps":[]}` |
| `score_summary` | object | Lightweight summary (parts, verses, composer). | `{...}` |

**Example output**
```json
{
  "title": "Amazing Grace",
  "tempos": [
    {"offset_beats": 0.0, "bpm": 90.0}
  ],
  "parts": [
    {
      "part_id": "P1",
      "part_name": "Soprano",
      "notes": [
        {
          "offset_beats": 0.0,
          "duration_beats": 1.0,
          "measure_number": 1,
          "voice": "1",
          "pitch_midi": 69,
          "pitch_hz": 440.0,
          "lyric": "A-",
          "syllabic": "begin",
          "lyric_is_extended": false,
          "is_rest": false,
          "tie_type": null
        }
      ]
    }
  ],
  "structure": {"repeats": [], "endings": [], "jumps": []},
  "score_summary": {
    "title": "Amazing Grace",
    "composer": "John Newton",
    "parts": [
      {"part_index": 0, "part_id": "P1", "part_name": "Soprano", "has_lyrics": true, "note_count": 128}
    ],
    "available_verses": ["1"]
  }
}
```

---

### `modify_score`
Execute sandboxed Python to mutate a score JSON.

**Input parameters**
| name | type | required | default | description | example |
| --- | --- | --- | --- | --- | --- |
| `score` | object | yes | n/a | Parsed score JSON from `parse_score`. | `{ "title": "Amazing Grace", ... }` |
| `code` | string | yes | n/a | Python snippet that mutates `score` in-place. | `score["title"] = "Demo"` |

**Output structure**
| field | type | description | example |
| --- | --- | --- | --- |
| `score` | object | Mutated score JSON (same schema as `parse_score` output). | `{ "title": "Demo", ... }` |

**Example output**
```json
{
  "title": "Demo",
  "tempos": [...],
  "parts": [...],
  "structure": {...},
  "score_summary": {...}
}
```

---

### `save_audio`
Save waveform to disk and return file metadata.

**Input parameters**
| name | type | required | default | description | example |
| --- | --- | --- | --- | --- | --- |
| `waveform` | array[number] | yes | n/a | Mono PCM samples in the range -1..1. | `[0.0, 0.0012, -0.0008]` |
| `output_path` | string | yes | n/a | Target output path (extension is normalized). | `data/output/demo.wav` |
| `sample_rate` | integer | no | `44100` | Sample rate to write. | `44100` |
| `format` | string | no | `wav` | Output format: `wav` or `mp3`. | `"mp3"` |
| `mp3_bitrate` | string | no | `256k` | MP3 bitrate when `format=mp3`. | `"256k"` |
| `keep_wav` | boolean | no | `false` | When writing mp3, keep the wav as well. | `false` |

**Output structure**
| field | type | description | example |
| --- | --- | --- | --- |
| `path` | string | Absolute path of the saved audio file. | `/app/data/output/demo.mp3` |
| `duration_seconds` | number | Audio duration in seconds. | `12.34` |
| `sample_rate` | integer | Sample rate used. | `44100` |

**Example output**
```json
{
  "path": "/app/data/output/demo.mp3",
  "duration_seconds": 12.34,
  "sample_rate": 44100
}
```

---

### `synthesize`
Convenience end-to-end synthesis (runs internal steps 2–6).

**Input parameters**
| name | type | required | default | description | example |
| --- | --- | --- | --- | --- | --- |
| `score` | object | yes | n/a | Parsed score JSON from `parse_score`. | `{ "title": "Amazing Grace", ... }` |
| `voicebank` | string | no | `null` | Voicebank ID (defaults to configured primary). | `Raine_Rena_2.01` |
| `part_id` | string\|null | no | `null` | Part ID to sing (overrides `part_index`). | `P1` |
| `part_index` | integer\|null | no | `null` | Zero-based part index when `part_id` not set. | `0` |
| `verse_number` | integer\|string\|null | no | `null` | Verse number to synthesize. | `1` |
| `voice_id` | string\|null | no | `null` | Speaker ID when multiple speakers exist. | `Rena` |
| `voice_color` | string\|null | no | `null` | Voice color preset name/suffix. | `"02: soft"` |
| `articulation` | number (-1..1) | no | `0.0` | Controls timing tightness. | `0.2` |
| `airiness` | number (0..1) | no | `0.0` | Controls breathiness. | `0.3` |
| `intensity` | number (0..1) | no | `0.5` | Controls vocal intensity. | `0.5` |
| `clarity` | number (0..1) | no | `0.0` | Controls clarity (formant/brightness). | `0.6` |

**Output structure**
| field | type | description | example |
| --- | --- | --- | --- |
| `waveform` | array[number] | PCM samples in -1..1. | `[0.0, 0.0012, -0.0008]` |
| `sample_rate` | integer | Sample rate of the waveform. | `44100` |
| `duration_seconds` | number | Audio duration in seconds. | `12.34` |

**Example output**
```json
{
  "waveform": [0.0, 0.0012, -0.0008],
  "sample_rate": 44100,
  "duration_seconds": 12.34
}
```

---

### `list_voicebanks`
List available voicebanks.

**Input parameters**
| name | type | required | default | description | example |
| --- | --- | --- | --- | --- | --- |
| `search_path` | string | no | `null` | Root path to scan; defaults to configured assets path. | `assets/voicebanks` |

**Output structure**
| field | type | description | example |
| --- | --- | --- | --- |
| `[]` | array[object] | List of voicebanks with `id`, `name`, `path`. | `[{"id":"Raine_Rena_2.01",...}]` |

**Example output**
```json
[
  {"id": "Raine_Rena_2.01", "name": "Raine Rena", "path": "assets/voicebanks/Raine_Rena_2.01"},
  {"id": "Raine_Reizo_2.01", "name": "Raine Reizo", "path": "assets/voicebanks/Raine_Reizo_2.01"}
]
```

---

### `get_voicebank_info`
Return capabilities for a voicebank.

**Input parameters**
| name | type | required | default | description | example |
| --- | --- | --- | --- | --- | --- |
| `voicebank` | string | yes | n/a | Voicebank ID or path. | `Raine_Rena_2.01` |

**Output structure**
| field | type | description | example |
| --- | --- | --- | --- |
| `name` | string | Voicebank name. | `Raine Rena` |
| `languages` | array[string] | Supported language codes. | `["en", "ja", "zh"]` |
| `has_duration_model` | boolean | Duration model availability. | `true` |
| `has_pitch_model` | boolean | Pitch model availability. | `true` |
| `has_variance_model` | boolean | Variance model availability. | `true` |
| `speakers` | array[string] | Speaker IDs. | `["Rena"]` |
| `voice_colors` | array[object] | Voice color presets with `name`/`suffix`. | `[{"name":"Soft","suffix":"_soft"}]` |
| `default_voice_color` | string\|null | Default voice color. | `"Normal"` |
| `sample_rate` | integer | Audio sample rate. | `44100` |
| `hop_size` | integer | Hop size used in vocoder. | `512` |
| `use_lang_id` | boolean | Whether language IDs are used. | `true` |

**Example output**
```json
{
  "name": "Raine Rena",
  "languages": ["en", "ja", "zh"],
  "has_duration_model": true,
  "has_pitch_model": true,
  "has_variance_model": true,
  "speakers": ["Rena"],
  "voice_colors": [{"name": "Normal", "suffix": ""}, {"name": "Soft", "suffix": "_soft"}],
  "default_voice_color": "Normal",
  "sample_rate": 44100,
  "hop_size": 512,
  "use_lang_id": true
}
```

---

## Internal Pipeline APIs (Not Exposed via MCP)

These functions are used internally by `synthesize`.

### `phonemize`
- Input: `lyrics`, `voicebank`, `language`
- Output: `phonemes`, `phoneme_ids`, `language_ids`, `word_boundaries`

### `align_phonemes_to_notes`
- Input: `score`, `voicebank`, `part_id/part_index`, `verse_number`, `voice_id`
- Output: aligned phonemes, word boundaries, note timing

### `predict_durations`
- Input: phoneme IDs + note timing
- Output: per-phoneme durations

### `predict_pitch`
- Input: phoneme IDs + durations + note pitches
- Output: f0 curve (+ pitch_midi if available)

### `predict_variance`
- Input: phoneme IDs + durations + f0
- Output: breathiness, tension, voicing curves

### `synthesize_audio`
- Input: phonemes + durations + f0 + variance curves
- Output: waveform + sample rate
