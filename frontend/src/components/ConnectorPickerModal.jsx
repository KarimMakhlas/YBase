import React, { useEffect } from 'react'
import { X, Check } from 'lucide-react'
import { SrcBadge } from '../ybase/ui.jsx'

// All connectors YBase can sync from, in display order. `unit` matches
// Sources.jsx's PROVIDERS map (what one "stream" is called for that provider).
export const CONNECTOR_DEFS = [
  { provider: 'slack', label: 'Slack' },
  { provider: 'jira', label: 'Jira' },
  { provider: 'github', label: 'GitHub' },
  { provider: 'linear', label: 'Linear' },
  { provider: 'notion', label: 'Notion' },
  { provider: 'discord', label: 'Discord' },
  { provider: 'confluence', label: 'Confluence' },
  { provider: 'googledocs', label: 'Google Docs' },
  { provider: 'figma', label: 'Figma' },
]

// Modal listing every connector YBase supports — connected ones show a green
// check, unconnected-but-ready ones start the existing OAuth flow, and ones
// missing backend config show why they can't be clicked yet.
export default function ConnectorPickerModal({ connectedProviders, ready, setupHints, onClose, onConnect }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="connector-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Add a connector">
        <div className="invite-modal-head">
          <h3>Add a connector</h3>
          <button className="wb-iconbtn" onClick={onClose} aria-label="Close">
            <X size={17} strokeWidth={1.8} />
          </button>
        </div>
        <p className="settings-sub">
          Connect the tools where decisions actually happen. YBase keeps them in sync and cites every answer back to the source.
        </p>
        <div className="connector-grid">
          {CONNECTOR_DEFS.map(({ provider, label }) => {
            const connected = connectedProviders.has(provider)
            const isReady = ready[provider] !== false
            const card = (
              <>
                {connected && (
                  <span className="connector-check" aria-label="Connected">
                    <Check size={13} strokeWidth={2.4} />
                  </span>
                )}
                <SrcBadge provider={provider} className="connector-logo">{label}</SrcBadge>
                <span className="connector-name">{label}</span>
                <span className="connector-status">
                  {connected ? 'Connected' : isReady ? 'Click to connect' : 'Needs setup'}
                </span>
              </>
            )
            const cls = [
              'connector-card',
              connected && 'is-connected',
              !connected && !isReady && 'is-disabled',
            ].filter(Boolean).join(' ')
            return isReady && !connected ? (
              <button
                key={provider}
                type="button"
                className={cls}
                onClick={() => onConnect(provider)}
              >
                {card}
              </button>
            ) : (
              <div
                key={provider}
                className={cls}
                title={!isReady ? setupHints[provider] : undefined}
              >
                {card}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
