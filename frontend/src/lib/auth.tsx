"use client";

/**
 * Session state.
 *
 * The token is the whole session — there is no cookie and no refresh flow.
 * A 401 anywhere clears it (see lib/api.ts), because both causes, expiry and
 * a password change elsewhere, mean the same thing here: what is stored is
 * worthless.
 *
 * This gates the UI only. Every real access decision is made by the server;
 * hiding a button is a convenience, never a control.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, api, getToken, setToken } from "./api";
import type { Me } from "./types";

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
  const router = useRouter();

  useEffect(() => {
    // A stored token can be expired or revoked, so it is verified against the
    // server rather than trusted — otherwise the first real action fails with
    // a 401 after the user already believes they are signed in.
    if (!getToken()) {
      setLoading(false);
      return;
    }
    api
      .me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
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
    router.push("/login");
  }, [router]);

  const value = useMemo(
    () => ({ user, loading, signIn, signOut }),
    [user, loading, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
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
