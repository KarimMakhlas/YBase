import ybaseMark from '../assets/ybase-mark-exact.png'
import './pageheader.css'

// The YBase page header — a mono kicker, a display title, a lede, and an
// actions slot. Shared by every page so headers read as one brand. Static by
// design: headers are chrome, not content, so they never animate in.
export default function PageHeader({ kicker, title, lede, actions, align = 'left', children }) {
  return (
    <header className={`wb-pageheader wb-pageheader--${align}`}>
      <span className="ph-wash" aria-hidden="true" />
      <div className="ph-inner">
        <div className="ph-lead">
          {kicker && (
            <div className="ph-kicker">
              <img src={ybaseMark} alt="" className="ph-mark" width="22" height="22" />
              <span>{kicker}</span>
            </div>
          )}
          <h1 className="ph-title">{title}</h1>
          {lede && <p className="ph-lede">{lede}</p>}
          {children && <div className="ph-children">{children}</div>}
        </div>
        {actions && <div className="ph-actions">{actions}</div>}
      </div>
    </header>
  )
}
