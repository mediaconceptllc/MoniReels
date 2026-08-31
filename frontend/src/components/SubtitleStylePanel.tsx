"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import type { SubtitleStyle, SubtitleTemplate } from "@/lib/types";
import { useAuth } from "@/lib/auth";
import { Alert, Badge, Button, Field } from "@/components/ui";

const SELECT = "rounded-md border border-rule bg-surface px-3 py-2 text-sm text-ink";

const POSITIONS: [SubtitleStyle["position"], string][] = [
  ["bottom", "Доор"],
  ["center", "Голд"],
  ["top", "Дээр"],
];

export function SubtitleStylePanel({
  projectId,
  style,
  onSaved,
}: {
  projectId: string;
  style: SubtitleStyle;
  onSaved: () => void;
}) {
  const { user } = useAuth();
  const [draft, setDraft] = useState<SubtitleStyle>(style);
  const [families, setFamilies] = useState<string[]>([]);
  const [templates, setTemplates] = useState<SubtitleTemplate[]>([]);
  const [newName, setNewName] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = JSON.stringify(draft) !== JSON.stringify(style);

  useEffect(() => {
    // Asked of the RENDER image, never hard-coded: libass substitutes a
    // missing family without failing, so a font this list invented would be
    // a setting that looks applied and is not.
    void (async () => {
      try {
        setFamilies((await api.subtitleFonts()).families);
      } catch {
        setFamilies([]);
      }
      try {
        setTemplates((await api.subtitleTemplates()).templates);
      } catch {
        setTemplates([]);
      }
    })();
  }, []);

  async function saveTemplate() {
    const name = newName.trim();
    if (!name) return;
    setSaving(true);
    setError(null);
    try {
      const created = await api.saveSubtitleTemplate(name, draft);
      setTemplates((prev) => [created, ...prev]);
      setNewName("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  async function removeTemplate(id: string) {
    setError(null);
    try {
      await api.deleteSubtitleTemplate(id);
      setTemplates((prev) => prev.filter((t) => t.id !== id));
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  function update<K extends keyof SubtitleStyle>(key: K, value: SubtitleStyle[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api.updateProject(projectId, { subtitle_style: draft });
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Preview style={draft} />

      <Templates
        templates={templates}
        canManage={user?.role === "admin"}
        name={newName}
        onName={setNewName}
        onApply={(t) => {
          // Copied in, not linked: deleting the template later must not
          // restyle work that was already finished with it.
          setDraft(t.style);
          setSaved(false);
        }}
        onSave={() => void saveTemplate()}
        onDelete={(id) => void removeTemplate(id)}
        busy={saving}
      />

      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Фонт"
          hint={
            families.length
              ? "Зөвхөн сервер дээр байгаа фонтууд — сонгосон нь яг тэр байдлаар шатаана."
              : "Фонтын жагсаалт уншигдсангүй."
          }
        >
          <select
            className={SELECT}
            value={draft.font_family}
            disabled={families.length === 0}
            onChange={(e) => update("font_family", e.target.value)}
          >
            {/* A stored font the image no longer has must still be visible
                here, or the dropdown silently reads as something else. */}
            {!families.includes(draft.font_family) && (
              <option value={draft.font_family}>{draft.font_family} (байхгүй)</option>
            )}
            {families.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </Field>

        <Field label={`Үсгийн хэмжээ — ${draft.font_size}`}>
          <input
            type="range"
            min={16}
            max={120}
            step={1}
            value={draft.font_size}
            onChange={(e) => update("font_size", Number(e.target.value))}
            className="w-full"
          />
        </Field>

        <Field label="Байрлал">
          <select
            className={SELECT}
            value={draft.position}
            onChange={(e) => update("position", e.target.value as SubtitleStyle["position"])}
          >
            {POSITIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>

        <Field label={`Доод зай — ${draft.margin_v}px`}>
          <input
            type="range"
            min={0}
            max={300}
            step={5}
            value={draft.margin_v}
            onChange={(e) => update("margin_v", Number(e.target.value))}
            className="w-full"
          />
        </Field>

        <Field label="Үсгийн өнгө">
          <input
            type="color"
            value={draft.primary_color}
            onChange={(e) => update("primary_color", e.target.value.toUpperCase())}
            className="h-10 w-full rounded-md border border-rule bg-surface"
          />
        </Field>

        <Field label="Хүрээний өнгө" hint="Дэвсгэр дээр уншигдахуйц байлгана.">
          <input
            type="color"
            value={draft.outline_color}
            onChange={(e) => update("outline_color", e.target.value.toUpperCase())}
            className="h-10 w-full rounded-md border border-rule bg-surface"
          />
        </Field>
      </div>

      {error && <Alert>{error}</Alert>}

      <div className="flex items-center gap-3">
        <Button tone="primary" onClick={save} loading={saving} disabled={!dirty}>
          Хадмалын загвар хадгалах
        </Button>
        {saved && !dirty && <Badge tone="fit">✓ Хадгалагдсан</Badge>}
      </div>
    </div>
  );
}

/**
 * An approximation, and labelled as one.
 *
 * The real thing is drawn by libass onto a 1080x1920 frame in a font served
 * from the render container; a browser can match the colours and the rough
 * placement and nothing else. Saying "roughly" is better than a preview
 * someone trusts to the pixel.
 */
function Preview({ style }: { style: SubtitleStyle }) {
  const align =
    style.position === "top" ? "items-start" : style.position === "center" ? "items-center" : "items-end";
  return (
    <div>
      <div
        className={`flex ${align} justify-center overflow-hidden rounded-md border border-rule bg-ink-1 p-3`}
        style={{ aspectRatio: "9 / 16", maxHeight: 220, background: "#1c1c1e" }}
      >
        <span
          style={{
            // The frame here is a fraction of 1920px tall, so the stored size
            // is scaled to keep the proportion honest rather than the number.
            fontSize: `${Math.max(8, style.font_size * 0.11)}px`,
            color: style.primary_color,
            textShadow: `0 0 3px ${style.outline_color}, 0 0 3px ${style.outline_color}`,
            textAlign: "center",
            lineHeight: 1.25,
          }}
        >
          Жишээ хадмал энэ маягаар харагдана
        </span>
      </div>
      <p className="mt-1 text-xs text-ink-3">
        Ойролцоо харагдац. Жинхэнэ хадмалыг сервер дээрх фонтоор кадарт шатаана.
      </p>
    </div>
  );
}


/**
 * Saved house styles.
 *
 * Studio-wide, not per user: a house style each producer keeps their own
 * copy of stops being one the first time two of them drift. Applying is open
 * to anyone — a template nobody can use is not a house style — while saving
 * and deleting are admin's, the same line the brand assets draw.
 */
function Templates({
  templates,
  canManage,
  name,
  onName,
  onApply,
  onSave,
  onDelete,
  busy,
}: {
  templates: SubtitleTemplate[];
  canManage: boolean;
  name: string;
  onName: (value: string) => void;
  onApply: (template: SubtitleTemplate) => void;
  onSave: () => void;
  onDelete: (id: string) => void;
  busy: boolean;
}) {
  if (templates.length === 0 && !canManage) return null;

  return (
    <div className="rounded-md border border-rule bg-surface-2 p-3">
      <p className="text-xs font-medium text-ink-2">Хадгалсан загвар</p>

      {templates.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {templates.map((t) => (
            <span
              key={t.id}
              className="inline-flex items-center gap-1 rounded-full border border-rule bg-surface pl-3 pr-1 py-1 text-xs"
            >
              <button type="button" className="text-ink hover:text-accent" onClick={() => onApply(t)}>
                {t.name}
              </button>
              {canManage && (
                <button
                  type="button"
                  aria-label={`${t.name} загварыг устгах`}
                  className="rounded-full px-1.5 text-ink-3 hover:text-tally"
                  onClick={() => onDelete(t.id)}
                >
                  ×
                </button>
              )}
            </span>
          ))}
        </div>
      ) : (
        <p className="mt-1 text-xs text-ink-3">Одоогоор алга.</p>
      )}

      {canManage && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            value={name}
            onChange={(e) => onName(e.target.value)}
            placeholder="Загварын нэр"
            maxLength={80}
            className="rounded-md border border-rule bg-surface px-3 py-1.5 text-sm text-ink"
          />
          <Button tone="quiet" onClick={onSave} disabled={busy || !name.trim()}>
            Одоогийн тохиргоог хадгалах
          </Button>
        </div>
      )}
    </div>
  );
}
