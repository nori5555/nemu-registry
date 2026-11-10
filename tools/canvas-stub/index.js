"use strict";
// Headless stub for the node-canvas module. The Aidoku runtime only touches
// canvas for image processors; the probe exercises search/detail/chapter/page
// paths and fetches raw image bytes, which never go through canvas.
function createCanvas() {
  throw new Error("canvas stub: not available in headless probe");
}
function loadImage() {
  throw new Error("canvas stub: not available in headless probe");
}
function registerFont() {
  throw new Error("canvas stub: not available in headless probe");
}
module.exports = { createCanvas, loadImage, registerFont };