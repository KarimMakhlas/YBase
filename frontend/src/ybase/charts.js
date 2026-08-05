// Bridge the CSS design tokens into JS for Recharts, which needs concrete
// colour strings (it can't read CSS custom properties itself). We read the
// computed values off :root and re-read whenever the theme flips, so charts
// stay correct in light and dark.

import { useEffect, useState } from 'react'

// Read a single CSS custom property (e.g. cssVar('--accent')) from :root.
function cssVar(name, fallback = '') {
  if (typeof window === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name)
  return v ? v.trim() : fallback
}

// Read a map of { key: '--token' } into { key: 'resolvedColor' }.
function cssVars(map) {
  const out = {}
  for (const k in map) out[k] = cssVar(map[k])
  return out
}

// Resolve a token map and keep it in sync with theme changes (the app toggles
// the `data-theme` attribute on <html>). Returns the resolved colour map.
export function useThemeColors(map) {
  const read = () => cssVars(map)
  const [colors, setColors] = useState(read)

  useEffect(() => {
    const update = () => setColors(read())
    update() // pick up the real values after mount
    const obs = new MutationObserver(update)
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return colors
}

// The reserved per-source brand hues (mirror of --src-* in colors.css).
export const SOURCE_VARS = {
  slack: '--src-slack',
  notion: '--src-notion',
  github: '--src-github',
  meeting: '--src-meeting',
}
