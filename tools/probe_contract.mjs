// Probe v2 never treats chapter/page URL enumeration as readable images.
export const PROBE_SCHEMA = 2;
export function strictPass(r) {
  const s = r.stages || {};
  return r.probeSchema >= 2 && r.imageCheck === true &&
    ['search', 'details', 'description', 'chapters', 'pages',
     'coverImage', 'pageImage'].every(k => s[k] === true);
}
export function queryFor(info, supplied) {
  if (supplied && supplied !== 'auto') return supplied;
  const langs = info.languages || [info.lang || 'en'];
  if (langs.some(l => /^zh/i.test(l))) return '恋爱';
  if (langs.includes('ja')) return '恋';
  if (langs.includes('ko')) return '사랑';
  if (langs.includes('ru')) return 'любовь';
  return 'love';
}
export function imagePositions(length, limit) {
  if (!length) return [];
  return [...new Set([0, Math.floor(length / 2), length - 1])].slice(0, limit);
}
