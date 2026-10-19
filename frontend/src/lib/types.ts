/**
 * Mirrors the backend's JSON. Hand-written rather than generated, so a
 * mismatch is a compile error in the component that reads the field rather
 * than `undefined` at runtime in front of a user.
 *
 * Timestamps are seconds as floats everywhere, never formatted strings —
 * formatting happens once, in lib/format.ts.
 */

export type Role = "admin" | "editor";

export interface Me {
  id: string;
  username: string;
  role: Role;
}

export interface TokenResponse {
  token: string;
  username: string;
  role: Role;
  expires_in_s: number;
}

export interface VideoMeta {
  source_key: string;
  duration_sec: number;
  width: number;
  height: number;
  fps: number;
  has_audio: boolean;
  codec: string;
  thumbnail_key: string;
  /** The speech track, extracted once at import. Empty on projects imported
   *  before it existed and on sources with no audio. */
  audio_key: string;
}

export interface Segment {
  id: string;
  start: number;
  end: number;
  /** What was SAID, in the video's own language — the record of the audio. */
  text: string;
  speaker: string | null;
  /** The Mongolian subtitle for `text`, when the video is not in Mongolian.
   *  Null until translated, and cleared again when `text` is corrected: a
   *  translation of words that are no longer there says something nobody
   *  said. Optional so a document from before translation still reads. */
  translation?: string | null;
}

/** What is spoken in the video. The audience is always Mongolian, so this is
 *  the only language that varies — and it decides which recogniser hears the
 *  video and whether its text needs translating. */
export type SourceLanguage = "mn" | "en";

export interface Transcript {
  language: string;
  segments: Segment[];
  full_text: string;
  /** True when a chunk held several sentences, so the split within it was
   *  estimated. Segment boundaries themselves are always exact. */
  timings_estimated: boolean;
}

/** One piece of a reel: a separate, non-contiguous range of the source. */
export interface Cut {
  start: number;
  end: number;
  role: "hook" | "context" | "proof" | "payoff";
  reason: string;
}

export interface ShortIdea {
  id: string;
  title: string;
  hook_text: string;
  hook_quote: string;
  cuts: Cut[];
  on_screen_texts: string[];
  b_roll: string[];
  caption: string;
  hashtags: string[];
  why_it_works: string;
}

export interface KeepRange {
  start: number;
  end: number;
  reason: string;
}

export interface YoutubePlan {
  title: string;
  throughline: string;
  ranges: KeepRange[];
  total_duration: number;
}

export interface Suggestions {
  /** Between 1 and `requested_shorts` — fewer when some did not hold up,
   *  never padded up to the number with a weaker one. */
  shorts: ShortIdea[];
  youtube: YoutubePlan[];
  /** What the producer asked for, kept beside what came back so a shortfall
   *  can be SAID. Null on sets made before there was a choice — those were
   *  always asked for three. */
  requested_shorts?: number | null;
  requested_youtube?: number | null;
}

/** The range the count picker may offer, from the same rule the server
 *  enforces. Never worked out here: a second copy of the rule is how a number
 *  reaches the page that the server then refuses. */
export interface SuggestLimits {
  shorts_max: number;
  youtube_max: number;
  shorts_default: number;
  youtube_default: number;
}

export type LogoPosition = "top-left" | "top-right" | "bottom-left" | "bottom-right";

/** Where the brand logo goes on THIS project's exports. The image itself is
 *  global and lives in the admin settings — one studio, one mark. */
export interface LogoSettings {
  enabled: boolean;
  position: LogoPosition;
  /** Percentages of the frame, never pixels: the same project renders
   *  portrait and landscape. */
  width_pct: number;
  opacity: number;
  margin_pct: number;
}

export interface ExportSettings {
  orientation: "portrait" | "landscape";
  portrait_fill: "blur" | "crop" | "pad";
  crf: number;
  preset: string;
  burn_subtitles: boolean;
  write_srt: boolean;
  logo: LogoSettings;
  /** The intro/outro FILES are global brand assets. These say whether this
   *  project's exports carry them. */
  use_intro: boolean;
  use_outro: boolean;
  /** For a video not in Mongolian: subtitle with the translation ("mn") or
   *  with what was said ("source"). Mongolian by default, because the
   *  audience is. */
  subtitle_language: "mn" | "source";
  /** Read the Mongolian translation aloud over the video. Only for a video
   *  not in Mongolian; billed per character. */
  voice_over: boolean;
  /** How loud the original sound stays under the voice, 0–1. Only while the
   *  speech is ducked: removed, what is left plays at its own level. */
  original_volume: number;
  /** What happens to the speech the voice replaces: "duck" keeps the whole
   *  original under the voice at `original_volume`; "remove" separates the
   *  speech out on the worker (Demucs) and keeps the music and the room.
   *  Ducked by default — it needs nothing the worker may not have. */
  source_speech: "duck" | "remove";
  /** A voice per speaker: {transcript speaker label: ElevenLabs voice id}.
   *  Sent whole. A speaker with no entry — and a line with no speaker — is
   *  read in the default voice chosen on the admin page. */
  speaker_voices: Record<string, string>;
}

export interface BrandLogo {
  key: string;
  /** Signed and short-lived; refetch rather than cache. */
  url: string | null;
}

export type BrandAsset = "logo" | "intro" | "outro";

export interface BrandSettings {
  logo: BrandLogo | null;
  intro: BrandLogo | null;
  outro: BrandLogo | null;
  storage: boolean;
}

export interface SubtitleStyle {
  enabled: boolean;
  font_family: string;
  font_size: number;
  primary_color: string;
  outline_color: string;
  outline_width: number;
  shadow: number;
  position: "bottom" | "top" | "center";
  margin_v: number;
}

export interface Clip {
  id: string;
  source_path: string;
  start: number;
  end: number;
  order: number;
}

export interface TransitionSetting {
  type: string;
  duration: number;
}

export type JobState = "queued" | "running" | "done" | "failed" | "canceled";

/**
 * What a handler returned, with the two fields every kind carries named.
 *
 * It was `Record<string, unknown>`, which is why nothing ever read it: the
 * worker has metered `llm.cost_usd` since the first day and put it here, and
 * the only way to see a bill was to open the database. The rest of the object
 * is genuinely per-kind — `segments` for a transcription, `shorts` for a
 * suggestion — so the index signature stays, but the parts that ARE a
 * contract are declared and checked by `npm run verify-shape`.
 */
export interface JobResult {
  /** How long the job RAN, excluding the time it waited in the queue.
   *  Recorded for failures too: "it broke after four minutes" and "it broke
   *  instantly" point at different causes. */
  elapsed_sec?: number;
  /** Present only when the job actually spent money at the model provider.
   *  Absent means no paid call — which is not the same as a zero. */
  llm?: {
    calls: number;
    prompt_tokens: number;
    completion_tokens: number;
    cost_usd: number;
    models: string[];
  };
  /** Present only on an export that read a Mongolian voice. */
  voice?: VoiceReport;
  [field: string]: unknown;
}

/** What a voice-over did. ElevenLabs bills per character and the price per
 *  character depends on the account's plan, so the characters are the cost
 *  this system can state — not dollars it would have to guess. */
export interface VoiceReport {
  /** Lines read aloud across every clip. */
  lines: number;
  /** Clips paid for by this export, and the characters sent for them. */
  synthesized: number;
  characters: number;
  /** Clips already in storage — paid for before, not again. */
  cached: number;
  /** Lines with no translation to read; they kept the original sound. */
  missing: number;
  /** Lines played faster to fit, and lines cut at the end of their clip —
   *  the ones worth listening to before publishing. */
  sped_up: number;
  cut: number;
  /** With the source speech removed: the cuts separated by this export and
   *  the seconds of sound that took, and the cuts separated before. Worker
   *  time rather than money — nothing is billed — but the reason an export
   *  ran long. Absent on an export made before the choice existed. */
  beds_separated?: number;
  bed_seconds?: number;
  beds_cached?: number;
}

export interface Job {
  job_id: string;
  kind: string;
  project_id: string | null;
  state: JobState;
  progress: number;
  stage: string;
  message: string;
  result: JobResult | null;
  error: string | null;
  attempts: number;
  created_at: number;
  updated_at: number;
  finished_at: number | null;
  /** How many waiting jobs a worker takes before this one, in its lane; null
   *  once it is not waiting. Sent with a job followed on its own — GET
   *  /jobs/{id} and the stream — and absent from the project's job list. */
  queue_ahead?: number | null;
}

/** A project's newest job, as the list card shows it (`GET /jobs/latest`). */
export interface LatestJob {
  project_id: string;
  job_id: string;
  kind: string;
  state: JobState;
  progress: number;
  finished_at: number | null;
}

export interface LatestJobs {
  jobs: LatestJob[];
}

/**
 * What this project has cost, and what the next paid run is likely to cost.
 *
 * The two numbers are different in kind and are never merged: `spent_usd` is
 * what the provider actually charged, `suggest_estimate_usd` is this owner's
 * own measured rate applied to this transcript. The estimate arrives with the
 * number of runs behind it, because "≈ $0.03 from one run" and "≈ $0.03 from
 * twenty" are not the same claim.
 */
export interface ProjectSpend {
  spent_usd: number;
  /** How many jobs reported a charge. Fewer than the jobs that ran. */
  priced_jobs: number;
  /** Jobs older than this are pruned, so the total is short by whatever was
   *  deleted — which the page says rather than presenting it as complete. */
  keep_days: number;
  /** Null until something has been measured. Never 0 as a stand-in: a zero
   *  beside a paid button is a promise. */
  suggest_estimate_usd: number | null;
  suggest_samples: number;
  /** How many shorts the measured runs were asked for. The model writes out
   *  every short it is told to, so a figure measured on three is not a price
   *  for eight — the page says which it was. Null when nothing measured. */
  suggest_basis_shorts: number | null;
  /** Speech-to-text is billed per minute by the recogniser and NOTHING here
   *  counts it, so a transcribe job's cost is unknown rather than nil. */
  stt_measured: boolean;
}

export interface ProjectSummary {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  has_video: boolean;
  has_transcript: boolean;
  has_suggestions: boolean;
  duration_sec: number;
  n_outputs: number;
  /** Signed and short-lived, like every other media URL here. Null until the
   *  import has made one — or when storage is not configured at all. */
  thumbnail_url: string | null;
}

/**
 * The stored document, and nothing else — what the detail page's `Project`
 * is built on. It does NOT carry `media` or `jobs`, because both are
 * assembled per read: the URLs are freshly signed and the job list is a
 * query.
 */
export interface ProjectDocument {
  schema_version: number;
  id: string;
  name: string;
  language: SourceLanguage;
  created_at: number;
  updated_at: number;
  video: VideoMeta | null;
  transcript: Transcript | null;
  suggestions: Suggestions | null;
  clips: Clip[];
  transition: TransitionSetting;
  subtitle_style: SubtitleStyle;
  export: ExportSettings;
}

/**
 * What `PATCH /projects/{id}` answers with: every field it can change, as
 * saved, and when. Not the document — a 2-hour transcript made that 4 MB,
 * encoded for a caller that threw it away and read the project again.
 *
 * Its own type rather than `ProjectDocument` (or `Project`): a caller reading
 * `.transcript` or `.media` off the result gets a compile error, not an
 * `undefined` at runtime with nothing failing anywhere earlier.
 */
export type ProjectSettingsSaved = Pick<
  ProjectDocument,
  "id" | "name" | "language" | "updated_at" | "transition" | "subtitle_style" | "export"
>;

/** What the detail page reads: the document plus what only a read can give. */
/** How far the translation has got, and whether the export guard will
 *  refuse because of it — the guard's own verdict, so the page cannot
 *  disable a button the server would accept. */
export interface TranslationStatus {
  /** False for a Mongolian video: there is nothing to translate. */
  needed: boolean;
  /** Lines with words in them; a blank line is neither translated nor not. */
  lines: number;
  translated: number;
  missing: number;
  /** The lines the suggestions cut, counted by the server's one rule (a line
   *  touching a cut counts): what the translation that follows the
   *  suggestions translates, and all an export of them needs. */
  suggested_lines: number;
  suggested_missing: number;
  suggested_ids: string[];
  /** The export guard's verdict for exporting EVERY suggestion — what the
   *  page's export buttons do. Only the suggested lines count. */
  blocks_export: boolean;
  /** What the export would use the translation for. The way out of a gap
   *  depends on it: subtitles can go out in the spoken language, a Mongolian
   *  voice has nothing else to read. */
  used_for: ("subtitles" | "voice")[];
}

/** Whether this project's export reads a Mongolian voice, and — when it
 *  cannot be made right now — why, in the guard's own words. */
export interface VoiceStatus {
  on: boolean;
  blocked: string | null;
}

/** One person in the transcript, as the voice picker shows them. A bare
 *  label like `speaker_1` identifies nobody; how much they say and their
 *  first line do. */
export interface SpeakerSummary {
  id: string;
  lines: number;
  /** The start of their first line, in the language it was said in. */
  sample: string;
}

export interface Project extends ProjectDocument {
  /** Signed and short-lived. Regenerated on every read, so a page left open
   *  past the expiry must refetch rather than reuse what it has. */
  media: {
    source_url: string | null;
    thumbnail_url: string | null;
    expires_in_s: number;
  };
  jobs: Job[];
  /** How far back `jobs` reaches. A list at exactly this length is truncated,
   *  not complete — the page says so instead of letting a full list read as
   *  the project's whole history. */
  job_history_limit: number;
  spend: ProjectSpend;
  suggest_limits: SuggestLimits;
  translation: TranslationStatus;
  voice: VoiceStatus;
  /** Who speaks, in the order they first do. Empty before a transcript. */
  speakers: SpeakerSummary[];
}

export interface Output {
  id: string;
  kind: "reel" | "youtube" | "export";
  title: string;
  duration_sec: number;
  size_bytes: number;
  created_at: number;
  /** Plays inline. */
  play_url: string;
  /** Carries a Content-Disposition, so a browser saves instead of playing —
   *  which is why it cannot be the same URL as play_url. */
  download_url: string;
  srt_url: string | null;
}

export interface CreateProjectResponse {
  project_id: string;
  upload_url: string;
  upload_key: string;
  upload_expires_in_s: number;
}

export interface QueueStatus {
  counts: Record<string, number>;
  waiting: number;
  live_workers: number;
  /** When a worker last reported itself alive, busy or not; null before any
   *  worker running this code has. */
  worker_seen_at: number | null;
  /** Work is waiting and nothing alive is doing it — the worker service is
   *  down. The single most common cause of "my job never starts". */
  stalled: boolean;
  /** The API container's own disk. It renders nothing, so this is NOT the
   *  disk an export fills — it used to be shown as if it were. */
  api_disk: {
    free_bytes: number;
    total_bytes: number;
    used_bytes: number;
    min_free_bytes: number;
  };
  /** The disk the renders are written to, as the worker last reported it.
   *  Null until a worker running this code has said so. */
  worker_disk: {
    free_bytes: number;
    total_bytes: number;
    min_free_bytes: number;
    measured_at: number;
  } | null;
}

/** One account as the admin's users screen lists it. */
export interface AdminUser {
  id: string;
  username: string;
  role: Role;
  active: boolean;
  created_at: number;
  projects: number;
}

/** A job on the admin's jobs screen: the job, plus who it belongs to and
 *  whether the server would take it back. */
export interface AdminJob extends Job {
  project_name: string | null;
  owner: string | null;
  lane: string;
  /** Billed per attempt: a retry is a second charge. */
  no_retry: boolean;
  claimed_by: string | null;
  /** When an admin last put it back in the queue. */
  requeued_at: number | null;
  /** Failed or canceled — the only states the server re-runs. */
  retryable: boolean;
}

export interface AdminJobsPage {
  jobs: AdminJob[];
  /** Under the same filters, so a page can say how many it is not showing. */
  total: number;
  limit: number;
  offset: number;
}

/** Null wherever there was nothing to measure — never a 0 standing in. */
export interface JobStatsRow {
  done: number;
  failed: number;
  canceled: number;
  /** done / (done + failed); a cancel is a decision, not a failure. */
  success_pct: number | null;
  run_p50_sec: number | null;
  run_p95_sec: number | null;
  /** Measured only on jobs that worked first time — see wait_samples. */
  wait_p50_sec: number | null;
  wait_p95_sec: number | null;
  wait_samples: number;
}

export interface JobStats {
  /** The window actually used: never longer than jobs are kept. */
  days: number;
  keep_days: number;
  overall: JobStatsRow;
  kinds: (JobStatsRow & { kind: string })[];
  queue: {
    waiting: number;
    running: number;
    live_workers: number;
    stalled: boolean;
    oldest_waiting_sec: number | null;
  };
}

/** One line of a provider's balance, by the provider's own name for it. */
export interface BalanceRow {
  name: string;
  value: number | string | boolean;
}

/** The selected recogniser, asked live. `balance` is present only when it
 *  answered; `error` only when it did not. */
export interface SttStatus {
  provider: string;
  configured: boolean;
  ok?: boolean;
  error?: string;
  balance?: BalanceRow[];
}

export interface ProviderStatus {
  capabilities: Capability[];
  stt_providers: string[];
  stt: SttStatus;
  secret_storage: SecretStorage;
}

/** How the keys entered on the admin page are kept in the database. Field
 *  names only — never a value. `plaintext` are stored unencrypted;
 *  `unreadable` are sealed with a key this server does not have; `error`
 *  says the variable itself is malformed. */
export interface SecretStorage {
  encrypted: boolean;
  variable: string;
  plaintext: string[];
  unreadable: string[];
  error: string | null;
}

/** One provider value as the server is willing to describe it. The value
 *  itself never crosses this boundary — `hint` is the masked tail of a
 *  secret, or the plain value of a field that is not one. */
export interface ProviderField {
  source: "db" | "env" | "unset";
  set: boolean;
  hint: string;
}

export type SttProviderName = "duudlaga" | "elevenlabs";

export interface ProviderSettings {
  openrouter_api_key: ProviderField;
  duudlaga_api_key: ProviderField;
  elevenlabs_api_key: ProviderField;
  openrouter_model: ProviderField;
  /** Which recogniser runs. A name, not a guess from which key is filled
   *  in — two keys can be set at once. */
  stt_provider: ProviderField;
  /** The voice-over's model and voice. Not secrets — read back in full. */
  elevenlabs_tts_model: ProviderField;
  elevenlabs_tts_voice_id: ProviderField;
}

export interface TtsVoice {
  id: string;
  name: string;
  category: string;
  gender: string;
  accent: string;
  /** ElevenLabs' own sample, in English: enough to hear the timbre. */
  preview_url: string | null;
}

/** The voice picker. `mongolian` is ASKED of ElevenLabs: true or false when
 *  its model list says so, null when that could not be read — unknown, which
 *  is not the same as no. */
export interface TtsVoices {
  model: string;
  voice_id: string | null;
  voices: TtsVoice[];
  mongolian: boolean | null;
  error: string | null;
}

/** The voices a speaker can be given — the producer's list, thinner than
 *  the admin's: nothing about the model or the account. `default_voice_id`
 *  is what a speaker without a voice of their own is read in. */
export interface ProjectVoices {
  voices: TtsVoice[];
  default_voice_id: string | null;
  error: string | null;
}

/** Only the fields the operator actually edited. An omitted field is left
 *  alone; an empty string clears the stored value and falls back to the
 *  server's environment. */
export type ProviderSettingsPatch = Partial<
  Record<keyof ProviderSettings, string>
>;

/** One outside service and what it powers. `ready` folds three things
 *  together — the code exists, a key is set, nothing is blocking it — and
 *  `blocked` says which one is missing, in the operator's language. */
export interface Capability {
  /** `separation` is answered by the worker, not a key — admin view only. */
  name: "stt" | "llm" | "tts" | "separation";
  label: string;
  ready: boolean;
  blocked: string | null;
  /** Admin view only; the project page is told less on purpose. */
  provider?: string;
  powers?: string;
  configured?: boolean;
  implemented?: boolean;
}

/** Whether each brand file has been uploaded — never where it is. */
export interface BrandAvailability {
  logo: boolean;
  intro: boolean;
  outro: boolean;
}

export interface ProviderReadiness {
  capabilities: Capability[];
  /** The export panel's logo/intro/outro choices read this: a choice with
   *  no file behind it is one the render silently skips. */
  brand?: BrandAvailability;
}

export interface SubtitleFonts {
  /** Families the RENDER image can actually use. Asked of the image, not
   *  hard-coded: libass substitutes a missing family without failing, so a
   *  free-text font name is a setting that looks applied and is not. */
  families: string[];
  default: string;
}

export interface SubtitleTemplate {
  id: string;
  name: string;
  /** Copied INTO a project when applied, never linked: deleting a template
   *  must not restyle finished work. */
  style: SubtitleStyle;
  created_at: number;
}

/** What a group of ledger rows adds up to. `cost_usd` is the KNOWN part —
 *  null when nothing in the group had a price — and `unpriced_requests`
 *  counts the requests that had none. Those are unknown, never free. */
export interface UsageTotals {
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  audio_seconds: number;
  characters: number;
  cost_usd: number | null;
  unpriced_requests: number;
}

/** One UTC month × provider × kind × user of the spend ledger. The user is
 *  the project's owner; null when the project was deleted mid-job. */
export interface UsageRow extends UsageTotals {
  month: string;
  provider: string;
  kind: "llm" | "stt" | "tts";
  user_id: string | null;
  username: string | null;
}

export interface UsageReport {
  months: number;
  since: number;
  timezone: string;
  rows: UsageRow[];
  month_totals: (UsageTotals & { month: string })[];
  totals: UsageTotals;
  /** The operator's speech prices in effect now; null = not configured. */
  rates: {
    duudlaga_usd_per_minute: number | null;
    elevenlabs_stt_usd_per_minute: number | null;
    elevenlabs_tts_usd_per_1k_chars: number | null;
  };
}
