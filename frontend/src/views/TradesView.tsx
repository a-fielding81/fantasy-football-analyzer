import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

interface TradeAsset {
  asset_type: "player" | "draft_pick";
  description: string;
  position: string | null;
  player_name: string | null;
  resolved_player: string | null;
  fantasy_points: number;
}

interface TradeSide {
  manager: string;
  value_received: number;
  value_share: number;
  grade: string;
  grade_label: "Won" | "Even" | "Lost" | "Pending";
  assets: TradeAsset[];
}

interface Trade {
  trade_id: number;
  year: number;
  week: number | null;
  transaction_date: string | null;
  total_value: number;
  graded: boolean;
  sides: TradeSide[];
}

const GRADE_COLORS: Record<string, string> = {
  "A+": "#22c55e",
  "A":  "#4ade80",
  "B+": "#86efac",
  "B":  "#bbf7d0",
  "C":  "#fde68a",
  "D":  "#fca5a5",
  "F":  "#f87171",
  "F-": "#ef4444",
  "?":  "var(--muted)",
};

const GRADE_TEXT_DARK: Record<string, string> = {
  "A+": "#052e16", "A": "#052e16", "B+": "#052e16", "B": "#052e16",
  "C":  "#451a03",
  "D":  "#450a0a", "F": "#1a0000", "F-": "#1a0000",
  "?":  "var(--text)",
};

function GradeBadge({ grade, label }: { grade: string; label: string }) {
  const bg = GRADE_COLORS[grade] ?? "var(--muted)";
  const color = GRADE_TEXT_DARK[grade] ?? "var(--text)";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      background: bg, color, borderRadius: 6,
      padding: "2px 10px", fontWeight: 700, fontSize: 13,
    }}>
      {grade}
      <span style={{ fontWeight: 400, fontSize: 11, opacity: 0.8 }}>{label}</span>
    </span>
  );
}

function PosBadge({ pos }: { pos: string | null }) {
  if (!pos) return null;
  const known = ["QB", "RB", "WR", "TE", "K", "DEF"];
  return (
    <span className={`badge badge-pos-${known.includes(pos) ? pos : "default"}`}>
      {pos}
    </span>
  );
}

export default function TradesView() {
  const { data: seasons } = useQuery({
    queryKey: ["seasons"],
    queryFn: api.seasons.list,
  });
  const years: number[] = seasons
    ? [...new Set<number>(seasons.map((s: { year: number }) => s.year))].sort()
    : [];

  const [year, setYear] = useState<number | null>(null);
  const [managerFilter, setManagerFilter] = useState("");
  const [showUngraded, setShowUngraded] = useState(false);

  const { data: trades, isLoading } = useQuery<Trade[]>({
    queryKey: ["trades-grades", year],
    queryFn: () => api.trades.grades(year ?? undefined),
  });

  const filtered = useMemo(() => {
    if (!trades) return [];
    return trades.filter((t) => {
      if (!showUngraded && !t.graded) return false;
      if (managerFilter) {
        const f = managerFilter.toLowerCase();
        if (!t.sides.some((s) => s.manager.toLowerCase().includes(f))) return false;
      }
      return true;
    });
  }, [trades, managerFilter, showUngraded]);

  // Sort newest first
  const sorted = useMemo(
    () => [...filtered].sort((a, b) => b.year - a.year || (b.week ?? 0) - (a.week ?? 0)),
    [filtered]
  );

  // Summary stats
  const managerStats = useMemo(() => {
    if (!trades) return [];
    const map = new Map<string, { won: number; lost: number; even: number; total: number }>();
    for (const t of trades) {
      if (!t.graded) continue;
      for (const s of t.sides) {
        if (!map.has(s.manager)) map.set(s.manager, { won: 0, lost: 0, even: 0, total: 0 });
        const m = map.get(s.manager)!;
        m.total++;
        if (s.grade_label === "Won") m.won++;
        else if (s.grade_label === "Lost") m.lost++;
        else m.even++;
      }
    }
    return [...map.entries()]
      .map(([name, stats]) => ({ name, ...stats }))
      .sort((a, b) => b.won / b.total - a.won / a.total);
  }, [trades]);

  return (
    <div>
      <div className="view-title">Trade History &amp; Grades</div>

      {/* Manager trade record summary */}
      {managerStats.length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em" }}>
            All-time trade record (graded trades only)
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {managerStats.map((m) => (
              <div key={m.name} style={{
                background: "var(--bg2)", border: "1px solid var(--border)",
                borderRadius: 8, padding: "8px 14px", fontSize: 13,
                cursor: "pointer",
              }} onClick={() => setManagerFilter(m.name === managerFilter ? "" : m.name)}>
                <span style={{ fontWeight: 600 }}>{m.name.split(" ")[0]}</span>
                {"  "}
                <span style={{ color: "#4ade80" }}>{m.won}W</span>
                {" "}
                <span style={{ color: "var(--muted)" }}>{m.even}E</span>
                {" "}
                <span style={{ color: "#f87171" }}>{m.lost}L</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Filters */}
      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        <div className="year-selector" style={{ marginBottom: 0 }}>
          <button className={`year-btn ${year === null ? "active" : ""}`} onClick={() => setYear(null)}>All</button>
          {years.map((y) => (
            <button key={y} className={`year-btn ${year === y ? "active" : ""}`} onClick={() => setYear(y)}>{y}</button>
          ))}
        </div>
        <input
          className="filter-input"
          placeholder="Filter by manager…"
          value={managerFilter}
          onChange={(e) => setManagerFilter(e.target.value)}
        />
        <label style={{ fontSize: 13, color: "var(--muted)", display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
          <input type="checkbox" checked={showUngraded} onChange={(e) => setShowUngraded(e.target.checked)} />
          Show ungraded (no stats yet)
        </label>
      </div>

      <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 16 }}>
        {sorted.length} trade{sorted.length !== 1 ? "s" : ""}
        {year ? ` in ${year}` : " total"}
        {managerFilter ? ` involving "${managerFilter}"` : ""}
      </div>

      {isLoading ? (
        <div className="loading">Loading…</div>
      ) : sorted.length === 0 ? (
        <div className="empty">No trades found.</div>
      ) : (
        <div className="trade-list">
          {sorted.map((trade) => (
            <TradeCard key={trade.trade_id} trade={trade} />
          ))}
        </div>
      )}
    </div>
  );
}

function TradeCard({ trade }: { trade: Trade }) {
  const [expanded, setExpanded] = useState(false);
  const dateStr = trade.transaction_date
    ? new Date(Number(trade.transaction_date)).toLocaleDateString()
    : null;

  return (
    <div className="trade-card" style={{ cursor: "pointer" }} onClick={() => setExpanded(!expanded)}>
      {/* Header */}
      <div className="trade-card-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <span style={{ fontWeight: 600 }}>{trade.year}</span>
          {trade.week && <span style={{ color: "var(--muted)", fontSize: 13 }}>Week {trade.week}</span>}
          {dateStr && <span style={{ color: "var(--muted)", fontSize: 12 }}>{dateStr}</span>}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {trade.graded && (
            <span style={{ fontSize: 12, color: "var(--muted)" }}>
              {trade.total_value.toFixed(0)} pts total
            </span>
          )}
          <span style={{ fontSize: 12, color: "var(--muted)" }}>{expanded ? "▲" : "▼"}</span>
        </div>
      </div>

      {/* Sides summary (always visible) */}
      <div className="trade-sides" style={{ marginTop: 10 }}>
        {trade.sides.map((side, i) => (
          <div key={i} className="trade-side">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
              <div className="trade-side-manager">{side.manager}</div>
              {trade.graded
                ? <GradeBadge grade={side.grade} label={side.grade_label} />
                : <span style={{ fontSize: 12, color: "var(--muted)" }}>Pending</span>
              }
            </div>
            {/* Asset list */}
            <div className="trade-assets">
              {side.assets.map((a, j) => (
                <div key={j} className="trade-asset">
                  {a.asset_type === "player"
                    ? <PosBadge pos={a.position} />
                    : <span className="badge badge-pos-default">PICK</span>
                  }
                  <span style={{ flex: 1 }}>{a.description}</span>
                  {expanded && trade.graded && (
                    <span style={{ fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap" }}>
                      {a.fantasy_points > 0 ? `${a.fantasy_points} pts` : "—"}
                    </span>
                  )}
                </div>
              ))}
            </div>
            {expanded && trade.graded && (
              <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6, textAlign: "right" }}>
                Total received: <strong style={{ color: "var(--text)" }}>{side.value_received} pts</strong>
                {" "}({(side.value_share * 100).toFixed(0)}% of trade)
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
