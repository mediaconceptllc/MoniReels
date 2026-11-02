import type { Metadata, Viewport } from "next";
import "./globals.css";
import { AuthProvider } from "@/lib/auth";
import { PRODUCT } from "@/lib/page-title";

/*
 * The three faces ship INSIDE the build, from the @fontsource packages, not
 * fetched from Google while it runs. next/font/google downloaded them at
 * `next build` time, so every build depended on fonts.googleapis.com
 * answering in the shape its loader expects. MEASURED (2026-11-01): twice in
 * one day it answered with a font URL its regex could not read
 * ("Cannot read properties of null (reading '1')", google/loader.js:122) —
 * once in CI, once in a Vercel build — and the build failed on a change that
 * did not touch the frontend. A production deploy can fail the same way.
 *
 * Each weight's file declares EVERY subset with its unicode-range, and the
 * browser fetches only the ranges a page uses. That matters more than it
 * looks: Mongolian's Ө ө Ү ү (U+04E8/9, U+04AE/F) are NOT in the `cyrillic`
 * subset but in `cyrillic-ext`. Importing per-subset files ("cyrillic",
 * "latin") would have left those four letters to a system face.
 *
 * Served from this origin (/_next/static/media), so the CSP's
 * `font-src 'self'` holds.
 */
import "@fontsource/geologica/400.css";
import "@fontsource/geologica/500.css";
import "@fontsource/geologica/600.css";
import "@fontsource/golos-text/400.css";
import "@fontsource/golos-text/500.css";
import "@fontsource/golos-text/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";

// No `title` here. Each page names itself (lib/title), and a metadata title
// is re-applied by the framework after the page has set its own — MEASURED:
// the list, admin and profile tabs read "MoniReels" again a moment after
// load. The plain <title> below is the fallback until a page says more.
export const metadata: Metadata = {
  description: "Урт видеог богино хэмжээний видео болгон хувиргах студи",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // The viewer's OS theme decides; both are designed for.
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f4f5f3" },
    { media: "(prefers-color-scheme: dark)", color: "#131519" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="mn">
      <body>
        {/* Hoisted into <head> by React. Static, so nothing re-renders it
            over the name a page sets for itself. */}
        <title>{PRODUCT}</title>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
