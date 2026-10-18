"use client";

/**
 * Accounts, without Swagger.
 *
 * Creating an account, resetting a forgotten password and closing the account
 * of someone who has left all existed as routes and nowhere else: the only way
 * in was /docs, typing JSON by hand, with the password for a colleague sitting
 * in a request body the browser kept in its history.
 *
 * The server decides everything here — who may do it, whether a name is free,
 * whether an account may be closed. The checks below only say the obvious
 * thing in Mongolian before a round trip would say it in pydantic's English.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { relativeTime } from "@/lib/format";
import type { AdminUser, Role } from "@/lib/types";
import { Alert, Badge, Button, Card, Field, Loading, Select, Skeleton, TextInput } from "@/components/ui";
import { ConfirmDialog } from "@/components/ConfirmDialog";

/** The server's own rules (app.schemas.Username, PASSWORD_MIN), repeated only
 *  to answer before the request does — the server still refuses either way. */
const USERNAME = /^[A-Za-z0-9._-]{3,64}$/;
const PASSWORD_MIN = 8;

const ROLE_LABEL: Record<Role, string> = { admin: "Админ", editor: "Продюсер" };

function passwordProblem(password: string): string | null {
  return password.length < PASSWORD_MIN ? `Нууц үг дор хаяж ${PASSWORD_MIN} тэмдэгт байна.` : null;
}

export function AdminUsers({ meId }: { meId: string }) {
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("editor");
  const [creating, setCreating] = useState(false);

  // One reset form open at a time: two would leave it unclear which password
  // the Save under them sends.
  const [resetting, setResetting] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [closing, setClosing] = useState<AdminUser | null>(null);

  const load = useCallback(async () => {
    try {
      setUsers(await api.adminUsers());
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function create() {
    const name = username.trim();
    const problem = !USERNAME.test(name)
      ? "Нэр 3–64 тэмдэгт, зөвхөн латин үсэг, тоо, «.», «_», «-» байна."
      : passwordProblem(password);
    if (problem) {
      setError(problem);
      return;
    }
    setCreating(true);
    setError(null);
    setNotice(null);
    try {
      const made = await api.createUser({ username: name, password, role });
      setUsername("");
      setPassword("");
      setRole("editor");
      // The list first, then the words: a notice that arrives before the row
      // it describes reads as a claim the page does not yet back up.
      await load();
      setNotice(`«${made.username}» бүртгэл үүслээ (${ROLE_LABEL[made.role]}).`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setCreating(false);
    }
  }

  async function reset(user: AdminUser) {
    const problem = passwordProblem(newPassword);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(user.id);
    setError(null);
    setNotice(null);
    try {
      await api.resetPassword(user.id, newPassword);
      // Said, because it is the half nobody expects: a reset signs them out.
      setNotice(`«${user.username}»-ийн нууц үг шинэчлэгдлээ. Нээлттэй байсан нэвтрэлтүүд нь дууслаа.`);
      setResetting(null);
      setNewPassword("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function setActive(user: AdminUser, active: boolean) {
    setBusy(user.id);
    setError(null);
    setNotice(null);
    try {
      await api.setUserActive(user.id, active);
      setClosing(null);
      await load();
      setNotice(active ? `«${user.username}» дахин нээгдлээ.` : `«${user.username}» хаагдлаа.`);
    } catch (err) {
      setError(errorMessage(err));
      setClosing(null);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {error && <Alert>{error}</Alert>}
      {notice && <Alert tone="accent">{notice}</Alert>}

      <Card className="p-5">
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <div>
            <h2 className="font-display text-lg font-semibold text-ink">Шинэ бүртгэл</h2>
            <p className="mt-1 text-sm text-ink-3">
              Нууц үгийг нь өөрт нь өгнө үү — нэвтэрсний дараа «Профайл» хэсгээс өөрөө сольж болно.
            </p>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Нэвтрэх нэр" hint="Латин үсэг, тоо, «.», «_», «-»">
              <TextInput
                value={username}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setUsername(e.target.value)}
              />
            </Field>
            <Field label="Нууц үг" hint={`Дор хаяж ${PASSWORD_MIN} тэмдэгт`}>
              <TextInput
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <Field label="Эрх">
              <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
                <option value="editor">{ROLE_LABEL.editor}</option>
                <option value="admin">{ROLE_LABEL.admin}</option>
              </Select>
            </Field>
          </div>
          <div>
            <Button type="submit" tone="primary" loading={creating} disabled={!username || !password}>
              Бүртгэл үүсгэх
            </Button>
          </div>
        </form>
      </Card>

      <Card className="p-5">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="font-display text-lg font-semibold text-ink">Хэрэглэгчид</h2>
            {users && (
              <span className="tabular text-xs text-ink-3">
                {users.length} бүртгэл · {users.filter((u) => u.active).length} идэвхтэй
              </span>
            )}
          </div>

          {!users ? (
            <Loading className="flex flex-col gap-3">
              <Skeleton className="h-16 rounded-md" />
              <Skeleton className="h-16 rounded-md" />
              <Skeleton className="h-16 rounded-md" />
            </Loading>
          ) : (
            <ul className="flex flex-col divide-y divide-rule-soft">
              {users.map((user) => {
                const self = user.id === meId;
                return (
                  <li key={user.id} className="flex flex-col gap-3 py-3">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                      <span className="font-medium text-ink [overflow-wrap:anywhere]">{user.username}</span>
                      <Badge tone={user.role === "admin" ? "accent" : "default"}>
                        {ROLE_LABEL[user.role]}
                      </Badge>
                      {user.active ? (
                        <Badge tone="fit">Идэвхтэй</Badge>
                      ) : (
                        <Badge tone="warn">Хаалттай</Badge>
                      )}
                      {self && <Badge>Та</Badge>}
                      <span className="tabular text-xs text-ink-3">
                        {user.projects} төсөл · Үүссэн: {relativeTime(user.created_at)}
                      </span>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        tone="quiet"
                        onClick={() => {
                          setResetting(resetting === user.id ? null : user.id);
                          setNewPassword("");
                        }}
                      >
                        Нууц үг шинэчлэх
                      </Button>
                      {/* Not drawn for your own row: the server refuses it,
                          and a button that can only fail is a trap. */}
                      {!self &&
                        (user.active ? (
                          <Button tone="danger" disabled={busy === user.id} onClick={() => setClosing(user)}>
                            Хаах
                          </Button>
                        ) : (
                          <Button loading={busy === user.id} onClick={() => void setActive(user, true)}>
                            Дахин нээх
                          </Button>
                        ))}
                    </div>

                    {resetting === user.id && (
                      <form
                        className="flex flex-col gap-3 rounded-md border border-rule bg-surface-2 p-3 sm:flex-row sm:items-end"
                        onSubmit={(e) => {
                          e.preventDefault();
                          void reset(user);
                        }}
                      >
                        <div className="flex-1">
                          <Field label={`«${user.username}»-ийн шинэ нууц үг`} hint="Хадгалмагц нээлттэй нэвтрэлтүүд нь дуусна.">
                            <TextInput
                              type="password"
                              autoComplete="new-password"
                              value={newPassword}
                              onChange={(e) => setNewPassword(e.target.value)}
                            />
                          </Field>
                        </div>
                        <div className="flex gap-2">
                          <Button type="submit" tone="primary" loading={busy === user.id} disabled={!newPassword}>
                            Хадгалах
                          </Button>
                          <Button type="button" onClick={() => setResetting(null)}>
                            Болих
                          </Button>
                        </div>
                      </form>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </Card>

      {closing && (
        <ConfirmDialog
          title={`«${closing.username}» бүртгэлийг хаах уу?`}
          confirmLabel="Хаах"
          busy={busy === closing.id}
          onConfirm={() => void setActive(closing, false)}
          onCancel={() => setClosing(null)}
          undo="«Дахин нээх» дарвал бүгд хэвээрээ буцаж ирнэ."
        >
          <p className="text-sm text-ink-2">
            Нэвтэрсэн байгаа бол тэр даруй гарна, дахин нэвтэрч чадахгүй. Түүний {closing.projects} төсөл,
            гаргасан видео нь устахгүй.
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
