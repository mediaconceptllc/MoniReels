"use client";

import { Fragment, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { usd } from "@/lib/format";
import type { UsageReport, UsageRow, UsageTotals } from "@/lib/types";
import { Alert, Card, Empty, Loading, Select, Skeleton } from "@/components/ui";

/**
 * What the paid providers cost, month by month and person by person.
 *
 * Read from the spend ledger, which every job writes however it ended — a
 * failed transcription bought its minutes too, and the job history only
 * ever showed the ones that worked.
 *
 * An unknown price is said, never shown as $0: the speech providers state
 * no price, so theirs is the operator's rate or nothing, and a month whose
 * minutes are unpriced is not a cheap month.
 */

const WINDOWS = [
  { months: 1, label: "Энэ сар" },
  { months: 3, label: "Сүүлийн 3 сар" },
  { months: 6, label: "Сүүлийн 6 сар" },
  { months: 12, label: "Сүүлийн 12 сар" },
];

const PROVIDER_LABELS: Record<string, string> = {
  openrouter: "OpenRouter",
  duudlaga: "duudlaga.dev",
  elevenlabs: "ElevenLabs",
};

const KIND_LABELS: Record<UsageRow["kind"], string> = {
  llm: "Хиймэл оюун",
  stt: "Яриа таних",
  tts: "Монгол дуу",
};

/** The setting that would price each kind of speech, by the field the
 *  server reports it under. */
const RATE_SETTINGS: { field: keyof UsageReport["rates"]; env: string }[] = [
  { field: "duudlaga_usd_per_minute", env: "DUUDLAGA_USD_PER_MINUTE" },
  { field: "elevenlabs_stt_usd_per_minute", env: "ELEVENLABS_STT_USD_PER_MINUTE" },
  { field: "elevenlabs_tts_usd_per_1k_chars", env: "ELEVENLABS_TTS_USD_PER_1K_CHARS" },
];

// "en-US" on purpose: CLDR's Mongolian number format is the same 1,234.5,
// and Chromium ships no "mn" data, so "mn" would only have been en-US by
// fallback — and asking for "mn" reads as if the page set Mongolian words.
const count = new Intl.NumberFormat("en-US");
const minutes = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

function monthLabel(month: string): string {
  const [year, m] = month.split("-");
  return `${year} оны ${Number(m)}-р сар`;
}

/** What was bought, in the unit it is billed by. Minutes keep a decimal: a
 *  provider bills by the minute, and rounding 90 seconds to "2 мин" would
 *  not reconcile against the invoice. */
function amount(row: UsageRow): string {
  if (row.kind === "llm") {
    return `${count.format(row.prompt_tokens)} + ${count.format(row.completion_tokens)} токен`;
  }
  if (row.kind === "stt") {
    return row.audio_seconds < 60
      ? `${Math.round(row.audio_seconds)} сек`
      : `${minutes.format(row.audio_seconds / 60)} мин`;
  }
  return `${count.format(row.characters)} тэмдэгт`;
}

function Cost({ total }: { total: UsageTotals }) {
  if (total.cost_usd === null) return <span className="text-warn">тодорхойгүй</span>;
  return (
    <>
      {usd(total.cost_usd)}
      {total.unpriced_requests > 0 && <span className="text-warn"> + тодорхойгүй</span>}
    </>
  );
}

export function UsageReportCard() {
  const [months, setMonths] = useState(3);
  const [report, setReport] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.usageReport(months).then(
      (r) => {
        if (!live) return;
        setReport(r);
        setError(null);
      },
      (err) => {
        if (live) setError(errorMessage(err));
      },
    );
    return () => {
      live = false;
    };
  }, [months]);

  // The answer for the window on screen, or nothing yet: a slow answer for
  // the previous window must not stand in for this one.
  const current = report && report.months === months ? report : null;
  const unsetRates = current
    ? RATE_SETTINGS.filter((r) => current.rates[r.field] === null).map((r) => r.env)
    : [];

  return (
    <Card className="p-5">
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-48 flex-1">
            <h2 className="font-display text-lg font-semibold text-ink">Зарлага</h2>
            <p className="mt-1 text-sm text-ink-3">
              Төлбөртэй үйлчилгээнд ажил бүр юу зарцуулсан — амжилттай болсон, унасан, цуцлагдсан
              ч хамаагүй. UTC сараар, үйлчилгээ үзүүлэгчийн нэхэмжлэлтэй адил. Хэрэглэгч нь
              төслийн эзэн.
            </p>
          </div>
          <Select
            aria-label="Хугацаа"
            value={months}
            onChange={(e) => {
              setError(null);
              setMonths(Number(e.target.value));
            }}
          >
            {WINDOWS.map((w) => (
              <option key={w.months} value={w.months}>
                {w.label}
              </option>
            ))}
          </Select>
        </div>

        {error && <Alert>{error}</Alert>}

        {current === null ? (
          !error && (
            <Loading className="flex flex-col gap-3">
              <Skeleton className="h-6 w-40" />
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </Loading>
          )
        ) : current.rows.length === 0 ? (
          <Empty
            title="Энэ хугацаанд зарлага алга"
            hint="Төлбөртэй дуудлага хийсэн ажил бүр энд мөр болж бичигдэнэ."
          />
        ) : (
          <>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-sm text-ink-3">Нийт</span>
              <span className="tabular font-display text-2xl font-semibold text-ink">
                <Cost total={current.totals} />
              </span>
              <span className="tabular text-sm text-ink-3">
                {count.format(current.totals.requests)} хүсэлт
              </span>
            </div>

            {current.totals.unpriced_requests > 0 && (
              <Alert tone="warn">
                {count.format(current.totals.unpriced_requests)} хүсэлтийн үнэ тодорхойгүй тул
                нийт дүнд ороогүй.{" "}
                {unsetRates.length > 0
                  ? `Яриа таних, дуу оруулалтын үнийг үйлчилгээ үзүүлэгч хэлдэггүй — таны төлөвлөгөөний үнийг серверийн орчинд тохируулбал түүнээс хойших ажил үнэлэгдэнэ: ${unsetRates.join(", ")}.`
                  : "Үнэ тохируулахаас өмнөх ажил, эсвэл үнээ хэлээгүй хариу байна."}
              </Alert>
            )}

            {/* Scrolls inside the card on a phone; the page itself never
                scrolls sideways. */}
            <div className="-mx-5 overflow-x-auto px-5">
              <table className="tabular w-full min-w-[36rem] text-sm">
                <caption className="sr-only">Сар, үйлчилгээ, хэрэглэгчээр задалсан зарлага</caption>
                <thead>
                  <tr className="border-b border-rule text-left text-xs text-ink-3">
                    <th scope="col" className="py-2 pl-2 pr-3 font-medium">Хэрэглэгч</th>
                    <th scope="col" className="py-2 pr-3 font-medium">Үйлчилгээ</th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">Хэмжээ</th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">Хүсэлт</th>
                    <th scope="col" className="py-2 pr-2 text-right font-medium">Зардал</th>
                  </tr>
                </thead>
                <tbody>
                  {current.month_totals.map((month) => (
                    <Fragment key={month.month}>
                      <tr className="border-b border-rule bg-surface-2">
                        <th scope="rowgroup" colSpan={3} className="px-2 py-2 text-left font-medium text-ink">
                          {monthLabel(month.month)}
                        </th>
                        <td className="py-2 pr-3 text-right text-ink-2">{count.format(month.requests)}</td>
                        <td className="py-2 pr-2 text-right font-medium text-ink">
                          <Cost total={month} />
                        </td>
                      </tr>
                      {current.rows
                        .filter((row) => row.month === month.month)
                        .map((row) => (
                          <tr
                            key={`${row.month}|${row.provider}|${row.kind}|${row.user_id ?? ""}`}
                            className="border-b border-rule-soft last:border-0"
                          >
                            <td className="py-2 pl-2 pr-3 text-ink">{row.username ?? "Тодорхойгүй"}</td>
                            <td className="py-2 pr-3 text-ink-2">
                              {KIND_LABELS[row.kind] ?? row.kind}
                              <span className="text-ink-3">
                                {" · "}
                                {PROVIDER_LABELS[row.provider] ?? row.provider}
                              </span>
                            </td>
                            <td className="py-2 pr-3 text-right text-ink-2">{amount(row)}</td>
                            <td className="py-2 pr-3 text-right text-ink-2">{count.format(row.requests)}</td>
                            <td className="py-2 pr-2 text-right text-ink">
                              <Cost total={row} />
                            </td>
                          </tr>
                        ))}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
