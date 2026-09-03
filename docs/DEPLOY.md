# Байршуулах

Гурван зүйл: Railway дээр **хоёр сервис** (API ба worker), Vercel дээр вэб,
Cloudflare дээр R2 сан.

Хоёр Railway сервис нь **ижил Docker image** ажиллуулна, зөвхөн эхлэх
команд нь өөр. Хоёр image барих шаардлагагүй ба тэгвэл кодын хувилбар нь
зөрөх эрсдэлтэй.

---

## 1. Cloudflare R2

1. R2 → **Create bucket** → нэр нь `monireels`.
2. **Manage R2 API Tokens** → *Object Read & Write*, зөвхөн тэр сан руу.
3. Account ID, Access Key ID, Secret Access Key гурвыг тэмдэглэ.

**`R2_ACCOUNT_ID` нь токены утга БИШ.** Токен үүсгэсний дараа гарах
хуудсанд дөрвөн зүйл зэрэг харагдана — Token value (`cfat_…`), Access Key
ID, Secret Access Key, ба S3 endpoint. Account ID нь **зөвхөн endpoint-ийн
дотор** байдаг:

```
https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com
        └──────── R2_ACCOUNT_ID (16 хос hex) ───────┘
```

Endpoint-ыг бүтнээр нь тавьсан ч ажиллана (`config.r2_account` таслаж
авна). Токены утгыг тавьбал `r2_config_error` эхний хүсэлт дээр
татгалзана — тэр утга ХЭЗЭЭ Ч алдааны мессеж, лог руу орохгүй (токеноос
Secret Access Key гардаг тул лог руу орсон токен нь алдагдсан нууц).

**CORS ЗААВАЛ.** Хөтөч шууд PUT хийдэг тул CORS-гүй бол хуулалт preflight
дээр л унана — алдаа нь сүлжээний алдаа мэт харагдана.

```json
[
  {
    "AllowedOrigins": ["https://<таны-vercel-домэйн>", "http://localhost:3000"],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["content-type"],
    "ExposeHeaders": ["etag"],
    "MaxAgeSeconds": 3600
  }
]
```

Сан **хаалттай** үлдэнэ (public access идэвхжүүлэхгүй): бүх хандалт signed
URL-аар явна.

---

## 2. Railway — Postgres

**New → Database → PostgreSQL.** `DATABASE_URL`-ыг сервис хоёуланд нь өг.

Миграц нь **API сервисийн эхлэлд** ажиллана (`app/migrate.py`, Postgres
advisory lock-оор хамгаалагдсан). Worker миграц хийхгүй — нэг бичигч л
хангалттай, ба хоёулаа хийвэл deploy бүрд уралдана.

---

## 3. Railway — API сервис

- **Root directory:** `backend`
- **Builder:** Dockerfile
- **Start command:** `python -m app.main`
- **Healthcheck path:** `/health`

Хувьсагчид:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
JWT_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=<хүчтэй нууц үг>
CORS_ORIGINS=https://<таны-vercel-домэйн>
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=monireels
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=anthropic/claude-sonnet-4.5
DUUDLAGA_API_KEY=...
DUUDLAGA_BASE_URL=https://api.duudlaga.dev/v1
ELEVENLABS_API_KEY=...
```

`OPENROUTER_API_KEY`, `DUUDLAGA_API_KEY`, `ELEVENLABS_API_KEY` ба
`OPENROUTER_MODEL` дөрвийг **Тохиргоо хуудсанаас** (админ, `/admin`) ч
оруулж болно. Хуудсан дээрх утга орчны хувьсагчийг дарах ба **дараагийн
ажлаас эхлэн** хүчинтэй: API ба worker хоёулаа тэр мөрийг ажил тутам
уншдаг тул redeploy ч, restart ч шаардахгүй. Талбарыг хоосон болгож
хадгалбал мөр устаж, орчны хувьсагч руу буцна — эс бөгөөс нэг андуурч
бичсэн түлхүүр мөнхөд хоцорно.

Түлхүүр хаана байхаас үл хамааран **буцаж уншигдахгүй**: `GET
/admin/settings` нь эх сурвалж ба сүүлийн 4 тэмдэгтийг л өгнө.

`BOOTSTRAP_ADMIN_*` нь **users хүснэгт хоосон байхад л** ажиллана. Тиймээс
дараа нь солиход байгаа бүртгэл чимээгүй дахин тохируулагдахгүй. Эхний
удаа нэвтэрсний дараа нууц үгээ солино — тэр үед өмнөх бүх токен нэн
даруй хүчингүй болно.

`CORS_ORIGINS` **хоосон бол хөтөч бүх хүсэлтийг хаана.** Wildcard
зөвшөөрөгдөхгүй: credentials-тай хамт хөтөч өөрөө татгалздаг тул ажиллахгүй
ба шалтгаан нь ойлгомжгүй харагдана.

---

## 4. Railway — Worker сервис

Ижил репо, ижил Dockerfile, зөвхөн:

- **Start command:** `python -m app.worker`
- **Healthcheck:** ТАВИХГҮЙ (worker HTTP сонсдоггүй)

Хувьсагчид нь API-тай ижил (`CORS_ORIGINS`-оос бусад). Нэмж:

```
WORKER_CONCURRENCY=2
WORK_FREE_MIN_MB=2048
```

**Worker-гүй бол ямар ч ажил ажиллахгүй** — job нь `queued`-д мөнхөд
үлдэнэ. Вэб дээрх «Ажлын дараалалд N ажил хүлээж байгаа ч ажиллаж буй
worker алга» гэсэн сэрэмжлүүлэг яг үүнийг барина.

### Зогсолтын хүлээлт (deploy ажлыг алахгүйн тулд)

Railway нь шинэ deploy идэвхжмэгц хуучинд `SIGTERM` илгээж, тодорхой
хугацааны дараа `SIGKILL` хийнэ. **Анхдагч нь секундээр хэмжигдэнэ** —
ХЭМЖИГДСЭН: 4–5 минутын speech-to-text дуудлагын 58 дахь секундэд deploy
буусан ба контейнер 10 секундын дараа алга болсон. `transcribe`, `suggest`
хоёр нь оролдлого тутам төлбөртэй ба ДАХИН АЖИЛЛАДАГГҮЙ, тиймээс тэр нь
дэмий зарцуулсан мөнгө.

Хоёр тоо хамтдаа ажиллана:

| Хаана | Юу | Утга |
|---|---|---|
| `backend/railway.worker.json` | `drainingSeconds` | `600` — платформ хэзээ алах вэ |
| `backend/app/config.py` | `worker_shutdown_grace_s` | `580` — worker хэдийг хүлээх вэ |

Аппын хүлээлт нь платформынхаас **заавал бага** байх ёстой: эс бөгөөс
worker хүлээж байхдаа алагдаж, юу ч бүртгэгдэхгүй. `test_worker_shutdown`
тэр харьцааг ХОЁР файлаас уншиж тулгана.

Хугацаа дуусахад дуусаагүй ажил **НЭРЛЭГДЭНЭ**: дахин ажиллаж болох
төрлүүд дараалалд буцна, төлбөртэй нь «deploy зогсоосон» гэсэн шалтгаантай
`failed` болно (дахин оролдох нь хоёр дахь төлбөр). Хоосон worker дээр энэ
нь юу ч зардаггүй — ажилгүй бол хоёр секундэд гарна.

**Config-as-code хуучирч байгаа.** Railway `railway.json` / `railway.toml`-ыг
**2026-12-01** хүртэл дэмжинэ; түүнээс хойш Infrastructure as Code руу
шилжих шаардлагатай. Тэр үед энэ хоёр тоог хамт зөөнө.

### Хэрэв нарийвчлал сайжруулах бол (сонголт)

VAD ба Demucs-ыг асаах:

1. `Dockerfile`-д `RUN pip install -r requirements-ml.txt` нэмнэ
2. `ENABLE_SEPARATION=true`

**Зөвхөн worker сервист.** torch нь контейнерийн cgroup CPU хязгаарыг
УНШИХГҮЙ — thread pool-оо хостын цөмөөр гаргаж quota давна, цөм нь бүх
контейнерийг throttle хийнэ. Тэр контейнер HTTP ч хариулдаг бол healthcheck
хожимдож, Railway сервисийг ажлын дундуур ДАХИН АСААНА.
`config.heavy_threads` жинхэнэ quota-г уншиж хязгаарыг тавьдаг ч аюулгүй
зохион байгуулалт нь тусдаа сервис хэвээр.

---

## 5. Vercel

- **Root directory:** `frontend`
- **Framework:** Next.js (автоматаар танина)

```
NEXT_PUBLIC_API_URL=https://<railway-api-домэйн>
```

Энэ нь **build-д шингэдэг** тул солиход дахин deploy шаардана. Тэр нь зөв:
энэ бол апп юутай ярьдгийн нэг хэсэг болохоос хэрэглэгчийн өөрчлөх ёстой
тохиргоо биш.

Deploy хийсний дараа Vercel-ийн домэйныг Railway-гийн `CORS_ORIGINS` руу
БУЦААЖ нэмэхээ мартаж болохгүй. Урьдчилсан (preview) домэйн бүр өөр тул
production домэйныг л нэмэхэд хангалттай.

---

## Шалгах жагсаалт

```bash
curl https://<api>/health
# {"status":"ok","ffmpeg":true,"storage":true,...}
```

- `ffmpeg: false` → image буруу баригдсан
- `storage: false` → `R2_*` дутуу. `GET /admin/providers` аль хувьсагч
  буруу болохыг нэрлэнэ; хуулалт 503 буцаавал шалтгаан нь хариунд гарна
- Нэвтэрч чадахгүй → `JWT_SECRET` тавигдаагүй, эсвэл bootstrap ажиллаагүй
  (users хүснэгт аль хэдийн хоосон биш)
- Хуулалт 403 → R2-гийн CORS
- Job мөнхөд `queued` → worker сервис ажиллаагүй
- Transcribe «кредит дууссан» гэж унасан → `GET /admin/providers`

### Төлбөрийн хязгаар ЗААВАЛ тавь

duudlaga.dev-ийн шинэ түлхүүр **хязгааргүй** эхэлдэг. Консол дээрээс
дараах гурвыг тавь:

| Хязгаар | Яагаад |
|---|---|
| Өдрийн зарлага | Гацсан давталт, эсвэл алдаагаар олон удаа эхлүүлсэн ажил нэг шөнийн дотор данс хоослох боломжтой. |
| Зэрэгцээ хүсэлт | `WORKER_CONCURRENCY` нь **ажлыг** хязгаарладаг, хүсэлтийг БИШ. Нэг transcribe ажил аудионы хэсэг тутамд нэг хүсэлт явуулна — 40 минутын видео 40+ хүсэлт болно. |
| Хүсэлтийн хурд | Дээрхтэй ижил шалтгаан. |

Хязгаарт хүрвэл API `429`-ийг `Retry-After`-тэй буцаана. Клиент түүнийг
хүндэтгэж дахин оролдоно — **өдрийн зарлагын хязгаараас бусад тохиолдолд**:
тэр нь ажлын наслалтын дотор арилахгүй тул дахин оролдох нь зөвхөн worker-ийн
слот иднэ.
