"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/auth";
import { uploadToStorage } from "@/lib/upload";
import type { BrandSettings } from "@/lib/types";
import { Alert, Button, Card, Spinner } from "@/components/ui";

/** What ffmpeg in this image can actually read. SVG is absent on purpose —
 *  rasterising it needs librsvg, which the render container does not carry,
 *  so accepting one here would fail at export instead of at upload. */
const ACCEPT = "image/png,image/webp,image/jpeg";

export function BrandLogoCard() {
  const [brand, setBrand] = useState<BrandSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

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

  async function pick(file: File) {
    setError(null);
    setBusy(true);
    setProgress(0);
    try {
      const { key, url } = await api.logoUploadUrl(file.type);
      // Straight to storage; the API only ever learns the key.
      await uploadToStorage(url, file, setProgress).promise;
      setBrand(await api.saveLogo(key));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
      setProgress(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function clear() {
    setError(null);
    setBusy(true);
    try {
      setBrand(await api.saveLogo(null));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex flex-col gap-4">
        <div>
          <h2 className="font-display text-lg font-semibold text-ink">Брэндийн лого</h2>
          <p className="mt-1 text-sm text-ink-3">
            Нэг студи — нэг лого. Энд нэг удаа оруулна; аль экспортод тавихыг төсөл бүр өөрөө
            сонгоно. PNG-г тунгалаг дэвсгэртэй нь оруулбал хамгийн сайн.
          </p>
        </div>

        {error && <Alert>{error}</Alert>}
        {brand && !brand.storage && (
          <Alert>Хадгалах сан (R2) тохируулагдаагүй тул лого оруулах боломжгүй.</Alert>
        )}

        {brand === null ? (
          <Spinner />
        ) : (
          <div className="flex flex-wrap items-center gap-4">
            {brand.logo?.url ? (
              // Checkerboard behind it: a white logo on a white card looks
              // like no logo at all, and that is the common case.
              <div
                className="flex h-24 w-24 items-center justify-center rounded-md border border-rule"
                style={{
                  backgroundImage:
                    "linear-gradient(45deg,#e5e5e5 25%,transparent 25%,transparent 75%,#e5e5e5 75%)," +
                    "linear-gradient(45deg,#e5e5e5 25%,transparent 25%,transparent 75%,#e5e5e5 75%)",
                  backgroundSize: "12px 12px",
                  backgroundPosition: "0 0, 6px 6px",
                }}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={brand.logo.url}
                  alt="Брэндийн лого"
                  className="max-h-20 max-w-20 object-contain"
                />
              </div>
            ) : (
              <div className="flex h-24 w-24 items-center justify-center rounded-md border border-dashed border-rule text-xs text-ink-3">
                Лого алга
              </div>
            )}

            <div className="flex flex-col gap-2">
              <input
                ref={fileInput}
                type="file"
                accept={ACCEPT}
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) void pick(file);
                }}
              />
              <div className="flex gap-2">
                <Button
                  onClick={() => fileInput.current?.click()}
                  disabled={busy || !brand.storage}
                >
                  {brand.logo ? "Солих" : "Оруулах"}
                </Button>
                {brand.logo && (
                  <Button tone="quiet" onClick={clear} disabled={busy}>
                    Устгах
                  </Button>
                )}
              </div>
              {progress !== null && (
                <span className="text-xs text-ink-3">
                  Хуулж байна… {Math.round(progress * 100)}%
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
