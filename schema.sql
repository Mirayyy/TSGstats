-- TSGstats — Supabase Schema
-- Запускать в SQL Editor: https://supabase.com/dashboard/project/<id>/sql

-- ── games ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS games (
    id           TEXT PRIMARY KEY,        -- "2026-05-24T21:08:09"
    server       TEXT NOT NULL,           -- "T1", "T2"
    map          TEXT NOT NULL,
    mission      TEXT NOT NULL,
    duration_sec FLOAT NOT NULL,
    played_at    TIMESTAMPTZ NOT NULL,
    player_count INT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ── players ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS players (
    steam_id     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ── player_game_stats ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS player_game_stats (
    game_id    TEXT NOT NULL REFERENCES games(id)   ON DELETE CASCADE,
    steam_id   TEXT NOT NULL REFERENCES players(steam_id),
    kills      INT NOT NULL DEFAULT 0,
    deaths     INT NOT NULL DEFAULT 0,
    teamkills  INT NOT NULL DEFAULT 0,
    suicides   INT NOT NULL DEFAULT 0,
    extra      JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (game_id, steam_id)
);

-- ── Индексы для частых запросов ───────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_pgs_steam_id ON player_game_stats(steam_id);
CREATE INDEX IF NOT EXISTS idx_games_played_at ON games(played_at DESC);

-- ── RLS: читать может всё (публичная статистика) ──────────────────────────────
ALTER TABLE games             ENABLE ROW LEVEL SECURITY;
ALTER TABLE players           ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_game_stats ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public read games"
    ON games FOR SELECT USING (true);

CREATE POLICY "public read players"
    ON players FOR SELECT USING (true);

CREATE POLICY "public read stats"
    ON player_game_stats FOR SELECT USING (true);

-- Запись только через service_role (GitHub Actions) — RLS не применяется к нему
