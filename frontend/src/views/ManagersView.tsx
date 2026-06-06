import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { api } from "../api";

const COLORS = ["#4f7cff","#22c55e","#f59e0b","#ef4444","#a78bfa","#38bdf8","#fb7185","#34d399","#facc15","#60a5fa"];

export default function ManagersView() {
  const { data: managers, isLoading } = useQuery({
    queryKey: ["managers"],
    queryFn: api.teams.list,
  });

  const [selected, setSelected] = useState<string | null>(null);

  const { data: history } = useQuery({
    queryKey: ["manager-history", selected],
    queryFn: () => api.teams.history(selected!),
    enabled: !!selected,
  });

  if (isLoading) return <div className="loading">Loading…</div>;

  const sorted = [...(managers ?? [])].sort(
    (a: Record<string, number>, b: Record<string, number>) => b.total_wins - a.total_wins
  );

  return (
    <div>
      <div className="view-title">Manager Career Stats</div>

      <div className="stat-grid">
        <div className="stat-box">
          <div className="stat-label">Total Managers</div>
          <div className="stat-value">{sorted.length}</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">Total Seasons</div>
          <div className="stat-value">{sorted.reduce((s: number, m: Record<string, number>) => s + m.seasons_played, 0)}</div>
        </div>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Manager</th>
                <th className="num">Seasons</th>
                <th className="num">W</th>
                <th className="num">L</th>
                <th className="num">Win %</th>
                <th className="num">Total PF</th>
                <th className="num">Avg PF/Season</th>
                <th className="num">Best Finish</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((m: Record<string, number | string>, i: number) => {
                const wins = m.total_wins as number;
                const losses = m.total_losses as number;
                const pct = wins + losses > 0 ? ((wins / (wins + losses)) * 100).toFixed(1) : "—";
                return (
                  <tr
                    key={i}
                    style={{ cursor: "pointer" }}
                    onClick={() => setSelected(selected === m.display_name ? null : m.display_name as string)}
                  >
                    <td style={{ color: selected === m.display_name ? "var(--accent)" : undefined, fontWeight: 500 }}>
                      {m.display_name}
                    </td>
                    <td className="num">{m.seasons_played}</td>
                    <td className="num">{wins}</td>
                    <td className="num">{losses}</td>
                    <td className="num">{pct}%</td>
                    <td className="num">{Number(m.total_pf).toFixed(1)}</td>
                    <td className="num">{Number(m.avg_pf_per_season).toFixed(1)}</td>
                    <td className="num">{m.best_finish ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {selected && history && history.length > 0 && (
        <div className="detail-panel">
          <h3>{selected} — Season History</h3>
          <div style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={history} margin={{ top: 4, right: 16, bottom: 4, left: 0 }}>
                <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
                <XAxis dataKey="year" tick={{ fill: "var(--muted)", fontSize: 11 }} />
                <YAxis tick={{ fill: "var(--muted)", fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12 }}
                />
                <Line type="monotone" dataKey="points_for" stroke={COLORS[0]} strokeWidth={2} dot={{ r: 3 }} name="PF" />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="table-wrap" style={{ marginTop: 16 }}>
            <table>
              <thead>
                <tr>
                  <th>Year</th>
                  <th>Platform</th>
                  <th className="num">W</th>
                  <th className="num">L</th>
                  <th className="num">PF</th>
                  <th className="num">PA</th>
                  <th className="num">+/-</th>
                  <th className="num">Rank</th>
                </tr>
              </thead>
              <tbody>
                {history.map((row: Record<string, number | string>, i: number) => {
                  const diff = (row.point_diff as number) ?? 0;
                  const rank = row.final_rank as number | null;
                  return (
                    <tr key={i}>
                      <td>{row.year}</td>
                      <td><span className={`badge badge-${row.platform}`}>{String(row.platform).toUpperCase()}</span></td>
                      <td className="num">{row.wins}</td>
                      <td className="num">{row.losses}</td>
                      <td className="num">{Number(row.points_for).toFixed(1)}</td>
                      <td className="num">{Number(row.points_against).toFixed(1)}</td>
                      <td className="num" style={{ color: diff >= 0 ? "var(--green)" : "var(--red)" }}>
                        {diff >= 0 ? "+" : ""}{Number(diff).toFixed(1)}
                      </td>
                      <td className={`num ${rank === 1 ? "rank-1" : rank === 2 ? "rank-2" : rank === 3 ? "rank-3" : ""}`}>
                        {rank ?? "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
