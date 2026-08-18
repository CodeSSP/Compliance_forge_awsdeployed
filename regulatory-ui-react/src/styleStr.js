// Parse an inline CSS string ("a:b;c:d") into a React style object.
// Lets us reuse the original DC UI's inline-style strings verbatim, since React
// requires style objects with camelCased keys.
export function S(str) {
  if (str == null || str === '') return undefined
  if (typeof str === 'object') return str
  const obj = {}
  for (const decl of String(str).split(';')) {
    const i = decl.indexOf(':')
    if (i === -1) continue
    const prop = decl.slice(0, i).trim()
    const val = decl.slice(i + 1).trim()
    if (!prop) continue
    // Keep CSS custom properties (--foo) as-is; camelCase everything else.
    const key = prop.startsWith('--')
      ? prop
      : prop.replace(/^-ms-/, 'ms-').replace(/-([a-z])/g, (_, c) => c.toUpperCase())
    obj[key] = val
  }
  return obj
}
