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
  [field: string]: unknown;
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
 * The stored document, and nothing else.
 *
 * A write path answers with exactly this: it confirms what was saved. It does
 * NOT carry `media` or `jobs`, because both are assembled per read — the URLs
 * are freshly signed and the job list is a query — and re-signing a URL the
 * client already holds on every settings save buys nothing.
 *
 * Separated from `Project` rather than left implied: `PATCH /projects/{id}`
 * was declared as returning a whole `Project`, so a caller reading
 * `.media.source_url` off the result would have got `undefined` at runtime
 * with nothing failing anywhere earlier. Found by `npm run verify-shape` on
 * its first run.
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
  blocks_export: boolean;
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
  /** Work is waiting and nothing alive is doing it — the worker service is
   *  down. The single most common cause of "my job never starts". */
  stalled: boolean;
  disk: {
    free_bytes: number;
    total_bytes: number;
    used_bytes: number;
    min_free_bytes: number;
  };
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
  name: "stt" | "llm" | "tts";
  label: string;
  ready: boolean;
  blocked: string | null;
  /** Admin view only; the project page is told less on purpose. */
  provider?: string;
  powers?: string;
  configured?: boolean;
  implemented?: boolean;
}

export interface ProviderReadiness {
  capabilities: Capability[];
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
