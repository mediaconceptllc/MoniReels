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
  BrandSettings,
  Capability,
  CreateProjectResponse,
  Job,
  Me,
  Output,
  Project,
  ProjectDocument,
  ProjectSummary,
  ProviderReadiness,
  ProviderSettings,
  QueueStatus,
  SubtitleFonts,
  SubtitleTemplate,
  TokenResponse,
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
const queue = contracts.queue as QueueStatus;
const providerSettings = contracts.provider_settings as ProviderSettings;
const readiness = contracts.readiness as ProviderReadiness;
const brand = contracts.brand as BrandSettings;
const brandEmpty = contracts.brand_empty as BrandSettings;
const fonts = contracts.fonts as SubtitleFonts;
const templateSaved = contracts.template_saved as SubtitleTemplate;

/**
 * The admin providers view, declared inline in api.ts rather than in types.ts
 * — which is exactly why it needs pinning here: an inline type has no other
 * reader to notice it drifting.
 */
const providers = contracts.providers as {
  capabilities: Capability[];
  stt_providers: string[];
  stt: { provider: string };
};

/** The small write-path answers. Each one is a single field, and a single
 *  field is what a rename removes entirely. */
const writes = {
  upload_complete: contracts.upload_complete as { project_id: string; job_id: string },
  transcript_updated: contracts.transcript_updated as { updated: number; unknown_ids: string[] },
  ranges_selected: contracts.ranges_selected as { clips: number },
  transcribe_started: contracts.transcribe_started as { job_id: string },
  suggest_started: contracts.suggest_started as { job_id: string },
  export_started: contracts.export_started as { job_id: string },
  export_all_started: contracts.export_all_started as { job_id: string },
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
  project_patched: contracts.project_patched as ProjectDocument,
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
  ]),

  // the project itself
  raw.project.schema_version, raw.project.id, raw.project.name,
  raw.project.created_at, raw.project.updated_at,
  raw.project.video.source_key, raw.project.video.duration_sec,
  raw.project.video.width, raw.project.video.height, raw.project.video.fps,
  raw.project.video.has_audio, raw.project.video.codec,
  raw.project.video.thumbnail_key, raw.project.video.audio_key,
  raw.project.transcript.language, raw.project.transcript.full_text,
  raw.project.transcript.timings_estimated,
  raw.project.transcript.segments.map((s) => [s.id, s.start, s.end, s.text, s.speaker]),
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
  raw.project.export.logo.enabled, raw.project.export.logo.position,
  raw.project.export.logo.width_pct, raw.project.export.logo.opacity,
  raw.project.export.logo.margin_pct,
  // Signed and short-lived: a page left open past the expiry has to refetch,
  // which it cannot do without being told how long it has.
  raw.project.media.source_url, raw.project.media.thumbnail_url,
  raw.project.media.expires_in_s,
  raw.project.jobs.map((j) => [j.job_id, j.kind, j.state, j.progress, j.stage]),

  // creating one, and the upload that follows
  raw.create_project.project_id, raw.create_project.upload_url,
  raw.create_project.upload_key, raw.create_project.upload_expires_in_s,
  raw.upload_complete.project_id, raw.upload_complete.job_id,

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
  raw.queue.counts, raw.queue.waiting, raw.queue.live_workers, raw.queue.stalled,
  // The disk block answers "will the next export fit" — the question that
  // used to require reading the worker's logs.
  raw.queue.disk.free_bytes, raw.queue.disk.total_bytes,
  raw.queue.disk.used_bytes, raw.queue.disk.min_free_bytes,

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

  // what can and cannot run
  raw.readiness.capabilities.map((c) => [c.name, c.label, c.ready, c.blocked]),
  raw.providers.capabilities.map((c) => [
    c.name, c.label, c.ready, c.blocked, c.provider, c.powers, c.configured,
    c.implemented,
  ]),
  raw.providers.stt_providers, raw.providers.stt.provider,

  // brand assets, empty and filled
  raw.brand.storage, raw.brand.logo.key, raw.brand.logo.url,
  raw.brand_empty.logo, raw.brand_empty.intro, raw.brand_empty.outro,
  raw.brand_upload_url.key, raw.brand_upload_url.url,

  // subtitles
  raw.fonts.families, raw.fonts.default,
  raw.templates.templates.map((t) => [t.id, t.name, t.created_at, t.style.font_family]),
  raw.template_saved.id, raw.template_saved.name, raw.template_saved.style.enabled,
  raw.template_deleted.deleted,

  // the small write answers
  raw.transcript_updated.updated, raw.transcript_updated.unknown_ids,
  raw.ranges_selected.clips,
  raw.transcribe_started.job_id, raw.suggest_started.job_id,
  raw.export_started.job_id, raw.export_all_started.job_id,
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
  token, me, projects, project, created, outputs, job, jobFailed, queue,
  providerSettings, readiness, brand, brandEmpty, fonts, templateSaved,
  providers, writes, present,
];
