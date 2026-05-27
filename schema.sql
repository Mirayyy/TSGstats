-- TSGstats — Supabase Schema
-- Запускать в SQL Editor: https://supabase.com/dashboard/project/<id>/sql
-- Единственный источник истины. schema_additions.sql устарел и удалён.

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
    game_id    TEXT NOT NULL REFERENCES games(id)    ON DELETE CASCADE,
    steam_id   TEXT NOT NULL REFERENCES players(steam_id),
    kills      INT NOT NULL DEFAULT 0,
    deaths     INT NOT NULL DEFAULT 0,
    teamkills  INT NOT NULL DEFAULT 0,
    suicides   INT NOT NULL DEFAULT 0,
    extra      JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (game_id, steam_id)
);

-- ── processed_replays ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS processed_replays (
    filename     TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    status       TEXT NOT NULL DEFAULT 'ok'   -- 'ok' | 'error'
);

-- ── Индексы ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_pgs_steam_id  ON player_game_stats(steam_id);
CREATE INDEX IF NOT EXISTS idx_games_played  ON games(played_at DESC);

-- ── Leaderboard view ──────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW leaderboard AS
SELECT
    p.steam_id,
    p.display_name,
    COUNT(DISTINCT s.game_id)::int                                   AS games_played,
    COALESCE(SUM(s.kills),      0)::int                              AS total_kills,
    COALESCE(SUM(s.deaths),     0)::int                              AS total_deaths,
    COALESCE(SUM(s.teamkills),  0)::int                              AS total_teamkills,
    COALESCE(SUM(s.suicides),   0)::int                              AS total_suicides,
    CASE
        WHEN SUM(s.deaths) = 0 THEN SUM(s.kills)::numeric
        ELSE ROUND(SUM(s.kills)::numeric / SUM(s.deaths), 2)
    END                                                              AS kd_ratio
FROM players p
JOIN player_game_stats s ON s.steam_id = p.steam_id
GROUP BY p.steam_id, p.display_name;

-- ── RLS: читать может всё (публичная статистика) ──────────────────────────────
ALTER TABLE games             ENABLE ROW LEVEL SECURITY;
ALTER TABLE players           ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_game_stats ENABLE ROW LEVEL SECURITY;
-- processed_replays без публичных политик — анонимный доступ заблокирован
ALTER TABLE processed_replays ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "public read games"
    ON games FOR SELECT USING (true);

CREATE POLICY IF NOT EXISTS "public read players"
    ON players FOR SELECT USING (true);

CREATE POLICY IF NOT EXISTS "public read stats"
    ON player_game_stats FOR SELECT USING (true);

-- ── GRANT ─────────────────────────────────────────────────────────────────────
-- Публичные таблицы — читает anon (фронтенд через Supabase JS SDK)
GRANT SELECT ON public.games             TO anon, authenticated;
GRANT SELECT ON public.players           TO anon, authenticated;
GRANT SELECT ON public.player_game_stats TO anon, authenticated;
GRANT SELECT ON public.leaderboard       TO anon, authenticated;

-- Запись только через service_role (GitHub Actions / admin.py) — RLS не применяется
