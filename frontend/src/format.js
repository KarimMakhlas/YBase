// Short date-time formatter shared by the admin views ("Mar 5, 02:14").
// Empty values render as 'never'; an unparseable value falls back to a raw slice.
export function formatDateTime(value) {
  if (!value) return 'never'
  try {
    return new Date(value).toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return String(value).slice(0, 16)
  }
}
