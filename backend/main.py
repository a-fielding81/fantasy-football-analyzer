from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from db.database import init_db
from routers import seasons, teams, trades, draft, players

app = FastAPI(title="Fantasy Football Analyzer", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(seasons.router, prefix="/api/seasons", tags=["seasons"])
app.include_router(teams.router, prefix="/api/teams", tags=["teams"])
app.include_router(trades.router, prefix="/api/trades", tags=["trades"])
app.include_router(draft.router, prefix="/api/draft", tags=["draft"])
app.include_router(players.router, prefix="/api/players", tags=["players"])


@app.on_event("startup")
def startup():
    init_db()


@app.get("/api/health")
def health():
    return {"status": "ok"}
