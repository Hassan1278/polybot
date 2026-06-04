\echo --- top 12 by realized PnL ---
WITH latest AS (
  SELECT DISTINCT ON (address) address, win_rate, sharpe, pnl_usdc, realized_pnl_usdc,
                               n_decisions, n_open_positions, trade_count
  FROM wallet_stats WHERE "window"='30d'
  ORDER BY address, computed_at DESC
)
SELECT w.category,
       substring(l.address,1,12) AS addr,
       l.realized_pnl_usdc::int AS realised,
       l.pnl_usdc::int           AS total_pnl,
       CASE WHEN l.win_rate IS NULL THEN '—' ELSE ROUND((l.win_rate*100)::numeric,1)::text || '%' END AS wr,
       l.n_decisions AS n_dec,
       CASE WHEN l.sharpe IS NULL THEN '—' ELSE ROUND(l.sharpe::numeric,2)::text END AS sharpe,
       l.n_open_positions AS open,
       l.trade_count AS trades
FROM wallets w JOIN latest l USING(address)
WHERE w.is_active
ORDER BY l.realized_pnl_usdc DESC NULLS LAST LIMIT 12;

\echo
\echo --- worst 5 (so you can see why our discovery picked them) ---
WITH latest AS (
  SELECT DISTINCT ON (address) address, win_rate, sharpe, pnl_usdc, realized_pnl_usdc, n_decisions
  FROM wallet_stats WHERE "window"='30d'
  ORDER BY address, computed_at DESC
)
SELECT w.category,
       substring(l.address,1,12) AS addr,
       l.realized_pnl_usdc::int AS realised,
       l.pnl_usdc::int           AS total_pnl,
       CASE WHEN l.win_rate IS NULL THEN '—' ELSE ROUND((l.win_rate*100)::numeric,1)::text || '%' END AS wr,
       l.n_decisions AS n_dec
FROM wallets w JOIN latest l USING(address)
WHERE w.is_active
ORDER BY l.realized_pnl_usdc ASC NULLS LAST LIMIT 5;

\echo
\echo --- coverage ---
SELECT
  COUNT(*) AS n,
  COUNT(*) FILTER (WHERE win_rate IS NOT NULL) AS with_wr,
  COUNT(*) FILTER (WHERE sharpe IS NOT NULL) AS with_sharpe,
  COUNT(*) FILTER (WHERE realized_pnl_usdc > 0) AS profitable_realised
FROM (
  SELECT DISTINCT ON (address) win_rate, sharpe, realized_pnl_usdc
  FROM wallet_stats WHERE "window"='30d'
  ORDER BY address, computed_at DESC
) s;
