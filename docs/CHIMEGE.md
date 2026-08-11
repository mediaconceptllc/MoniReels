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

## How long audio is actually handled: client-side pause splitting

Chimege's real API has two STT paths — a synchronous short-audio endpoint
and an asynchronous long-audio endpoint (both documented below) — but this
client only ever calls the **synchronous** one. Audio at or under
`CHIMEGE_MAX_AUDIO_SEC` (default 60s) goes straight to `/transcribe`. Audio
longer than that is first split into pieces on `ChimegeClient`'s side, at
detected pauses in the audio (ffmpeg `silencedetect`) — an "almost sentence
by sentence" split, since a pause is the only signal available for where a
sentence might end *before* we have a transcript. Every resulting piece is
guaranteed `<= CHIMEGE_MAX_AUDIO_SEC` by construction (`compute_pause_boundaries`
forces a hard cut, with a small overlap, if no pause appears for too long),
so every piece always fits the sync endpoint's limits — there's nothing left
for the async long-audio path to do.

This also fixes a real bug found against a live account: `/stt-long-transcript`
returned a list of plain strings (not the documented `{done, transcription,
duration}` objects) while a job was still processing, crashing the client.
Since that path is no longer called, the failure mode is moot — though the
defensive fix (treat unexpected shapes as "still processing" rather than
crash) is worth keeping in mind if `/stt-long` is ever reintroduced.

The payoff of client-side chunking: each piece's `[start, end]` is known
*exactly*, since we're the ones who cut it — no more guessing full-file
segment timing from character counts. See `compute_pause_boundaries` in
`chimege_client.py`.

### `POST /transcribe` — synchronous, short audio (the only endpoint used)

```
POST {base}/transcribe
Token: <token>
Content-Type: application/octet-stream
Punctuate: true            # optional; adds punctuation to the output

<raw audio bytes as the request body — NOT multipart/form-data>
```

Response is **plain text** (`text/plain`), not JSON — just the transcribed
string. No timestamps, no segments, no words.

Hard limits (return HTTP 400 with an `Error-Code` header):
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

### `POST /stt-long` + `GET /stt-long-transcript` — asynchronous, any length (documented, not currently used)

Designed for "хэдэн ч цагийн яриа" (speech of any number of hours) — no
documented size limit. Push-then-poll by UUID. **Not called by this client**
(see above) — documented here in case a case turns up later where
client-side pause chunking isn't the right call, e.g. wanting fewer, larger
requests for long silence-free audio.

```
POST {base}/stt-long
Token: <token>
Content-Type: audio/wav

<raw audio bytes>
```
```json
{"uuid": "...", "duration": 3600.0}
```

Then poll (spec: no more often than every 1 second; ~1h of audio takes ~4
minutes to fully transcribe):

```
GET {base}/stt-long-transcript
Token: <token>
UUID: <uuid from the push response>
```

Response is a JSON **array** of chunk results, in time order:

```json
[
  {"done": true, "transcription": "Эхний хэсэг.", "duration": 187.3},
  {"done": true, "transcription": "Хоёр дахь хэсэг.", "duration": 212.9},
  {"done": false, "transcription": "", "duration": 0}
]
```

Poll until **every** item has `done: true`. `/stt-long-hq` +
`/stt-long-hq-transcript` are identical, higher-quality variants (not
currently used — swap the path in `chimege_client.py` if needed).

No `start`/`end` fields on these chunks, only a `duration` each — if this
path is ever reintroduced, `chimege_client.merge_transcripts()` already
knows how to consume exactly this shape: treat each array item as one chunk
with `offset = sum(duration of all earlier items)`, run it through
`text_to_transcript()`, and merge. It's the same offset-shift logic used for
our own pause-based chunks (`shift_transcript`/`merge_transcripts` don't
care whether a chunk's offset came from real audio cutting or from summing
a provider's self-reported durations) — forgetting to add the cumulative
offset here is the classic bug in any chunked-STT pipeline, and is what
`test_transcribe_long_audio_splits_into_pause_chunks_with_correct_offsets`
in `tests/test_chimege_client.py` guards against for the path actually used.

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
- `MIN_CHUNK_SEC` (default `2.0`) — candidate cuts closer together than this
  are merged away. Set just above Chimege's own `/transcribe` minimums
  (0.5s duration, 50KB ≈ 1.56s in our WAV format) so no chunk gets rejected
  as "too short"/"too small".
- `FORCED_CUT_OVERLAP_SEC` (default `0.4`) — only applied when a stretch has
  no pause for longer than `CHIMEGE_MAX_AUDIO_SEC` and a cut has to happen
  mid-speech anyway; genuine pause cuts don't need it, the gap is the buffer.

Known trade-offs (by design, not bugs):
- **Not grammatically exact** — a chunk may be half a sentence (mid-thought
  pause) or several short sentences run together (no pause between them).
  "Almost sentence by sentence," not exactly.
- **More requests than the async path would need** — one `/transcribe` call
  per pause-bounded chunk, which can be dozens for a long video. Sequential
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
