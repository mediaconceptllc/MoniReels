"use client";

import type { Capability } from "@/lib/types";
import { Badge, Card } from "@/components/ui";

/**
 * Which outside service powers which feature, and whether it can run.
 *
 * The keys sat in one list on this page with nothing to say what each one
 * did — and one of them, ElevenLabs, powers nothing at all yet. A key stored
 * for a feature no code reads looks exactly like a working feature until
 * somebody depends on it.
 */
export function CapabilityTable({ capabilities }: { capabilities: Capability[] }) {
  return (
    <Card>
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
                  <StatusBadge capability={c} />
                </div>
                <p className="mt-0.5 text-xs text-ink-3">{c.provider}</p>
                {c.powers && <p className="mt-1 text-xs text-ink-3">{c.powers}</p>}
                {c.blocked && <p className="mt-1 text-xs text-ink-2">{c.blocked}</p>}
              </div>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

function StatusBadge({ capability }: { capability: Capability }) {
  if (capability.ready) return <Badge tone="fit">Ажиллаж байна</Badge>;
  // Three states, not two: "no key" is something the operator fixes here,
  // "not built" is not, and telling them apart is the whole point.
  if (capability.implemented === false) return <Badge>Хэрэгжээгүй</Badge>;
  return <Badge tone="warn">Тохируулаагүй</Badge>;
}
