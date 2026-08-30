"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { uploadToStorage } from "@/lib/upload";
import type { BrandAsset, BrandSettings } from "@/lib/types";
import { Alert, Button, Card, Spinner } from "@/components/ui";

/** What ffmpeg in this image can actually read.
 *
 *  SVG is absent on purpose — rasterising it needs librsvg, which the render
 *  container does not carry, so accepting one here would fail at export
 *  instead of at upload. */
const ACCEPT: Record<BrandAsset, string> = {
  logo: "image/png,image/webp,image/jpeg",
  intro: "video/mp4,video/quicktime,video/webm",
  outro: "video/mp4,video/quicktime,video/webm",
};

const ROWS: { asset: BrandAsset; title: string; hint: string }[] = [
  {
    asset: "logo",
    title: "Лого",
    hint: "Тунгалаг дэвсгэртэй PNG хамгийн сайн. Аль буланд, ямар том байхыг төсөл бүр өөрөө сонгоно.",
  },
  {
    asset: "intro",
    title: "Эхлэлийн видео",
    hint: "Экспортын өмнө залгагдана. Нягтралт, кадрын давтамжийг нь систем өөрөө тааруулна.",
  },
  {
    asset: "outro",
    title: "Төгсгөлийн видео",
    hint: "Экспортын араас залгагдана.",
  },
];

export function BrandAssetsCard() {
  const [brand, setBrand] = useState<BrandSettings | null>(null);
  const [busy, setBusy] = useState<BrandAsset | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setBrand(await api.brand());
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function upload(asset: BrandAsset, file: File) {
    setError(null);
    setBusy(asset);
    setProgress(0);
    try {
      const { key, url } = await api.brandUploadUrl(asset, file.type);
      // Straight to storage; the API only ever learns the key.
      await uploadToStorage(url, file, setProgress).promise;
      setBrand(await api.saveBrandAsset(asset, key));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
      setProgress(null);
    }
  }

  async function clear(asset: BrandAsset) {
    setError(null);
    setBusy(asset);
    try {
      setBrand(await api.saveBrandAsset(asset, null));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <div className="flex flex-col gap-5">
        <div>
          <h2 className="font-display text-lg font-semibold text-ink">Брэндийн материал</h2>
          <p className="mt-1 text-sm text-ink-3">
            Нэг студи — нэг багц. Энд нэг удаа оруулбал бүх төсөл эндээс авна; аль экспортод
            хэрэглэхийг төсөл тутам сонгоно.
          </p>
        </div>

        {error && <Alert>{error}</Alert>}
        {brand && !brand.storage && (
          <Alert>Хадгалах сан (R2) тохируулагдаагүй тул файл оруулах боломжгүй.</Alert>
        )}

        {brand === null ? (
          <Spinner />
        ) : (
          <div className="flex flex-col divide-y divide-rule">
            {ROWS.map(({ asset, title, hint }) => (
              <BrandRow
                key={asset}
                asset={asset}
                title={title}
                hint={hint}
                current={brand[asset]}
                disabled={!brand.storage || busy !== null}
                progress={busy === asset ? progress : null}
                onPick={(file) => void upload(asset, file)}
                onClear={() => void clear(asset)}
              />
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}

function BrandRow({
  asset,
  title,
  hint,
  current,
  disabled,
  progress,
  onPick,
  onClear,
}: {
  asset: BrandAsset;
  title: string;
  hint: string;
  current: { key: string; url: string | null } | null;
  disabled: boolean;
  progress: number | null;
  onPick: (file: File) => void;
  onClear: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);

  return (
    <div className="flex flex-wrap items-center gap-4 py-4 first:pt-0 last:pb-0">
      <Preview asset={asset} url={current?.url ?? null} />

      <div className="min-w-48 flex-1">
        <p className="text-sm font-medium text-ink">{title}</p>
        <p className="mt-0.5 text-xs text-ink-3">{hint}</p>
        {progress !== null && (
          <p className="mt-1 text-xs text-ink-3">Хуулж байна… {Math.round(progress * 100)}%</p>
        )}
      </div>

      <div className="flex gap-2">
        <input
          ref={input}
          type="file"
          accept={ACCEPT[asset]}
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onPick(file);
            e.target.value = "";
          }}
        />
        <Button onClick={() => input.current?.click()} disabled={disabled}>
          {current ? "Солих" : "Оруулах"}
        </Button>
        {current && (
          <Button tone="quiet" onClick={onClear} disabled={disabled}>
            Устгах
          </Button>
        )}
      </div>
    </div>
  );
}

function Preview({ asset, url }: { asset: BrandAsset; url: string | null }) {
  if (!url) {
    return (
      <div className="flex h-20 w-20 shrink-0 items-center justify-center rounded-md border border-dashed border-rule text-xs text-ink-3">
        Алга
      </div>
    );
  }
  if (asset === "logo") {
    return (
      // Checkerboard behind it: a white logo on a white card looks like no
      // logo at all, and that is the common case.
      <div
        className="flex h-20 w-20 shrink-0 items-center justify-center rounded-md border border-rule"
        style={{
          backgroundImage:
            "linear-gradient(45deg,#e5e5e5 25%,transparent 25%,transparent 75%,#e5e5e5 75%)," +
            "linear-gradient(45deg,#e5e5e5 25%,transparent 25%,transparent 75%,#e5e5e5 75%)",
          backgroundSize: "12px 12px",
          backgroundPosition: "0 0, 6px 6px",
        }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={url} alt="Брэндийн лого" className="max-h-16 max-w-16 object-contain" />
      </div>
    );
  }
  return (
    <video
      src={url}
      muted
      playsInline
      preload="metadata"
      className="h-20 w-20 shrink-0 rounded-md border border-rule object-cover"
    />
  );
}
