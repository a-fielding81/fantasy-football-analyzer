import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

interface TradeAsset {
  year: number;
  trade_week: number | null;
  transaction_date: string | null;
  sender: string;
  receiver: string;
  asset_type: "player" | "draft_pick";
  asset_description: string;
  position: string | null;
  trade_id: number;
}

function groupByTrade(rows: TradeAsset[]): Map<number, TradeAsset[]> {
  const map = new Map<number, TradeAsset[]>();
  for (const row of rows) {
    if (!map.has(row.trade_id)) map.set(row.trade_id, []);
    map.get(row.trade_id)!.push(row);
  }
  return map;
}

function posBadge(pos: string | null) {
  if (!pos) return null;
  const known = ["QB","RB","WR","TE","K","DEF"];
  return <span className={`badge badge-pos-${known.includes(pos) ? pos : "default"}`}>{pos}</span>;
}

export default function TradesView() {
  const { data: seasons } = useQuery({ queryKey: ["seasons"], queryFn: api.seasons.list });
  const years: number[] = seasons
    ? [...new Set<number>(seasons.map((s: { year: number }) => s.year))].sort()
    : [];

  const [year, setYear] = useState<number | null>(null);
  const [managerFilter, setManagerFilter] = useState("");

  const { data: trades, isLoading } = useQuery({
    queryKey: ["trades-detail", year],
    queryFn: () => api.trades.detail(year ?? undefined),
  });

  const grouped = useMemo(() => {
    if (!trades) return new Map<number, TradeAsset[]>();
    const filtered = managerFilter
      ? trades.filter(
          (r: TradeAsset) =>
            r.sender.toLowerCase().includes(managerFilter.toLowerCase()) ||
            r.receiver.toLowerCase().includes(managerFilter.toLowerCase())
        )
      : trades;
    return groupByTrade(filtered);
  }, [trades, managerFilter]);

  const tradeIds = [...grouped.keys()].sort((a, b) => {
    const ra = grouped.get(a)![0];
    const rb = grouped.get(b)![0];
    return rb.year - ra.year || (rb.trade_week ?? 0) - (ra.trade_week ?? 0);
  });

  return (
    <div>
      <div className="view-title">Trade History</div>

      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        <div className="year-selector" style={{ marginBottom: 0 }}>
          <button className={`year-btn ${year === null ? "active" : ""}`} onClick={() => setYear(null)}>All</button>
          {years.map((y) => (
            <button key={y} className={`year-btn ${year === y ? "active" : ""}`} onClick={() => setYear(y)}>{y}</button>
          ))}
        </div>
        <input
          placeholder="Filter by manager…"
          value={managerFilter}
          onChange={(e) => setManagerFilter(e.target.value)}
          style={{
            background: "var(--bg2)", border: "1px solid var(--border)", borderRadius: 6,
            padding: "5px 12px", color: "var(--text)", fontSize: 13, outline: "none",
          }}
        />
      </div>

      <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 16 }}>
        {tradeIds.length} trade{tradeIds.length !== 1 ? "s" : ""}
        {year ? ` in ${year}` : " total"}
        {managerFilter ? ` involving "${managerFilter}"` : ""}
      </div>

      {isLoading ? (
        <div className="loading">Loading…</div>
      ) : tradeIds.length === 0 ? (
        <div className="empty">No trades found.</div>
      ) : (
        <div className="trade-list">
          {tradeIds.map((tradeId) => {
            const assets = grouped.get(tradeId)!;
            const first = assets[0];

            // Build two-sided view: group assets by sender
            const sides = new Map<string, { receiving: TradeAsset[] }>();
            for (const a of assets) {
              if (!sides.has(a.sender)) sides.set(a.sender, { receiving: [] });
              sides.get(a.sender)!.receiving.push(a);
            }
            // Each side "receives" what the OTHER sender sent
            const sideEntries = [...sides.entries()];

            return (
              <div key={tradeId} className="trade-card">
                <div className="trade-card-header">
                  <span><strong>Week {first.trade_week ?? "?"}</strong></span>
                  <span>{first.year}</span>
                  {first.transaction_date && (
                    <span>{new Date(Number(first.transaction_date)).toLocaleDateString()}</span>
                  )}
                </div>
                <div className="trade-sides">
                  {sideEntries.map(([sender, { receiving }], idx) => (
                    <div key={idx} className="trade-side">
                      <div className="trade-side-label">Sends</div>
                      <div className="trade-side-manager">{sender}</div>
                      <div className="trade-assets">
                        {receiving.map((a, i) => (
                          <div key={i} className="trade-asset">
                            {a.asset_type === "player" && posBadge(a.position)}
                            {a.asset_type === "draft_pick" && <span className="badge badge-pos-default">PICK</span>}
                            <span>{a.asset_description}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
