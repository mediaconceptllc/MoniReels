# MoniReels

Урт видеог автоматаар богино хэмжээний видео болгон хувиргах вэб студи.
Видео оруулахад яриаг нь текст болгож, LLM хамгийн сонирхолтой хэсгүүдийг
сонгон Reels/Shorts болон YouTube хураангуй санал болгож, FFmpeg-ээр угсарч
экспортолно.

Бүх интерфэйс монгол хэл дээр.

```
Хөтөч (Vercel)  ──presigned PUT──►  Cloudflare R2  ◄──  Worker (Railway)
      │                                   ▲                   ▲
      └──────► API (Railway) ─────────────┘                   │
                    │                                          │
                    └──────────► Postgres (ажлын дараалал) ────┘
```

- **`backend/`** — FastAPI API + worker (нэг Docker image, хоёр entrypoint) → Railway
- **`frontend/`** — Next.js App Router → Vercel
- **`docs/`** — DEPLOY.md · ARCHITECTURE.md

## Хэлхээ

```
Видео хуулах        хөтчөөс шууд R2 руу, сервер дундуур дамжихгүй
   ↓ import_video   ffprobe → метадата, хальс
   ↓ transcribe     duudlaga.dev → монгол хадмал (завсраар хэсэглэн)
   ↓ suggest        OpenRouter → 3 богино видео + 3 YouTube төлөвлөгөө
   ↓ export_all     ffmpeg → угсралт, хадмал, R2 руу
```

## Гадаад үйлчилгээ

| Ажил | Үйлчилгээ | Тохиргоо |
|---|---|---|
| Текстийн бүх ажил | [OpenRouter](https://openrouter.ai) | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` |
| Монгол яриа таних | [duudlaga.dev](https://duudlaga.dev) | `DUUDLAGA_API_KEY`, `DUUDLAGA_BASE_URL` |
| Дуу оруулах (TTS) | [ElevenLabs](https://elevenlabs.io) | `ELEVENLABS_API_KEY` — **хараахан хэрэгжээгүй** |
| Медиа хадгалалт | [Cloudflare R2](https://developers.cloudflare.com/r2/) | `R2_*` |

Түлхүүрүүдийг **Тохиргоо хуудсанаас** (админ) оруулж, дараагийн ажлаас
эхлэн хүчинтэй болгож болно — орчны хувьсагч нь суурь утга хэвээр, хуудсан
дээрх утга түүнийг дарна. Хадгалагдсан түлхүүр буцаж уншигдахгүй: API нь
эх сурвалж ба сүүлийн 4 тэмдэгтийг л буцаана. `OPENROUTER_BASE_URL` нь
ЗОРИУД засагдахгүй — түлхүүр ба хаягийг хамт солих боломж нь түлхүүрийг
гадагш урсгах шууд зам.

> **STT ба LLM хоёулаа хүсэлт тутам ТӨЛБӨРТЭЙ.** duudlaga.dev-ийн консол
> дээр түлхүүр тутам өдрийн зарлагын хязгаар ба зэрэгцээ хүсэлтийн тоог
> ЗААВАЛ тавь: `WORKER_CONCURRENCY` нь АЖЛЫГ хязгаарладаг, хүсэлтийг биш —
> нэг transcribe ажил аудионы хэсэг тутамд нэг хүсэлт явуулна.
> `GET /admin/providers` үлдэгдлийг харуулна, тиймээс «кредит дууссан»
> гэдгийг ажил worker эзэлж, видеогоо татсаны ДАРАА биш, өмнө нь мэднэ.

## Хөгжүүлэлт

```bash
# Backend — ЖИНХЭНЭ Postgres шаардана (JSONB ба FOR UPDATE SKIP LOCKED)
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
createdb monireels && createdb monireels_test
export DATABASE_URL=postgresql://localhost/monireels JWT_SECRET=dev-secret
.venv/bin/alembic upgrade head
.venv/bin/python -m app.main            # API  → :8000/docs
.venv/bin/python -m app.worker          # Worker (тусдаа терминал!)

DATABASE_URL=postgresql://localhost/monireels_test .venv/bin/python -m pytest tests -q
.venv/bin/ruff check app tests

# Frontend
cd frontend
npm install
npm run dev                             # :3000
npm run typecheck && npm run lint && npm run build
```

## Хэзээ ч зөрчиж болохгүй дүрмүүд

1. **Медиа backend-ээр дамжихгүй.** Хуулалт нь presigned PUT, унших нь
   presigned GET. Хэдэн ГБ файлыг dyno дундуур нэвтрүүлэх нь timeout ба
   давхар трафикийн зардал.
2. **R2-ийн ТҮЛХҮҮР бол ХАЯГ, татагдах НЭР бол ХАРАГДАЦ.** Түлхүүр нь
   `project_id` дээр тогтдог ба хэзээ ч өөрчлөгддөггүй — төслийн нэр
   солиход байгаа объект эзэнгүй үлдэх ёсгүй. Татагдах нэрийг signed URL
   бүрд `response-content-disposition`-оор өгнө.
3. **Хүнд ажил үргэлж job, үргэлж worker дээр.** HTTP хүсэлт дотор ffmpeg,
   torch, LLM ажиллуулахгүй. API-тай нэг контейнерт ffmpeg ажиллуулах нь
   healthcheck-ийг унагааж, ажлыг дундуур нь тасалдаг.
4. **`running` төлөв нь амьдын нотолгоо БИШ.** Зөвхөн хөдөлж байгаа
   heartbeat нотолно — үхсэн worker-ийн цогцос эс бөгөөс эгнээгээ мөнхөд
   хаана.
5. **Эрхийн шалгалт ЗӨВХӨН backend дээр.** Frontend зөвхөн UI нуудаг.
6. **Секрет кодод ч, ДБ-д ч бичихгүй** — зөвхөн орчны хувьсагч. Тэднийг
   бичдэг HTTP зам БАЙХГҮЙ (ширээний хувилбарт байсан, тэр нь нээлттэй
   сервер дээр бүрэн эрх алдагдал).
7. **Багтац, хугацааг клиент дээр тооцохгүй.** Сервер НЭГ газраас өгнө.
