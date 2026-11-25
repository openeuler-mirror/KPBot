-- pgvector 冒烟验证：需要在已安装 vector 扩展的目标数据库中执行。
-- 期望最后一行输出 1（查询向量 [1,2,3] 的最近邻是 id=1）。
CREATE TEMP TABLE pgvector_smoke (id int, embedding vector(3));
INSERT INTO pgvector_smoke VALUES (1, '[1,2,3]'), (2, '[4,5,6]');
SELECT id FROM pgvector_smoke ORDER BY embedding <-> '[1,2,3]' LIMIT 1;
