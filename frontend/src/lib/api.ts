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

import type {
  BrandAsset,
  Capability,
  ProviderReadiness,
  SubtitleFonts,
  SubtitleStyle,
  SubtitleTemplate,
  BrandSettings,
  CreateProjectResponse,
  Job,
  Me,
  Output,
  Project,
  ProjectSummary,
  ProviderSettings,
  ProviderSettingsPatch,
  QueueStatus,
  TokenResponse,
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
    // Either expired, or ended by a password change. Both mean the same
    // thing to this client: the stored token is worthless, drop it.
    setToken(null);
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
    // FastAPI validation errors arrive as a list of {loc, msg}.
    if (Array.isArray(detail)) {
      return detail.map((d: { msg?: string }) => d.msg ?? "Буруу утга").join("; ");
    }
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

  createProject: (name: string, filename: string, size_bytes: number) =>
    request<CreateProjectResponse>("/projects", {
      method: "POST",
      body: JSON.stringify({ name, filename, size_bytes }),
    }),

  uploadComplete: (id: string) =>
    request<{ project_id: string; job_id: string }>(`/projects/${id}/upload-complete`, {
      method: "POST",
    }),

  updateProject: (id: string, patch: Record<string, unknown>) =>
    request<Project>(`/projects/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),

  deleteProject: (id: string) =>
    request<{ deleted: boolean }>(`/projects/${id}`, { method: "DELETE" }),

  updateTranscript: (id: string, segments: { id: string; text: string }[]) =>
    request<{ updated: number; unknown_ids: string[] }>(`/projects/${id}/transcript`, {
      method: "PUT",
      body: JSON.stringify({ segments }),
    }),

  selectRanges: (id: string, ranges: [number, number][]) =>
    request<{ clips: number }>(`/projects/${id}/select`, {
      method: "POST",
      body: JSON.stringify({ ranges }),
    }),

  // -- pipeline -----------------------------------------------------------
  transcribe: (id: string) =>
    request<{ job_id: string }>(`/projects/${id}/transcribe`, { method: "POST" }),

  suggest: (id: string) => request<{ job_id: string }>(`/projects/${id}/suggest`, { method: "POST" }),

  exportAll: (id: string) =>
    request<{ job_id: string }>(`/projects/${id}/export-all`, { method: "POST" }),

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

  saveProviderSettings: (patch: ProviderSettingsPatch) =>
    request<{ changed: string[]; settings: ProviderSettings }>("/admin/settings", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  /** The admin view: what each key powers, plus reachability and balance. */
  providers: () =>
    request<{ capabilities: Capability[]; stt_providers: string[]; stt: { provider: string } }>(
      "/admin/providers",
    ),

  brand: () => request<BrandSettings>("/admin/brand"),

  /** What can and cannot run. Readable by anyone signed in — the warning has
   *  to appear on the page with the paid button, and that page is not
   *  admin-only. */
  providerReadiness: () => request<ProviderReadiness>("/projects/providers/status"),

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
