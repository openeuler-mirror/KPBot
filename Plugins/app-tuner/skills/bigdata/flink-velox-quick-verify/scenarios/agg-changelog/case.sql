-- agg-changelog: GROUP BY aggregation over a bounded event stream. The
-- print sink sees the full changelog (+I, -U, +U chains) as running counts
-- update; the reference model folds the same semantics into the final
-- result set.
CREATE TABLE events (
  user_name STRING,
  amount BIGINT
) WITH (
  'connector' = 'filesystem',
  'path' = 'file://{{INPUT_DIR}}/events',
  'format' = 'csv'
);
CREATE TABLE sink (
  user_name STRING,
  cnt BIGINT,
  total BIGINT
) WITH ('connector' = 'print');
INSERT INTO sink
SELECT user_name, COUNT(*), SUM(amount)
FROM events
GROUP BY user_name;
