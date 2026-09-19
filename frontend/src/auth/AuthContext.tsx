import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, setAccessToken, setSignedOutHandler } from "@/api/client";
import type { Principal } from "@/types/api";

interface AuthState {
  user: Principal | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string, fullName?: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Principal | null>(null);
  const [loading, setLoading] = useState(true);

  const loadMe = useCallback(async () => {
    try {
      setUser(await api<Principal>("/auth/me"));
    } catch {
      setUser(null);
    }
  }, []);

  useEffect(() => {
    setSignedOutHandler(() => setUser(null));
    // on a fresh page load the access token is gone (memory only); the HttpOnly refresh cookie may still be valid
    (async () => {
      try {
        const r = await fetch("/api/v1/auth/refresh", { method: "POST", credentials: "include" });
        if (r.ok) {
          const body = (await r.json()) as { access_token: string };
          setAccessToken(body.access_token);
          await loadMe();
        }
      } finally {
        setLoading(false);
      }
    })();
  }, [loadMe]);

  const login = useCallback(async (email: string, password: string) => {
    const body = await api<{ access_token: string }>("/auth/login", { method: "POST", body: { email, password } });
    setAccessToken(body.access_token);
    await loadMe();
  }, [loadMe]);

  const signup = useCallback(async (email: string, password: string, fullName?: string) => {
    const body = await api<{ access_token: string }>("/auth/signup", {
      method: "POST",
      body: { email, password, full_name: fullName || null },
    });
    setAccessToken(body.access_token);
    await loadMe();
  }, [loadMe]);

  const logout = useCallback(async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      setAccessToken(null);
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, signup, logout }),
    [user, loading, login, signup, logout],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
