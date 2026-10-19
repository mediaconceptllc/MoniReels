/**
 * A failed job, said in words the producer can act on.
 *
 * The worker records what went wrong for the logs, in English and in the
 * provider's own terms ("Not enough scratch space: 1.84 GB free…",
 * "OpenRouterError: OpenRouter upstream error: …"), and that text was all the
 * page ever showed — in a product whose every other line is Mongolian, and
 * without saying whether trying again could help. Each known cause is mapped
 * here to what happened and what to do; `retry` says whether pressing
 * «Дахин оролдох» can change the outcome. The original text stays available
 * beside it, because an admin chasing a cause needs the provider's words.
 *
 * Order matters: the first pattern that matches wins, so the specific causes
 * (a missing key, a used-up balance) come before the general "could not
 * reach the service".
 */

export interface JobErrorExplained {
  message: string;
  /** Whether running the same step again could succeed. A missing audio
   *  track or a missing API key will fail the same way every time. */
  retry: boolean;
}

const RULES: [RegExp, JobErrorExplained][] = [
  [
    /Not enough scratch space/i,
    {
      message: "Серверийн түр зай дүүрсэн байна. Хэдэн минутын дараа дахин оролдоно уу; давтагдвал админд хэлнэ үү.",
      retry: true,
    },
  ],
  [
    /not retried automatically because each attempt is billed/i,
    {
      message:
        "Ажил гүйцэтгэх сервер дундуур нь дахин асаагдсан тул ажил тасарлаа. Төлбөртэй алхам тул автоматаар дахин оролдоогүй — дахин эхлүүлж болно.",
      retry: true,
    },
  ],
  [
    /Worker stopped responding/i,
    { message: "Ажил гүйцэтгэх сервер хариу өгөхөө больсон тул ажил тасарлаа. Дахин оролдоно уу.", retry: true },
  ],
  [/project was deleted/i, { message: "Төсөл устгагдсан байна.", retry: false }],
  [
    /no audio track/i,
    { message: "Энэ видеонд дууны зам алга тул яриаг текст болгох боломжгүй.", retry: false },
  ],
  // Before the key, balance and rate rules: these messages quote the model's
  // own output, and a summary that happens to say "balance" or "forbidden"
  // must not read as a used-up account.
  [
    /SuggestionValidationError|required JSON shape|No short came back|no cut with role|not valid JSON|Unexpected OpenRouter response shape/i,
    {
      message: "Загвар зөв хэлбэрийн хариу буцаасангүй. Дахин оролдоно уу — дахин төлбөр гарна.",
      retry: true,
    },
  ],
  [
    /TranslationError|could be translated/i,
    { message: "Орчуулга амжилтгүй боллоо. Дахин оролдоно уу.", retry: true },
  ],
  [
    /removed while it was being translated/i,
    { message: "Орчуулж байх хооронд текст өөрчлөгдсөн байна. Дахин оролдоно уу.", retry: true },
  ],
  [
    /selected ideas are still in this project/i,
    { message: "Сонгосон санал төсөлд үлдээгүй байна. «Санал» хэсгээс дахин сонгоно уу.", retry: false },
  ],
  [
    /OverlayError|Logo (?:width|opacity)|logo position/i,
    { message: "Логоны тохиргоо буруу байна. Тохиргоогоо засаад дахин экспортлоно уу.", retry: false },
  ],
  [
    /not configured|тавигдаагүй|сонгогдоогүй/i,
    {
      message: "Үйлчилгээний түлхүүр эсвэл тохиргоо дутуу байна. Админ «Тохиргоо» хэсгээс оруулсны дараа дахин оролдоно уу.",
      retry: false,
    },
  ],
  [
    /\(40[13]\)|\b40[13] (?:unauthori[sz]ed|forbidden)|unauthori[sz]ed|forbidden|invalid.?api.?key/i,
    { message: "Үйлчилгээний түлхүүр буруу эсвэл хүчингүй болсон байна. Админд хэлнэ үү.", retry: false },
  ],
  [
    /\(402\)|insufficient|credits?\b|quota|balance/i,
    { message: "Үйлчилгээний данс эсвэл кредит дууссан байна. Админд хэлнэ үү.", retry: false },
  ],
  [
    /\(429\)|rate.?limit|too many requests/i,
    {
      message: "Үйлчилгээ богино хугацаанд хэт олон хүсэлт ирсэн гэж түр хязгаарласан. Хэдэн минутын дараа дахин оролдоно уу.",
      retry: true,
    },
  ],
  [
    /ProbeError|No video stream|ffprobe/i,
    {
      message: "Видео файлыг уншиж чадсангүй — эвдэрсэн эсвэл дэмжигдээгүй формат байж магадгүй. Өөр файлаар дахин хуулна уу.",
      retry: false,
    },
  ],
  [/no uploaded source video|has no video/i, { message: "Видео хараахан бэлэн болоогүй байна.", retry: false }],
  [/Transcribe the video before/i, { message: "Эхлээд яриаг текст болгоно уу.", retry: false }],
  [/already in Mongolian/i, { message: "Видео монгол хэлтэй тул орчуулах шаардлагагүй.", retry: false }],
  [
    /no suggestions to export|No suggested ideas|has no clips|empty clip list/i,
    { message: "Экспортлох санал алга.", retry: false },
  ],
  [
    /timed? ?out|timeout|ConnectError|request failed|upstream error|\(5\d\d\)/i,
    { message: "Гадаад үйлчилгээтэй холбогдож чадсангүй эсвэл тэр нь алдаа буцаалаа. Дахин оролдоно уу.", retry: true },
  ],
];

const UNKNOWN: JobErrorExplained = {
  message: "Тодорхойгүй алдаа гарлаа. Дахин оролдоно уу; давтагдвал доорх техникийн дэлгэрэнгүйг админд илгээнэ үү.",
  retry: true,
};

const CYRILLIC = /[\u0400-\u04FF]/;

export function explainJobError(error: string | null | undefined): JobErrorExplained {
  if (!error) return UNKNOWN;
  for (const [pattern, explained] of RULES) {
    if (pattern.test(error)) return explained;
  }
  // The voice and speech clients already say what went wrong in Mongolian
  // ("duudlaga.dev API түлхүүрийг татгалзлаа…"). Without a rule that knows
  // the cause, those words — minus the exception's class name — are better
  // than "unknown".
  const said = error.replace(/^[A-Za-z]+(?:Error|Exception): /, "");
  if (CYRILLIC.test(said)) return { message: said, retry: true };
  return UNKNOWN;
}
