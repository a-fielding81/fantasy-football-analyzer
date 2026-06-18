import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

type SortKey = "final_rank" | "wins" | "points_for" | "points_against" | "point_diff";


export default function StandingsView() {
  const { data: seasons, isLoading: loadingSeasons } = useQuery({
    queryKey: ["seasons"],
    queryFn: api.seasons.list,
  });

  const years: number[] = seasons
    ? [...new Set<number>(seasons.map((s: { year: number }) => s.year))].sort()
    : [];

  const [year, setYear] = useState<number | null>(null);
  const selectedYear = year ?? years[years.length - 1] ?? null;

  const { data: standings, isLoading } = useQuery({
    queryKey: ["standings", selectedYear],
    queryFn: () => api.seasons.standings(selectedYear!),
    enabled: selectedYear != null,
  });

  const [sort, setSort] = useState<SortKey>("final_rank");
  const [asc, setAsc] = useState(true);

  function toggleSort(key: SortKey) {
    if (sort === key) setAsc((a) => !a);
    else { setSort(key); setAsc(true); }
  }

  const sorted = standings
    ? [...standings].sort((a: Record<string, number>, b: Record<string, number>) => {
        const diff = (a[sort] ?? 0) - (b[sort] ?? 0);
        return asc ? diff : -diff;
      })
    : [];

  const platform = standings?.[0]?.platform;

  if (loadingSeasons) return <div className="loading">Loading seasons…</div>;

  return (
    <div>
      <div className="view-title">Season Standings</div>

      <div className="year-selector">
        {years.map((y) => (
          <button
            key={y}
            className={`year-btn ${selectedYear === y ? "active" : ""}`}
            onClick={() => setYear(y)}
          >
            {y}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className="loading">Loading…</div>
      ) : sorted.length === 0 ? (
        <div className="empty">No data for this season.</div>
      ) : (
        <div className="card">
          {platform && (
            <div className="card-header" style={{ display: "flex", justifyContent: "space-between" }}>
              <span>{selectedYear} Season</span>
              <span className={`badge badge-${platform}`}>{platform.toUpperCase()}</span>
            </div>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th onClick={() => toggleSort("final_rank")}>Rank {sort === "final_rank" ? (asc ? "↑" : "↓") : ""}</th>
                  <th>Manager</th>
                  <th>Team</th>
                  <th className="num" onClick={() => toggleSort("wins")}>W {sort === "wins" ? (asc ? "↑" : "↓") : ""}</th>
                  <th className="num">L</th>
                  <th className="num" onClick={() => toggleSort("points_for")}>PF {sort === "points_for" ? (asc ? "↑" : "↓") : ""}</th>
                  <th className="num" onClick={() => toggleSort("points_against")}>PA {sort === "points_against" ? (asc ? "↑" : "↓") : ""}</th>
                  <th className="num" onClick={() => toggleSort("point_diff")}>+/- {sort === "point_diff" ? (asc ? "↑" : "↓") : ""}</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((row: Record<string, number | string>, i: number) => {
                  const rank = row.final_rank as number | null;
                  const rankClass = rank === 1 ? "rank-1" : rank === 2 ? "rank-2" : rank === 3 ? "rank-3" : "";
                  const diff = (row.point_diff as number) ?? 0;
                  return (
                    <tr key={i}>
                      <td className={rankClass}>{rank ?? "—"}</td>
                      <td>{row.manager}</td>
                      <td className="muted">{row.team_name || "—"}</td>
                      <td className="num">{row.wins}</td>
                      <td className="num">{row.losses}</td>
                      <td className="num">{Number(row.points_for).toFixed(1)}</td>
                      <td className="num">{Number(row.points_against).toFixed(1)}</td>
                      <td className="num" style={{ color: diff >= 0 ? "var(--green)" : "var(--red)" }}>
                        {diff >= 0 ? "+" : ""}{Number(diff).toFixed(1)}
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
