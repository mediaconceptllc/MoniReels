"use client";

import type { Capability, SttStatus } from "@/lib/types";
import { Badge, Card, Select } from "@/components/ui";

/**
 * Which outside service powers which feature, and whether it can run.
 *
 * The keys sat in one list on this page with nothing to say what each one
 * did — and one of them, ElevenLabs, powers nothing at all yet. A key stored
 * for a feature no code reads looks exactly like a working feature until
 * somebody depends on it.
 */
const STT_LABEL: Record<string, string> = {
  duudlaga: "duudlaga.dev",
  elevenlabs: "ElevenLabs Scribe",
};

export function CapabilityTable({
  capabilities,
  stt,
  sttProvider,
  sttProviders,
  onSttProvider,
}: {
  capabilities: Capability[];
  /** The recogniser's own answer to this visit's balance check, when asked. */
  stt?: SttStatus | null;
  sttProvider?: string;
  sttProviders?: string[];
  onSttProvider?: (name: string) => void;
}) {
  return (
    <Card className="p-5">
      <div className="flex flex-col gap-4">
        <div>
          <h2 className="font-display text-lg font-semibold text-ink">Юу юуг ажиллуулж байна</h2>
          <p className="mt-1 text-sm text-ink-3">
            Ажил эхлэхээс өмнө шалгагдана — бэлэн биш үйлчилгээгээр ажил дараалалд орохгүй.
          </p>
        </div>

        <div className="flex flex-col divide-y divide-rule">
          {capabilities.map((c) => (
            <div key={c.name} className="flex flex-wrap items-start gap-3 py-3 first:pt-0 last:pb-0">
              <div className="min-w-40 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-ink">{c.label}</span>
                  <StatusBadge capability={c} stt={c.name === "stt" ? stt : null} />
                </div>
                <p className="mt-0.5 text-xs text-ink-3">{c.provider}</p>
                {c.powers && <p className="mt-1 text-xs text-ink-3">{c.powers}</p>}
                {c.blocked && <p className="mt-1 text-xs text-ink-2">{c.blocked}</p>}

                {c.name === "stt" && sttProviders && sttProviders.length > 1 && (
                  <label className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-3">
                    Яриа таних систем:
                    <Select
                      value={sttProvider ?? sttProviders[0]}
                      onChange={(e) => onSttProvider?.(e.target.value)}
                    >
                      {sttProviders.map((name) => (
                        <option key={name} value={name}>
                          {STT_LABEL[name] ?? name}
                        </option>
                      ))}
                    </Select>
                  </label>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

function StatusBadge({ capability, stt }: { capability: Capability; stt?: SttStatus | null }) {
  if (capability.ready) {
    // Ready means CONFIGURED. The recogniser is the one service this page
    // actually calls, and when that call fails the row must not say it
    // works (audit UX-8: "Ажиллаж байна" beside "All connection attempts
    // failed"). The others are not called here, so they claim no more than
    // what was checked.
    if (stt?.ok === false) return <Badge tone="danger">Холбогдохгүй байна</Badge>;
    if (stt?.ok === true) return <Badge tone="fit">Ажиллаж байна</Badge>;
    return <Badge>Тохируулсан</Badge>;
  }
  // Three states, not two: "no key" is something the operator fixes here,
  // "not built" is not, and telling them apart is the whole point.
  if (capability.implemented === false) return <Badge>Хэрэгжээгүй</Badge>;
  return <Badge tone="warn">Тохируулаагүй</Badge>;
}
