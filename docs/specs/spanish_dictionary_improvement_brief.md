# Qixuan Spanish Approximation Dictionary — Suggested Changes

## Goal

Improve the intelligibility and Spanish character of `dsdict-es.yaml` while continuing to use only Qixuan's officially supported EN, JA and ZH phoneme inventories.

The current dictionary is already understandable, but the generated singing has three main weaknesses:

1. Some word boundaries and consonants are too soft.
2. Spanish single `r`, initial `r`, and rolled `rr` are not sufficiently distinguished.
3. Spanish diphthongs and vowel glides can sound too syllabified.

Do not add any `es/*` phonemes.

---

## 1. Safe changes to implement now

These changes use phonemes that are already declared in the current dictionary.

### 1.1 Map nonsyllabic glides to semivowels

Change:

```yaml
- {from: I, to: ja/i}
- {from: U, to: ja/u}
```

To:

```yaml
- {from: I, to: ja/y}
- {from: U, to: ja/w}
```

Reason:

- Uppercase `I` and `U` from the Spanish G2P are likely nonsyllabic glide variants.
- Mapping them to full vowels can create an overly separated pronunciation.
- `ja/y` and `ja/w` should produce smoother diphthongs and linked vowels.

Important: add a unit/debug test that prints the raw OpenUtau `g2p-es` output for words such as `cielo`, `pies`, `Triana`, `escapulario`, `devoción`, `cual`, and `nuestro`. Confirm that uppercase `I` and `U` are actually emitted as nonsyllabic glides before deploying globally.

### 1.2 Do not globally map `rr` to one `ja/r`

Current:

```yaml
- {from: r, to: ja/r}
- {from: rr, to: ja/r}
```

Keep the single tap:

```yaml
- {from: r, to: ja/r}
```

For `rr`, use one of the following approaches, in order of preference:

1. If the replacement engine accepts a phoneme list as `to`, use:

```yaml
- {from: rr, to: [ja/r, ja/r]}
```

2. If replacement values must be scalar, remove the global `rr` replacement and add lexical overrides for common words containing initial strong `r` or `rr`.

Do not silently leave `rr` mapped to a single `ja/r`, because that makes `pero` and `perro` effectively identical.

### 1.3 Keep the current five vowel mappings

Do not change:

```yaml
- {from: a, to: ja/a}
- {from: e, to: ja/e}
- {from: i, to: ja/i}
- {from: o, to: ja/o}
- {from: u, to: ja/u}
```

These vowels are one of the strongest parts of the current Spanish approximation.

### 1.4 Keep seseo and yeísmo as the default

Keep:

```yaml
- {from: ll, to: ja/y}
- {from: y, to: ja/y}
- {from: Y, to: ja/y}
- {from: z, to: ja/s}
```

This is a reasonable broad Latin-American-style default and avoids unnecessary regional complexity in the first version.

---

## 2. Add targeted lexical overrides

Lexical overrides are preferable where a global substitution would be too risky.

Add the following entries after the existing curated overrides. These entries use only phonemes already declared in the current dictionary.

```yaml
# Strong initial Spanish r approximated with two short Japanese taps.
- grapheme: rosa
  phonemes: [ja/r, ja/r, ja/o, ja/s, ja/a]

# Preserve a compact glide in cielo instead of three equally separate vowels.
- grapheme: cielo
  phonemes: [ja/s, ja/y, ja/e, en/l, ja/o]

# Preserve the glide in pies and retain the final s.
- grapheme: pies
  phonemes: [ja/p, ja/y, ja/e, ja/s]

# Avoid inserting a vowel between t and r in Triana.
- grapheme: triana
  phonemes: [ja/t, ja/r, ja/y, ja/a, ja/n, ja/a]

# Keep the two intervocalic taps explicit for testing.
- grapheme: marinera
  phonemes: [ja/m, ja/a, ja/r, ja/i, ja/n, ja/e, ja/r, ja/a]

# Song-specific clarity tests.
- grapheme: corazón
  phonemes: [en/k, ja/o, ja/r, ja/a, ja/s, ja/o, ja/n]

- grapheme: escalera
  phonemes: [ja/e, ja/s, en/k, ja/a, en/l, ja/e, ja/r, ja/a]

- grapheme: escapulario
  phonemes: [ja/e, ja/s, en/k, ja/a, ja/p, ja/u, en/l, ja/a, ja/r, ja/y, ja/o]

- grapheme: emperatriz
  phonemes: [ja/e, ja/m, ja/p, ja/e, ja/r, ja/a, ja/t, ja/r, ja/i, ja/s]
```

Notes:

- These are approximation tests, not claims of native Spanish phonetic accuracy.
- Keep each override only if it sounds better in an A/B render.
- Make lexical matching case-insensitive and accent-aware, or normalize the input consistently before lookup.
- Confirm whether accented graphemes such as `corazón` are matched before or after Unicode normalization.

---

## 3. Experimental changes — verify voicebank support first

Do not add these symbols merely because they exist in another language generally. First inspect Qixuan's actual EN/ZH dictionaries, acoustic-model phoneme inventory, or known-good generated inputs.

### 3.1 Test a clearer `s`

The current mapping is:

```yaml
- {from: s, to: ja/s}
- {from: z, to: ja/s}
```

The synthesized final `s` is sometimes weak in words such as `tus`, `pies`, `llevas`, and `emperatriz`.

Check whether Qixuan genuinely supports `en/s`. If it does, add it to `symbols` and A/B test:

```yaml
- symbol: en/s
  type: fricative
```

```yaml
- {from: s, to: en/s}
- {from: z, to: en/s}
```

Do not deploy this globally until testing confirms that `en/s` is valid and clearer than `ja/s`.

Also consider a contextual implementation:

- use the clearer variant for syllable-final or word-final `s`;
- keep `ja/s` where it blends more naturally before a vowel.

### 3.2 Test a clearer Spanish-style `l`

The current mapping is:

```yaml
- {from: l, to: en/l}
```

English `l` can sound dark, while Spanish `l` is normally clearer and more forward.

Check whether Qixuan genuinely supports `zh/l`. If it does, add:

```yaml
- symbol: zh/l
  type: liquid
```

Then A/B test:

```yaml
- {from: l, to: zh/l}
```

Test with `Salve`, `Carmelo`, `cielo`, `luminosa`, `escalera`, and `galana`.

### 3.3 Test a softer intervocalic `B`

The current mapping is:

```yaml
- {from: B, to: en/b}
```

If uppercase `B` represents the Spanish approximant `[β]`, a hard English `b` may sound too strong. If Qixuan supports `en/v`, A/B test:

```yaml
- symbol: en/v
  type: fricative
```

```yaml
- {from: B, to: en/v}
```

This is only an approximation. English `v` is not the same as Spanish `[β]`, so keep it only if the rendered result is perceptually better.

### 3.4 Keep `G` unchanged unless a proven alternative exists

Keep:

```yaml
- {from: G, to: en/g}
```

There is no clearly superior substitute for Spanish `[ɣ]` in the currently declared inventory. A timing or duration adjustment may work better than replacing it with an unrelated consonant.

---

## 4. Investigate phrase-level causes outside the dictionary

Some overly syllabified singing may not be fixable in the dictionary alone.

Check the synthesis pipeline for these issues:

### 4.1 Consonant duration

- Avoid giving every consonant the same fixed duration.
- Shorten intervocalic `r`, `d`, `B`, and `G` approximations.
- Allow final `s` enough duration to remain audible without becoming exaggerated.
- Avoid excessive consonant preutterance that inserts a pause before every syllable.

### 4.2 Word and syllable boundaries

For joined MusicXML lyrics such as:

```text
capitana_y
luminosa_escalera
hasta_el
tro_auxilio_y
tu_inefable
te_implora
tu_escapulario
fulgurante_aurora
bella_y
rosa_incólume_y
y_emperatriz
```

Ensure the underscore means a connected lyric transition, not a new hard onset or an inserted pause.

The renderer should preserve a small word boundary without resetting the entire phoneme envelope.

### 4.3 Stress and duration

The dictionary controls phoneme identity, but natural Spanish also needs stress hierarchy. Verify that stressed syllables receive appropriate note energy or vowel duration where the score permits it.

Useful test words from this song:

```text
capitana
marinera
corazón
devoción
emperatriz
Triana
amén
```

### 4.4 Prevent epenthetic vowels

Log the final phoneme sequence sent to Qixuan. Confirm that consonant clusters do not acquire unintended vowels, especially:

```text
tr in Triana
tr in contrito / emperatriz
fl in flor
kr-like transitions in Cristo
```

`Triana` should not become something resembling `tu-ri-a-na`.

---

## 5. Recommended implementation order

1. Add raw G2P logging and tests for uppercase `I`, `U`, `B`, `D`, `G`, `Y`, `rr`, and initial `r`.
2. Change `I -> ja/y` and `U -> ja/w` after confirming their G2P meaning.
3. Fix `rr` so it is not identical to single `r`.
4. Add the lexical overrides listed above.
5. Re-render the same Spanish song and compare it against the current baseline.
6. Inspect Qixuan's real phoneme inventory for `en/s`, `zh/l`, and `en/v`.
7. A/B test those optional mappings one at a time.
8. Tune consonant duration and connected-lyric handling outside the dictionary.

---

## 6. Candidate revised dictionary

Use the following as a safe candidate version. It includes only symbols already present in the original dictionary. The `rr` line assumes the replacement parser accepts a list; otherwise remove that line and rely on lexical overrides.

```yaml
# Qixuan Spanish approximation dictionary.
#
# Qixuan has native EN, JA, and ZH language IDs only. This file makes Spanish
# renderable by mapping OpenUtau g2p-es output onto that existing inventory;
# it does not add native Spanish model support. Do not emit es/* phonemes.

symbols:
- symbol: AP
  type: vowel
- symbol: SP
  type: vowel
- symbol: ja/a
  type: vowel
- symbol: ja/e
  type: vowel
- symbol: ja/i
  type: vowel
- symbol: ja/o
  type: vowel
- symbol: ja/u
  type: vowel
- symbol: en/b
  type: fricative
- symbol: en/ch
  type: fricative
- symbol: en/d
  type: fricative
- symbol: en/dh
  type: fricative
- symbol: en/f
  type: fricative
- symbol: en/g
  type: fricative
- symbol: en/k
  type: fricative
- symbol: en/l
  type: liquid
- symbol: ja/m
  type: fricative
- symbol: ja/n
  type: fricative
- symbol: ja/ny
  type: fricative
- symbol: ja/p
  type: fricative
- symbol: ja/r
  type: liquid
- symbol: ja/ry
  type: liquid
- symbol: ja/s
  type: fricative
- symbol: ja/t
  type: fricative
- symbol: ja/w
  type: semivowel
- symbol: ja/y
  type: semivowel
- symbol: zh/h
  type: fricative

entries:
# Curated overrides where a lexical pronunciation is preferable to a
# one-symbol replacement. These use the same entry structure as Qixuan's
# native language dictionaries and intentionally mix native language prefixes.
- grapheme: hola
  phonemes: [ja/o, en/l, ja/a]
- grapheme: niño
  phonemes: [ja/n, ja/i, ja/ny, ja/o]
- grapheme: perro
  phonemes: [ja/p, ja/e, ja/r, ja/r, ja/o]
- grapheme: jamón
  phonemes: [zh/h, ja/a, ja/m, ja/o, ja/n]

# Strong initial r approximation.
- grapheme: rosa
  phonemes: [ja/r, ja/r, ja/o, ja/s, ja/a]

# Diphthong and glide tests.
- grapheme: cielo
  phonemes: [ja/s, ja/y, ja/e, en/l, ja/o]
- grapheme: pies
  phonemes: [ja/p, ja/y, ja/e, ja/s]
- grapheme: triana
  phonemes: [ja/t, ja/r, ja/y, ja/a, ja/n, ja/a]

# Song-specific pronunciation tests.
- grapheme: marinera
  phonemes: [ja/m, ja/a, ja/r, ja/i, ja/n, ja/e, ja/r, ja/a]
- grapheme: corazón
  phonemes: [en/k, ja/o, ja/r, ja/a, ja/s, ja/o, ja/n]
- grapheme: escalera
  phonemes: [ja/e, ja/s, en/k, ja/a, en/l, ja/e, ja/r, ja/a]
- grapheme: escapulario
  phonemes: [ja/e, ja/s, en/k, ja/a, ja/p, ja/u, en/l, ja/a, ja/r, ja/y, ja/o]
- grapheme: emperatriz
  phonemes: [ja/e, ja/m, ja/p, ja/e, ja/r, ja/a, ja/t, ja/r, ja/i, ja/s]

replacements:
# OpenUtau g2p-es output -> closest native Qixuan phoneme. The default uses
# Latin-American seseo and yeismo; use explicit entries for exceptions.
- {from: a, to: ja/a}
- {from: e, to: ja/e}
- {from: i, to: ja/i}
- {from: I, to: ja/y}
- {from: o, to: ja/o}
- {from: u, to: ja/u}
- {from: U, to: ja/w}
- {from: b, to: en/b}
- {from: B, to: en/b}
- {from: ch, to: en/ch}
- {from: d, to: en/d}
- {from: D, to: en/dh}
- {from: f, to: en/f}
- {from: g, to: en/g}
- {from: G, to: en/g}
- {from: gn, to: ja/ny}
- {from: k, to: en/k}
- {from: l, to: en/l}
- {from: ll, to: ja/y}
- {from: m, to: ja/m}
- {from: n, to: ja/n}
- {from: p, to: ja/p}
- {from: r, to: ja/r}
# Use this only if the parser accepts a list-valued replacement.
- {from: rr, to: [ja/r, ja/r]}
- {from: s, to: ja/s}
- {from: t, to: ja/t}
- {from: w, to: ja/w}
- {from: x, to: zh/h}
- {from: y, to: ja/y}
- {from: Y, to: ja/y}
- {from: z, to: ja/s}
```

---

## 7. Acceptance test

Render the same song with the original and revised dictionaries using identical synthesis settings.

Evaluate these phrases specifically:

```text
Salve, Madre del Carmelo
Capitana y Marinera
Cual luminosa escalera
Tú nos llevas hasta el cielo
Tu escapulario bendito
Rosa incólume y galana
A tus pies, mi corazón
Y emperatriz de Triana
```

Listen for:

- clearer final `s` in `nos`, `llevas`, `pies`, and `tus`;
- smoother glides in `cielo`, `pies`, `Triana`, and `escapulario`;
- no inserted vowel in `tr` clusters;
- a stronger initial `r` in `Rosa`;
- short taps rather than English-style `r` in `Carmelo`, `Marinera`, and `corazón`;
- less mechanical separation between syllables and connected words.

Keep each change only when it improves the A/B result. The candidate dictionary should be treated as an experimental v2, not as a final native-Spanish pronunciation model.
