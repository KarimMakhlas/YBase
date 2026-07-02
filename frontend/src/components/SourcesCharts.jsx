// Sources health ring, split out so Recharts stays a lazily-loaded chunk
// (shared with HomeCharts).
import { ResponsiveContainer, RadialBarChart, RadialBar, PolarAngleAxis } from 'recharts'

// Radial gauge of sync success rate.
export function SyncHealthGauge({ pct, accent, trackColor, size = 150 }) {
  return (
    <div className="gauge-wrap" style={{ width: size, height: size }}>
      <ResponsiveContainer width="100%" height="100%">
        <RadialBarChart
          innerRadius="72%"
          outerRadius="100%"
          data={[{ name: 'health', value: pct, fill: accent }]}
          startAngle={90}
          endAngle={-270}
        >
          <PolarAngleAxis type="number" domain={[0, 100]} angleAxisId={0} tick={false} />
          <RadialBar background={{ fill: trackColor }} dataKey="value" cornerRadius={20} angleAxisId={0} isAnimationActive={false} />
        </RadialBarChart>
      </ResponsiveContainer>
      <div className="gauge-center">
        <span className="gauge-num tnum">{pct}%</span>
        <span className="gauge-lab">healthy</span>
      </div>
    </div>
  )
}
