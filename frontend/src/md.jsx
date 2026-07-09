
// Minimal markdown renderer (headings, lists, fenced code, bold, inline code)
// plus [C12]-style citation chips. Dependency-free on purpose.
function sanitizeCitationMarkers(text) {
  if (!text) return ''
  // Graph node IDs are retrieval internals, never user-facing citations.
  let cleaned = text.replace(/\[N\d+\]/g, '')
  // Some models collapse several source markers at the end of a response
  // (C199C204C208). Expand them so the existing citation-chip renderer can
  // make each source clickable.
  cleaned = cleaned.replace(/(?<![A-Za-z0-9])(?:C\d+){2,}(?![A-Za-z0-9])/g, (run) =>
    run.match(/C\d+/g).map((id) => `[${id}]`).join(' ')
  )
  return cleaned
}

function inline(text, onCite, keyPrefix) {
  const parts = []
  // [C12] and combined [C12, C13] forms both become chips
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[C\d+(?:\s*,\s*C?\d+)*\])/g
  let last = 0
  let m
  let i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('**')) {
      parts.push(<strong key={`${keyPrefix}-b${i}`}>{tok.slice(2, -2)}</strong>)
    } else if (tok.startsWith('`')) {
      parts.push(<code key={`${keyPrefix}-c${i}`}>{tok.slice(1, -1)}</code>)
    } else {
      const ids = tok.slice(1, -1).split(',').map((s) => s.trim().replace(/^C/, ''))
      ids.forEach((id, j) => {
        parts.push(
          <button
            key={`${keyPrefix}-cite${i}-${j}`}
            className="cite"
            title={`Source chunk ${id}`}
            onClick={() => onCite && onCite(Number(id))}
          >
            C{id}
          </button>
        )
      })
    }
    last = m.index + tok.length
    i += 1
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

export default function Md({ text, onCite }) {
  if (!text) return null
  const blocks = []
  const lines = sanitizeCitationMarkers(text).split('\n')
  let list = null
  let code = null // { lang, lines }
  const flushList = (key) => {
    if (list && list.length) {
      blocks.push(<ul key={`ul-${key}`}>{list}</ul>)
    }
    list = null
  }
  lines.forEach((line, idx) => {
    const trimmed = line.trim()
    if (code) {
      if (trimmed.startsWith('```')) {
        blocks.push(
          <pre key={`pre-${idx}`} className="md-code">
            <code>{code.lines.join('\n')}</code>
          </pre>
        )
        code = null
      } else {
        code.lines.push(line)
      }
      return
    }
    if (trimmed.startsWith('```')) {
      flushList(idx)
      code = { lang: trimmed.slice(3), lines: [] }
      return
    }
    const li = trimmed.match(/^[-*]\s+(.*)/)
    if (li) {
      if (!list) list = []
      list.push(<li key={`li-${idx}`}>{inline(li[1], onCite, `li-${idx}`)}</li>)
      return
    }
    flushList(idx)
    if (!trimmed) return
    const h = trimmed.match(/^(#{1,4})\s+(.*)/)
    if (h) {
      const Tag = `h${Math.min(h[1].length + 2, 6)}`
      blocks.push(<Tag key={`h-${idx}`}>{inline(h[2], onCite, `h-${idx}`)}</Tag>)
      return
    }
    blocks.push(<p key={`p-${idx}`}>{inline(trimmed, onCite, `p-${idx}`)}</p>)
  })
  flushList('end')
  if (code) {
    blocks.push(
      <pre key="pre-end" className="md-code">
        <code>{code.lines.join('\n')}</code>
      </pre>
    )
  }
  return <div className="md">{blocks}</div>
}
