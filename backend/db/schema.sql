-- ============================================================
-- Fantasy Football Analyzer — Core Schema
-- ============================================================
-- Design notes:
--   • Seasons from ESPN (2021) and Sleeper (2022+) share the
--     same tables; source_platform distinguishes them.
--   • All picks and trades link back to a season + league,
--     enabling multi-horizon grading queries.
--   • keeper_eligible is derived but stored for query perf.
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- Leagues & Seasons
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS leagues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT    NOT NULL CHECK (platform IN ('espn', 'sleeper')),
    platform_id     TEXT    NOT NULL,  -- ESPN leagueId or Sleeper league_id
    name            TEXT    NOT NULL,
    scoring_format  TEXT    NOT NULL DEFAULT 'half_ppr',
    team_count      INTEGER NOT NULL DEFAULT 10,
    keepers_per_team INTEGER NOT NULL DEFAULT 6,
    UNIQUE (platform, platform_id)
);

CREATE TABLE IF NOT EXISTS seasons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id       INTEGER NOT NULL REFERENCES leagues(id),
    year            INTEGER NOT NULL,
    platform        TEXT    NOT NULL CHECK (platform IN ('espn', 'sleeper')),
    platform_season_id TEXT,           -- Sleeper league_id for that year (changes per season)
    draft_date      TEXT,
    season_start    TEXT,
    season_end      TEXT,
    champion_team_id INTEGER,          -- FK set after insert
    UNIQUE (league_id, year)
);

-- ------------------------------------------------------------
-- Teams / Managers
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS managers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name    TEXT    NOT NULL,
    sleeper_user_id TEXT UNIQUE,
    espn_owner_id   TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(id),
    manager_id      INTEGER NOT NULL REFERENCES managers(id),
    platform_team_id TEXT   NOT NULL,  -- roster_id (Sleeper) or teamId (ESPN)
    team_name       TEXT,
    final_rank      INTEGER,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    ties            INTEGER DEFAULT 0,
    points_for      REAL    DEFAULT 0,
    points_against  REAL    DEFAULT 0,
    UNIQUE (season_id, platform_team_id)
);

-- Update champion FK now that teams table exists
-- (Applied at runtime via migration, not inline DDL)

-- ------------------------------------------------------------
-- Players
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeper_id      TEXT    UNIQUE,
    espn_id         TEXT    UNIQUE,
    full_name       TEXT    NOT NULL,
    first_name      TEXT,
    last_name       TEXT,
    position        TEXT    NOT NULL CHECK (position IN ('QB','RB','WR','TE','K','DEF','DL','LB','DB','FLEX','SUPER_FLEX','IDP','UNKNOWN')),
    nfl_team        TEXT,
    birth_date      TEXT,
    years_exp       INTEGER,
    status          TEXT    -- 'Active', 'Injured Reserve', 'Retired', etc.
);

-- ------------------------------------------------------------
-- Draft Picks
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS draft_picks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(id),
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    player_id       INTEGER REFERENCES players(id),  -- NULL for traded future picks
    round           INTEGER NOT NULL,
    pick_number     INTEGER NOT NULL,           -- overall pick number
    pick_in_round   INTEGER NOT NULL,
    is_keeper       INTEGER NOT NULL DEFAULT 0, -- boolean
    adp_at_draft    REAL,                       -- ADP at time of draft
    platform_pick_id TEXT,
    UNIQUE (season_id, pick_number)
);

-- ------------------------------------------------------------
-- Rosters (weekly snapshots)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS roster_players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(id),
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    player_id       INTEGER NOT NULL REFERENCES players(id),
    week            INTEGER NOT NULL,   -- 0 = end-of-season/final roster
    acquisition_type TEXT,              -- 'draft', 'waiver', 'trade', 'keeper', 'free_agent'
    UNIQUE (season_id, team_id, player_id, week)
);

-- ------------------------------------------------------------
-- Trades
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(id),
    week            INTEGER,
    transaction_date TEXT,
    platform_transaction_id TEXT UNIQUE,
    status          TEXT DEFAULT 'completed'  -- 'completed', 'vetoed', 'pending'
);

CREATE TABLE IF NOT EXISTS trade_sides (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id),
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    direction       TEXT    NOT NULL CHECK (direction IN ('sent', 'received'))
);

CREATE TABLE IF NOT EXISTS trade_assets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id),
    sending_team_id  INTEGER NOT NULL REFERENCES teams(id),
    receiving_team_id INTEGER NOT NULL REFERENCES teams(id),
    asset_type      TEXT    NOT NULL CHECK (asset_type IN ('player', 'draft_pick')),
    player_id       INTEGER REFERENCES players(id),
    -- For future draft picks traded before the draft:
    pick_season_year INTEGER,
    pick_round       INTEGER,
    pick_original_team_id INTEGER REFERENCES teams(id),
    -- player assets: one row per player per trade
    -- pick assets: one row per pick per trade direction
    UNIQUE (trade_id, sending_team_id, receiving_team_id, player_id),
    UNIQUE (trade_id, sending_team_id, receiving_team_id, pick_season_year, pick_round)
);

-- ------------------------------------------------------------
-- Weekly Scores
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS weekly_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(id),
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    week            INTEGER NOT NULL,
    points_scored   REAL    NOT NULL,
    points_against  REAL,
    is_playoff      INTEGER NOT NULL DEFAULT 0,
    matchup_id      TEXT,
    UNIQUE (season_id, team_id, week)
);

CREATE TABLE IF NOT EXISTS player_weekly_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(id),
    player_id       INTEGER NOT NULL REFERENCES players(id),
    week            INTEGER NOT NULL,
    fantasy_points  REAL    NOT NULL DEFAULT 0,
    -- Raw stat buckets (populated where available)
    pass_yds        REAL,
    pass_tds        INTEGER,
    interceptions   INTEGER,
    rush_yds        REAL,
    rush_tds        INTEGER,
    rec_yds         REAL,
    rec_tds         INTEGER,
    receptions      REAL,
    UNIQUE (season_id, player_id, week)
);

-- ------------------------------------------------------------
-- Ingestion Audit Log
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ingestion_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    platform        TEXT    NOT NULL,
    season_year     INTEGER NOT NULL,
    records_inserted INTEGER DEFAULT 0,
    status          TEXT    NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    notes           TEXT
);

-- ============================================================
-- Useful Views
-- ============================================================

-- Season-level team summary with manager name
CREATE VIEW IF NOT EXISTS v_team_season_summary AS
SELECT
    s.year,
    m.display_name  AS manager,
    t.team_name,
    t.wins,
    t.losses,
    t.ties,
    t.points_for,
    t.points_against,
    ROUND(t.points_for - t.points_against, 2) AS point_diff,
    t.final_rank,
    s.platform
FROM teams t
JOIN seasons s ON s.id = t.season_id
JOIN managers m ON m.id = t.manager_id
ORDER BY s.year, t.final_rank;

-- Draft pick summary: who picked whom, when, and how they finished the season
CREATE VIEW IF NOT EXISTS v_draft_pick_summary AS
SELECT
    s.year,
    m.display_name      AS manager,
    dp.round,
    dp.pick_number,
    dp.pick_in_round,
    p.full_name         AS player_name,
    p.position,
    p.nfl_team,
    dp.adp_at_draft,
    dp.is_keeper,
    COALESCE(
        (SELECT ROUND(SUM(pws.fantasy_points), 2)
         FROM player_weekly_stats pws
         WHERE pws.player_id = dp.player_id
           AND pws.season_id = dp.season_id),
        0
    ) AS season_fantasy_points
FROM draft_picks dp
JOIN seasons s ON s.id = dp.season_id
JOIN teams t ON t.id = dp.team_id
JOIN managers m ON m.id = t.manager_id
LEFT JOIN players p ON p.id = dp.player_id
ORDER BY s.year, dp.pick_number;

-- Trade asset detail: who got what in each trade
CREATE VIEW IF NOT EXISTS v_trade_detail AS
SELECT
    s.year,
    t.week            AS trade_week,
    t.transaction_date,
    send_m.display_name  AS sender,
    recv_m.display_name  AS receiver,
    ta.asset_type,
    CASE ta.asset_type
        WHEN 'player' THEN p.full_name
        ELSE 'Pick ' || ta.pick_season_year || ' Rd ' || ta.pick_round
    END                  AS asset_description,
    p.position,
    t.id                 AS trade_id
FROM trade_assets ta
JOIN trades t ON t.id = ta.trade_id
JOIN seasons s ON s.id = t.season_id
JOIN teams send_t ON send_t.id = ta.sending_team_id
JOIN managers send_m ON send_m.id = send_t.manager_id
JOIN teams recv_t ON recv_t.id = ta.receiving_team_id
JOIN managers recv_m ON recv_m.id = recv_t.manager_id
LEFT JOIN players p ON p.id = ta.player_id
ORDER BY s.year, t.week, t.id;
