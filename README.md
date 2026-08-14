# autoReel

autoReel нь урт хэмжээний видеог автоматаар богино хэмжээний видео болгон хувиргадаг. Видео оруулахад аудиог нь текст болгон хөрвүүлж, хиймэл оюун ухаан хамгийн сонирхолтой хэсгүүдийг сонгон, эхлэл (hook) болон хадмал текстийг бэлдэж өгнө. Үүний дараа Instagram Reels, YouTube Shorts болон товчилсон YouTube хувилбарыг бүгдийг нь нэг Windows програм дотроос экспортлох боломжтой.

## Татаж авах

**[Windows-д зориулсан autoReel татах (autoReel-Setup-1.0.0.exe)](https://github.com/tuvshinorg/autoReel/releases/latest)**

- Windows 10/11, 64-bit
- Суулгахад админ эрх шаардлагагүй
- Нэмэлт програм суулгах шаардлагагүй — програм, backend болон FFmpeg бүгд суулгагчид багтсан

Татаж авсан `.exe` файлаа ажиллуулаад суулгах алхмуудыг дагана уу. Суулгасны дараа **Start Menu** (эсвэл суулгах үед сонгосон бол Desktop Shortcut)-оос autoReel-ийг ажиллуулна.

> Windows SmartScreen анх удаа ажиллуулах үед **"Unrecognized app"** гэсэн анхааруулга гарч болно. Учир нь энэ хувилбар одоогоор code signing хийгдээгүй байгаа. **More info → Run anyway** дээр дарж үргэлжлүүлнэ үү.

## Юу хийдэг вэ

1. **Видео импортлох** — Видео файлаа сонгоно.
2. **Текст болгон хөрвүүлэх** — Яриаг автоматаар текст болгоно (Монгол хэл, [Chimege](https://chimege.mn)-ийг ашиглана).
3. **AI санал болгох** — Хиймэл оюун ухаан текстийг уншаад богино хэмжээний 3 видео санаа (hook, context, proof, payoff) болон 20 минутаас урт видеонд зориулсан YouTube highlight хувилбаруудыг санал болгоно. **OpenAI (ChatGPT)** эсвэл **Claude**-ийг ашиглах боломжтой бөгөөд Settings хэсгээс хүссэн үедээ сольж, эсвэл **Regenerate with ChatGPT / Regenerate with Claude** товчоор дахин үүсгэж болно.
4. **Шалгаж засварлах** — Текстийн алдааг засах эсвэл өөрийн хүссэн мөрүүдийг сонгон custom видео үүсгэх боломжтой.
5. **Экспортлох** — Сонгосон санал бүрийг тусдаа видео болгон transition, хадмал текст болон боломжтой бол hardware encoder ашиглан өндөр хурдтайгаар экспортлоно.

## Анхны тохиргоо

Програмыг суулгасны дараа **Settings** хэсгийг нээгээд дараах мэдээллүүдийг оруулна уу.

- **Chimege API Token** — [console.chimege.com](https://console.chimege.com)-оос авна (текст болгон хөрвүүлэхэд шаардлагатай).
- **OpenAI API Key** ([platform.openai.com](https://platform.openai.com)) болон/эсвэл **Anthropic (Claude) API Key** ([console.anthropic.com](https://console.anthropic.com)) — Эхлэхэд аль нэг нь байхад хангалттай. Харин хоёуланг нь ашиглавал хоёр AI-ийн үр дүнг харьцуулж болно.

Эдгээр түлхүүрүүд нь програмын хажууд байрлах локал `.env` файлд хадгалагдах бөгөөд зөвхөн Chimege, OpenAI болон Anthropic-ийн албан ёсны API руу шууд илгээгдэнэ. Өөр газар дамжуулахгүй.

## Эх кодоос ажиллуулах

Windows суулгагч нь autoReel-ийг ашиглах хамгийн хялбар арга юм. Харин эх кодоос ажиллуулахыг хүсвэл:

**Backend** (Python 3.11, FastAPI)

```bash
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m app.main
```

**Frontend** (Flutter, Windows Desktop)

```bash
cd frontend
flutter pub get
flutter run -d windows
```

Хоёуланг нь ажиллуулсны дараа desktop програм нь локал backend-тэй автоматаар холбогдоно (`frontend/lib/application/backend_launcher.dart`).

GitHub дээрхтэй ижил Windows суулгагчийг бүтээх бол `installer/setup.iss` файлыг ашиглана (Inno Setup 6, backend-д PyInstaller болон `flutter build windows --release` шаардлагатай).

## Ашигласан технологи

- Яриаг текст болгох: [Chimege](https://chimege.mn)
- AI санал боловсруулах: [OpenAI](https://openai.com) эсвэл [Anthropic Claude](https://www.anthropic.com)
- Видео боловсруулах: [FFmpeg](https://ffmpeg.org) (суулгагчид багтсан. FFmpeg нь LGPL/GPL лицензтэй үнэгүй нээлттэй эхийн програм бөгөөд лицензийн мэдээллийг [ffmpeg.org/legal.html](https://ffmpeg.org/legal.html)-ээс үзнэ үү.)
