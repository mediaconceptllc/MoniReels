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

## Two STT endpoints, chosen by audio length

Chimege exposes both a synchronous short-audio endpoint and an asynchronous
long-audio endpoint. `ChimegeClient.transcribe()` picks between them based on
`CHIMEGE_MAX_AUDIO_SEC` (default 60s) and a byte-size safety check.

### `POST /transcribe` — synchronous, short audio

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

### `POST /stt-long` + `GET /stt-long-transcript` — asynchronous, any length

Designed for "хэдэн ч цагийн яриа" (speech of any number of hours) — no
documented size limit. Push-then-poll by UUID:

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

**Critical detail: no `start`/`end` fields on these chunks, but they're
ordered with a known `duration` each.** That's exactly what
`chimege_client.merge_transcripts()` needs: treat each array item as one
"chunk" with `offset = sum(duration of all earlier items)`, run it through
`text_to_transcript()`, and merge — the same offset-shift logic a
client-side-chunked provider would need, just fed by Chimege's own chunking
instead of ours. See `test_transcribe_long_pushes_then_polls_and_merges_with_correct_offsets`
in `tests/test_chimege_client.py` for the regression coverage — forgetting
to add the cumulative offset here is the classic bug in any chunked-STT
pipeline.

## No word- or segment-level timestamps, ever

Neither endpoint returns per-word or per-segment timing — just flat text
(plus, for `/stt-long-transcript`, a duration per chunk). `timings_estimated`
is therefore **always `true`** coming out of this client:
`text_to_transcript()` synthesizes segment timings by splitting on sentence
boundaries and allocating each sentence a share of the chunk's duration
proportional to its character count.

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
