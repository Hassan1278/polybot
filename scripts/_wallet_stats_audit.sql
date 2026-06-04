\echo === wallet stats coverage ===
WITH latest AS (
  SELECT DISTINCT ON (address) address, win_rate, sharpe,
         n_decisions, n_trade_days, n_open_positions,
         pnl_usdc, realized_pnl_usdc
  FROM wallet_stats WHERE "window"='30d'
  ORDER BY address, computed_at DESC
)
SELECT
  COUNT(*)                                       AS total,
  COUNT(*) FILTER (WHERE win_rate IS NULL)       AS no_winrate,
  COUNT(*) FILTER (WHERE sharpe   IS NULL)       AS no_sharpe,
  COUNT(*) FILTER (WHERE n_decisions <  5)       AS too_few_dec,
  COUNT(*) FILTER (WHERE n_trade_days < 5)       AS too_few_days,
  COUNT(*) FILTER (WHERE win_rate IS NOT NULL AND sharpe IS NOT NULL) AS fully_scored
FROM latest;

\echo === why wallets miss sharpe (n_trade_days distribution) ===
WITH latest AS (
  SELECT DISTINCT ON (address) address, n_trade_days, sharpe
  FROM wallet_stats WHERE "window"='30d'
  ORDER BY address, computed_at DESC
)
SELECT
  CASE
    WHEN n_trade_days = 0 THEN '0 (no trades)'
    WHEN n_trade_days = 1 THEN '1 day'
    WHEN n_trade_days BETWEEN 2 AND 4 THEN '2-4 days (below threshold)'
    WHEN n_trade_days BETWEEN 5 AND 9 THEN '5-9 days'
    ELSE '10+ days'
  END AS bucket,
  COUNT(*) AS wallets,
  COUNT(*) FILTER (WHERE sharpe IS NULL) AS still_null
FROM latest GROUP BY 1 ORDER BY 1;

\echo === top 6 wallets with FULL stats ===
WITH latest AS (
  SELECT DISTINCT ON (address) address, win_rate, sharpe,
         realized_pnl_usdc, n_decisions, n_trade_days
  FROM wallet_stats WHERE "window"='30d'
  ORDER BY address, computed_at DESC
)
SELECT substring(l.address,1,10) AS addr, w.category,
       ROUND(l.win_rate::numeric,2) AS wr,
       ROUND(l.sharpe::numeric,2)   AS sharpe,
       l.realized_pnl_usdc::int     AS pnl,
       l.n_decisions                AS dec,
       l.n_trade_days               AS days
FROM wallets w JOIN latest l USING(address)
WHERE w.is_active AND l.win_rate IS NOT NULL AND l.sharpe IS NOT NULL
ORDER BY l.realized_pnl_usdc DESC NULLS LAST LIMIT 6;
