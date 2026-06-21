// Home dashboard charts, split into their own module so Recharts (a heavy
// dependency) is code-split into a separate chunk — loaded lazily only when
// the home page renders, and never shipped to logged-out marketing visitors.
import React from 'react'
import {
  PieChart, Pie, Cell, ResponsiveContainer, RadialBarChart, RadialBar, PolarAngleAxis,
} from 'recharts'

// Donut of document share per connected source, in the reserved brand hues.
export function SourceDonut({ sources, total, srcColors, trackColor }) {
  const data = sources.filter((s) => s.n > 0)
  return (
    <div className="donut-wrap">
      <ResponsiveContainer width="100%" height={172}>
        <PieChart>
          <Pie
            data={data}
            dataKey="n"
            nameKey="source"
            innerRadius={56}
            outerRadius={82}
            paddingAngle={data.length > 1 ? 2 : 0}
            stroke="none"
            startAngle={90}
            endAngle={-270}
            isAnimationActive={false}
          >
            {data.map((d) => (
              <Cell key={d.source} fill={srcColors[d.source] || trackColor} />
            ))}
          </Pie>
        </PieChart>
      </ResponsiveContainer>
      <div className="donut-center">
        <span className="donut-num tnum">{total.toLocaleString()}</span>
        <span className="donut-lab">documents</span>
      </div>
    </div>
  )
}

// Radial gauge for the resolution rate (decisions vs. still-open questions).
export function ResolutionGauge({ pct, accent, trackColor }) {
  return (
    <div className="gauge-wrap">
      <ResponsiveContainer width="100%" height={172}>
        <RadialBarChart
          innerRadius="74%"
          outerRadius="100%"
          data={[{ name: 'resolution', value: pct, fill: accent }]}
          startAngle={90}
          endAngle={-270}
        >
          <PolarAngleAxis type="number" domain={[0, 100]} angleAxisId={0} tick={false} />
          <RadialBar
            background={{ fill: trackColor }}
            dataKey="value"
            cornerRadius={20}
            angleAxisId={0}
            isAnimationActive={false}
          />
        </RadialBarChart>
      </ResponsiveContainer>
      <div className="gauge-center">
        <span className="gauge-num tnum">{pct}%</span>
        <span className="gauge-lab">resolved</span>
      </div>
    </div>
  )
}
