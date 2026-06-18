import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

interface KeyFactors {
  prior_ppr?: number;
  age?: number;
  position?: string;
  team?: string;
  target_share?: number;
  carries?: number;
  wopr?: number;
  team_pass_rate?: number;
  team_11_rate?: number;
  hc_tenure?: number;
  new_oc?: boolean;
  hc_midseason_change?: boolean;
  coaching_tree?: string;
  round?: number;
  avg_ppr?: number;
  keep_weight?: number;
  years_out?: number;
  note?: string;
  data_year?: number;
  // Phase 1A: rookie projection fields
  proj_basis?: string;
  draft_round?: number | null;
}

interface TradeAsset {
  asset_type: "player" | "draft_pick";
  description: string;
  position: string | null;
  player_name: string | null;
  // Outcome
  outcome_points: number;
  // Process
  process_value: number;
  predicted_2yr: number;
  keep_weight: number | null;
  keeper_prob: number | null;
  key_factors: KeyFactors;
  data_year: number | null;
  missing_data: boolean;
  is_rookie_proj: boolean;
  low_confidence: boolean;
  stale_data: boolean;
}

interface TradeSide {
  manager: string;
  // Process
  process_value: number;
  process_share: number;
  process_share_pct: number;
  process_grade: string;
  process_label: string;
  // Outcome
  outcome_value: number;
  outcome_share: number;
  outcome_share_pct: number;
  outcome_grade: string;
  outcome_label: string;
  low_confidence: boolean;
  assets: TradeAsset[];
}

interface Trade {
  trade_id: number;
  year: number;
  week: number | null;
  transaction_date: string | null;
  ml_graded: boolean;
  outcome_graded: boolean;
  outcome_partial: boolean;
  outcome_seasons_available: number;
  outcome_window: string;
  low_confidence: boolean;
  total_process: number;
  total_outcome: number;
  sides: TradeSide[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const GRADE_COLORS: Record<string, string> = {
  "A+": "#22c55e", "A": "#4ade80", "B+": "#86efac", "B": "#bbf7d0",
  "C":  "#fde68a",
  "D":  "#fca5a5", "F": "#f87171", "F-": "#ef4444",
  "?":  "var(--muted)",
};
const GRADE_TEXT_DARK: Record<string, string> = {
  "A+": "#052e16", "A": "#052e16", "B+": "#052e16", "B": "#052e16",
  "C":  "#451a03",
  "D":  "#450a0a", "F": "#1a0000", "F-": "#1a0000",
  "?":  "var(--text)",
};
const TREE_LABELS: Record<string, string> = {
  shanahan_mcvay: "McVay/Shanahan",
  belichick:      "Belichick",
  reid_wco:       "Reid/WCO",
  payton_no:      "Payton",
  other:          "",
};

function GradeBadge({ grade, label, sharePct, partial }: {
  grade: string; label: string; sharePct?: number; partial?: boolean;
}) {
  const bg    = GRADE_COLORS[grade] ?? "var(--muted)";
  const color = GRADE_TEXT_DARK[grade] ?? "var(--text)";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      background: bg, color, borderRadius: 6,
      padding: "2px 10px", fontWeight: 700, fontSize: 13,
      opacity: partial ? 0.7 : 1,
    }}
    title={sharePct !== undefined
      ? `${sharePct}% of total trade value${partial ? " (partial — not all seasons complete)" : ""}`
      : undefined}
    >
      {grade}
      <span style={{ fontWeight: 400, fontSize: 11, opacity: 0.8 }}>
        {label}
        {sharePct !== undefined && <> {sharePct}%</>}
        {partial && <> ⏳</>}
      </span>
    </span>
  );
}


function PosBadge({ pos }: { pos: string | null }) {
  if (!pos) return null;
  const known = ["QB", "RB", "WR", "TE", "K", "DEF"];
  return (
    <span className={`badge badge-pos-${known.includes(pos) ? pos : "default"}`}>{pos}</span>
  );
}

function KeeperBar({ prob }: { prob: number }) {
  const pct  = Math.round(prob * 100);
  const color = pct >= 70 ? "#4ade80" : pct >= 40 ? "#fde68a" : "#f87171";
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11 }}>
      <span style={{
        display: "inline-block", width: 36, height: 6,
        background: "var(--border)", borderRadius: 3, overflow: "hidden",
      }}>
        <span style={{
          display: "block", width: `${pct}%`, height: "100%",
          background: color, borderRadius: 3,
        }} />
      </span>
      <span style={{ color: "var(--muted)" }}>{pct}% keep</span>
    </span>
  );
}

function AssetFactors({ asset }: { asset: TradeAsset }) {
  const kf = asset.key_factors;
  if (!kf || Object.keys(kf).length === 0) return null;

  const flags: string[] = [];
  if (kf.new_oc)              flags.push("🔄 New OC");
  if (kf.hc_midseason_change) flags.push("🔥 HC fired mid-season");
  if (kf.hc_tenure && kf.hc_tenure >= 4) flags.push(`✅ HC stability (yr ${kf.hc_tenure})`);
  const treeLabel = kf.coaching_tree ? TREE_LABELS[kf.coaching_tree] : "";
  if (treeLabel) flags.push(`🌲 ${treeLabel} tree`);

  return (
    <div style={{
      marginTop: 6, paddingLeft: 8, fontSize: 11,
      color: "var(--muted)", borderLeft: "2px solid var(--border)",
      display: "flex", flexDirection: "column", gap: 2,
    }}>
      {/* Rookie projection note */}
      {asset.is_rookie_proj && (
        <span style={{ color: "#a78bfa" }}>
          🔮 Rookie projection via {kf.proj_basis ?? "position baseline"}
          {kf.draft_round ? ` (Rd${kf.draft_round} ADP)` : ""}
        </span>
      )}
      {/* Core stats */}
      {kf.prior_ppr !== undefined && (
        <span>
          {kf.age}yo {kf.position} · {kf.prior_ppr} PPR pts ({kf.data_year ?? "?"})
          {kf.team && <> · <strong style={{ color: "var(--text)" }}>{kf.team}</strong></>}
        </span>
      )}
      {/* Opportunity */}
      {(kf.target_share ?? 0) > 0.05 && (
        <span>{(kf.target_share! * 100).toFixed(0)}% target share · {kf.wopr ? `${kf.wopr.toFixed(2)} WOPR` : ""}</span>
      )}
      {(kf.carries ?? 0) > 50 && (
        <span>{kf.carries} carries</span>
      )}
      {/* Scheme */}
      {kf.team_pass_rate && kf.team_pass_rate > 0 && (
        <span>
          {(kf.team_pass_rate * 100).toFixed(0)}% pass rate
          {kf.team_11_rate && kf.team_11_rate > 0
            ? ` · ${(kf.team_11_rate * 100).toFixed(0)}% 11-personnel`
            : ""}
        </span>
      )}
      {/* Pick note (includes keep-weight + future discount) */}
      {kf.note && <span style={{ color: asset.low_confidence ? "#f59e0b" : undefined }}>{kf.note}</span>}
      {/* Coaching flags */}
      {flags.length > 0 && <span>{flags.join("  ")}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export default function TradesView() {
  const { data: seasons } = useQuery({
    queryKey: ["seasons"],
    queryFn: api.seasons.list,
  });
  const years: number[] = seasons
    ? [...new Set<number>(seasons.map((s: { year: number }) => s.year))].sort()
    : [];

  const [year, setYear]                 = useState<number | null>(null);
  const [managerFilter, setManagerFilter] = useState("");
  const [showUngraded, setShowUngraded]   = useState(false);
  const [gradeMode, setGradeMode]         = useState<"process" | "outcome">("process");

  const { data: trades, isLoading } = useQuery<Trade[]>({
    queryKey: ["trades-grades", year],
    queryFn: () => api.trades.grades(year ?? undefined),
  });

  const filtered = useMemo(() => {
    if (!trades) return [];
    return trades.filter((t) => {
      if (!showUngraded && !t.ml_graded && !t.outcome_graded) return false;
      if (managerFilter) {
        const f = managerFilter.toLowerCase();
        if (!t.sides.some((s) => s.manager.toLowerCase().includes(f))) return false;
      }
      return true;
    });
  }, [trades, managerFilter, showUngraded]);

  const sorted = useMemo(
    () => [...filtered].sort((a, b) => b.year - a.year || (b.week ?? 0) - (a.week ?? 0)),
    [filtered]
  );

  // Manager trade records by mode
  const managerStats = useMemo(() => {
    if (!trades) return [];
    const map = new Map<string, { won: number; lost: number; even: number }>();
    for (const t of trades) {
      for (const s of t.sides) {
        if (!map.has(s.manager)) map.set(s.manager, { won: 0, lost: 0, even: 0 });
        const m = map.get(s.manager)!;
        const label = gradeMode === "process" ? s.process_label : s.outcome_label;
        const graded = gradeMode === "process" ? t.ml_graded : t.outcome_graded;
        if (!graded) continue;
        if (label === "Won") m.won++;
        else if (label === "Lost") m.lost++;
        else m.even++;
      }
    }
    return [...map.entries()]
      .map(([name, s]) => ({ name, ...s, total: s.won + s.lost + s.even }))
      .filter((m) => m.total > 0)
      .sort((a, b) => b.won / b.total - a.won / a.total);
  }, [trades, gradeMode]);

  return (
    <div>
      <div className="view-title">Trade History &amp; Grades</div>

      {/* Grade mode toggle */}
      <div style={{ display: "flex", gap: 0, marginBottom: 16, border: "1px solid var(--border)", borderRadius: 8, width: "fit-content", overflow: "hidden" }}>
        {(["process", "outcome"] as const).map((mode) => (
          <button key={mode} onClick={() => setGradeMode(mode)} style={{
            padding: "6px 16px", fontSize: 13, fontWeight: gradeMode === mode ? 700 : 400,
            background: gradeMode === mode ? "var(--accent)" : "transparent",
            color: gradeMode === mode ? "#fff" : "var(--muted)",
            border: "none", cursor: "pointer",
          }}>
            {mode === "process" ? "⚙ Process" : "📊 Outcome"}
          </button>
        ))}
      </div>

      {/* Mode explanation + grade scale */}
      <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 16, maxWidth: 600 }}>
        <div style={{ marginBottom: 4 }}>
          {gradeMode === "process"
            ? "Process grade: ML-predicted value at time of trade (age, production trends, scheme, keeper probability). A good process grade means you got the better side based on what was knowable."
            : "Outcome grade: actual fantasy points accumulated in the 2 seasons after the trade. Captures results but includes luck. ⏳ = outcome window not yet complete."}
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", opacity: 0.7, marginTop: 4 }}>
          <span style={{ fontWeight: 600, marginRight: 2 }}>Grade scale (% of total trade value):</span>
          {[["A+","≥68%"],["A","62-68%"],["B+","57-62%"],["B","52-57%"],["C","≈50%"],["D","42-47%"],["F","35-42%"],["F-","≤35%"]].map(([g,r])=>(
            <span key={g} style={{ background: GRADE_COLORS[g], color: GRADE_TEXT_DARK[g], borderRadius: 4, padding: "1px 5px", fontSize: 11, fontWeight: 600 }}>
              {g}<span style={{ fontWeight: 400 }}> {r}</span>
            </span>
          ))}
        </div>
      </div>

      {/* Manager summary */}
      {managerStats.length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em" }}>
            All-time record ({gradeMode === "process" ? "process grades" : "outcome grades"})
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {managerStats.map((m) => (
              <div key={m.name} style={{
                background: "var(--bg2)", border: "1px solid var(--border)",
                borderRadius: 8, padding: "8px 14px", fontSize: 13, cursor: "pointer",
                outline: m.name === managerFilter ? "2px solid var(--accent)" : "none",
              }} onClick={() => setManagerFilter(m.name === managerFilter ? "" : m.name)}>
                <span style={{ fontWeight: 600 }}>{m.name.split(" ")[0]}</span>
                {"  "}
                <span style={{ color: "#4ade80" }}>{m.won}W</span>
                {" "}<span style={{ color: "var(--muted)" }}>{m.even}E</span>
                {" "}<span style={{ color: "#f87171" }}>{m.lost}L</span>
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
          Show ungraded
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
            <TradeCard key={trade.trade_id} trade={trade} gradeMode={gradeMode} />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Trade card
// ---------------------------------------------------------------------------

function TradeCard({ trade, gradeMode }: { trade: Trade; gradeMode: "process" | "outcome" }) {
  const [expanded, setExpanded] = useState(false);
  const dateStr = trade.transaction_date
    ? new Date(Number(trade.transaction_date)).toLocaleDateString()
    : null;

  const graded = gradeMode === "process" ? trade.ml_graded
                                         : (trade.outcome_graded || trade.outcome_partial);
  const isPartialOutcome = gradeMode === "outcome" && trade.outcome_partial && !trade.outcome_graded;
  const totalVal = gradeMode === "process" ? trade.total_process : trade.total_outcome;

  // Detect interesting divergences (process grade differs from outcome grade)
  const hasDivergence = trade.ml_graded && trade.outcome_graded &&
    trade.sides.some((s) => s.process_label !== s.outcome_label && s.process_label !== "Pending" && s.outcome_label !== "Pending");

  return (
    <div className="trade-card" style={{ cursor: "pointer" }} onClick={() => setExpanded(!expanded)}>
      <div className="trade-card-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <span style={{ fontWeight: 600 }}>{trade.year}</span>
          {trade.week && <span style={{ color: "var(--muted)", fontSize: 13 }}>Week {trade.week}</span>}
          {dateStr && <span style={{ color: "var(--muted)", fontSize: 12 }}>{dateStr}</span>}
          {hasDivergence && (
            <span style={{
              fontSize: 11, background: "#7c3aed22", color: "#a78bfa",
              borderRadius: 4, padding: "1px 7px", border: "1px solid #7c3aed44",
            }}>
              ⚡ process ≠ outcome
            </span>
          )}
          {trade.low_confidence && gradeMode === "process" && (
            <span style={{
              fontSize: 11, background: "#78350f22", color: "#f59e0b",
              borderRadius: 4, padding: "1px 7px", border: "1px solid #78350f44",
            }}>
              ⚠ low confidence
            </span>
          )}
          {isPartialOutcome && (
            <span style={{
              fontSize: 11, background: "#16537e22", color: "#60a5fa",
              borderRadius: 4, padding: "1px 7px", border: "1px solid #16537e44",
            }}
            title={`Outcome window: ${trade.outcome_window}. Only ${trade.outcome_seasons_available}/2 seasons complete.`}
            >
              ⏳ partial outcome ({trade.outcome_seasons_available}/2 seasons)
            </span>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {graded && (
            <span style={{ fontSize: 12, color: "var(--muted)" }}
              title={gradeMode === "process"
                ? "Sum of each side's ML process value (keeper-weighted 2yr PPR prediction)"
                : "Sum of actual fantasy points accumulated post-trade (2-season window)"}>
              {totalVal.toFixed(0)} {gradeMode === "process" ? "process value" : "actual pts"}
            </span>
          )}
          <span style={{ fontSize: 12, color: "var(--muted)" }}>{expanded ? "▲" : "▼"}</span>
        </div>
      </div>

      <div className="trade-sides" style={{ marginTop: 10 }}>
        {trade.sides.map((side, i) => {
          const grade = gradeMode === "process" ? side.process_grade : side.outcome_grade;
          const label = gradeMode === "process" ? side.process_label : side.outcome_label;
          const value = gradeMode === "process" ? side.process_value : side.outcome_value;

          return (
            <div key={i} className="trade-side">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 6 }}>
                <div className="trade-side-manager">{side.manager}</div>
                <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 3 }}>
                  {graded
                    ? <GradeBadge
                        grade={grade} label={label}
                        sharePct={gradeMode === "process" ? side.process_share_pct : side.outcome_share_pct}
                        partial={gradeMode === "outcome" && isPartialOutcome}
                      />
                    : <span style={{ fontSize: 12, color: "var(--muted)" }}>Pending</span>
                  }
                  {/* Show alternate grade hint when expanded */}
                  {expanded && trade.ml_graded && (trade.outcome_graded || trade.outcome_partial) && (
                    <div style={{ fontSize: 10, color: "var(--muted)", display: "flex", gap: 4 }}>
                      {gradeMode === "process"
                        ? <>Outcome: <GradeBadge grade={side.outcome_grade} label={side.outcome_label} partial={isPartialOutcome} /></>
                        : <>Process: <GradeBadge grade={side.process_grade} label={side.process_label} /></>
                      }
                    </div>
                  )}
                </div>
              </div>

              <div className="trade-assets">
                {side.assets.map((a, j) => (
                  <div key={j} style={{ marginBottom: expanded ? 8 : 4 }}>
                    <div className="trade-asset" style={{ alignItems: "flex-start" }}>
                      {a.asset_type === "player"
                        ? <PosBadge pos={a.position} />
                        : <span className="badge badge-pos-default">PICK</span>
                      }
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <span style={{ fontSize: 13 }}>
                          {a.is_rookie_proj && <span title="Rookie projection — no prior NFL data" style={{ marginRight: 3, opacity: 0.8 }}>🔮</span>}
                          {a.description}
                        </span>
                        {expanded && a.keeper_prob !== null && a.asset_type === "player" && (
                          <div style={{ marginTop: 3 }}>
                            <KeeperBar prob={a.keeper_prob} />
                          </div>
                        )}
                      </div>
                      {expanded && (
                        <div style={{ textAlign: "right", fontSize: 12, color: "var(--muted)", whiteSpace: "nowrap", paddingLeft: 8 }}>
                          {gradeMode === "process" ? (
                            <>
                              {a.process_value > 0 ? (
                                <>
                                  {/* Raw 2yr prediction (what the model actually predicts) */}
                                  <div style={{ color: "var(--text)", fontWeight: 600 }}>
                                    {a.predicted_2yr.toFixed(0)} 2yr pred
                                  </div>
                                  {/* Per-season average for context */}
                                  <div style={{ fontSize: 11 }}>
                                    ~{(a.predicted_2yr / 2).toFixed(0)}/yr avg
                                  </div>
                                  {/* Process value = predicted_2yr × keep_weight */}
                                  {a.keep_weight !== null && a.keep_weight !== 1.0 && (
                                    <div style={{ fontSize: 11 }}>
                                      × {((a.keep_weight ?? 0) * 100).toFixed(0)}% keep-wt = {a.process_value.toFixed(0)}
                                    </div>
                                  )}
                                  {/* Stale data warning */}
                                  {a.stale_data && a.data_year && (
                                    <div style={{ color: "#f59e0b", fontSize: 10 }}>
                                      ⚠ using {a.data_year} stats
                                    </div>
                                  )}
                                </>
                              ) : (
                                <div>—</div>
                              )}
                              <div style={{ marginTop: 2 }}>
                                {a.outcome_points > 0 ? `${a.outcome_points.toFixed(0)} actual` : "no data"}
                              </div>
                            </>
                          ) : (
                            <>
                              <div style={{ color: "var(--text)", fontWeight: 600 }}>{a.outcome_points > 0 ? `${a.outcome_points.toFixed(0)} pts` : "—"}</div>
                              {a.process_value > 0 && (
                                <div style={{ fontSize: 11 }}>{a.predicted_2yr.toFixed(0)} 2yr pred</div>
                              )}
                            </>
                          )}
                        </div>
                      )}
                    </div>
                    {/* Key factors — show when expanded */}
                    {expanded && !a.missing_data && (
                      <AssetFactors asset={a} />
                    )}
                  </div>
                ))}
              </div>

              {expanded && graded && (
                <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6, textAlign: "right" }}>
                  {gradeMode === "process" ? "Predicted value" : "Actual value"}:{" "}
                  <strong style={{ color: "var(--text)" }}>{value.toFixed(0)}</strong>
                  {" "}({((gradeMode === "process" ? side.process_share : side.outcome_share) * 100).toFixed(0)}%)
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
