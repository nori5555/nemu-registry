// Bundle the headless probe runtime.
// Stubs the "canvas" package (an optional dependency of
// @nemu.pm/aidoku-runtime) so no native canvas is needed.
//
// Usage:  node tools/build_probe.mjs   (run after `npm install ...`)
import { build } from "esbuild";

const CANVAS_STUB = `
module.exports = {
  createCanvas() { throw new Error("canvas stub: not available in headless probe"); },
  loadImage() { throw new Error("canvas stub: not available in headless probe"); },
  registerFont() { throw new Error("canvas stub: not available in headless probe"); },
};
`;

await build({
  entryPoints: ["tools/probe_source.mjs"],
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node20",
  outfile: "tools/probe-runtime.cjs",
  logLevel: "warning",
  plugins: [
    {
      name: "canvas-stub",
      setup(build) {
        build.onResolve({ filter: /^canvas$/ }, () => ({
          path: "canvas-stub",
          namespace: "stub",
        }));
        build.onLoad({ filter: /.*/, namespace: "stub" }, () => ({
          contents: CANVAS_STUB,
          loader: "js",
        }));
      },
    },
  ],
});
console.log("wrote tools/probe-runtime.cjs");