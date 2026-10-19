/**
 * Does the backend's real JSON still match the types this app reads?
 *
 * `typecheck`, `lint` and `build` all ask whether the frontend agrees with
 * ITSELF. None of them has ever seen a response. Rename a field on the server
 * and all three stay green while the page renders `undefined` — which is how
 * a contract break reaches a user rather than a reviewer.
 *
 * The fixtures come from `backend/tests/test_frontend_shape.py`, built from
 * synthetic data and committed, so this runs in CI like any other check.
 *
 * Two different mistakes need two different mechanisms, and neither catches
 * the other:
 *
 *   1. A field CHANGED TYPE — caught by the `as` assertions below. TypeScript
 *      refuses an assertion between two object types that share a field of
 *      incompatible types.
 *   2. A field WAS REMOVED or RENAMED — NOT caught by an assertion: a type
 *      with more fields is still assignable to one with fewer, so the cast
 *      succeeds and every later `.field` access compiles against the declared
 *      type. It is caught by reading the field off the RAW import, whose type
 *      comes from the JSON itself.
 *
 * So each contract is read twice. Dropping either half quietly halves the
 * check while leaving it green.
 *
 * One gap, stated rather than hidden: inside a list, a change affecting only
 * SOME elements is absorbed by the union TypeScript infers for the element
 * type. A serialiser changing a field changes it for every element, so the
 * realistic case is covered — a per-element difference would not be.
 *
 * And a fixture nobody regenerated would pass this happily, which is why the
 * backend CI job runs `git diff --exit-code` over `.shape/` after its tests
 * rewrite it. Without that step this file checks the frontend against a
 * contract from before the change.
 */
import contracts from "../.shape/contracts.json";
import registry from "../.shape/registry.json";
import { JOB_LABELS, STAGE_LABELS } from "../src/lib/jobs";
import type {
  AdminJobsPage,
  AdminUser,
  BrandSettings,
  CreateProjectResponse,
  Job,
  JobStats,
  LatestJobs,
  Me,
  MultipartSigned,
  MultipartStart,
  MultipartStatus,
  Output,
  Project,
  ProjectSettingsSaved,
  ProjectSummary,
  ProjectVoices,
  ProviderReadiness,
  ProviderSettings,
  ProviderStatus,
  QueueStatus,
  SubtitleFonts,
  SubtitleTemplate,
  TokenResponse,
  TtsVoices,
  UsageReport,
} from "../src/lib/types";

/* ------------------------------------------------------------------ *
 * 1. Type drift.
 * ------------------------------------------------------------------ */

const token = contracts.token as TokenResponse;
const me = contracts.me as Me;
const projects = contracts.projects as ProjectSummary[];
const project = contracts.project as Project;
const created = contracts.create_project as CreateProjectResponse;
const outputs = contracts.outputs as Output[];
const job = contracts.job as Job;
const jobFailed = contracts.job_failed as Job;
const jobVoiced = contracts.job_voiced as Job;
const jobQueued = contracts.job_queued as Job;
const latestJobs = contracts.latest_jobs as LatestJobs;
const ttsVoices = contracts.tts_voices as TtsVoices;
const projectVoices = contracts.project_voices as ProjectVoices;
const queue = contracts.queue as QueueStatus;
const providerSettings = contracts.provider_settings as ProviderSettings;
const readiness = contracts.readiness as ProviderReadiness;
const brand = contracts.brand as BrandSettings;
const brandEmpty = contracts.brand_empty as BrandSettings;
const fonts = contracts.fonts as SubtitleFonts;
const templateSaved = contracts.template_saved as SubtitleTemplate;
const usage = contracts.usage as UsageReport;

const providers = contracts.providers as ProviderStatus;

// The admin's own screens: accounts, the queue's jobs and how it has done.
const adminUsers = contracts.admin_users as AdminUser[];
const adminJobs = contracts.admin_jobs as AdminJobsPage;
const jobStats = contracts.admin_job_stats as JobStats;

/** The small write-path answers. Each one is a single field, and a single
 *  field is what a rename removes entirely. */
const writes = {
  upload_complete: contracts.upload_complete as { project_id: string; job_id: string },
  transcript_updated: contracts.transcript_updated as {
    updated: number;
    unknown_ids: string[];
    translations_cleared: number;
  },
  ranges_selected: contracts.ranges_selected as { clips: number },
  transcribe_started: contracts.transcribe_started as { job_id: string },
  suggest_started: contracts.suggest_started as { job_id: string },
  export_started: contracts.export_started as { job_id: string },
  export_all_started: contracts.export_all_started as { job_id: string },
  translate_started: contracts.translate_started as { job_id: string },
  job_canceled: contracts.job_canceled as { canceled: boolean },
  project_deleted: contracts.project_deleted as { deleted: boolean },
  output_deleted: contracts.output_deleted as { deleted: boolean },
  template_deleted: contracts.template_deleted as { deleted: string },
  brand_upload_url: contracts.brand_upload_url as { key: string; url: string },
  settings_saved: contracts.provider_settings_saved as {
    changed: string[];
    settings: ProviderSettings;
  },
  templates: contracts.templates as { templates: SubtitleTemplate[] },
  brand_saved: contracts.brand_saved as BrandSettings,
  project_patched: contracts.project_patched as ProjectSettingsSaved,
  admin_user_created: contracts.admin_user_created as { id: string; username: string; role: string },
  admin_password_reset: contracts.admin_password_reset as { updated: boolean },
  admin_user_active: contracts.admin_user_active as { active: boolean },
  admin_job_retried: contracts.admin_job_retried as { requeued: boolean },
  // A source in parts, and a new source for a project whose video failed.
  multipart_started: contracts.multipart_started as MultipartStart,
  multipart_signed: contracts.multipart_signed as MultipartSigned,
  multipart_status: contracts.multipart_status as MultipartStatus,
  multipart_completed: contracts.multipart_completed as { completed: boolean; parts: number },
  multipart_aborted: contracts.multipart_aborted as { aborted: boolean },
  upload_restarted: contracts.upload_restarted as CreateProjectResponse,
};

/* ------------------------------------------------------------------ *
 * 2. Field presence — read off the RAW import, never the cast value.
 *
 * Every field below is one a component actually renders. A field the app
 * does not read is not a contract and is deliberately absent from this list.
 * ------------------------------------------------------------------ */

const raw = contracts;
const present: unknown[] = [
  // auth
  raw.token.token, raw.token.username, raw.token.role, raw.token.expires_in_s,
  raw.me.id, raw.me.username, raw.me.role,

  // the project list card
  raw.projects.map((p) => [
    p.id, p.name, p.created_at, p.updated_at, p.has_video, p.has_transcript,
    p.has_suggestions, p.duration_sec, p.n_outputs,
    // The card's picture. Signed per request, so it is the one field on a
    // list row that cannot be cached with the row.
    p.thumbnail_url,
    // A video that will never be processed until a new one is uploaded.
    p.import_failed,
  ]),

  // the project itself
  raw.project.schema_version, raw.project.id, raw.project.name,
  // What is spoken in the video. It picks the recogniser and decides whether
  // a translation stage is drawn at all.
  raw.project.language,
  raw.project.created_at, raw.project.updated_at,
  raw.project.video.source_key, raw.project.video.duration_sec,
  raw.project.video.width, raw.project.video.height, raw.project.video.fps,
  raw.project.video.has_audio, raw.project.video.codec,
  raw.project.video.thumbnail_key, raw.project.video.audio_key,
  raw.project.transcript.language, raw.project.transcript.full_text,
  raw.project.transcript.timings_estimated,
  raw.project.transcript.segments.map((s) => [
    s.id, s.start, s.end, s.text, s.speaker, s.translation,
  ]),
  raw.project.suggestions.shorts.map((s) => [
    s.id, s.title, s.hook_text, s.hook_quote, s.on_screen_texts, s.b_roll,
    s.caption, s.hashtags, s.why_it_works,
    s.cuts.map((c) => [c.start, c.end, c.role, c.reason]),
  ]),
  raw.project.suggestions.youtube.map((y) => [
    y.title, y.throughline, y.total_duration,
    y.ranges.map((r) => [r.start, r.end, r.reason]),
  ]),
  raw.project.clips.map((c) => [c.id, c.source_path, c.start, c.end, c.order]),
  raw.project.transition.type, raw.project.transition.duration,
  raw.project.subtitle_style.enabled, raw.project.subtitle_style.font_family,
  raw.project.subtitle_style.font_size, raw.project.subtitle_style.primary_color,
  raw.project.subtitle_style.outline_color, raw.project.subtitle_style.outline_width,
  raw.project.subtitle_style.shadow, raw.project.subtitle_style.position,
  raw.project.subtitle_style.margin_v,
  raw.project.export.orientation, raw.project.export.portrait_fill,
  raw.project.export.crf, raw.project.export.preset,
  raw.project.export.burn_subtitles, raw.project.export.write_srt,
  raw.project.export.use_intro, raw.project.export.use_outro,
  raw.project.export.subtitle_language,
  raw.project.export.voice_over, raw.project.export.original_volume,
  // Ducked or removed: the second is a worker's job, the first is not.
  raw.project.export.source_speech,
  // A voice per speaker, sent whole; a speaker read in the default voice has
  // no entry.
  raw.project.export.speaker_voices,
  raw.project.export.logo.enabled, raw.project.export.logo.position,
  raw.project.export.logo.width_pct, raw.project.export.logo.opacity,
  raw.project.export.logo.margin_pct,
  // Signed and short-lived: a page left open past the expiry has to refetch,
  // which it cannot do without being told how long it has.
  raw.project.media.source_url, raw.project.media.thumbnail_url,
  raw.project.media.expires_in_s,
  raw.project.jobs.map((j) => [j.job_id, j.kind, j.state, j.progress, j.stage]),
  // What the project cost and what the next paid run will. `spent_usd` is
  // exact; `suggest_estimate_usd` is a measurement with its sample count
  // beside it, and the page is not allowed to show one without the other.
  raw.project.job_history_limit,
  raw.project.spend.spent_usd, raw.project.spend.priced_jobs,
  raw.project.spend.keep_days, raw.project.spend.suggest_estimate_usd,
  raw.project.spend.suggest_samples, raw.project.spend.stt_measured,
  raw.project.spend.suggest_basis_shorts,
  // How many ideas to ask for. The picker's range comes from the server, and
  // what was asked travels with what came back so a shortfall can be said.
  raw.project.suggest_limits.shorts_max, raw.project.suggest_limits.youtube_max,
  raw.project.suggest_limits.shorts_default, raw.project.suggest_limits.youtube_default,
  raw.project.suggestions.requested_shorts, raw.project.suggestions.requested_youtube,
  // The export guard's own verdict. The page disables its export buttons
  // from `blocks_export` rather than working the rule out a second time.
  raw.project.translation.needed, raw.project.translation.lines,
  raw.project.translation.translated, raw.project.translation.missing,
  raw.project.translation.blocks_export, raw.project.translation.used_for,
  // The lines the suggestions cut: what follows them on its own, what the
  // export asks about, and which rows the editor opens on.
  raw.project.translation.suggested_lines, raw.project.translation.suggested_missing,
  raw.project.translation.suggested_ids,
  // Whether the export reads a Mongolian voice and why it cannot right now —
  // the voice guard's own verdict, as for the translation above.
  raw.project.voice.on, raw.project.voice.blocked,
  // Who speaks, for a voice each: counted and quoted by the server, because
  // a bare `speaker_1` identifies nobody.
  raw.project.speakers.map((s) => [s.id, s.lines, s.sample]),

  // creating one, and the upload that follows
  raw.create_project.project_id, raw.create_project.upload_url,
  raw.create_project.upload_key, raw.create_project.upload_expires_in_s,
  raw.upload_complete.project_id, raw.upload_complete.job_id,
  // the same upload in parts (lib/upload.ts), and starting it again
  raw.multipart_started.mode, raw.multipart_started.upload_id, raw.multipart_started.part_size,
  raw.multipart_started.part_count, raw.multipart_started.sign_batch,
  raw.multipart_signed.urls,
  raw.multipart_status.part_size, raw.multipart_status.part_count, raw.multipart_status.sign_batch,
  raw.multipart_status.parts.map((part) => [part.part_number, part.size]),
  raw.multipart_completed.completed, raw.multipart_aborted.aborted,
  raw.upload_restarted.project_id, raw.upload_restarted.upload_url,

  // outputs: play and download are two different URLs on purpose — the
  // second carries a Content-Disposition, so one link cannot do both.
  raw.outputs.map((o) => [
    o.id, o.kind, o.title, o.duration_sec, o.size_bytes, o.created_at,
    o.play_url, o.download_url, o.srt_url,
  ]),

  // jobs
  raw.job.job_id, raw.job.kind, raw.job.project_id, raw.job.state,
  raw.job.progress, raw.job.stage, raw.job.message, raw.job.result,
  raw.job.attempts, raw.job.created_at, raw.job.updated_at, raw.job.finished_at,
  raw.job_failed.error,
  // `result` was `Record<string, unknown>`, which is how a metered cost sat
  // in the payload from the first day with nothing able to read it. These
  // two are a contract now; the rest of the object is still per-kind.
  raw.job.result.elapsed_sec, raw.job.result.llm.calls,
  raw.job.result.llm.prompt_tokens, raw.job.result.llm.completion_tokens,
  raw.job.result.llm.cost_usd, raw.job.result.llm.models,
  raw.job_failed.result.elapsed_sec,
  // What a voice-over did, in characters — the one cost of it this system
  // can state — and the lines it had to hurry or cut.
  raw.job_voiced.result.voice.lines, raw.job_voiced.result.voice.synthesized,
  raw.job_voiced.result.voice.characters, raw.job_voiced.result.voice.cached,
  raw.job_voiced.result.voice.missing, raw.job_voiced.result.voice.sped_up,
  raw.job_voiced.result.voice.cut,
  // With the speech removed: the separation is worker time, and the reason
  // an export ran long.
  raw.job_voiced.result.voice.beds_separated, raw.job_voiced.result.voice.bed_seconds,
  raw.job_voiced.result.voice.beds_cached,
  raw.queue.counts, raw.queue.waiting, raw.queue.live_workers, raw.queue.stalled,
  raw.queue.worker_seen_at,
  // The disk block answers "will the next export fit" — the question that
  // used to require reading the worker's logs. Two machines, named: the
  // worker's disk is the one a render fills.
  raw.queue.api_disk.free_bytes, raw.queue.api_disk.total_bytes,
  raw.queue.api_disk.used_bytes, raw.queue.api_disk.min_free_bytes,
  raw.queue.worker_disk.free_bytes, raw.queue.worker_disk.total_bytes,
  raw.queue.worker_disk.min_free_bytes, raw.queue.worker_disk.measured_at,

  // provider settings: the VALUE never crosses this boundary, only where it
  // came from and enough of a tail to tell two keys apart.
  raw.provider_settings.openrouter_api_key.source,
  raw.provider_settings.openrouter_api_key.set,
  raw.provider_settings.openrouter_api_key.hint,
  raw.provider_settings.duudlaga_api_key.set,
  raw.provider_settings.elevenlabs_api_key.set,
  raw.provider_settings.openrouter_model.hint,
  raw.provider_settings.stt_provider.hint,
  raw.provider_settings_saved.changed,
  raw.provider_settings.elevenlabs_tts_voice_id.hint,
  raw.provider_settings.elevenlabs_tts_model.hint,

  // the voice picker: asked, never guessed — `mongolian` may be null
  raw.tts_voices.model, raw.tts_voices.voice_id, raw.tts_voices.mongolian,
  raw.tts_voices.error,
  raw.tts_voices.voices.map((v) => [v.id, v.name, v.category, v.gender, v.accent, v.preview_url]),
  // the producer's list for a speaker's voice — thinner than the admin's,
  // with the default a speaker without one is read in
  raw.project_voices.default_voice_id, raw.project_voices.error,
  raw.project_voices.voices.map((v) => [v.id, v.name, v.gender, v.accent, v.preview_url]),

  // what can and cannot run
  raw.readiness.capabilities.map((c) => [c.name, c.label, c.ready, c.blocked]),
  raw.readiness.brand.logo, raw.readiness.brand.intro, raw.readiness.brand.outro,
  // the waiting job's place in line, and the list cards' newest job
  raw.job_queued.queue_ahead,
  raw.latest_jobs.jobs.map((j) => [j.project_id, j.job_id, j.kind, j.state, j.progress, j.finished_at]),
  raw.providers.capabilities.map((c) => [
    c.name, c.label, c.ready, c.blocked, c.provider, c.powers, c.configured,
    c.implemented,
  ]),
  raw.providers.stt_providers, raw.providers.stt.provider, raw.providers.stt.ok,
  // The recogniser's balance — fetched on every visit and, until it was
  // read here, thrown away.
  raw.providers.stt.balance.map((b) => [b.name, b.value]),
  // how the stored keys are kept: the admin page's warning reads all five
  raw.providers.secret_storage.encrypted, raw.providers.secret_storage.variable,
  raw.providers.secret_storage.plaintext, raw.providers.secret_storage.unreadable,
  raw.providers.secret_storage.error,

  // the admin's users screen
  raw.admin_users.map((u) => [u.id, u.username, u.role, u.active, u.created_at, u.projects]),
  raw.admin_user_created.id, raw.admin_password_reset.updated,
  raw.admin_user_active.active,
  // the admin's jobs screen
  raw.admin_jobs.total, raw.admin_jobs.limit, raw.admin_jobs.offset,
  raw.admin_jobs.jobs.map((j) => [
    j.job_id, j.kind, j.state, j.error, j.attempts, j.created_at, j.finished_at,
    j.project_name, j.owner, j.no_retry, j.requeued_at, j.retryable, j.result,
  ]),
  raw.admin_job_retried.requeued,
  raw.admin_job_stats.days, raw.admin_job_stats.keep_days,
  [raw.admin_job_stats.overall, ...raw.admin_job_stats.kinds].map((r) => [
    r.done, r.failed, r.canceled, r.success_pct, r.run_p50_sec, r.run_p95_sec,
    r.wait_p50_sec, r.wait_p95_sec, r.wait_samples,
  ]),
  raw.admin_job_stats.kinds.map((k) => k.kind),
  raw.admin_job_stats.queue.waiting, raw.admin_job_stats.queue.running,
  raw.admin_job_stats.queue.live_workers, raw.admin_job_stats.queue.stalled,
  raw.admin_job_stats.queue.oldest_waiting_sec,

  // brand assets, empty and filled
  raw.brand.storage, raw.brand.logo.key, raw.brand.logo.url,
  raw.brand_empty.logo, raw.brand_empty.intro, raw.brand_empty.outro,
  raw.brand_upload_url.key, raw.brand_upload_url.url,

  // the spend ledger: every column the admin table draws, and the null
  // beside a price, which it draws as "тодорхойгүй" rather than $0
  raw.usage.months, raw.usage.timezone,
  raw.usage.rows.map((r) => [
    r.month, r.provider, r.kind, r.user_id, r.username, r.requests, r.prompt_tokens,
    r.completion_tokens, r.audio_seconds, r.characters, r.cost_usd, r.unpriced_requests,
  ]),
  raw.usage.month_totals.map((m) => [m.month, m.requests, m.cost_usd, m.unpriced_requests]),
  raw.usage.totals.requests, raw.usage.totals.cost_usd, raw.usage.totals.unpriced_requests,
  raw.usage.rates.duudlaga_usd_per_minute, raw.usage.rates.elevenlabs_stt_usd_per_minute,
  raw.usage.rates.elevenlabs_tts_usd_per_1k_chars,

  // subtitles
  raw.fonts.families, raw.fonts.default,
  raw.templates.templates.map((t) => [t.id, t.name, t.created_at, t.style.font_family]),
  raw.template_saved.id, raw.template_saved.name, raw.template_saved.style.enabled,
  raw.template_deleted.deleted,

  // the small write answers
  raw.transcript_updated.updated, raw.transcript_updated.unknown_ids,
  // A corrected line loses its translation; the editor says how many did.
  raw.transcript_updated.translations_cleared,
  raw.ranges_selected.clips,
  raw.transcribe_started.job_id, raw.suggest_started.job_id,
  raw.export_started.job_id, raw.export_all_started.job_id,
  raw.translate_started.job_id,
  raw.job_canceled.canceled, raw.project_deleted.deleted,
  raw.output_deleted.deleted,
];

/* ------------------------------------------------------------------ *
 * 3. Enumerations the frontend mirrors.
 *
 * A label map is not type-checked against anything: `Record<string, string>`
 * accepts any set of keys, so a kind added on the server simply has no entry
 * and the UI shows the English identifier to a user of a product whose every
 * other string is Mongolian. That is not hypothetical — `audio` shipped as a
 * stage with no label and was found by reading the screen.
 * ------------------------------------------------------------------ */

const backendKinds = Object.keys(registry.job_kinds);
const unlabelledKinds = backendKinds.filter((k) => !JOB_LABELS[k]);
if (unlabelledKinds.length) {
  throw new Error(
    `JOB_LABELS-д дутуу ажлын төрөл: ${unlabelledKinds.join(", ")} — ` +
      "`src/lib/jobs.ts`-д монгол нэр нэмнэ үү (backend `app/jobs/kinds.KINDS`).",
  );
}
const strayKinds = Object.keys(JOB_LABELS).filter((k) => !backendKinds.includes(k));
if (strayKinds.length) {
  throw new Error(
    `JOB_LABELS-д илүү ажлын төрөл: ${strayKinds.join(", ")} — backend-д тийм төрөл алга.`,
  );
}

/**
 * Stages are checked one way only. The backend emits them from a dozen call
 * sites with no registry to compare against, so this can prove a label is
 * unused but not that one is missing — and saying so is better than implying
 * a coverage it does not have.
 */
const emitted = new Set(contracts.project.jobs.map((j) => j.stage).filter(Boolean));
const strayStages = [...emitted].filter((s) => !STAGE_LABELS[s]);
if (strayStages.length) {
  throw new Error(
    `STAGE_LABELS-д дутуу шат: ${strayStages.join(", ")} — ` +
      "`src/lib/jobs.ts`-д монгол нэр нэмнэ үү.",
  );
}

console.log(
  `✓ Типүүд таарч байна · гэрээ ${Object.keys(contracts).length} · ` +
    `талбар ${present.length} · ажлын төрөл ${backendKinds.length} · ` +
    `гаралт ${Object.keys(registry.output_kinds).length} · ` +
    `огтлолын үүрэг ${Object.keys(registry.cut_roles).length}`,
);

// Keeps the compiler from pruning the two lists above as unused. They exist
// to be checked, not to be read.
void [
  token, me, projects, project, created, outputs, job, jobFailed, jobVoiced, jobQueued, latestJobs, ttsVoices,
  projectVoices, queue,
  providerSettings, readiness, brand, brandEmpty, fonts, templateSaved, usage,
  providers, adminUsers, adminJobs, jobStats, writes, present,
];
