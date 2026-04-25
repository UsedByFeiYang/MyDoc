# MySQL 向量索引设计文档

> 基于 `sql/vidx/` 与 `include/vidx/` 源码分析，版本：2025。

---

## 目录

1. [架构概览](#1-架构概览)
2. [支持的功能](#2-支持的功能)
3. [主要流程](#3-主要流程)
4. [使用约束](#4-使用约束)
5. [系统变量参考](#5-系统变量参考)

---

## 1. 架构概览

向量索引（vidx）是以 **MySQL Daemon Plugin** 形式挂载到 MySQL 8.0 内核的原生向量近邻搜索能力，主要由以下几个模块组成：

```
┌─────────────────────────────────────────────────────┐
│                    SQL 层                           │
│  Field_vector   Item_func_vec_distance              │
│  VEC_FromText   VEC_ToText   vector_dim             │
│  DDL rewriter (check_vector_ddl_and_rewrite_sql)    │
├─────────────────────────────────────────────────────┤
│                   索引管理层                        │
│  vidx::create_table / delete_table / rename_table   │
│  vidx::build_hlindex_key                            │
│  vidx::test_if_cheaper_vector_ordering (优化器钩子) │
├─────────────────────────────────────────────────────┤
│               HNSW 算法层（核心）                   │
│  MHNSW_Share (全局图缓存, TABLE_SHARE 级别)         │
│  MHNSW_Trx   (事务私有写缓存)                      │
│  FVectorNode (图节点, 含向量 + 邻居链表)            │
│  FVector     (量化后的向量, int16 + scale)          │
├─────────────────────────────────────────────────────┤
│               存储层（辅助表）                      │
│  InnoDB 隐藏表 vidx_<table_id>_<num>               │
│  列: layer(tinyint), tref(varbinary), vec(blob),    │
│       neighbors(blob)                               │
└─────────────────────────────────────────────────────┘
```

**关键设计决策：**

- 向量图（HNSW graph）存储在一张与主表同库的 **InnoDB 隐藏辅助表**（`HT_HIDDEN_HLINDEX`）中，命名规则为 `vidx_%016lx_%02x`（基于主表的 `se_private_id`），对用户不可见。
- 图缓存（`MHNSW_Share`）挂载在 `TABLE_SHARE` 上，多连接共享，受 `commit_lock`（读写锁）保护。写事务通过 `MHNSW_Trx`（事务私有缓存）隔离，提交时合并到共享缓存。
- 向量在内存中以 **int16 量化**（`FVector`）表示，存储时序列化为 `scale + int16[]` 的紧凑格式，计算距离用 SIMD（AVX512 / AVX2 / NEON / 标量）加速点积。

---

## 2. 支持的功能

### 2.1 数据类型：`VECTOR(N)`

`VECTOR(N)` 是一个原生向量列类型，其中 `N` 为维度数（正整数）。

- **底层存储**：继承自 `Field_varstring`，使用 `VARBINARY(N*4)` 存放 N 个 IEEE 754 单精度浮点数（小端，4 字节/维）。
- **SQL 展示**：`SHOW CREATE TABLE` 中以 `/*!99999 vector(N) */ varbinary(N*4)` 形式呈现，版本注释机制保证在不支持向量的 MySQL 实例上被忽略（降级为普通 `varbinary`）。
- **写入验证**：
  - 拒绝 `double`、`longlong`、`decimal` 类型的直接赋值。
  - 接受二进制字符串（`BINARY` charset），长度必须严格等于 `N*4`。
  - 每个 float 分量不能为 NaN 或 Inf，且各分量平方和必须是有限数。
  - 支持大端机器（`WORDS_BIGENDIAN`）的字节序转换。

**建表语法示例：**

```sql
CREATE TABLE t (
  id   INT PRIMARY KEY,
  emb  VECTOR(128)
);
```

---

### 2.2 内置函数

#### `VEC_FromText(text_str)`

将 JSON 数组格式的文本（`[f1, f2, ..., fN]`）转换为向量的二进制表示。

```sql
SELECT VEC_FromText('[1.0, 2.0, 3.0]');
-- 返回 binary(12)，即 3 个 float 的小端字节序
```

#### `VEC_ToText(vec_binary)`

将向量的二进制表示还原为可读文本。

```sql
SELECT VEC_ToText(emb) FROM t LIMIT 1;
-- 返回类似 '[0.12345,0.67890,...]'
```

#### `vector_dim(vec_binary)`

返回向量的维度数（`BIGINT`）。

```sql
SELECT vector_dim(emb) FROM t LIMIT 1;
-- 返回 128
```

#### `VEC_DISTANCE(vec1, vec2)`

计算两个向量的距离（距离类型由索引定义决定，若无索引则报错 `ER_VEC_DISTANCE_TYPE`）。

#### `VEC_DISTANCE_EUCLIDEAN(vec1, vec2)`

显式使用欧几里得距离（L2 距离的平方根）：

$$d = \sqrt{\sum_i (v1_i - v2_i)^2}$$

#### `VEC_DISTANCE_COSINE(vec1, vec2)`

显式使用余弦距离：

$$d = 1 - \frac{v1 \cdot v2}{\|v1\| \cdot \|v2\|}$$

---

### 2.3 向量索引（HNSW）

向量索引使用 **Hierarchical Navigable Small World（HNSW）** 算法，支持近似最近邻（ANN）搜索。

**创建索引语法：**

```sql
-- 建表时
CREATE TABLE t (
  id  INT PRIMARY KEY,
  emb VECTOR(128),
  VECTOR INDEX vidx (emb) M=6 DISTANCE=EUCLIDEAN
);

-- ALTER TABLE 追加（仅支持单独操作）
ALTER TABLE t ADD VECTOR INDEX vidx (emb) M=10 DISTANCE=COSINE;
```

**删除索引：**

```sql
ALTER TABLE t DROP INDEX vidx;
```

**重命名索引：**

```sql
ALTER TABLE t RENAME INDEX vidx TO vidx_new;
```

**索引选项：**

| 选项 | 含义 | 默认值 | 范围 |
|---|---|---|---|
| `M` | 每个节点的最大邻居数（影响精度、内存、写入速度） | 6（可由 `hnsw_default_m` 变量覆盖） | 3 ~ 200 |
| `DISTANCE` | 距离函数 | `EUCLIDEAN`（可由 `default_distance` 变量覆盖） | `EUCLIDEAN`、`COSINE` |

---

### 2.4 ANN 查询（近似最近邻搜索）

向量索引只对如下特定查询模式生效：

```sql
SELECT * FROM t
ORDER BY VEC_DISTANCE(emb, VEC_FromText('[...]'))
LIMIT K;
```

优化器钩子（`test_if_cheaper_vector_ordering`）在满足以下条件时自动切换为向量索引扫描：

1. `ORDER BY` 仅有一列，且为 `VEC_DISTANCE` 函数。
2. 排序方向为 `ASC`（距离从小到大）。
3. 参数之一为向量列，另一个为常量。
4. `LIMIT` 值小于全表扫描代价阈值（主键扫描：`limit <= rows / 4`；二级索引扫描：`limit < rows`）。

**强制使用向量索引：**

```sql
SELECT * FROM t FORCE INDEX (vidx)
ORDER BY VEC_DISTANCE(emb, VEC_FromText('[...]'))
LIMIT 10;
```

**搜索精度控制：**

```sql
-- 会话级设置，ef 越大结果越准确但越慢
SET vidx_hnsw_ef_search = 100;  -- 默认 20，上限 10000
```

---

### 2.5 DDL 兼容性重写

含 `vector(N)` 列定义的 DDL 语句在执行前会被内核自动改写，将 `vector(N)` 替换为 `/*!99999 vector(N) */ varbinary(N*4)`，使 DDL 可在不支持向量扩展的 MySQL 实例（如副本）上重放，并降级为普通 `varbinary` 列。

仅涉及向量索引本身的 DDL（`ADD/DROP/RENAME VECTOR INDEX`）会被包裹在 `/*!99999 ... */` 版本注释中，副本若版本号低于 99999 则直接跳过该语句。

---

## 3. 主要流程

### 3.1 DDL 流程：创建向量索引

```
CREATE TABLE / ALTER TABLE ADD INDEX
        │
        ▼
check_vector_ddl_and_rewrite_sql()
  - 识别向量索引 DDL
  - 将 SQL 包裹在 /*!99999 ... */ 注释中
        │
        ▼
mysql_prepare_create_table()
  - key_info 中末尾追加 HA_VECTOR 标志的 KEY
        │
        ▼
vidx::create_table()
  1. 构造辅助表名: vidx_%016lx_%02x (基于 table se_private_id)
  2. 申请 MDL X 锁
  3. 通过 create_dd_table() 构建辅助表 dd::Table 对象:
     - 4 列: layer(tinyint), tref(varbinary), vec(blob), neighbors(blob)
     - 2 索引: IDX_TREF(unique), IDX_LAYER
     - 表选项中记录 __vector_m__, __vector_distance__, __vector_column__
  4. ha_create_table() 物理建表
  5. 更新 dd::Table，设置 __hlindexes__ 选项
```

辅助表结构说明：

| 列 | 类型 | 说明 |
|---|---|---|
| `layer` | tinyint | 该节点所在的最高层 |
| `tref` | varbinary | 指向主表行的引用（主键或 row_id，6 字节） |
| `vec` | blob | 量化后的向量（`scale(float) + dims[](int16)`） |
| `neighbors` | blob | 每层邻居引用列表（`<count><gref>...` 编码） |

---

### 3.2 INSERT 流程

```
ha_write_row() (主表写入)
        │
        ▼
handler::ha_write_row() 后置钩子（InnoDB）
        │
        ▼
mhnsw_insert(table, keyinfo)
  1. MHNSW_Share::acquire() 获取图上下文（写路径走 MHNSW_Trx）
  2. 随机采样层高 max_layer（按 1/M 指数分布）
  3. 构造 FVectorNode，量化向量为 FVector (int16 + scale)
  4. 若图为空，直接作为入口节点写入
  5. 否则从入口节点逐层 greedy 下降到 max_layer+1 层:
     search_layer(ef=1, skip_deleted=true) → 找到插入层的入口
  6. 从 max_layer 到第 0 层:
     search_layer(ef=ef_construction=10) → 找候选邻居
     prune_candidates() → 用 heuristic 选出最优 ≤M 个邻居
     双向建边（新节点←→邻居）
     若邻居边数超过 max_neighbors 则同样 prune
  7. FVectorNode::save() → 写入辅助表
  8. 更新入口节点（如新节点层高更高）
  9. MHNSW_Trx 中暂存，事务提交时 ctx->version++ 并使共享缓存失效
```

**量化细节（`FVector::create`）：**

- 找到各维度绝对值最大值 `max_val`，令 `scale = max_val / 32767`。
- 每个维度 `dims[i] = round(v[i] / scale)`，存为 `int16`。
- 距离计算：`dist(a, b) = a.abs2 + b.abs2 - a.scale * b.scale * dot(a.dims, b.dims)`，等价于欧几里得距离平方（不开方）。
- COSINE 模式额外归一化：写入时 `scale /= sqrt(2 * abs2)`，`abs2 = 0.5`，使余弦距离与欧几里得距离公式统一。

---

### 3.3 SELECT/ANN 查询流程

```
SELECT ... ORDER BY VEC_DISTANCE(emb, const) LIMIT K
        │
        ▼
test_if_cheaper_vector_ordering()
  - 代价评估，满足条件则切换为 JT_INDEX_SCAN，绑定向量索引
        │
        ▼
mhnsw_read_first(table, keyinfo, dist_item)
  1. MHNSW_Share::acquire()（读路径，加 commit_lock 读锁）
  2. 从量化查询向量构造 FVector
  3. 从入口节点 greedy 下降，逐层 search_layer(ef=1) 直到第 1 层
  4. 第 0 层 search_layer(ef = max(ef_search, LIMIT)):
     - 使用 Bloom filter 加速 visited 判断（SIMD 8路并行）
     - 候选优先队列（min-heap by distance）
     - best 结果队列（max-heap by distance，容量 ef）
     - generous_furthest() heuristic 动态扩展搜索半径
  5. 过滤已删除节点（tref IS NULL）
  6. 结果按距离排序后缓存在 TABLE::hlindex 中
        │
        ▼
mhnsw_read_next()
  - 顺序返回已缓存的结果行（通过 tref 回表查主表）
        │
        ▼
mhnsw_read_end()
  - 释放 MHNSW_Share 引用（解 commit_lock 读锁）
```

---

### 3.4 DELETE 流程（软删除）

```
ha_delete_row() (主表删除)
        │
        ▼
mhnsw_invalidate(table, rec, keyinfo)
  - 找到对应 FVectorNode
  - 将辅助表中该节点的 tref 列置为 NULL（标记为 deleted）
  - 图结构本身不修改（邻居链路保留）
  - 搜索时自动跳过 deleted=true 的节点
```

> 注意：向量索引目前采用**软删除**策略，被删除的节点仍留在图中参与图遍历（用于连通性），仅在返回结果时过滤掉。这避免了图结构重组的高代价，但会随着删除比例增加而降低搜索效率。

---

### 3.5 事务并发控制

```
                    ┌─────────────────────┐
                    │  MHNSW_Share (共享) │
                    │  commit_lock (rwlock)│
                    │  version            │
                    └─────────────────────┘
                           ↑  ↑
              读：rdlock    │  │  写：wrlock（提交时）
                           │  │
              ┌────────────┘  └────────────┐
              │                            │
  ┌───────────────────┐        ┌───────────────────┐
  │  Reader Thread A  │        │  Writer Thread B  │
  │  MHNSW_Share.dup()│        │  MHNSW_Trx        │
  │  遍历图，不修改   │        │  私有写缓存       │
  └───────────────────┘        │  commit → 使共享  │
                               │  缓存失效(version++)│
                               └───────────────────┘
```

- **读事务**：持有 `commit_lock` 读锁期间，可以并发遍历图，互不阻塞。
- **写事务**：插入操作在 `MHNSW_Trx`（事务私有副本）中进行；提交时加写锁，递增 `version`，并将共享缓存中受影响节点的 `vec` 置为 `nullptr`（触发下次访问时从辅助表重新加载），然后释放写锁。
- **节点级锁**：`MHNSW_Share` 内有 8 个分片 mutex（`node_lock[]`），按节点指针哈希分桶，用于保护节点从磁盘懒加载时的 `load_from_record()` 临界区。
- **Savepoint Rollback**：`do_savepoint_rollback` 直接丢弃 `MHNSW_Trx` 中所有待写节点。

---

## 4. 使用约束

### 4.1 数据类型约束

| 约束 | 说明 |
|---|---|
| 最大维度 | `VECTOR(N)` 中 N ≤ 16383 |
| 元素精度 | 固定 `float`（32位单精度），不支持 `double` |
| 字节序 | 存储为小端格式；大端机器（SPARC 等）自动转换 |
| NULL 值 | 向量列允许 NULL；NULL 值不写入向量索引 |
| 不允许的值 | 任意维度为 NaN 或 Inf 时拒绝写入 |
| 类型转换 | 不支持从 `double`/`int`/`decimal` 隐式赋值给向量列 |

---

### 4.2 索引约束

| 约束 | 说明 |
|---|---|
| 每表索引数 | 每张表**仅支持一个**向量索引（`vidx_num = 0`，名称 `vidx_%016lx_00`） |
| 存储引擎 | 仅支持 **InnoDB**（`assert(dd_table->engine() == "InnoDB")`） |
| 索引列数 | 向量索引只能建在**单列**上（`user_defined_key_parts == 1`） |
| M 参数范围 | 3 ≤ M ≤ 200，默认 6 |
| 距离函数 | 仅 `EUCLIDEAN`（欧几里得）和 `COSINE`（余弦），不支持内积（IP） |
| 不可混合 DDL | 向量索引的 ADD/DROP/RENAME 不能与其他 DDL 操作在同一 `ALTER TABLE` 中组合 |
| 不支持可见性 | `ALTER INDEX ... VISIBLE/INVISIBLE` 对向量索引无效 |

---

### 4.3 查询约束

| 约束 | 说明 |
|---|---|
| 必须有 LIMIT | 向量索引仅对带 `LIMIT` 的 ANN 查询生效；无 LIMIT 时不使用向量索引 |
| ORDER BY 方向 | 只支持 `ASC`（距离由近到远）；`DESC` 不触发向量索引 |
| 参数形式 | `VEC_DISTANCE` 的两个参数必须一个是向量列，另一个是常量（含 `VEC_FromText(...)` 的表达式） |
| 距离函数匹配 | 查询中使用的距离函数类型必须与索引创建时的 `DISTANCE` 参数一致；`VEC_DISTANCE` 会自动推断，`VEC_DISTANCE_EUCLIDEAN`/`VEC_DISTANCE_COSINE` 则直接指定 |
| 视图限制 | 通过视图使用向量列时，`VEC_DISTANCE` 的 `field_arg` 解析依赖 `real_item()` 展开，需注意视图列的解析时机 |
| ef_search 下限 | 实际搜索候选数为 `max(ef_search, LIMIT)`；即使 `LIMIT 1` 也至少搜索 `ef_search`（默认 20）个候选 |

---

### 4.4 事务约束

| 约束 | 说明 |
|---|---|
| 隔离级别 | **仅支持 `READ COMMITTED`**；其他隔离级别（包括 `REPEATABLE READ`）在 `hlindex_open` 时报错 `ER_NOT_SUPPORTED_YET` |
| 读一致性 | 向量搜索读取的是**提交后的快照**（通过 version 机制），事务内未提交的插入对其他事务不可见，但对本事务内的查询是否可见取决于实现（当前实现中 `MHNSW_Trx` 不参与读路径） |

---

### 4.5 缓存约束

| 约束 | 说明 |
|---|---|
| 缓存上限 | 单个向量索引缓存上限由 `vidx_hnsw_cache_size` 控制，默认 16 MiB |
| 超限行为 | 缓存超限时调用 `MHNSW_Share::reset()` 清空缓存，下次查询全量从辅助表加载（冷启动） |
| 缓存失效 | 每次事务提交（写操作）后，受影响节点在共享缓存中的向量数据失效，下次访问时重新从辅助表加载 |

---

### 4.6 功能开关

向量索引功能默认**禁用**（`feature_disabled = true`，对应系统变量 `vidx_disabled = ON`）。需要在 MySQL 配置或运行时显式开启：

```ini
# my.cnf
vidx_disabled = OFF
```

或运行时（需 SUPER 权限）：

```sql
SET GLOBAL vidx_disabled = OFF;
```

---

## 5. 系统变量参考

| 变量名 | 作用域 | 默认值 | 说明 |
|---|---|---|---|
| `vidx_disabled` | GLOBAL | `ON` | 是否禁用向量索引功能 |
| `vidx_default_distance` | SESSION | `EUCLIDEAN` | 建索引时未指定 DISTANCE 的默认距离函数 |
| `vidx_hnsw_default_m` | SESSION | `6` | 建索引时未指定 M 的默认值 |
| `vidx_hnsw_ef_search` | SESSION | `20` | ANN 搜索时最少搜索的候选数；越大越准但越慢（上限 10000） |
| `vidx_hnsw_cache_size` | GLOBAL | `16777216`（16 MiB） | 单个向量索引 in-memory 图缓存的上限 |

---

## 附录：向量相关错误码

| 错误名 | 错误号 | 触发场景 |
|---|---|---|
| `ER_VECTOR_DISABLED` | 7518 | 向量索引功能被禁用时执行相关 DDL/DML |
| `ER_DATA_INCOMPATIBLE_WITH_VECTOR` | 7519 | 向量列收到类型不兼容的值 |
| `ER_TO_VECTOR_CONVERSION` | 7520 | `VEC_FromText` 输入无法解析为合法向量 |
| `ER_VEC_DISTANCE_TYPE` | 7521 | `VEC_DISTANCE` 无法自动推断距离类型（未找到关联索引） |
| `ER_VECTOR_BINARY_FORMAT_INVALID` | 7522 | `VEC_ToText` 输入的二进制格式不合法 |
| `ER_VECTOR_INDEX_USAGE` | 7523 | 向量索引使用方式不正确 |
| `ER_VECTOR_INDEX_FAILED` | 7524 | 向量索引的 Create/Drop/Rename/Open 操作失败 |
