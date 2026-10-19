"use client";

/** A screen that failed while drawing. The header and the rest of the app
 *  stay; this replaces only the page that broke, with a way to try again. */

import { useEffect } from "react";
import { ErrorScreen } from "@/components/ErrorScreen";
import { Button } from "@/components/ui";
import { usePageTitle } from "@/lib/title";

export default function PageError({ error, reset }: { error: Error; reset: () => void }) {
  usePageTitle("Алдаа гарлаа");
  useEffect(() => {
    // Kept in the browser console for whoever is chasing it.
    console.error(error);
  }, [error]);
  return (
    <ErrorScreen
      title="Хуудсыг харуулж чадсангүй"
      hint="Хуудас ачаалах явцад алдаа гарлаа. Дахин оролдоно уу; давтагдвал админд хэлнэ үү."
    >
      <Button tone="primary" onClick={reset}>
        Дахин оролдох
      </Button>
    </ErrorScreen>
  );
}
