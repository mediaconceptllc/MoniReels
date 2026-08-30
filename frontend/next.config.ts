import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The API lives on Railway and media lives on R2; this app renders and
  // never proxies either, so there are no rewrites here on purpose — a
  // proxy would put multi-gigabyte video back through a server hop that
  // the whole storage design exists to avoid.
  eslint: { ignoreDuringBuilds: false },
  typescript: { ignoreBuildErrors: false },
};

export default config;
