-- filter-project: predicate filter + projection + expression arithmetic
-- over a bounded item stream. Boundary rows pin the predicate operators:
-- price 100 is NOT taken (strict >), qty 2 IS taken (>=).
CREATE TABLE items (
  id BIGINT,
  name STRING,
  price BIGINT,
  qty INT
) WITH (
  'connector' = 'filesystem',
  'path' = 'file://{{INPUT_DIR}}/items',
  'format' = 'csv'
);
CREATE TABLE sink (
  id BIGINT,
  name STRING,
  total BIGINT,
  next_qty INT
) WITH ('connector' = 'print');
INSERT INTO sink
SELECT id, name, price * qty, qty + 1
FROM items
WHERE price > 100 AND qty >= 2;
