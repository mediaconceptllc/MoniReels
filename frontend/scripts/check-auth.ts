/**
 * Which 401s end a session, checked against the real request helper.
 *
 * Every 401 used to mean "your login expired": a mistyped password at sign-in
 * said so, and a mistyped CURRENT password on the profile page deleted a
 * working token while the header still showed the user signed in. Only a 401
 * on a call made with a token, other than the two credential checks, ends a
 * session — and that one must say so to the page (SESSION_EXPIRED), which is
 * what lets it ask for the password again without navigating away.
 *
 * Run: npm run check-auth
 */

const store = new Map<string, string>();
const fired: string[] = [];
(globalThis as Record<string, unknown>).window = {
  localStorage: {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  },
  dispatchEvent: (event: Event) => {
    fired.push(event.type);
    return true;
  },
  location: { origin: "http://localhost:3000" },
};

let detail = "";
globalThis.fetch = (async () =>
  new Response(JSON.stringify({ detail }), {
    status: 401,
    headers: { "Content-Type": "application/json" },
  })) as typeof fetch;

const problems: string[] = [];
function expect(name: string, ok: boolean, extra = "") {
  if (!ok) problems.push(`${name}${extra ? ` — ${extra}` : ""}`);
}

async function refusal(call: () => Promise<unknown>): Promise<{ status: number; message: string }> {
  try {
    await call();
  } catch (err) {
    const e = err as { status?: number; message: string };
    return { status: e.status ?? -1, message: e.message };
  }
  return { status: 200, message: "" };
}

async function main() {
  const { api, setToken, getToken, SESSION_EXPIRED } = await import("../src/lib/api");

  // A mistyped password at sign-in, with an old token still stored.
  setToken("stored");
  fired.length = 0;
  detail = "Нэвтрэх нэр эсвэл нууц үг буруу байна.";
  let r = await refusal(() => api.login("alice", "wrong"));
  expect("sign-in: the server's words are shown", r.message === detail, r.message);
  expect("sign-in: the stored token is kept", getToken() === "stored");
  expect("sign-in: no session-expired signal", fired.length === 0, fired.join());

  // A mistyped current password on the profile page.
  detail = "Одоогийн нууц үг буруу байна.";
  r = await refusal(() => api.changePassword("wrong", "brandnewpassword"));
  expect("password change: the server's words are shown", r.message === detail, r.message);
  expect("password change: the token is kept", getToken() === "stored");
  expect("password change: no session-expired signal", fired.length === 0, fired.join());

  // Any other call made with a token: the session has ended.
  detail = "Нэвтрэх хугацаа дууссан байна. Дахин нэвтэрнэ үү.";
  r = await refusal(() => api.listProjects());
  expect("expired session: status 401", r.status === 401);
  expect("expired session: the token is dropped", getToken() === null);
  expect("expired session: the page is told", fired.includes(SESSION_EXPIRED), fired.join());

  // Without a token there is no session to end; the server says why.
  fired.length = 0;
  detail = "Нэвтрэх шаардлагатай.";
  r = await refusal(() => api.listProjects());
  expect("no token: the server's words are shown", r.message === detail, r.message);
  expect("no token: no session-expired signal", fired.length === 0, fired.join());

  if (problems.length) {
    console.error(`check-auth: ${problems.length} problem(s)\n  ${problems.join("\n  ")}`);
    process.exit(1);
  }
  console.log("check-auth: credential 401s keep the token; a session 401 drops it and tells the page.");
}

void main();
