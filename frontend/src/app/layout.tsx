import type { Metadata, Viewport } from "next";
import { Geologica, Golos_Text, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";
import { AuthProvider } from "@/lib/auth";
import { PRODUCT } from "@/lib/page-title";

/*
 * Self-hosted at build time rather than linked from Google.
 *
 * The `cyrillic` subset is requested EXPLICITLY on all three. Without it the
 * browser gets a Latin-only file and every Mongolian glyph falls back to a
 * system face — which is not a subtle degradation: the interface is entirely
 * Cyrillic, so the whole product would render in a font nobody chose.
 *
 * These three were picked for that coverage in the first place.
 */
const display = Geologica({
  subsets: ["cyrillic", "latin"],
  weight: ["400", "500", "600"],
  variable: "--font-display-loaded",
  display: "swap",
});

const body = Golos_Text({
  subsets: ["cyrillic", "latin"],
  weight: ["400", "500", "600"],
  variable: "--font-sans-loaded",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["cyrillic", "latin"],
  weight: ["400", "500"],
  variable: "--font-mono-loaded",
  display: "swap",
});

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
    <html lang="mn" className={`${display.variable} ${body.variable} ${mono.variable}`}>
      <body>
        {/* Hoisted into <head> by React. Static, so nothing re-renders it
            over the name a page sets for itself. */}
        <title>{PRODUCT}</title>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
