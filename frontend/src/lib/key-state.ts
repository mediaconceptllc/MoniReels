import type { Capability, SttStatus } from "./types";

/** The live state of what this key switches on, beside the key itself.
 *
 *  The table further down already said all of this, and the operator who came
 *  here because something is not working had to read the field, scroll, find
 *  the matching row, and carry one back to the other.
 *
 *  "Ажиллаж байна" is said only of what was actually TRIED. A key being set
 *  is not the service answering: with duudlaga unreachable — the balance
 *  card right above reading "All connection attempts failed" — this badge
 *  said "working" (audit UX-8), and that is the one state an operator opens
 *  this page to tell apart. The recogniser is asked for its balance on every
 *  visit, so its answer decides; the other keys are not called here, so a
 *  set key is "Тохируулсан", not a claim nobody checked. */
export function keyState(
  secret: { capability: Capability["name"]; sttProvider?: string },
  capabilities: Capability[],
  sttProvider: string,
  stt: SttStatus | null,
): { tone: "fit" | "warn" | "danger" | "default"; label: string; title?: string } | null {
  // Nothing is claimed when the status read failed — the keys are still
  // editable, and a badge invented from no data is worse than no badge.
  if (capabilities.length === 0) return null;

  if (secret.sttProvider && sttProvider && secret.sttProvider !== sttProvider) {
    return { tone: "default", label: "Сонгогдоогүй" };
  }
  const capability = capabilities.find((c) => c.name === secret.capability);
  if (!capability) return null;
  if (!capability.ready) return { tone: "warn", label: "Тохируулаагүй" };
  if (secret.capability === "stt" && stt) {
    if (stt.ok === false) {
      return { tone: "danger", label: "Холбогдохгүй байна", title: stt.error || undefined };
    }
    if (stt.ok === true) return { tone: "fit", label: "Ажиллаж байна" };
  }
  return { tone: "default", label: "Тохируулсан" };
}
