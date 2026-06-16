import React from 'react'

// ============================================================
// WhyBase design-system primitives (React)
// Ported from the design-system component bundle so the real app
// composes the same markup as the UI kit. Token-driven via
// components.css — light/dark + motion come for free.
// ============================================================

const TONES = ['neutral', 'accent', 'success', 'warning', 'danger', 'info']

/** Compact label for status, counts and metadata. `mono` = Geist-Mono provenance register. */
export function Badge({
  tone = 'neutral',
  variant = 'plain', // plain | soft | outline
  mono = false,
  dot = false,
  className = '',
  children,
  ...rest
}) {
  const safeTone = TONES.includes(tone) ? tone : 'neutral'
  const cls = [
    'wb-badge',
    `wb-badge--${safeTone}`,
    variant !== 'plain' && `wb-badge--${variant}`,
    mono && 'wb-badge--mono',
    className,
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <span className={cls} {...rest}>
      {dot && <span className="wb-badge__dot" aria-hidden="true" />}
      {children}
    </span>
  )
}

// Status word → semantic tone (decided/reaffirmed = success, open/proposed = warning, …)
const STATUS_TONE = {
  decided: 'success',
  reaffirmed: 'success',
  resolved: 'success',
  complete: 'success',
  connected: 'success',
  idle: 'success',
  proposed: 'warning',
  open: 'warning',
  pending: 'warning',
  processing: 'warning',
  paused: 'warning',
  syncing: 'warning',
  revisited: 'info',
  in_review: 'info',
  running: 'info',
  reversed: 'danger',
  failed: 'danger',
  dismissed: 'danger',
}

/** Status pill in the mono register, tone derived from the status word. */
export function StatusBadge({ status, children, ...rest }) {
  const tone = STATUS_TONE[status] || 'neutral'
  return (
    <Badge tone={tone} variant="soft" mono {...rest}>
      {children || status}
    </Badge>
  )
}

// Deterministic, calm avatar colours derived from the name.
const PALETTE = ['#5e6ad2', '#1a9d63', '#c0820a', '#d24545', '#2b7fd4', '#7c5ce0', '#0f766e', '#b8508a']

function hashName(str) {
  let h = 0
  for (let i = 0; i < String(str).length; i++) h = (h * 31 + String(str).charCodeAt(i)) | 0
  return Math.abs(h)
}

function initials(name) {
  const parts = String(name).trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

/** Circular identity token — image if `src`, else colour-coded initials. */
export function Avatar({ name = '', src = null, size = 'md', className = '', ...rest }) {
  const cls = ['wb-avatar', size !== 'md' && `wb-avatar--${size}`, className].filter(Boolean).join(' ')
  const bg = src ? undefined : PALETTE[hashName(name) % PALETTE.length]
  return (
    <span className={cls} style={{ background: bg }} title={name || undefined} {...rest}>
      {src ? <img src={src} alt={name} /> : initials(name)}
    </span>
  )
}

// Normalise raw source strings to the five reserved hues.
const SRC_ALIAS = { github: 'github', slack: 'slack', notion: 'notion', jira: 'jira', meeting: 'meeting' }

/** Provenance pill — dot coloured by knowledge source (Slack/Notion/GitHub/Jira/Meeting). */
export function SrcBadge({ provider, children, className = '', ...rest }) {
  const key = SRC_ALIAS[String(provider || '').toLowerCase()] || 'other'
  return (
    <span className={`src-badge src-${key} ${className}`.trim()} {...rest}>
      <i className="src-dot" aria-hidden="true" />
      {children || provider}
    </span>
  )
}

/** Indeterminate spinner. */
export function Spinner({ size = '', className = '' }) {
  const cls = ['wb-spinner', size && `wb-spinner--${size}`, className].filter(Boolean).join(' ')
  return <span className={cls} aria-hidden="true" />
}
