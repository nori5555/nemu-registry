/**
 * Headless Aidoku source probe.
 *
 * Loads one .aix with the Nemu Aidoku runtime (Node build), then exercises the
 * same paths a reader uses: listings/home -> search -> details -> chapters ->
 * pages -> first page image. Prints a single JSON object on stdout.
 *
 * Usage: node probe-runtime.cjs <aix-path> <source-key> [query] [--image] [--timeout-ms N]
 * Exit: 0 ran (check .ok), 2 bad args, 3 load failure.
 */

import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { loadSource } from "@nemu.pm/aidoku-runtime";
import { PROBE_SCHEMA, strictPass, queryFor, imagePositions } from './probe_contract.mjs';

const argv = process.argv.slice(2);
const positional = argv.filter((a) => !a.startsWith("--"));
const flags = new Set(argv.filter((a) => a.startsWith("--")));
const [aixPath, sourceKey, suppliedQuery = 'auto'] = positional;
const settingsArg = argv.find(a => a.startsWith('--settings-file='));
const sourceSettings = settingsArg ? JSON.parse(readFileSync(settingsArg.split('=').slice(1).join('='), 'utf8')) : {};
const decoder = join(dirname(process.argv[1]), 'probe_image_decode.py');
let query = suppliedQuery;
const wantImage = flags.has("--image");
const timeoutArg = argv.find((a) => a.startsWith("--timeout-ms="));
const timeoutMs = timeoutArg ? Number(timeoutArg.split("=")[1]) : 45000;
const limitArg = argv.find((a) => a.startsWith("--limit="));
const sampleLimit = limitArg ? Number(limitArg.split("=")[1]) : 1;

if (!aixPath || !sourceKey) {
  console.log(JSON.stringify({ ok: false, error: "usage: <aix> <source-key> [query]" }));
  process.exit(2);
}

let srcRef = null;
const result = {
  ok: false,
  probeSchema: PROBE_SCHEMA,
  imageCheck: wantImage,
  environment: 'node-headless-direct',
  mobileVerified: false,
  id: sourceKey,
  manifest: {},
  stages: {},
  errors: [],
  timings: {},
};

function record(name, fn) {
  const started = Date.now();
  return Promise.resolve()
    .then(fn)
    .then((value) => {
      result.timings[name] = Date.now() - started;
      return value;
    })
    .catch((error) => {
      result.timings[name] = Date.now() - started;
      const message = String(error?.message || error).slice(0, 300);
      result.errors.push({ stage: name, message });
      return null;
    });
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms)
    ),
  ]);
}

function imageKind(bytes) {
  if (bytes.length < 12) return "too-small";
  const b = bytes;
  if (b[0] === 0xff && b[1] === 0xd8) return "jpeg";
  if (b[0] === 0x89 && b[1] === 0x50) return "png";
  if (b[0] === 0x47 && b[1] === 0x49) return "gif";
  if (b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return "webp";
  if (b[4] === 0x66 && b[5] === 0x74 && b[6] === 0x79 && b[7] === 0x70) return "avif/heif";
  if (b[0] === 0x42 && b[1] === 0x4d) return "bmp";
  if (b[0] === 0x52 && b[1] === 0x49) return "webp";
  return "unknown:" + Buffer.from(b.slice(0, 4)).toString("hex");
}

async function fetchImage(url, headers, context = null, cover = false) {
  const modified = await srcRef.modifyImageRequest(url, context);
  const finalUrl = modified?.url || url;
  const finalHeaders = { ...(headers || {}), ...(modified?.headers || {}) };
  const res = await fetch(finalUrl, { headers: finalHeaders, redirect: 'follow',
                                    signal: AbortSignal.timeout(Math.min(timeoutMs, 20000)) });
  if (!res.ok) throw new Error(`image HTTP ${res.status}`);
  const chunks = []; let size = 0;
  for await (const chunk of res.body) {
    size += chunk.length;
    if (size > 25 * 1024 * 1024) throw new Error('image exceeds 25 MiB');
    chunks.push(chunk);
  }
  const original = new Uint8Array(Buffer.concat(chunks));
  let processed = original;
  const responseHeaders = Object.fromEntries(res.headers.entries());
  const hasProcessor = cover ? await srcRef.hasCoverImageProcessor() : await srcRef.hasImageProcessor();
  if (hasProcessor) {
    processed = cover
      ? await srcRef.processCoverImage(original, finalUrl, finalHeaders, res.status, responseHeaders)
      : await srcRef.processPageImage(original, context, finalUrl, finalHeaders, res.status, responseHeaders);
    if (!processed?.length) throw new Error('image processor returned no bytes');
  }
  const decoded = spawnSync(process.env.PYTHON || 'python3', [decoder], {
    input: Buffer.from(processed), encoding: 'utf8', timeout: 15000, maxBuffer: 1024 * 1024,
  });
  let info;
  try { info = JSON.parse(decoded.stdout); }
  catch { throw new Error(`image decoder failed: ${decoded.stderr || decoded.error || 'no result'}`); }
  if (decoded.status !== 0 || !info.ok) throw new Error(`image decode failed: ${info.error}`);
  return { status: res.status, contentType: res.headers.get('content-type'),
           downloadedBytes: original.length, processed: hasProcessor, ...info };
}

async function main() {
  const bytes = new Uint8Array(readFileSync(aixPath));
  const src = await loadSource(bytes, sourceKey, { settings: { get: () => sourceSettings } });
  srcRef = src;
  result.id = src.id;
  const info = src.manifest?.info || {};
  query = queryFor(info, suppliedQuery);
  result.query = query;
  result.manifest = {
    name: info.name,
    version: info.version,
    url: info.url,
    languages: info.languages,
    contentRating: info.contentRating,
  };

  const listings = await record("listings", async () => {
    const dynamic = await src.getListings();
    return Array.isArray(dynamic) ? dynamic.length : 0;
  });
  const home = await record("home", async () => Boolean(await src.getHome()));
  result.stages.listings = (listings || 0) > 0;
  result.stages.home = Boolean(home);

  const search = await record("search", async () => {
    const page = await src.getSearchMangaList(query, 1, []);
    return {
      count: page?.entries?.length || 0,
      first: page?.entries?.[0] || null,
    };
  });
  result.stages.search = Boolean(search && search.count > 0);

  let manga = search?.first || null;
  if (!manga) {
    // Query language mismatch: fall back to the first listing entry so the
    // rest of the pipeline (details/chapters/pages) is still exercised.
    const browse = await record("listingBrowse", async () => {
      const listings = await src.getListings();
      if (!listings?.length) return null;
      const page = await src.getMangaListForListing(listings[0], 1);
      return { count: page?.entries?.length || 0, first: page?.entries?.[0] || null,
               listing: listings[0]?.id };
    });
    if (browse?.first) {
      result.stages.listingBrowse = true;
      manga = browse.first;
    } else {
      if (search !== null && search.count === 0)
        result.errors.push({ stage: "search", message: `search returned no results for "${query}"` });
      return result;
    }
  }
  result.sample = { title: manga.title, cover: String(manga.cover || "").slice(0, 300) };

  const detail = await record("details", async () => {
    const m = await src.getMangaDetails(manga);
    return {
      title: m?.title,
      descriptionLength: (m?.description || "").length,
      tags: m?.tags?.length || 0,
      status: m?.status,
      cover: Boolean(m?.cover),
      raw: m,
    };
  });
  if (detail) {
    result.stages.details = Boolean(detail.title);
    result.stages.description = detail.descriptionLength >= 10;
    result.stages.tags = detail.tags > 0;
  }

  if (wantImage && detail?.cover) {
    result.stages.coverImage = await record("coverImage", async () => {
      const img = await fetchImage(String(detail.raw?.cover), {}, null, true);
      result.sample.coverImage = img;
      return img.ok;
    }).then(Boolean);
  }

  const chapters = await record("chapters", async () => {
    const list = await src.getChapterList(detail?.raw || manga);
    return { count: list?.length || 0, first: list?.[0] || null };
  });
  result.stages.chapters = Boolean(chapters && chapters.count > 0);

  if (chapters?.first) {
    const pages = await record("pages", async () => {
      const list = await src.getPageList(detail?.raw || manga, chapters.first);
      const pages = (list || []).slice(0, sampleLimit);
      return { count: list?.length || 0, first: pages[0] || null };
    });
    result.stages.pages = Boolean(pages && pages.count > 0);
    if (pages?.first?.url) {
      result.sample.pages = pages.count;
      result.sample.pageUrl = String(pages.first.url).slice(0, 300);
      if (wantImage) {
        result.stages.pageImage = await record("pageImage", async () => {
          const img = await fetchImage(pages.first.url, pages.first.headers, pages.first.context || null);
          result.sample.pageImage = img;
          return img.ok;
        }).then(Boolean);
      }
    }
  }

  result.ok = strictPass(result);
  return result;
}

(async () => {
  let timer;
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`probe timed out after ${timeoutMs}ms`)), timeoutMs);
    });
    const out = await Promise.race([main(), timeout]);
    console.log(JSON.stringify(out));
    process.exit(0);
  } catch (error) {
    result.errors.push({ stage: "load", message: String(error?.message || error).slice(0, 300) });
    console.log(JSON.stringify(result));
    process.exit(3);
  } finally {
    clearTimeout(timer);
  }
})();
