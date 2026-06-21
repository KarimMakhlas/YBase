import React, { useEffect, useState } from 'react'
import { animate, useReducedMotion } from 'framer-motion'
import { ease } from './motionPresets.js'

// A number that counts up from 0 on mount (and whenever the value changes).
// Jumps straight to the final value when the user prefers reduced motion.
export default function CountUp({ value = 0, duration = 0.9 }) {
  const reduce = useReducedMotion()
  const [n, setN] = useState(reduce ? value : 0)
  useEffect(() => {
    if (reduce) { setN(value); return undefined }
    const controls = animate(0, value, {
      duration,
      ease: ease.out,
      onUpdate: (v) => setN(Math.round(v)),
    })
    return () => controls.stop()
  }, [value, reduce, duration])
  return <>{Number(n).toLocaleString()}</>
}
