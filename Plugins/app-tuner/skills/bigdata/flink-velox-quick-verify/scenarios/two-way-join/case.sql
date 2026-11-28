-- two-way-join: regular inner join between two bounded streams with
-- many events per key. The final result set is every (order, shipment)
-- pair sharing a key; keys present on only one side contribute nothing.
CREATE TABLE orders (
  oid BIGINT,
  key INT,
  tag STRING
) WITH (
  'connector' = 'filesystem',
  'path' = 'file://{{INPUT_DIR}}/orders',
  'format' = 'csv'
);
CREATE TABLE shipments (
  sid BIGINT,
  key INT,
  label STRING
) WITH (
  'connector' = 'filesystem',
  'path' = 'file://{{INPUT_DIR}}/shipments',
  'format' = 'csv'
);
CREATE TABLE sink (
  key INT,
  tag STRING,
  label STRING
) WITH ('connector' = 'print');
INSERT INTO sink
SELECT o.key, o.tag, s.label
FROM orders o
JOIN shipments s
ON o.key = s.key;
