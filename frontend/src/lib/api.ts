/**
 * Typed API client.
 *
 * Two rules carried over from the desktop client, both learned the hard way:
 *
 * 1. A POST is never retried automatically. A lost response does not mean the
 *    request never arrived — resubmitting one that starts a job can start a
 *    SECOND render against the same outputs.
 * 2. A job stream gets no timeout. A single LLM call regularly runs for
 *    minutes with no intermediate output; a normal request timeout kills a
 *    connection whose server is working perfectly.
 */

import { describeValidation } from "./messages";
import type {
  AdminJobsPage,
  AdminUser,
  BrandAsset,
  BrandSettings,
  CreateProjectResponse,
  Job,
  JobStats,
  Me,
  MultipartSigned,
  MultipartStart,
  MultipartStatus,
  Output,
  Project,
  ProjectSummary,
  ProjectSettingsSaved,
  ProjectVoices,
  ProviderReadiness,
  ProviderSettings,
  ProviderSettingsPatch,
  ProviderStatus,
  QueueStatus,
  Role,
  SourceLanguage,
  SubtitleFonts,
  SubtitleStyle,
  SubtitleTemplate,
  TokenResponse,
  TtsVoices,
  UsageReport,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "monireels.token";

/**
 * True when the bundle fell back to the localhost default but is running
 * somewhere that is not localhost.
 *
 * NEXT_PUBLIC_* is inlined at BUILD time. A deployment built without
 * NEXT_PUBLIC_API_URL therefore ships a client that calls localhost, and
 * every request fails instantly — indistinguishable, from the error alone,
 * from the server being down. Saying "check your network" there sends the
 * reader somewhere there is nothing to find. (Observed on the first
 * production deploy.)
 */
function isMisconfigured(): boolean {
  if (typeof window === "undefined") return false;
  const local = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:|$)/;
  return local.test(BASE) && !local.test(window.location.origin);
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    // A private window or blocked site data throws on access rather than
    // returning null — never let that take the whole page down.
    return null;
  }
}

export function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* see getToken */
  }
}

/** Calls whose 401 is about the password just typed, not about the session.
 *  Treating them like an expired session told someone who mistyped their
 *  password that their login had "expired" — and on the profile page it
 *  deleted a perfectly good token while the header still showed them signed
 *  in. Their answer is the server's own words, and the stored token stays. */
const CREDENTIAL_CHECKS = new Set(["/auth/login", "/auth/password"]);

/** Fired when a stored session stops working mid-page, so the app can ask
 *  for the password again over the page instead of navigating away from
 *  whatever unsaved work is on it. */
export const SESSION_EXPIRED = "monireels:session-expired";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch {
    // Name the actual cause. A configuration mistake and a network outage
    // look identical from a failed fetch, and only one of them is something
    // the person reading this can act on.
    if (isMisconfigured()) {
      throw new ApiError(
        0,
        "Энэ хувилбар серверийн хаяггүй угсрагдсан байна (NEXT_PUBLIC_API_URL). " +
          "Тохиргоог засаад дахин deploy хийх шаардлагатай.",
      );
    }
    throw new ApiError(0, "Серверт холбогдож чадсангүй. Сүлжээгээ шалгана уу.");
  }

  if (response.status === 401) {
    if (!token || CREDENTIAL_CHECKS.has(path)) {
      throw new ApiError(401, await detailOf(response));
    }
    // Either expired, or ended by a password change. Both mean the same
    // thing to this client: the stored token is worthless, drop it.
    setToken(null);
    if (typeof window !== "undefined") window.dispatchEvent(new Event(SESSION_EXPIRED));
    throw new ApiError(401, "Нэвтрэх хугацаа дууссан байна. Дахин нэвтэрнэ үү.");
  }

  if (!response.ok) {
    throw new ApiError(response.status, await detailOf(response));
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function detailOf(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    // FastAPI validation errors arrive as a list of {type, loc, msg}.
    if (Array.isArray(detail)) return describeValidation(detail);
  } catch {
    /* fall through to the status text */
  }
  return `Алдаа гарлаа (${response.status})`;
}

export const api = {
  // -- auth ---------------------------------------------------------------
  login: (username: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  me: () => request<Me>("/auth/me"),

  changePassword: (current_password: string, new_password: string) =>
    request<TokenResponse>("/auth/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),

  // -- projects -----------------------------------------------------------
  listProjects: () => request<ProjectSummary[]>("/projects"),

  getProject: (id: string) => request<Project>(`/projects/${id}`),

  createProject: (name: string, filename: string, size_bytes: number, language: SourceLanguage) =>
    request<CreateProjectResponse>("/projects", {
      method: "POST",
      body: JSON.stringify({ name, filename, size_bytes, language }),
    }),

  uploadComplete: (id: string) =>
    request<{ project_id: string; job_id: string }>(`/projects/${id}/upload-complete`, {
      method: "POST",
    }),

  /** A new source for a project whose video never arrived or failed to
   *  import: a fresh presigned PUT, the old object and parts cleared. */
  reupload: (id: string, filename: string, size_bytes: number) =>
    request<CreateProjectResponse>(`/projects/${id}/upload`, {
      method: "POST",
      body: JSON.stringify({ filename, size_bytes }),
    }),

  // Multipart upload — see lib/upload.ts. The server decides whether to
  // split, signs parts in batches, and completes from R2's own listing.
  startMultipart: (id: string) =>
    request<MultipartStart>(`/projects/${id}/upload/multipart`, { method: "POST" }),

  signParts: (id: string, upload_id: string, part_numbers: number[]) =>
    request<MultipartSigned>(`/projects/${id}/upload/multipart/sign`, {
      method: "POST",
      body: JSON.stringify({ upload_id, part_numbers }),
    }),

  multipartStatus: (id: string, upload_id: string) =>
    request<MultipartStatus>(`/projects/${id}/upload/multipart/status`, {
      method: "POST",
      body: JSON.stringify({ upload_id }),
    }),

  completeMultipart: (id: string, upload_id: string) =>
    request<{ completed: boolean; parts: number }>(`/projects/${id}/upload/multipart/complete`, {
      method: "POST",
      body: JSON.stringify({ upload_id }),
    }),

  abortMultipart: (id: string, upload_id: string) =>
    request<{ aborted: boolean }>(`/projects/${id}/upload/multipart/abort`, {
      method: "POST",
      body: JSON.stringify({ upload_id }),
    }),

  /** Answers with what it saved — the settings, not the document. Read the
   *  project again for anything derived from them (translation, voice). */
  updateProject: (id: string, patch: Record<string, unknown>) =>
    request<ProjectSettingsSaved>(`/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  deleteProject: (id: string) =>
    request<{ deleted: boolean }>(`/projects/${id}`, { method: "DELETE" }),

  /** Each line sends `text`, `translation`, or both. Correcting `text`
   *  clears that line's translation unless the same edit gives a new one —
   *  the response counts how many were cleared. */
  updateTranscript: (id: string, segments: { id: string; text?: string; translation?: string }[]) =>
    request<{ updated: number; unknown_ids: string[]; translations_cleared: number }>(
      `/projects/${id}/transcript`,
      { method: "PUT", body: JSON.stringify({ segments }) },
    ),

  selectRanges: (id: string, ranges: [number, number][]) =>
    request<{ clips: number }>(`/projects/${id}/select`, {
      method: "POST",
      body: JSON.stringify({ ranges }),
    }),

  // -- pipeline -----------------------------------------------------------
  /** Without `force` the server refuses (409) a project that already has a
   *  transcript: recognising again replaces every line, hand correction and
   *  translation, and is billed again — so the page asks first. */
  transcribe: (id: string, { force = false }: { force?: boolean } = {}) =>
    request<{ job_id: string }>(`/projects/${id}/transcribe`, {
      method: "POST",
      body: JSON.stringify({ force }),
    }),

  /** Translate what was said into Mongolian subtitles. Without `force` only
   *  lines that have no translation are sent — which is also how a run that
   *  failed part-way is finished without paying for the rest again. `force`
   *  sends every line and replaces hand edits. `scope: "suggested"` sends
   *  only the lines the suggestions cut — what runs by itself after them. */
  translate: (
    id: string,
    { force = false, scope = "all" }: { force?: boolean; scope?: "all" | "suggested" } = {},
  ) =>
    request<{ job_id: string }>(`/projects/${id}/translate`, {
      method: "POST",
      body: JSON.stringify({ force, scope }),
    }),

  /** Asks for `counts` ideas; omit them and the server applies what the
   *  button always meant. A count the video cannot hold is refused with 422,
   *  never quietly lowered. Replacing ideas that exist needs `force`, the
   *  same refusal as `transcribe`. */
  suggest: (
    id: string,
    counts?: { shorts: number; youtube: number },
    { force = false }: { force?: boolean } = {},
  ) =>
    request<{ job_id: string }>(`/projects/${id}/suggest`, {
      method: "POST",
      body: JSON.stringify({ ...counts, force }),
    }),

  /** Renders the model's ideas. `pick` names WHICH ones — omit it and every
   *  idea is rendered, which is what this did before there was a choice. */
  exportAll: (id: string, pick?: { shorts?: string[]; youtube?: number[] }) =>
    request<{ job_id: string }>(`/projects/${id}/export-all`, {
      method: "POST",
      ...(pick ? { body: JSON.stringify(pick) } : {}),
    }),

  exportTimeline: (id: string) =>
    request<{ job_id: string }>(`/projects/${id}/export`, { method: "POST" }),

  // -- outputs ------------------------------------------------------------
  listOutputs: (id: string) => request<Output[]>(`/projects/${id}/outputs`),

  deleteOutput: (projectId: string, outputId: string) =>
    request<{ deleted: boolean }>(`/projects/${projectId}/outputs/${outputId}`, {
      method: "DELETE",
    }),

  // -- jobs ---------------------------------------------------------------
  getJob: (jobId: string) => request<Job>(`/jobs/${jobId}`),

  cancelJob: (jobId: string) =>
    request<{ canceled: boolean }>(`/jobs/${jobId}/cancel`, { method: "POST" }),

  queueStatus: () => request<QueueStatus>("/jobs/queue"),

  // -- admin --------------------------------------------------------------
  providerSettings: () => request<ProviderSettings>("/admin/settings"),

  /** Free: ElevenLabs is asked for its voice list, nothing is synthesised. */
  ttsVoices: () => request<TtsVoices>("/admin/tts/voices"),

  saveProviderSettings: (patch: ProviderSettingsPatch) =>
    request<{ changed: string[]; settings: ProviderSettings }>("/admin/settings", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  /** The admin view: what each key powers, plus reachability and balance. */
  providers: () => request<ProviderStatus>("/admin/providers"),

  adminUsers: () => request<AdminUser[]>("/admin/users"),

  createUser: (body: { username: string; password: string; role: Role }) =>
    request<{ id: string; username: string; role: Role }>("/admin/users", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** In the body, never the query string: the request line is what access
   *  logs record. */
  resetPassword: (userId: string, newPassword: string) =>
    request<{ updated: boolean }>(`/admin/users/${userId}/password`, {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword }),
    }),

  setUserActive: (userId: string, active: boolean) =>
    request<{ active: boolean }>(`/admin/users/${userId}/active?active=${active}`, {
      method: "POST",
    }),

  adminJobs: (filters: { state?: string; kind?: string; limit: number; offset: number }) => {
    const query = new URLSearchParams({ limit: String(filters.limit), offset: String(filters.offset) });
    if (filters.state) query.set("state", filters.state);
    if (filters.kind) query.set("kind", filters.kind);
    return request<AdminJobsPage>(`/admin/jobs?${query}`);
  },

  jobStats: (days: number) => request<JobStats>(`/admin/jobs/stats?days=${days}`),

  retryJob: (jobId: string) =>
    request<{ requeued: boolean }>(`/admin/jobs/${jobId}/retry`, { method: "POST" }),

  brand: () => request<BrandSettings>("/admin/brand"),

  /** The spend ledger, whole UTC months back from this one. Admin only on
   *  the server. */
  usageReport: (months: number) => request<UsageReport>(`/admin/usage?months=${months}`),

  /** What can and cannot run. Readable by anyone signed in — the warning has
   *  to appear on the page with the paid button, and that page is not
   *  admin-only. */
  providerReadiness: () => request<ProviderReadiness>("/projects/providers/status"),

  /** The voices a speaker can be given. Readable by anyone signed in, like
   *  the readiness above: the producer choosing a speaker's voice is not an
   *  admin. Free — nothing is synthesised. */
  projectVoices: () => request<ProjectVoices>("/projects/tts/voices"),

  subtitleFonts: () => request<SubtitleFonts>("/projects/subtitle/fonts"),

  subtitleTemplates: () =>
    request<{ templates: SubtitleTemplate[] }>("/projects/subtitle/templates"),

  /** Admin only on the server; the button is simply not drawn otherwise. */
  saveSubtitleTemplate: (name: string, style: SubtitleStyle) =>
    request<SubtitleTemplate>("/projects/subtitle/templates", {
      method: "POST",
      body: JSON.stringify({ name, style }),
    }),

  deleteSubtitleTemplate: (id: string) =>
    request<{ deleted: string }>(`/projects/subtitle/templates/${id}`, { method: "DELETE" }),

  /** Presigned PUT for a brand asset. The file goes straight to storage —
   *  see lib/upload.ts — and only the key comes back here. */
  brandUploadUrl: (asset: BrandAsset, contentType: string) =>
    request<{ key: string; url: string }>(`/admin/brand/${asset}/upload-url`, {
      method: "POST",
      body: JSON.stringify({ content_type: contentType }),
    }),

  /** `key: null` clears the slot. */
  saveBrandAsset: (asset: BrandAsset, key: string | null) =>
    request<BrandSettings>(`/admin/brand/${asset}`, {
      method: "PUT",
      body: JSON.stringify({ key }),
    }),
};

export { BASE as API_BASE };
