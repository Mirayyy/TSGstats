-- TSGstats — Schema Additions
-- Запускать в SQL Editor после schema.sql

-- ── Leaderboard view ──────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW leaderboard AS
SELECT
    p.steam_id,
    p.display_name,
    COUNT(DISTINCT s.game_id)::int                                  AS games_played,
    COALESCE(SUM(s.kills), 0)::int                                  AS total_kills,
    COALESCE(SUM(s.deaths), 0)::int                                 AS total_deaths,
    COALESCE(SUM(s.teamkills), 0)::int                              AS total_teamkills,
    COALESCE(SUM(s.suicides), 0)::int                               AS total_suicides,
    CASE
        WHEN SUM(s.deaths) = 0 THEN SUM(s.kills)::numeric
        ELSE ROUND(SUM(s.kills)::numeric / SUM(s.deaths), 2)
    END                                                             AS kd_ratio
FROM players p
JOIN player_game_stats s ON s.steam_id = p.steam_id
GROUP BY p.steam_id, p.display_name;

-- Разрешаем читать через anon-ключ (фронтенд)
GRANT SELECT ON public.leaderboard TO anon, authenticated;

-- ── Трекинг обработанных реплеев ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS processed_replays (
    filename     TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ DEFAULT NOW()
);

-- Писать только service_role, читать только через него же (внутренняя таблица)
ALTER TABLE processed_replays ENABLE ROW LEVEL SECURITY;
-- Нет публичных политик — анонимный доступ заблокирован
