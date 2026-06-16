// Client-side export of the (already filtered) decision list. No backend
// round-trip — the decisions endpoint returns the full payload.

function csvCell(value) {
  const s = (value ?? '').toString()
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

export function decisionsToCSV(list) {
  const header = [
    'Title', 'Status', 'Date', 'Confidence', 'People', 'Topics',
    'Summary', 'Positions', 'Alternatives', 'Sources',
  ]
  const rows = list.map((d) => [
    d.title,
    d.status,
    d.date || '',
    typeof d.confidence === 'number' ? `${Math.round(d.confidence * 100)}%` : '',
    (d.made_by || []).join('; '),
    (d.topics || []).join('; '),
    d.summary || '',
    (d.positions || []).join(' | '),
    (d.alternatives_considered || []).join(' | '),
    (d.sources || []).map((s) => `${s.source}: ${s.title}`).join(' | '),
  ].map(csvCell).join(','))
  return [header.join(','), ...rows].join('\n')
}

export function decisionsToMarkdown(list) {
  const out = [
    '# Decision log',
    '',
    `_Exported ${new Date().toISOString().slice(0, 10)} · ${list.length} decisions_`,
    '',
  ]
  for (const d of list) {
    out.push(`## ${d.title}`)
    const meta = [d.status, d.date].filter(Boolean).join(' · ')
    if (meta) out.push(`**${meta}**`)
    if (d.summary) out.push('', d.summary)
    if (d.made_by?.length) out.push('', `**People:** ${d.made_by.join(', ')}`)
    if (d.topics?.length) out.push(`**Topics:** ${d.topics.join(', ')}`)
    if (d.positions?.length) {
      out.push('', '**Positions:**')
      d.positions.forEach((p) => out.push(`- ${p}`))
    }
    if (d.alternatives_considered?.length) {
      out.push('', `**Alternatives considered:** ${d.alternatives_considered.join(', ')}`)
    }
    if (d.sources?.length) {
      out.push('', '**Sources:**')
      d.sources.forEach((s) => out.push(`- ${s.source}: ${s.title}${s.date ? ` (${s.date})` : ''}`))
    }
    out.push('', '---', '')
  }
  return out.join('\n')
}

export function downloadFile(filename, content, mime) {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
