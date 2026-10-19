/** A page's title: its own name first, the product name last and as written.
 *  Plain — no React — so the server-rendered not-found page's metadata can
 *  read it too; the hook that sets it in the browser is lib/title. */

export const PRODUCT = "MoniReels";

export function pageTitle(title: string | null | undefined): string {
  const name = title?.trim();
  return name ? `${name} · ${PRODUCT}` : PRODUCT;
}
