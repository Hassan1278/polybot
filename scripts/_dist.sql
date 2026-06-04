WITH latest AS (
  SELECT DISTINCT ON (address) address, win_rate, sharpe, pnl_usdc, roi, trade_count
  FROM wallet_stats WHERE "window"='30d'
  ORDER BY address, computed_at DESC
)
SELECT ROUND(AVG(win_rate)::numeric,3) AS avg_wr,
       ROUND(MIN(win_rate)::numeric,3) AS min_wr,
       ROUND(MAX(win_rate)::numeric,3) AS max_wr,
       ROUND(AVG(sharpe)::numeric,2)   AS avg_sh,
       ROUND(MAX(sharpe)::numeric,2)   AS max_sh,
       COUNT(*) FILTER (WHERE win_rate >= 0.99) AS pct_100,
       COUNT(*) FILTER (WHERE sharpe = 0)       AS sh_0,
       COUNT(*) AS n
FROM latest;

\echo ---  top 10  ---

WITH latest AS (
  SELECT DISTINCT ON (address) address, win_rate, sharpe, pnl_usdc, trade_count
  FROM wallet_stats WHERE "window"='30d'
  ORDER BY address, computed_at DESC
)
SELECT w.category,
       substring(l.address,1,12) AS addr,
       ROUND(l.win_rate::numeric,3) AS wr,
       ROUND(l.sharpe::numeric,2)   AS sharpe,
       l.pnl_usdc::int AS pnl,
       l.trade_count AS n
FROM wallets w JOIN latest l USING(address)
WHERE w.is_active AND l.win_rate < 0.99
ORDER BY (l.win_rate * (1 + l.sharpe)) DESC NULLS LAST
LIMIT 10;
