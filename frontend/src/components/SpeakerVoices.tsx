"use client";

/**
 * A voice for each person in the transcript.
 *
 * A speaker is shown by what they say — how many lines, and the start of
 * their first one — because the label the recogniser gives them
 * (`speaker_1`) identifies nobody. A voice is picked from the account's own
 * list by name, with ElevenLabs' sample to hear it by, as on the admin page.
 *
 * What this cannot know is left to the server: which voices exist, which one
 * is the default, and why the list is empty. A speaker whose voice has since
 * been deleted from the account is NAMED here, because the export would stop
 * on that speaker's first line.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import type { ProjectVoices, SpeakerSummary, TtsVoice } from "@/lib/types";
import { Alert, Button, Loading, Select, Skeleton } from "@/components/ui";

function voiceLabel(voice: TtsVoice): string {
  const traits = [voice.gender, voice.accent].filter(Boolean).join(", ");
  return traits ? `${voice.name} · ${traits}` : voice.name;
}

export function SpeakerVoices({
  speakers,
  hasTranscript,
  value,
  onChange,
}: {
  speakers: SpeakerSummary[];
  hasTranscript: boolean;
  /** {speaker: voice id}. A speaker with no entry is read in the default. */
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}) {
  const [data, setData] = useState<ProjectVoices | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const player = useRef<HTMLAudioElement | null>(null);
  const wanted = speakers.length > 0;

  useEffect(() => {
    if (!wanted) return;
    let live = true;
    api
      .projectVoices()
      .then((answer) => live && setData(answer))
      .catch((err) => live && setError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, [wanted]);

  // A sample left playing would go on after the panel is gone.
  useEffect(() => () => player.current?.pause(), []);

  if (!wanted) {
    return (
      <p className="text-xs text-ink-3">
        {hasTranscript
          ? "Бичвэрт илтгэгч ялгагдаагүй тул бүх мөрийг анхдагч хоолойгоор уншина."
          : "Бичвэр гарсны дараа илтгэгч бүрд өөр хоолой сонгож болно. Түүнээс өмнө бүх мөрийг анхдагч хоолойгоор уншина."}
      </p>
    );
  }

  function choose(speaker: string, voice: string) {
    // The sample playing was the voice being replaced.
    if (playing === speaker) {
      player.current?.pause();
      setPlaying(null);
    }
    // The default is no entry, never an empty one: that is how the server
    // stores it, so a draft holding "" would never match what was saved.
    const next = { ...value };
    if (voice) next[speaker] = voice;
    else delete next[speaker];
    onChange(next);
  }

  function listen(speaker: string, url: string) {
    let audio = player.current;
    if (!audio) {
      audio = new Audio();
      audio.onended = () => setPlaying(null);
      player.current = audio;
    }
    if (playing === speaker) {
      audio.pause();
      setPlaying(null);
      return;
    }
    audio.src = url;
    audio
      .play()
      .then(() => setPlaying(speaker))
      .catch(() => setPlaying(null));
  }

  const byId = new Map((data?.voices ?? []).map((v) => [v.id, v]));
  const fallback = data?.default_voice_id ? byId.get(data.default_voice_id) : undefined;
  // Only an authoritative list can say a voice is gone: one that failed to
  // load says nothing about any voice.
  const listed = !!data && !data.error;
  const gone = speakers.filter((s) => value[s.id] && listed && !byId.has(value[s.id]));

  return (
    <div className="flex flex-col gap-3 pt-2">
      <div>
        <p className="text-[13px] font-medium text-ink-2">Илтгэгч бүрийн хоолой</p>
        <p className="mt-0.5 text-xs text-ink-3">
          Хоолой сонгоогүй илтгэгч анхдагч хоолойгоор уншигдана. Илтгэгчийн хоолойг солиход
          түүний мөрүүдийг шинээр үүсгэж, дахин төлнө.
        </p>
      </div>

      {error && <Alert>{error}</Alert>}
      {data?.error && <Alert tone="warn">{data.error}</Alert>}
      {gone.map((s) => (
        // No case ending on the number: the right one depends on how the
        // number is read (1-д, 3-т), so the name stands alone.
        <Alert key={s.id} tone="warn">
          Илтгэгч {speakers.indexOf(s) + 1}: оноосон хоолой ElevenLabs-ийн жагсаалтад алга.
          Экспорт энэ илтгэгчийн мөр дээр зогсоно — өөр хоолой сонгоно уу.
        </Alert>
      ))}

      {!data && !error ? (
        <Loading label="Хоолойн жагсаалт ачаалж байна" className="flex flex-col gap-2">
          {speakers.slice(0, 3).map((s) => (
            <Skeleton key={s.id} className="h-11 w-full rounded-md" />
          ))}
        </Loading>
      ) : (
        <ul className="flex flex-col divide-y divide-rule rounded-md border border-rule">
          {speakers.map((speaker, i) => {
            const chosen = value[speaker.id] ?? "";
            const voice = chosen ? byId.get(chosen) : fallback;
            // Only an https sample is played: the URL comes from a third party.
            const sample = voice?.preview_url?.startsWith("https://") ? voice.preview_url : null;
            const name = `Илтгэгч ${i + 1}`;
            return (
              <li
                key={speaker.id}
                className="flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:gap-4"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-ink">
                    {name} <span className="font-normal text-ink-3">· {speaker.lines} мөр</span>
                  </p>
                  <p className="truncate text-xs text-ink-3" title={speaker.sample}>
                    «{speaker.sample}»
                  </p>
                </div>
                <div className="flex items-center gap-2 sm:w-80 sm:shrink-0">
                  <Select
                    aria-label={`${name} — хоолой`}
                    value={chosen}
                    onChange={(e) => choose(speaker.id, e.target.value)}
                    className="min-w-0 flex-1"
                  >
                    <option value="">
                      {fallback ? `Анхдагч — ${fallback.name}` : "Анхдагч хоолой"}
                    </option>
                    {chosen && !byId.has(chosen) && (
                      <option value={chosen}>
                        {listed ? `${chosen} — жагсаалтад алга` : chosen}
                      </option>
                    )}
                    {data?.voices.map((v) => (
                      <option key={v.id} value={v.id}>
                        {voiceLabel(v)}
                      </option>
                    ))}
                  </Select>
                  {sample && (
                    <Button
                      tone="quiet"
                      aria-label={
                        playing === speaker.id ? `${name}: зогсоох` : `${name}: хоолойг сонсох`
                      }
                      aria-pressed={playing === speaker.id}
                      onClick={() => listen(speaker.id, sample)}
                      className="shrink-0 px-3"
                    >
                      {playing === speaker.id ? "■" : "▶"}
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
