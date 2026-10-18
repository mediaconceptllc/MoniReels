"use client";

/**
 * Session state.
 *
 * The token is the whole session — there is no cookie and no refresh flow.
 * A 401 on a call made with it clears it (see lib/api.ts), because both
 * causes, expiry and a password change elsewhere, mean the same thing here:
 * what is stored is worthless. The page stays where it is and asks for the
 * password over it (SignInAgain), so unsaved work survives the session.
 *
 * Only a 401 ends a session. A server that cannot be reached — a deploy
 * restarting, a phone between networks — used to sign the user out on page
 * load; it is now retried, and said, while the token is kept.
 *
 * This gates the UI only. Every real access decision is made by the server;
 * hiding a button is a convenience, never a control.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { SignInAgain } from "@/components/SignInAgain";
import { ApiError, SESSION_EXPIRED, api, getToken, setToken } from "./api";
import type { Me, TokenResponse } from "./types";

/** Waits between attempts to reach the server on page load. */
const RETRY_MS = [1000, 2000, 4000, 8000];

interface AuthState {
  user: Me | null;
  loading: boolean;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [expired, setExpired] = useState(false);
  // Why the server could not be reached, while the check is being retried.
  const [unreachable, setUnreachable] = useState<string | null>(null);
  const router = useRouter();

  useEffect(() => {
    // A stored token can be expired or revoked, so it is verified against the
    // server rather than trusted — otherwise the first real action fails with
    // a 401 after the user already believes they are signed in.
    if (!getToken()) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = (attempt: number) => {
      api
        .me()
        .then((me) => {
          if (cancelled) return;
          setUser(me);
          setUnreachable(null);
          setLoading(false);
        })
        .catch((err) => {
          if (cancelled) return;
          // No answer at all, or the server failing: the token may be fine.
          // A 401 has already cleared it; any other refusal ends the check.
          const transient = err instanceof ApiError && (err.status === 0 || err.status >= 500);
          if (!transient) {
            setUnreachable(null);
            setLoading(false);
            return;
          }
          setUnreachable(err.message);
          timer = setTimeout(() => check(attempt + 1), RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)]);
        });
    };
    check(0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    const onExpired = () => setExpired(true);
    window.addEventListener(SESSION_EXPIRED, onExpired);
    return () => window.removeEventListener(SESSION_EXPIRED, onExpired);
  }, []);

  const signedInAgain = useCallback(async (response: TokenResponse) => {
    setToken(response.token);
    setUser(await api.me());
    setExpired(false);
  }, []);

  const signIn = useCallback(async (username: string, password: string) => {
    const response = await api.login(username, password);
    setToken(response.token);
    setUser({ id: "", username: response.username, role: response.role });
    const me = await api.me();
    setUser(me);
  }, []);

  const signOut = useCallback(() => {
    setToken(null);
    setUser(null);
    setExpired(false);
    router.push("/login");
  }, [router]);

  const value = useMemo(
    () => ({ user, loading, signIn, signOut }),
    [user, loading, signIn, signOut],
  );

  return (
    <AuthContext.Provider value={value}>
      {unreachable && (
        <div
          role="status"
          className="fixed inset-x-0 top-0 z-40 border-b border-warn/40 bg-warn-soft px-4 py-2.5 text-center text-sm text-warn"
        >
          {unreachable} Дахин оролдож байна…
        </div>
      )}
      {children}
      {/* Only over a page someone was signed in to: a 401 on the first load
          has no page to keep, and goes to the sign-in page as before. */}
      {expired && user && (
        <SignInAgain username={user.username} onSignedIn={signedInAgain} onLeave={signOut} />
      )}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}

/** Redirects to the sign-in page once it is certain there is no session. */
export function useRequireAuth(): AuthState {
  const auth = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (!auth.loading && !auth.user) router.replace("/login");
  }, [auth.loading, auth.user, router]);
  return auth;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Тодорхойгүй алдаа гарлаа.";
}
