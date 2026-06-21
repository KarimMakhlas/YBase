// Motion presets — the bridge between the CSS design tokens (whybase/motion.css)
// and Framer Motion in JS. Defining the curves once here keeps animated
// components on-brand: the same easing the rest of the app uses in CSS, now
// available as the cubic-bezier arrays Framer Motion expects.
//
// Framer Motion takes easing as a [x1, y1, x2, y2] array, mirroring the
// cubic-bezier() values in motion.css.

// Easing curves — mirror of --ease-* in whybase/motion.css
export const ease = {
  out: [0.16, 1, 0.3, 1],
  outSoft: [0.22, 1, 0.36, 1],
  inOut: [0.65, 0, 0.35, 1],
  emphasis: [0.2, 0.9, 0.1, 1],
}

// Spring presets — for anything that moves through space (the whybase rule:
// springs for position, eased curves for colour & opacity).
export const spring = {
  // Gentle, premium — for entrances and layout.
  soft: { type: 'spring', stiffness: 120, damping: 18, mass: 0.9 },
  // Snappy — for hover/press micro-interactions like the magnetic CTA.
  snappy: { type: 'spring', stiffness: 320, damping: 24 },
  // Loose follow — for cursor-tracking tilt, so it trails the pointer.
  follow: { type: 'spring', stiffness: 150, damping: 20, mass: 0.6 },
}

// Durations (seconds) — mirror of --dur-* (which are in ms).
export const dur = {
  fast: 0.15,
  base: 0.22,
  slow: 0.34,
  slower: 0.52,
}

// ---- Reusable variants ---------------------------------------------------

// A staggered container: children animate in sequence. Pair with `fadeUp`.
export const staggerContainer = (stagger = 0.08, delayChildren = 0) => ({
  hidden: {},
  show: {
    transition: { staggerChildren: stagger, delayChildren },
  },
})

// The workhorse entrance: rise + fade + a touch of blur, on a spring.
export const fadeUp = {
  hidden: { opacity: 0, y: 20, filter: 'blur(6px)' },
  show: {
    opacity: 1,
    y: 0,
    filter: 'blur(0px)',
    transition: { ...spring.soft, opacity: { duration: dur.slow, ease: ease.emphasis } },
  },
}

// Scale-in for cards / nodes that should "pop" into place.
export const scaleIn = {
  hidden: { opacity: 0, scale: 0.92, y: 14 },
  show: {
    opacity: 1,
    scale: 1,
    y: 0,
    transition: spring.soft,
  },
}

// Shared viewport config for whileInView — trigger once, a bit before fully on screen.
export const inView = { once: true, amount: 0.3, margin: '0px 0px -8% 0px' }
