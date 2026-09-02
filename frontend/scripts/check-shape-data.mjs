/**
 * Runs before verify-shape and says what to do when the fixtures are absent.
 *
 * `.shape/*.json` is written by `backend/tests/test_frontend_shape.py` from
 * SYNTHETIC data, which is what makes it safe to commit — and being committed
 * is what makes it run in CI at all. Generated into .gitignore it would exist
 * on one machine and nowhere else.
 */
import fs from "node:fs";
import path from "node:path";

const dir = path.join(import.meta.dirname, "..", ".shape");
const need = ["contracts.json", "registry.json"];
const missing = need.filter((f) => !fs.existsSync(path.join(dir, f)));

if (missing.length) {
  console.error(
    "\n.shape/ дотор дараах файлууд алга: " + missing.join(", ") + "\n\n" +
      "Эдгээрийг backend-ийн тест үүсгэдэг. Дахин үүсгэх:\n" +
      "  cd backend && DATABASE_URL=… pytest tests/test_frontend_shape.py\n\n" +
      "Дэлгэрэнгүй: frontend/.shape/README.md\n",
  );
  process.exit(1);
}
console.log("✓ .shape дата бэлэн");
