# Chimege.mn STT API — implementation contract

**Status: UNVERIFIED ASSUMPTION.** I could not obtain a Chimege API token or
reach a rendered copy of `docs.api.chimege.com/v1.2/en/` (it's a JS-rendered
doc site that returned no extractable content, and no credentials were
available in this environment). Everything below is a best-guess REST
contract based on how comparable STT vendors shape their APIs. **Do not trust
it until it's been checked against a real account.**

All Chimege-specific code lives in `backend/app/stt/chimege_client.py`. If the
real contract differs, only that file (and this document) need to change —
nothing downstream of `ChimegeClient.transcribe()` should need to move, since
everything past that point only ever sees the normalized `Transcript` model.

## What to verify against a real account

1. Get a `CHIMEGE_TOKEN` and the real `CHIMEGE_STT_URL` from Chimege (contact
   info: info@chimege.mn, or via console.chimege.com).
2. Send one short WAV file through the real endpoint and compare the actual
   response JSON against the "Assumed response" section below.
3. Update `_to_transcript()` in `chimege_client.py` to match, and update this
   file to reflect the confirmed contract.

## Assumed request

```
POST {CHIMEGE_STT_URL}
Authorization: Bearer {CHIMEGE_TOKEN}
Content-Type: multipart/form-data

  audio: <16kHz, mono, 16-bit PCM WAV file>
```

Audio is always pre-converted to 16 kHz mono 16-bit PCM WAV before being sent
(see `app/video/audio.py`), regardless of what the real Chimege API turns out
to require — that conversion is cheap and correct for any ASR vendor, so it
stays even if the request shape above changes.

## Assumed response (with timings)

```json
{
  "language": "mn",
  "text": "Full transcript text ...",
  "segments": [
    {
      "start": 0.0,
      "end": 2.34,
      "text": "Сайн байна уу",
      "words": [
        {"start": 0.0, "end": 0.4, "text": "Сайн"},
        {"start": 0.45, "end": 0.9, "text": "байна"},
        {"start": 0.95, "end": 1.3, "text": "уу"}
      ]
    }
  ]
}
```

## Assumed response (no timings — fallback path)

Some ASR APIs return plain text with no per-segment timing. If Chimege does
this, the response is assumed to look like:

```json
{ "language": "mn", "text": "Full transcript text ..." }
```

In this case `chimege_client._to_transcript()` synthesizes segment timings by
splitting on sentence boundaries and allocating duration proportional to each
sentence's share of the total character count, and sets
`Transcript.timings_estimated = true` so the UI can indicate the timings are
approximate.

## Chunking (audio longer than `CHIMEGE_MAX_AUDIO_SEC`)

1. Run `ffmpeg -af silencedetect=noise=-30dB:d=0.5` over the WAV to find
   silence intervals.
2. Walk forward from `0`, and for each `max_chunk_sec` boundary, cut at the
   midpoint of the nearest silence interval that ends before the boundary.
3. If no usable silence is found near a boundary, fall back to a hard cut at
   the boundary, with the next chunk starting `0.5s` earlier (overlap) so
   words spoken right at the cut aren't lost.
4. Transcribe each chunk independently, then **add that chunk's start offset
   to every timestamp** in its transcript before merging
   (`chimege_client.shift_transcript` / `merge_transcripts`). This offset
   addition is unit-tested in `tests/test_chimege_client.py` — it's the
   single most common bug in chunked-STT pipelines.

## Retry policy

3 attempts, exponential backoff (`1s, 2s, 4s`), only on:
- HTTP 429
- HTTP 5xx
- request timeout

Any other error (4xx other than 429, connection refused, malformed response)
fails the job immediately with a typed `ChimegeError` rather than retrying.

## Auth

Assumed to be a static bearer token (`CHIMEGE_TOKEN`) sent as
`Authorization: Bearer <token>` on every request. If Chimege instead uses an
API-key header, query param, or short-lived OAuth token, update
`ChimegeClient._post_chunk()` accordingly — the config plumbing
(`CHIMEGE_TOKEN` in `.env`) does not need to change shape unless it becomes a
credential pair instead of a single token.
