import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

type DraftMode = "all" | "keepers" | "adp";

export default function DraftView() {
  const { data: seasons } = useQuery({ queryKey: ["seasons"], queryFn: api.seasons.list });
  const years: number[] = seasons
    ? [...new Set<number>(seasons.map((s: { year: number }) => s.year))].sort()
    : [];

  const [year, setYear] = useState<number | null>(null);
  const [mode, setMode] = useState<DraftMode>("all");
  const [managerFilter, setManagerFilter] = useState("");

  const { data: draftData, isLoading } = useQuery({
    queryKey: ["draft", mode, year],
    queryFn: () =>
      mode === "adp"
        ? api.draft.valueOverAdp(year ?? undefined)
        : mode === "keepers"
        ? api.draft.keepers(year ?? undefined)
        : api.draft.summary(year ?? undefined),
  });

  const rows = useMemo(() => {
    if (!draftData) return [];
    return managerFilter
      ? draftData.filter((r: Record<string, string>) =>
          r.manager?.toLowerCase().includes(managerFilter.toLowerCase())
        )
      : draftData;
  }, [draftData, managerFilter]);

  function posBadge(pos: string | null) {
    if (!pos) return null;
    const known = ["QB","RB","WR","TE","K","DEF"];
    return <span className={`badge badge-pos-${known.includes(pos) ? pos : "default"}`}>{pos}</span>;
  }

  return (
    <div>
      <div className="view-title">Draft History</div>

      <div className="draft-controls">
        <div className="year-selector" style={{ marginBottom: 0 }}>
          <button className={`year-btn ${year === null ? "active" : ""}`} onClick={() => setYear(null)}>All</button>
          {years.map((y) => (
            <button key={y} className={`year-btn ${year === y ? "active" : ""}`} onClick={() => setYear(y)}>{y}</button>
          ))}
        </div>

        <div className="radio-group">
          {(["all","keepers","adp"] as DraftMode[]).map((m) => (
            <button key={m} className={`radio-btn ${mode === m ? "active" : ""}`} onClick={() => setMode(m)}>
              {m === "all" ? "All Picks" : m === "keepers" ? "Keepers" : "ADP Value"}
            </button>
          ))}
        </div>

        <input
          className="filter-input"
          placeholder="Filter by manager…"
          value={managerFilter}
          onChange={(e) => setManagerFilter(e.target.value)}
        />
      </div>

      <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 16 }}>
        {rows.length} pick{rows.length !== 1 ? "s" : ""}
      </div>

      {isLoading ? (
        <div className="loading">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="empty">No draft data found.</div>
      ) : (
        <div className="card">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Year</th>
                  <th className="num">Rd</th>
                  <th className="num">Pick</th>
                  <th>Manager</th>
                  <th>Player</th>
                  <th>Pos</th>
                  <th>Team</th>
                  {mode === "adp" && <th className="num">ADP</th>}
                  {mode === "adp" && <th className="num">vs ADP</th>}
                  {mode === "adp" && <th>Grade</th>}
                  <th className="num">Season Pts</th>
                  {mode !== "adp" && <th>Type</th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((row: Record<string, string | number>, i: number) => {
                  const pts = Number(row.season_fantasy_points ?? 0);
                  const vsAdp = row.picks_relative_to_adp as number | null;
                  return (
                    <tr key={i}>
                      <td>{row.year}</td>
                      <td className="num">{row.round}</td>
                      <td className="num">{row.pick_number}</td>
                      <td>{row.manager}</td>
                      <td style={{ fontWeight: 500 }}>{row.player_name ?? <span className="muted">—</span>}</td>
                      <td>{posBadge(row.position as string | null)}</td>
                      <td className="muted">{row.nfl_team ?? "—"}</td>
                      {mode === "adp" && (
                        <td className="num muted">{row.adp_at_draft ?? "—"}</td>
                      )}
                      {mode === "adp" && (
                        <td className="num" style={{ color: vsAdp == null ? undefined : vsAdp > 5 ? "var(--red)" : vsAdp < -5 ? "var(--green)" : "var(--muted)" }}>
                          {vsAdp != null ? (vsAdp > 0 ? `+${vsAdp}` : String(vsAdp)) : "—"}
                        </td>
                      )}
                      {mode === "adp" && (
                        <td>
                          <span className={`badge badge-${row.adp_grade ?? "on_value"}`}>
                            {String(row.adp_grade ?? "—").replace("_", " ")}
                          </span>
                        </td>
                      )}
                      <td className="num" style={{ color: pts > 150 ? "var(--green)" : pts > 80 ? undefined : "var(--muted)" }}>
                        {pts > 0 ? pts.toFixed(1) : "—"}
                      </td>
                      {mode !== "adp" && (
                        <td>
                          {row.is_keeper ? (
                            <span className="badge badge-keeper">Keeper</span>
                          ) : (
                            <span className="muted">Draft</span>
                          )}
                        </td>
                      )}
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
