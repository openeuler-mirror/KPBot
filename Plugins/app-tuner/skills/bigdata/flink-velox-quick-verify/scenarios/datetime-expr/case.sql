-- datetime-expr: project the 10 time-extraction functions over a bounded
-- bid stream. {{INPUT_DIR}} is replaced by the executor with the absolute
-- path of this scenario's input directory.
CREATE TABLE bids (
  id BIGINT,
  price BIGINT,
  channel STRING,
  dt TIMESTAMP(3)
) WITH (
  'connector' = 'filesystem',
  'path' = 'file://{{INPUT_DIR}}/bids',
  'format' = 'csv'
);
CREATE TABLE sink (
  id BIGINT,
  yr BIGINT, mo BIGINT, dy BIGINT, doy BIGINT, dow BIGINT, q BIGINT,
  hr BIGINT, mi BIGINT, ss BIGINT, wk BIGINT
) WITH ('connector' = 'print');
INSERT INTO sink
SELECT id,
       YEAR(dt), MONTH(dt), DAYOFMONTH(dt), DAYOFYEAR(dt), DAYOFWEEK(dt),
       QUARTER(dt), HOUR(dt), MINUTE(dt), SECOND(dt), WEEK(dt)
FROM bids;
