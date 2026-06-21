import React from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { staggerContainer, fadeUp } from './motionPresets.js'
import whybaseMark from '../assets/whybase-mark.svg'
import './pageheader.css'

// The WhyBase branded page header — a band with the node mark, a mono kicker,
// a display title, a lede, and an actions slot, over a soft indigo wash.
// Shared by every page so headers read as one brand. Self-contained for
// reduced motion, so it's a drop-in on any view (no MotionConfig required).
export default function PageHeader({ kicker, title, lede, actions, align = 'left', children }) {
  const reduce = useReducedMotion()
  return (
    <motion.header
      className={`wb-pageheader wb-pageheader--${align}`}
      variants={staggerContainer(0.08)}
      initial={reduce ? false : 'hidden'}
      animate="show"
    >
      <span className="ph-wash" aria-hidden="true" />
      <div className="ph-inner">
        <div className="ph-lead">
          {kicker && (
            <motion.div className="ph-kicker" variants={fadeUp}>
              <img src={whybaseMark} alt="" className="ph-mark" width="22" height="22" />
              <span>{kicker}</span>
            </motion.div>
          )}
          <motion.h1 className="ph-title" variants={fadeUp}>{title}</motion.h1>
          {lede && <motion.p className="ph-lede" variants={fadeUp}>{lede}</motion.p>}
          {children && <motion.div className="ph-children" variants={fadeUp}>{children}</motion.div>}
        </div>
        {actions && <motion.div className="ph-actions" variants={fadeUp}>{actions}</motion.div>}
      </div>
    </motion.header>
  )
}
