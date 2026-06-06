import { useState } from "react";
import { Trophy, ArrowLeftRight, ClipboardList, Search, BarChart2 } from "lucide-react";
import StandingsView from "./views/StandingsView";
import TradesView from "./views/TradesView";
import DraftView from "./views/DraftView";
import PlayerSearchView from "./views/PlayerSearchView";
import ManagersView from "./views/ManagersView";
import "./App.css";

type Tab = "standings" | "managers" | "trades" | "draft" | "players";

const TABS: { id: Tab; label: string; Icon: React.FC<{ size?: number }> }[] = [
  { id: "standings", label: "Standings", Icon: Trophy },
  { id: "managers", label: "Managers", Icon: BarChart2 },
  { id: "trades", label: "Trades", Icon: ArrowLeftRight },
  { id: "draft", label: "Draft", Icon: ClipboardList },
  { id: "players", label: "Players", Icon: Search },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("standings");

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-inner">
          <div className="header-brand">
            <span className="header-emoji">🏈</span>
            <span className="header-title">Fantasy Football Analyzer</span>
          </div>
          <nav className="header-nav">
            {TABS.map(({ id, label, Icon }) => (
              <button
                key={id}
                className={`nav-btn ${tab === id ? "active" : ""}`}
                onClick={() => setTab(id)}
              >
                <Icon size={15} />
                {label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      <main className="app-main">
        {tab === "standings" && <StandingsView />}
        {tab === "managers" && <ManagersView />}
        {tab === "trades" && <TradesView />}
        {tab === "draft" && <DraftView />}
        {tab === "players" && <PlayerSearchView />}
      </main>
    </div>
  );
}
