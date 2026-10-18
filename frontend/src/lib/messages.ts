/**
 * A refused request, said in Mongolian.
 *
 * The server answers a malformed field with FastAPI's validation list —
 * `{type, loc, msg}` per field, `msg` in English ("String should have at most
 * 200 characters"). The page used to join the `msg`s and show them as they
 * came. The `type` is the stable part, so it is what is translated; the
 * server's own words survive only for `value_error`, whose message this
 * codebase writes itself, in Mongolian.
 */

export interface ValidationItem {
  type?: string;
  loc?: (string | number)[];
  msg?: string;
}

const FIELDS: Record<string, string> = {
  name: "Нэр",
  filename: "Файлын нэр",
  size_bytes: "Файлын хэмжээ",
  username: "Нэвтрэх нэр",
  password: "Нууц үг",
  new_password: "Шинэ нууц үг",
  current_password: "Одоогийн нууц үг",
  language: "Хэл",
  shorts: "Богино видеоны тоо",
  youtube: "YouTube хураангуйн тоо",
  text: "Текст",
  translation: "Орчуулга",
  segments: "Мөрүүд",
};

const KINDS: [RegExp, string][] = [
  [/^missing$/, "заавал бөглөнө"],
  [/^string_too_short$|^too_short$/, "хэт богино байна"],
  [/^string_too_long$|^too_long$/, "хэт урт байна"],
  [/^(greater|less)_than(_equal)?$/, "зөвшөөрөгдөх хязгаараас гадуур байна"],
  [/^string_pattern_mismatch$/, "зөвшөөрөгдөөгүй тэмдэгт агуулж байна"],
  [/^json_invalid$/, "хүсэлт буруу хэлбэртэй байна"],
];

export function describeValidation(items: ValidationItem[]): string {
  const lines = items.map((item) => {
    const raw = item.loc?.[item.loc.length - 1];
    const field = typeof raw === "string" ? (FIELDS[raw] ?? raw) : "Утга";
    if (item.type === "value_error" && item.msg) {
      // Pydantic prefixes a validator's own message with "Value error, ".
      return item.msg.replace(/^Value error,\s*/, "");
    }
    const kind = KINDS.find(([pattern]) => pattern.test(item.type ?? ""));
    return `${field}: ${kind ? kind[1] : "буруу утга байна"}`;
  });
  return [...new Set(lines)].join("; ") || "Оруулсан утга буруу байна.";
}
