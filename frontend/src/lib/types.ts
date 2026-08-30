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
  text: string;
  speaker: string | null;
}

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
  shorts: ShortIdea[];
  youtube: YoutubePlan[];
}

export interface ExportSettings {
  orientation: "portrait" | "landscape";
  portrait_fill: "blur" | "crop" | "pad";
  crf: number;
  preset: string;
  burn_subtitles: boolean;
  write_srt: boolean;
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

export interface Job {
  job_id: string;
  kind: string;
  project_id: string | null;
  state: JobState;
  progress: number;
  stage: string;
  message: string;
  result: Record<string, unknown> | null;
  error: string | null;
  attempts: number;
  created_at: number;
  updated_at: number;
  finished_at: number | null;
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
}

export interface Project {
  schema_version: number;
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  video: VideoMeta | null;
  transcript: Transcript | null;
  suggestions: Suggestions | null;
  clips: Clip[];
  transition: TransitionSetting;
  subtitle_style: SubtitleStyle;
  export: ExportSettings;
  /** Signed and short-lived. Regenerated on every read, so a page left open
   *  past the expiry must refetch rather than reuse what it has. */
  media: {
    source_url: string | null;
    thumbnail_url: string | null;
    expires_in_s: number;
  };
  jobs: Job[];
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

export interface ProviderSettings {
  openrouter_api_key: ProviderField;
  duudlaga_api_key: ProviderField;
  elevenlabs_api_key: ProviderField;
  openrouter_model: ProviderField;
}

/** Only the fields the operator actually edited. An omitted field is left
 *  alone; an empty string clears the stored value and falls back to the
 *  server's environment. */
export type ProviderSettingsPatch = Partial<
  Record<keyof ProviderSettings, string>
>;
