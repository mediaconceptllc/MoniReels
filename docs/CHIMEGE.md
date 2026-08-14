# Chimege.mn STT API — implementation contract

**Status: CONFIRMED.** This document reflects the real Chimege OpenAPI spec
(v1.2) — see [`chimege-openapi.yml`](./chimege-openapi.yml) in this folder
for the full source spec (covers STT, TTS, spell-check, and script
conversion; this doc only covers the STT slice this app uses). All
Chimege-specific code lives in `backend/app/stt/chimege_client.py`; nothing
downstream of `ChimegeClient.transcribe()` needs to know any of this, since
it only ever sees a normalized `Transcript`.

Base URL: `https://api.chimege.com/v1.2` (`CHIMEGE_STT_URL` in `.env`).
Get a token via [console.chimege.com](https://console.chimege.com).

## Auth

Every request carries the token in a `Token` header (HTTP headers are
case-insensitive; the spec uses both `token` and `Token` depending on the
endpoint) as the **raw token value** — no `Bearer ` prefix, no OAuth flow.

```
Token: <CHIMEGE_TOKEN>
```

## Which endpoint is actually used: `/stt-long`, not `/transcribe`

Chimege's real API has two STT paths: a synchronous short-audio endpoint
(`/transcribe`) and an asynchronous push+poll endpoint (`/stt-long` +
`/stt-long-transcript`). This client uses **only the async one**, confirmed
against a real account: probing both directly with the same real token,
`/transcribe` returned `403 Error-Code 1000` ("Invalid API token") while
`/stt-long` accepted the exact same token immediately and returned a real
job UUID. Some Chimege accounts/tokens are evidently authorized for the
async endpoint only — `/transcribe` is not implemented in this client at
all, so there's no dead code sitting around for an endpoint that outright
403s on at least one real account.

On top of that, **every file is split before being sent, regardless of
length** — nothing is ever sent to Chimege as one whole-file request, even
audio well under `CHIMEGE_MAX_AUDIO_SEC`. Splitting happens at detected
pauses in the audio (ffmpeg `silencedetect`) — an "almost sentence by
sentence" split, since a pause is the only signal available for where a
sentence might end *before* we have a transcript. A chunk targets a minimum
of `TARGET_CHUNK_MIN_SEC` (5s, so real pauses still drive fine-grained
splitting); the *ceiling* is `CHIMEGE_MAX_AUDIO_SEC` itself (default 60s,
Chimege's real per-request limit, ~98s/3MB from the `/transcribe` spec,
likely similar for `/stt-long`) — `compute_pause_boundaries` only forces a
hard cut, with a small overlap, once a pause-free stretch reaches that
ceiling. This is deliberate: some people talk continuously for a long time
without a natural pause, and force-cutting them every few seconds regardless
(an earlier, smaller hardcoded ceiling did exactly that) chops sentences
apart for no benefit — a forced cut should only be the fallback for a
genuinely pause-free stretch, not the common case.

The payoff of client-side chunking: each piece's `[start, end]` is known
*exactly*, since we're the ones who cut it — no more guessing full-file
segment timing from character counts. See `compute_pause_boundaries` in
`chimege_client.py`.

The trade-off: every one of our own chunks (at least `TARGET_CHUNK_MIN_SEC`,
typically well under the `CHIMEGE_MAX_AUDIO_SEC` ceiling since a real pause
usually shows up first) now needs its own full push+poll round trip, instead
of one instant response per chunk. A long video with frequent pauses can
still produce many chunks, each paying that async overhead sequentially.
Accepted for now since it's the only endpoint this token can reach at all;
revisit if a token that also accepts `/transcribe` turns up and the overhead
matters enough to use it for chunks specifically.

### `POST /stt-long` + `GET /stt-long-transcript` — the only endpoint used

Designed for "хэдэн ч цагийн яриа" (speech of any number of hours) — no
documented size limit, though this client never sends it anything longer
than `CHIMEGE_MAX_AUDIO_SEC` by choice (see above).

```
POST {base}/stt-long
Token: <token>
Content-Type: application/octet-stream

<raw audio bytes>
```
```json
{"uuid": "...", "duration": 3600.0}
```

Then poll (spec: no more often than every 1 second; ~1h of audio takes ~4
minutes to fully transcribe — our own chunks are much smaller and finish
fast, confirmed against a real 2s probe clip completing within 3s):

```
GET {base}/stt-long-transcript
Token: <token>
UUID: <uuid from the push response>
```

Response is documented as a JSON **array** of chunk results, in time order:

```json
[
  {"done": true, "transcription": "Эхний хэсэг.", "duration": 187.3},
  {"done": true, "transcription": "Хоёр дахь хэсэг.", "duration": 212.9},
  {"done": false, "transcription": "", "duration": 0}
]
```

**Real behavior deviates from the spec in more than one way**, confirmed
against a live account:
- For a single-segment job, the real response can be a **bare object**
  (`{"done": true, "transcription": "...", "duration": 2.0}`), not wrapped
  in an array at all.
- While a job is still processing, the array has been observed containing
  **plain strings** (e.g. `["processing"]`) instead of the documented
  `{done, transcription, duration}` objects.

`_transcribe_chunk()` in `chimege_client.py` handles both defensively:
normalizes a bare object into a single-item list, and treats any list item
that isn't a `dict` with `done: true` as "still processing" rather than
crashing on it. Poll until **every** item is a dict with `done: true`, then
concatenate their `transcription` fields. `/stt-long-hq` +
`/stt-long-hq-transcript` are identical, higher-quality variants (not
currently used — swap the path in `chimege_client.py` if needed).

No `start`/`end` fields on these chunks, only a `duration` each — moot for
this client since it always pushes one of its own already-cut chunks (whose
`[start, end]` is already known exactly) and just concatenates the
resulting text. If a single push ever covered multiple internal Chimege
segments, `chimege_client.merge_transcripts()` already knows how to consume
that shape: treat each array item as one chunk with `offset = sum(duration
of all earlier items)`, run it through `text_to_transcript()`, and merge —
the same offset-shift logic used for our own pause-based chunks
(`shift_transcript`/`merge_transcripts` don't care whether a chunk's offset
came from real audio cutting or from summing a provider's self-reported
durations). Forgetting to add the cumulative offset is the classic bug in
any chunked-STT pipeline, and is what
`test_transcribe_long_audio_splits_into_pause_chunks_with_correct_offsets`
in `tests/test_chimege_client.py` guards against.

### `POST /transcribe` — synchronous, short audio (not used)

Documented here only because it's in the spec and was the first thing this
client tried. **403s with Error-Code 1000 on at least one real, otherwise-
working token** — see above. Not implemented.

```
POST {base}/transcribe
Token: <token>
Content-Type: application/octet-stream
Punctuate: true            # optional; adds punctuation to the output

<raw audio bytes as the request body — NOT multipart/form-data>
```

Response would be **plain text** (`text/plain`), not JSON — just the
transcribed string. No timestamps, no segments, no words.

Hard limits per the spec (return HTTP 400 with an `Error-Code` header):
- Max file size: **3MB** (~98s of 16kHz mono 16-bit PCM WAV — the format
  this app always sends, see `app/video/audio.py`)
- Min file size: 50KB for WAV, 2KB for other formats
- Min duration: 0.5s
- Must be valid WAV audio

| Error-Code | Meaning |
|---|---|
| 2000 | Error receiving audio data |
| 2001 | Audio file too large (>3MB) |
| 2002 | Audio file too small |
| 2003 | Audio too short (<0.5s) |
| 2004 | Invalid audio encoding (must be WAV) |
| 2005 | Failed to convert audio to WAV |

## No word- or segment-level timestamps, ever

Chimege never returns per-word or per-segment timing on either endpoint —
just flat text. For a chunk this client itself cut (the normal case), that
chunk's `[start, end]` is exact (we cut it), but if that chunk's text still
contains more than one sentence, `timings_estimated` is `true` and
`text_to_transcript()` estimates the sub-splits by allocating the chunk's
(exactly known) duration proportional to each sentence's character count.

## Pause-detection tuning and trade-offs

Constants in `chimege_client.py`:
- `SILENCE_NOISE_DB` (default `-30dB`) / `SILENCE_MIN_DURATION_SEC` (default
  `0.35`) — passed to ffmpeg's `silencedetect` filter. Untuned against real
  speech; if chunks come out too fragmented (every breath treated as a
  sentence break) or barely split at all, adjust these first.
- `TARGET_CHUNK_MIN_SEC` (default `5.0`) — the minimum chunk-size target for
  pause-based splitting. Candidate cuts closer together than this are merged
  into the previous chunk *unless* doing so would push that chunk past the
  ceiling (can happen right after a forced cut leaves a short tail) — in
  that case the short tail is kept as its own trailing chunk instead.
- The *ceiling* is `CHIMEGE_MAX_AUDIO_SEC` (`self._config.max_audio_sec`,
  default 60s) — not a separate small constant. A pause is force-cut into a
  new chunk only once a stretch reaches that ceiling with no pause found, so
  continuous, pause-free speech grows a chunk up to Chimege's real
  per-request limit instead of getting chopped every few seconds.
- `MIN_CHUNK_SEC` (default `2.0`) — the absolute floor, separate from the
  target above. Set just above Chimege's own `/transcribe` minimums (0.5s
  duration, 50KB ≈ 1.56s in our WAV format) so no chunk gets rejected as
  "too short"/"too small".
- `FORCED_CUT_OVERLAP_SEC` (default `0.4`) — only applied on a forced
  (no-pause-found) cut; genuine pause cuts don't need it, the gap is the
  buffer.

Known trade-offs (by design, not bugs):
- **Not grammatically exact** — a chunk may be half a sentence (mid-thought
  pause) or several short sentences run together (no pause between them).
  "Almost sentence by sentence," not exactly.
- **A push+poll round trip per pause-bounded chunk**, which can be dozens
  for a long video — see the async-overhead trade-off note above. Sequential
  today, not parallelized.
- **Forced-cut overlap can duplicate a word** at the boundary between two
  chunks in a long pause-free stretch — same word may appear in both chunks'
  text. Minor and rare in normal speech.

## Retry policy

3 attempts, exponential backoff (`1s, 2s, 4s`), on:
- HTTP 429, 500, 502, 503, 504
- Request timeout

Any other error (400, 403, malformed response) fails immediately with a
typed `ChimegeError` — `_describe_http_error()` reads the `Error-Code`
response header and maps known codes (audio errors 2000s, token errors
1000s) to a human-readable message where possible.

| Error-Code | Meaning |
|---|---|
| 1000 | Invalid API token |
| 1001 | API token missing |
| 1002 | Inactive API token |
| 1003 | Suspended API token |

## Other endpoints in the spec (not used by this app)

The full Chimege/Bolorsoft API also covers text-to-speech (`/synthesize`),
text normalization (`/normalize-text`), spell-check (`/spell-check`,
`/spell-check-short`, `/spell-suggest`), and Mongolian-script conversion
(`/kimo`, `/kimo-short`). None of these are needed for this app's STT-only
usage (see HARD RULES §0) and are not implemented here.
