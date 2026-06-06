import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { api } from "../api";

interface Player {
  id: number;
  full_name: string;
  position: string;
  nfl_team: string | null;
  sleeper_id: string | null;
  espn_id: string | null;
}

export default function PlayerSearchView() {
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Player | null>(null);

  const { data: results, isLoading } = useQuery({
    queryKey: ["player-search", q],
    queryFn: () => api.players.search(q),
    enabled: q.length >= 2,
  });

  const { data: history } = useQuery({
    queryKey: ["player-history", selected?.id],
    queryFn: () => api.players.history(selected!.id),
    enabled: !!selected,
  });

  const { data: tradeHistory } = useQuery({
    queryKey: ["player-trades", selected?.id],
    queryFn: () => api.players.trades(selected!.id),
    enabled: !!selected,
  });

  const known = ["QB","RB","WR","TE","K","DEF"];
  function posBadge(pos: string) {
    return <span className={`badge badge-pos-${known.includes(pos) ? pos : "default"}`}>{pos}</span>;
  }

  return (
    <div>
      <div className="view-title">Player Search</div>

      <div className="search-bar">
        <Search className="search-icon" size={16} />
        <input
          placeholder="Search players by name…"
          value={q}
          onChange={(e) => { setQ(e.target.value); setSelected(null); }}
          autoFocus
        />
      </div>

      {q.length >= 2 && (
        <div className="card" style={{ marginBottom: 20 }}>
          {isLoading ? (
            <div className="loading">Searching…</div>
          ) : !results?.length ? (
            <div className="empty">No players found.</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Position</th>
                    <th>NFL Team</th>
                    <th>IDs</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((p: Player) => (
                    <tr
                      key={p.id}
                      style={{ cursor: "pointer" }}
                      onClick={() => setSelected(selected?.id === p.id ? null : p)}
                    >
                      <td style={{ fontWeight: 500, color: selected?.id === p.id ? "var(--accent)" : undefined }}>
                        {p.full_name}
                      </td>
                      <td>{posBadge(p.position)}</td>
                      <td className="muted">{p.nfl_team ?? "—"}</td>
                      <td className="muted" style={{ fontSize: 11 }}>
                        {p.sleeper_id ? `SL:${p.sleeper_id.slice(0,8)}` : ""}
                        {p.espn_id ? ` ES:${p.espn_id}` : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {selected && (
        <div className="detail-panel">
          <h3>
            {selected.full_name} {posBadge(selected.position)}
            {selected.nfl_team && <span className="muted" style={{ fontWeight: 400, marginLeft: 8, fontSize: 14 }}>{selected.nfl_team}</span>}
          </h3>

          {history && history.length > 0 && (
            <div className="detail-section">
              <h4>Fantasy Points by Season</h4>
              <div style={{ height: 180 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={history} margin={{ top: 4, right: 16, bottom: 4, left: 0 }}>
                    <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
                    <XAxis dataKey="year" tick={{ fill: "var(--muted)", fontSize: 11 }} />
                    <YAxis tick={{ fill: "var(--muted)", fontSize: 11 }} />
                    <Tooltip
                      contentStyle={{ background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12 }}
                    />
                    <Bar dataKey="total_fantasy_points" fill="var(--accent)" radius={[3,3,0,0]} name="Total Pts" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="table-wrap" style={{ marginTop: 12 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Year</th>
                      <th>Platform</th>
                      <th className="num">Total Pts</th>
                      <th className="num">Weeks</th>
                      <th className="num">Avg/Wk</th>
                      <th className="num">Best Wk</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((row: Record<string, string | number>, i: number) => (
                      <tr key={i}>
                        <td>{row.year}</td>
                        <td><span className={`badge badge-${row.platform}`}>{String(row.platform).toUpperCase()}</span></td>
                        <td className="num">{Number(row.total_fantasy_points).toFixed(1)}</td>
                        <td className="num">{row.weeks_played}</td>
                        <td className="num">{Number(row.avg_per_week).toFixed(1)}</td>
                        <td className="num">{Number(row.best_week).toFixed(1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {tradeHistory && tradeHistory.length > 0 && (
            <div className="detail-section">
              <h4>Trade History</h4>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Year</th>
                      <th className="num">Week</th>
                      <th>Sent By</th>
                      <th>Received By</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tradeHistory.map((row: Record<string, string | number>, i: number) => (
                      <tr key={i}>
                        <td>{row.year}</td>
                        <td className="num">{row.week ?? "—"}</td>
                        <td>{row.sent_by}</td>
                        <td>{row.received_by}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {(!history || history.length === 0) && (!tradeHistory || tradeHistory.length === 0) && (
            <div className="empty">No history found for this player in league data.</div>
          )}
        </div>
      )}
    </div>
  );
}
