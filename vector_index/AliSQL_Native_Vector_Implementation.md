# AliSQL 原生向量索引实现详解

> 分析基于 AliSQL 源码：`/home/yf/Project/AliSQL`
> 核心文件：`sql/vidx/vidx_hnsw.cc`（1551行）、`sql/vidx/vidx_index.cc`（1162行）、`sql/vidx/vidx_func.cc`、`include/vidx/`

---

## 一、总体架构

### 1.1 模块组成

AliSQL 的向量索引以 **MySQL Daemon 插件**（plugin name: `vidx`，vendor: `AliCloud`）形式集成到数据库引擎中，不依赖 DuckDB，完全原生实现。

```
┌────────────────────────────────────────────────────────────────┐
│                       SQL 层                                    │
│  ┌──────────────┐  ┌─────────────────────────┐                 │
│  │ DDL 解析/改写 │  │  查询优化器 (sql_planner) │                │
│  │ vector(N)    │  │  test_if_cheaper_vector  │                 │
│  │ → varbinary  │  │  _ordering()             │                 │
│  └──────┬───────┘  └────────────┬────────────┘                 │
│         │                       │                              │
│  ┌──────▼───────────────────────▼────────────────────────┐     │
│  │              vidx 插件（daemon plugin）                 │     │
│  │  ┌──────────────────────────────────────────────────┐  │     │
│  │  │           HNSW 算法核心 (vidx_hnsw.cc)           │  │     │
│  │  │  insert / read_first / read_next / invalidate    │  │     │
│  │  └──────────────────────────────────────────────────┘  │     │
│  │  ┌─────────────────┐  ┌────────────────────────────┐   │     │
│  │  │  MHNSW_Share    │  │  MHNSW_Trx                 │   │     │
│  │  │  (公共节点缓存)  │  │  (事务私有缓存)             │   │     │
│  │  │  挂在TABLE_SHARE│  │  挂在 thd->ha_data[]       │   │     │
│  │  └────────┬────────┘  └───────────┬────────────────┘   │     │
│  └───────────┼────────────────────────┼───────────────────┘     │
│              │                        │                          │
└──────────────┼────────────────────────┼──────────────────────────┘
               │                        │
┌──────────────▼────────────────────────▼──────────────────────────┐
│                    InnoDB 存储引擎                                  │
│  ┌────────────────────────┐  ┌────────────────────────────────┐  │
│  │     用户主表 (base)     │  │    辅助表 (hlindex)             │  │
│  │  - 业务字段            │  │  命名: vidx_<se_id>_00          │  │
│  │  - VECTOR(N) 列        │  │  列: layer, tref, vec, neighbors│  │
│  └────────────────────────┘  └────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

### 1.2 设计理念

- **物理存储**：HNSW 图以一张隐藏 InnoDB 表（`hlindex`）持久化，每行对应图中一个节点，支持 WAL、Crash Recovery 和事务。
- **内存缓存**：内存中维护节点缓存（`MHNSW_Share`），避免每次搜索都读磁盘，极大加速查询。
- **事务隔离**：每个写事务有独立的 `MHNSW_Trx`，提交时合并到公共缓存，Rollback 直接丢弃，保证事务安全。
- **只支持 RC 隔离级别**：`TABLE::hlindex_open()` 中强制检查 `tx_isolation == ISO_READ_COMMITTED`。

---

## 二、核心数据结构

### 2.1 `FVector`：压缩向量表示

**文件**：`sql/vidx/vidx_hnsw.cc`，第 77~256 行

```cpp
#pragma pack(push, 1)
struct FVector {
  static constexpr size_t data_header = sizeof(float);      // abs2 字段
  static constexpr size_t alloc_header = data_header + sizeof(float); // + scale

  float abs2;       // 预计算的模的平方的一半：scale² × dot(dims, dims) / 2
  float scale;      // 量化比例因子
  int16_t dims[4];  // 量化后的维度数组（实际长度由 vec_len 决定）
};
```

#### 量化压缩（Float32 → Int16）

原始向量使用 `float32`（4字节/维），AliSQL 将其量化为 `int16`（2字节/维），**内存占用减半**：

```
scale = max(|v[i]|) / 32767
dims[i] = round(v[i] / scale)
```

**关键代码**（`FVector::create()`）：
```cpp
static const FVector *create(distance_kind metric, void *mem,
                             const void *src, size_t src_len) {
    float scale = 0, *v = (float *)src;
    size_t vec_len = src_len / sizeof(float);
    // 找最大绝对值作为量化基准
    for (size_t i = 0; i < vec_len; i++)
        scale = std::max(scale, std::abs(get_float(v + i)));

    FVector *vec = align_ptr(mem);
    vec->scale = scale ? scale / 32767 : 1;
    // 量化每个维度
    for (size_t i = 0; i < vec_len; i++)
        vec->dims[i] = static_cast<int16_t>(
            std::round(get_float(v + i) / vec->scale));
    vec->postprocess(vec_len);  // 计算 abs2

    if (metric == COSINE) {
        // 余弦模式：将向量归一化为单位向量，使 abs2 = 0.5
        if (vec->abs2 > 0.0f) vec->scale /= std::sqrt(2 * vec->abs2);
        vec->abs2 = 0.5f;
    }
    return vec;
}
```

#### 距离计算公式

AliSQL 利用 `abs2` 预计算将距离计算统一为点积形式：

```
distance(a, b) = a.abs2 + b.abs2 - a.scale × b.scale × dot(a.dims, b.dims)
```

这等价于：
- **欧式距离平方**：`|a - b|² = |a|² + |b|² - 2·a·b`
- **余弦距离**：通过预归一化，使 `abs2 = 0.5`，则公式变为 `1 - cos(a, b)`

实现：
```cpp
float FVector::distance_to(const FVector *other, size_t vec_len) const {
    return abs2 + other->abs2 -
           scale * other->scale * dot_product(dims, other->dims, vec_len);
}
```

#### SIMD 加速

`dot_product()` 根据编译期检测到的 CPU 特性选择最优实现：

| 指令集 | 每次处理维度数 | 实现方式 |
|--------|------------|---------|
| AVX512 | 32 × int16 | `_mm512_madd_epi16` + `_mm512_reduce_add_ps` |
| AVX2   | 16 × int16 | `_mm256_madd_epi16` + 水平求和 |
| ARM NEON | 8 × int16 | `vmull_s16` + `vaddlvq_s32` |
| 默认标量 | 1 × int16 | 普通循环 |

---

### 2.2 `Neighborhood`：邻居集合

```cpp
struct Neighborhood {
    FVectorNode **links;  // 邻居节点指针数组
    size_t num;           // 实际邻居数量

    FVectorNode **init(FVectorNode **ptr, size_t n) {
        num = 0;
        links = ptr;
        n = MY_ALIGN(n, 8);  // 对齐到 8，方便 SIMD Bloom filter 批量处理
        bzero(ptr, n * sizeof(*ptr));
        return ptr + n;
    }
};
```

- **第 0 层**：每节点最多 `2M` 个邻居
- **其他层**：每节点最多 `M` 个邻居（论文中的经验值，M 默认为 6，范围 3~200）

---

### 2.3 `FVectorNode`：图节点

```cpp
#pragma pack(push, 1)
class FVectorNode {
  MHNSW_Share *ctx;        // 所属的图上下文

 public:
  const FVector *vec = nullptr;     // 压缩向量（懒加载）
  Neighborhood *neighbors = nullptr; // 各层邻居数组（懒加载）
  uint8_t max_layer;                 // 该节点存在的最高层号
  bool stored : 1, deleted : 1;     // 是否已写入磁盘 / 是否软删除
  // 内存布局（pack1，每字节都很重要）：
  // [FVectorNode对象] [gref: gref_len字节] [tref: tref_len字节]
  //                  [FVector对齐空间: alloc_size字节]
};
```

- `gref`：该节点在辅助表（hlindex）中的物理位置引用（InnoDB row reference）
- `tref`：该节点在主表中的物理位置引用（主键或 6字节 DB_ROW_ID）
- `vec`：懒加载，首次访问时从辅助表读取并缓存
- `deleted = true`：软删除，搜索时第 0 层跳过该节点

**内存分配**（两段式）：
```
ctx->alloc_node_internal() 分配:
[sizeof(FVectorNode)] + [gref_len] + [tref_len] + [FVector::alloc_size(vec_len)]
```

---

### 2.4 `MHNSW_Share`：公共图上下文

存储在辅助表的 `TABLE_SHARE::hlindex->hlindex_data` 上，所有连接共享。

```cpp
class MHNSW_Share {
    mysql_mutex_t cache_lock;       // 保护 node_cache 的互斥锁
    mysql_mutex_t node_lock[8];     // 分区互斥锁，保护节点的懒加载

  protected:
    std::atomic<uint> refcnt{0};
    MEM_ROOT root;                  // 所有节点内存的 arena 分配器
    Hash_set<FVectorNode> node_cache; // gref → FVectorNode* 的哈希表

  public:
    ulonglong version = 0;          // 每次提交递增，用于版本检测
    mysql_rwlock_t commit_lock;     // 读：查询持有；写：提交时独占
    size_t vec_len = 0;             // 向量维度数
    size_t byte_len = 0;            // 向量字节数
    Atomic_relaxed<double> ef_power{0.6}; // Bloom filter 大小的启发式参数
    Atomic_relaxed<float> diameter{0};    // 图直径，用于"慷慨"启发式
    FVectorNode *start = 0;         // 入口节点（最高层中的任意节点）
    const uint tref_len;            // 主表 row reference 长度
    const uint gref_len;            // 辅助表 row reference 长度
    const uint M;                   // HNSW 参数 M
    distance_kind metric;           // EUCLIDEAN 或 COSINE
};
```

**缓存淘汰**：当 `root.allocated_size() > max_cache_size` 时，整个缓存被清空重建（`reset()`）。默认 `max_cache_size = 16 MiB`，可通过 `vidx_hnsw_cache_size` 配置。

---

### 2.5 `MHNSW_Trx`：事务私有缓存

继承自 `MHNSW_Share`，每个读写事务创建一个实例，存储在 `thd->ha_data[]`（通过伪 handlerton `MHNSW_hton`）。

```cpp
class MHNSW_Trx : public MHNSW_Share {
  MDL_ticket *table_id;              // 用于在提交时找到对应的 TABLE_SHARE
  bool list_of_nodes_is_lost = false; // 溢出时置 true，提交时强制清空公共缓存
  MHNSW_Trx *next = nullptr;         // 单链表，一个 thd 可能有多个表的 trx

  // 伪 handlerton：注册 commit/rollback 回调
  static struct MHNSW_hton : public handlerton {
      static int do_commit(handlerton *, THD *, bool);
      static int do_rollback(handlerton *, THD *, bool);
      static int do_savepoint_rollback(handlerton *, THD *, void *);
  } hnsw_hton;
};
```

**事务语义**：
- 写操作（INSERT/DELETE）在 `MHNSW_Trx` 上操作，不污染公共缓存。
- Commit 时：`ctx->version++`，并使公共缓存中被修改节点的 `vec = nullptr`（强制下次重新加载）。
- Rollback 时：`MHNSW_Trx` 直接析构，修改丢弃。

---

### 2.6 辅助表（hlindex）结构

创建向量索引时，AliSQL 在 InnoDB 中创建一张隐藏表作为图的持久化存储：

**表名**：`vidx_<se_private_id>_00`（例如 `vidx_000000000000000a_00`）

**Schema**（由 `hnsw::create_dd_table()` 定义）：

| 列名 | 类型 | 说明 |
|------|------|------|
| `layer` | TINYINT | 节点所在最高层号 |
| `tref` | VARBINARY(N) | 主表行引用（主键或 DB_ROW_ID），NULL 表示已软删除 |
| `vec` | BLOB | 压缩后的向量数据（scale + int16数组） |
| `neighbors` | BLOB | 各层邻居列表（格式见下） |

**索引**：
- `tref`（唯一索引 `IDX_TREF = 1`）：通过主表引用快速查找节点（DELETE 时使用）
- `layer`（普通索引 `IDX_LAYER = 2`）：通过 `ha_index_last` 找到最高层的节点作为入口

**neighbors 字段格式**：
```
[N0][gref][gref]...[N1][gref][gref]...[Nk][gref]...
```
每层以 1 字节存邻居数量，后跟 N 个 gref（辅助表行引用，长度为 `gref_len`）。

---

## 三、HNSW 算法实现

### 3.1 搜索单层：`search_layer()`

**文件**：`sql/vidx/vidx_hnsw.cc`，第 782~872 行

这是 HNSW 的核心搜索函数，在一层图上找到与目标向量最近的 `result_size` 个节点。

```
输入：ctx, graph, target(FVector*), threshold, result_size, layer, inout(初始候选集)
输出：inout 变为 result_size 个最近节点
```

**算法流程**：

```
1. 初始化两个优先队列：
   - candidates：按距离升序，待探索节点
   - best：按距离降序（max-heap），当前最佳 result_size 个结果
   - VisitedSet：SIMD Bloom filter，记录已访问节点

2. 将 inout 中的初始节点加入 candidates 和 best

3. 主循环（双优先队列 + 贪心剪枝）：
   while candidates 非空：
     cur = candidates.pop()  // 取最近未探索节点
     if cur.distance > furthest_best AND best已满：
         break  // 剪枝：候选集中最好的都比当前结果差，无需继续
     
     for each neighbor of cur (每次处理 8 个，SIMD 批量检查):
         if Bloom filter 显示已访问：skip
         计算 neighbor 到 target 的距离
         if distance < threshold：skip（用于流式读取的去重）
         if best 未满 OR distance < furthest_best：
             加入 candidates
             若非 deleted，加入 best

4. 截断 best 到 result_size 个，写回 inout
```

**关键优化：慷慨的"最远"距离（Generosity Heuristic）**：

```cpp
static inline float generous_furthest(const Queue<Visited> &q, float maxd, float g) {
    float d0 = maxd * g / 2;   // 参考距离
    float d = q.top()->distance_to_target;  // 当前最远结果
    // sigmoid 函数平滑过渡，使阈值随搜索进度动态调整
    float sigmoid = k * x / std::sqrt(1 + (k*k - 1) * x * x);
    return d * (1 + (g - 1) / 2 * (1 - sigmoid));
}
```
`generosity = 1.1 + M/500`，确保搜索不会过早终止，在精度和速度间取得平衡。

**Bloom Filter 批量去重**：

`VisitedSet` 使用 `PatternedSimdBloomFilter`（基于 AVX2/AVX512 实现），每次批量处理 8 个节点指针：
```cpp
uint8_t res = visited.seen(links);  // 一次查询 8 个，返回 8 位掩码
if (res == 0xff) continue;          // 8 个都已访问，跳过
for (size_t i = 0; i < 8; i++) {
    if (res & (1 << i)) continue;   // 该节点已访问
    // ... 处理该节点
}
```

---

### 3.2 邻居筛选：`select_neighbors()`

**文件**：`sql/vidx/vidx_hnsw.cc`，第 699~748 行

从候选集中选出最优的 `max_neighbor_connections` 个邻居，实现 HNSW 论文中的启发式算法（HNSW+）：

```
算法：启发式邻居选择（避免聚集）
1. 将所有候选放入优先队列 pq（按距离升序）
2. while pq 非空 AND 已选邻居 < max_connections:
     node = pq.pop()  // 取最近的候选
     // 检查是否与已选邻居过近（会产生"聚集"）
     if node 到所有已选邻居的距离 > target_distance / alpha:
         选择 node 作为邻居
     else:
         放入 discarded 列表（备用）
3. 若邻居数量不足，从 discarded 中补充
```

`alpha = 1.1`（常量），稍大于1，允许轻微冗余以提高图连通性。

---

### 3.3 插入：`mhnsw_insert()`

**文件**：`sql/vidx/vidx_hnsw.cc`，第 1258~1357 行

```
1. 从字段读取向量数据，获取主表 tref
2. MHNSW_Share::acquire(&ctx, table, true) → 获取事务缓存 MHNSW_Trx
3. 若图为空（HA_ERR_END_OF_FILE）：
   - 创建 FVectorNode(layer=0)，save 到辅助表，设为 start 节点
   - 返回

4. 随机确定新节点的最高层（指数分布）：
   log = -ln(random()) / ln(M)
   target_layer = min(floor(log), max_layer + 1)

5. 创建新节点 target（含向量，目标层 = target_layer）

6. 贪心下降（从 max_layer 到 target_layer+1）：
   for layer = max_layer downto target_layer+1:
       search_layer(result_size=1)  // 找该层最近节点作为下层入口

7. 在目标层及以下建立连接（从 target_layer 到 0）：
   for layer = target_layer downto 0:
       search_layer(result_size=max_neighbors)  // 找候选邻居
       select_neighbors(candidates) → 写入 target.neighbors[layer]

8. target.save(graph) → 写入辅助表，获得 gref

9. 若 target_layer > 旧 max_layer：ctx->start = target（更新入口节点）

10. 更新反向邻居（双向图）：
    for layer = target_layer downto 0:
        update_second_degree_neighbors(target)
        // 对 target 的每个邻居 neigh：
        //   若 neigh 邻居数 < max_neighbors：直接添加 target 为邻居
        //   否则：重新 select_neighbors，保持邻居质量
        //   neigh.save(graph)
```

---

### 3.4 查询：`mhnsw_read_first()` + `mhnsw_read_next()`

**文件**：`sql/vidx/vidx_hnsw.cc`，第 1359~1458 行

查询由优化器触发（`ORDER BY VEC_DISTANCE(...) LIMIT N`），分两步：

**`mhnsw_read_first()`（初始化搜索）**：
```
1. 从 Item_func_vec_distance 获取查询向量和 LIMIT N
2. 获取公共缓存 MHNSW_Share（只读，不需要 Trx）
3. 从 start 节点开始，贪心下降到第 1 层（search_layer result=1）
4. 在第 0 层搜索 ef_search 个候选（ef_search 默认 20，可配置）：
   search_layer(result_size=max(limit, ef_search), layer=0)
5. 结果存入 Search_context，关联到 graph->context
6. 调用 mhnsw_read_next() 返回第一行
```

**`mhnsw_read_next()`（逐行返回）**：
```
1. 从 Search_context 中取下一个节点的 tref
2. table->file->ha_rnd_pos(record[0], tref) → 从主表读取完整行
3. 若所有缓存结果已返回：
   - 版本检测：若 ctx->version 变化，切换到 MHNSW_Trx 重新映射节点
   - 以新的 threshold（上次最后一个结果的距离）继续扩展搜索
4. 递归调用 mhnsw_read_next() 返回
```

流式搜索设计：通过 `threshold` 参数避免返回重复节点，实现超过 `ef_search` 限制时的按需扩展。

---

### 3.5 删除：`mhnsw_invalidate()`（软删除）

**文件**：`sql/vidx/vidx_hnsw.cc`，第 1477~1523 行

AliSQL 采用**软删除**策略，不重建图结构：

```
1. 通过 IDX_TREF 索引在辅助表中找到该节点
2. 将 tref 列设为 NULL（标记为已删除）
3. 更新辅助表行
4. 在内存缓存中将该节点的 deleted = true
```

搜索时，被删除的节点在第 0 层不会加入 `best` 集合，但仍作为中间节点参与图遍历（保证图连通性）。

---

## 四、SQL 层集成

### 4.1 向量字段类型：`Field_vector`

**文件**：`include/vidx/vidx_field.h`

继承自 `Field_varstring`，使用二进制字符集存储 float32 数组：

```cpp
class Field_vector : public Field_varstring {
  // 语法展示：/*!99999 vector(128) */ varbinary(512)
  void sql_type(String &res) const final {
    snprintf(res.ptr(), ..., "/*!99999 vector(%u) */ varbinary(%u)",
             get_dimensions(), VECTOR_PRECISION * get_dimensions());
  }
  bool is_vector() const final { return true; }
};
```

`vector(N)` 语法通过正则表达式重写为 MySQL 能理解的 `varbinary(4N)`，向量注释嵌在 MySQL 版本注释中：`/*!99999 vector(N) */`，普通 MySQL 会忽略该注释，AliSQL 会解析处理。

---

### 4.2 向量计算函数

**文件**：`include/vidx/vidx_func.h`，`sql/vidx/vidx_func.cc`

| 函数 | 类 | 功能 |
|------|----|------|
| `VEC_DISTANCE(a, b)` | `Item_func_vec_distance` | 自动选择索引配置的距离类型 |
| `VEC_DISTANCE_EUCLIDEAN(a, b)` | `Item_func_vec_distance_euclidean` | 强制欧式距离 |
| `VEC_DISTANCE_COSINE(a, b)` | `Item_func_vec_distance_cosine` | 强制余弦距离 |
| `VEC_FromText("[1,2,3]")` | `Item_func_vec_fromtext` | JSON数组字符串 → 二进制向量 |
| `VEC_ToText(vec_col)` | `Item_func_vec_totext` | 二进制向量 → JSON数组字符串 |
| `VECTOR_DIM(vec_col)` | `Item_func_vector_dim` | 获取向量维度数 |

距离计算的**全精度实现**（`vidx_func.cc`，用于不走索引的情况）：

```cpp
// 欧式距离（L2）：sqrt(Σ(a_i - b_i)²)
static double calc_distance_euclidean(float *v1, float *v2, size_t v_len) {
    double d = 0;
    for (size_t i = 0; i < v_len; i++, v1++, v2++) {
        double dist = get_float(v1) - get_float(v2);
        d += dist * dist;
    }
    return sqrt(d);
}

// 余弦距离：1 - cos(a, b) = 1 - dot(a,b) / (|a|·|b|)
static double calc_distance_cosine(float *v1, float *v2, size_t v_len) {
    double dotp = 0, abs1 = 0, abs2 = 0;
    for (size_t i = 0; i < v_len; i++, v1++, v2++) {
        float f1 = get_float(v1), f2 = get_float(v2);
        abs1 += f1 * f1; abs2 += f2 * f2; dotp += f1 * f2;
    }
    return 1 - dotp / sqrt(abs1 * abs2);
}
```

---

### 4.3 查询优化器集成：`test_if_cheaper_vector_ordering()`

**文件**：`sql/vidx/vidx_index.cc`，第 809~887 行

优化器调用此函数判断是否用向量索引代替全表扫描：

```cpp
bool test_if_cheaper_vector_ordering(JOIN_TAB *tab, ORDER *order,
                                     ha_rows limit, int *order_idx) {
    // 1. 判断是否满足条件：
    //    - ORDER BY 只有一列
    //    - 排序方向为 ASC
    //    - 排序表达式为 VEC_DISTANCE 函数
    if (!is_function_of_type(*order->item, Item_func::VECTOR_DISTANCE_FUNC))
        return false;

    // 2. 获取函数对应的向量索引编号（item->get_key()）

    // 3. 代价估算（SCAN_COST = 4）：
    //    - PRIMARY 索引扫描：若 limit > rows/4，用全表扫描更划算
    //    - 其他索引：若 limit > rows/4，用原索引更划算

    // 4. 若向量索引更优：
    //    tab->set_type(JT_INDEX_SCAN);
    //    tab->set_index(item_idx);
    //    tab->set_vec_func(item);  // 关联距离函数
    //    return true;
}
```

---

### 4.4 DML 钩子集成

**文件**：`sql/vidx/vidx_index.cc`，第 1046~1144 行

向量索引通过 `TABLE` 类的方法钩入 DML 操作：

```cpp
// INSERT 时自动调用
int TABLE::hlindexes_on_insert() {
    for (uint key = s->keys; key < s->total_keys; key++) {
        hlindex_open(key);    // 打开辅助表
        hlindex_lock(key);    // 加锁
        mhnsw_insert(this, key_info + key);  // 插入 HNSW 图
    }
}

// DELETE 时自动调用
int TABLE::hlindexes_on_delete(const uchar *buf) {
    mhnsw_invalidate(this, buf, key_info + key);  // 软删除
}

// UPDATE 时自动调用：先 invalidate 旧值，再 insert 新值
int TABLE::hlindexes_on_update() {
    mhnsw_invalidate(this, record[1], ...);  // 软删除旧行
    mhnsw_insert(this, key_info + key);       // 插入新行
}

// TRUNCATE 时自动调用
int TABLE::hlindexes_on_delete_all() {
    mhnsw_delete_all(this, key_info + key);  // 清空辅助表，重置缓存
}
```

---

## 五、DDL 管理

### 5.1 创建向量索引：`vidx::create_table()`

**文件**：`sql/vidx/vidx_index.cc`，第 461~596 行

```
1. 生成辅助表名：vidx_<se_private_id>_00
2. 申请 MDL X 锁（排他锁）
3. 检查辅助表名不与已有表冲突
4. 调用 hnsw::create_dd_table() 构建辅助表的 DD 元数据
5. 将 DD 元数据写入 data dictionary
6. 调用 ha_create_table() 在 InnoDB 中物理创建表
7. 在主表的 DD 中设置 __hlindexes__ 选项（记录索引名）
```

辅助表的 DD 属性中存储索引配置：
```
__vector_m__        = M 值（默认 6）
__vector_distance__ = 距离类型（EUCLIDEAN=0, COSINE=1）
__vector_column__   = 向量列的 fieldnr（1-based）
```

### 5.2 删除/重命名向量索引

- **删除**：`vidx::delete_table()` - ha_delete_table() 删除物理文件，dd::drop_table() 删除 DD 记录，从主表 DD 中移除 `__hlindexes__`
- **重命名**：`vidx::rename_table()` - 通过 mysql_rename_table() 重命名辅助表
- **TRUNCATE**：重命名旧辅助表（`vidx_<old_id>_00`），创建新辅助表（`vidx_<new_id>_00`）

### 5.3 打开向量索引：`TABLE::hlindex_open()`

**文件**：`sql/vidx/vidx_index.cc`，第 894~1008 行

```
1. 检查隔离级别：必须是 READ COMMITTED
2. 构建辅助表名，申请 MDL_SHARED_READ 锁
3. 若 TABLE_SHARE::hlindex 为空（首次打开）：
   - alloc_table_share() 分配 TABLE_SHARE
   - open_table_def() 从 DD 读取表结构
4. open_table_from_share() 打开辅助表（HA_OPEN_KEYFILE | HA_OPEN_RNDFILE）
5. 返回打开的 TABLE* 存入 TABLE::hlindex
```

---

## 六、并发控制

### 6.1 锁层次

```
commit_lock（rw-lock）
  └─ 写（独占）：事务提交时持有（保证公共缓存版本一致）
  └─ 读（共享）：查询获取公共缓存时持有

cache_lock（mutex）
  └─ 保护 node_cache（哈希表）和 MEM_ROOT 分配

node_lock[8]（partitioned mutex，8分区）
  └─ 基于节点指针地址哈希分区
  └─ 保护单个节点的懒加载（防止多线程重复加载同一节点）
```

### 6.2 读写并发策略

- **只读查询**：持有 `commit_lock` 读锁，使用公共 `MHNSW_Share`，多线程并发安全。
- **写操作（INSERT/UPDATE/DELETE）**：使用 `MHNSW_Trx`，事务间完全隔离，提交时才需要 `commit_lock` 写锁。
- **版本检测**：查询跨页时检查 `ctx->version`，若变化则切换到 `MHNSW_Trx`，防止读到不一致状态。

### 6.3 MyISAM vs InnoDB 差异

- **MyISAM**：通过 `TL_WRITE` 自动获得排他写锁，无需 `commit_lock`。
- **InnoDB**：多版本并发，必须使用 `commit_lock` 保证 HNSW 图的写序列化。

---

## 七、插件系统与系统变量

### 7.1 插件注册

```cpp
mysql_declare_plugin(vidx) {
    MYSQL_DAEMON_PLUGIN,    // 类型：daemon 插件
    &vidx::daemon,
    "vidx",                 // 插件名
    "AliCloud",
    "A plugin for vector index algorithm",
    PLUGIN_LICENSE_GPL,
    vidx::plugin_init,      // 初始化：注册伪 handlerton 为事务参与者
    nullptr,
    vidx::plugin_deinit,
    0x0100,                 // 版本 1.0
    nullptr,
    vidx::sys_vars,
    nullptr, 0,
} mysql_declare_plugin_end;
```

`plugin_init()` 调用 `setup_transaction_participant(vidx_plugin)` 将 `MHNSW_hton` 注册为事务参与者，从而接收 commit/rollback 通知。

### 7.2 系统变量

| 变量名 | 范围 | 类型 | 默认值 | 值域 | 说明 |
|--------|------|------|--------|------|------|
| `vidx_disabled` | GLOBAL | BOOL | ON | - | 禁用向量功能（默认禁用，需显式开启） |
| `vidx_default_distance` | SESSION | ENUM | EUCLIDEAN | EUCLIDEAN/COSINE | 默认距离类型 |
| `vidx_hnsw_default_m` | SESSION | UINT | 6 | [3, 200] | HNSW 参数 M |
| `vidx_hnsw_ef_search` | SESSION | UINT | 20 | [1, 10000] | 搜索候选集大小 |
| `vidx_hnsw_cache_size` | GLOBAL | ULONGLONG | 16 MiB | [1MiB, MAX] | 单索引最大缓存 |

### 7.3 索引参数

```sql
CREATE VECTOR INDEX vidx ON t(vec_col) M=6 DISTANCE=COSINE;
```

- `M`（`vector_m`）：每节点最大邻居数，第0层为 `2M`，其他层为 `M`
- `DISTANCE`（`vector_distance`）：构建时确定，搜索时不能更改
- `ef_search`：通过 session 变量动态调整，不影响索引结构

---

## 八、关键设计权衡与限制

### 8.1 设计权衡

| 设计选择 | 原因 |
|---------|------|
| 软删除（不重建图） | 避免高代价的图重构，接受轻微精度下降 |
| Int16 量化 | 内存减半，SIMD 效率翻倍，精度损失可接受 |
| abs2 预计算 | 将距离计算转化为纯点积，充分利用 SIMD `madd` 指令 |
| 辅助 InnoDB 表 | 获得免费的 WAL、Crash Recovery、事务支持 |
| MEM_ROOT arena | 避免大量 malloc/free 碎片，批量分配和释放 |
| Bloom filter 批量去重 | 8个节点一次 SIMD 查询，显著减少 hash 开销 |

### 8.2 当前限制

1. **只支持 READ COMMITTED 隔离级别**（源码中硬编码检查）
2. **只支持 InnoDB 引擎**（`assert(dd_table->engine() == "InnoDB")`）
3. **每张表只支持一个向量索引**（`vidx_num = 0` 固定后缀）
4. **向量列不能为 NULL**（`assert(res)` 在插入时检查）
5. **ALTER TABLE 限制**：向量索引相关 DDL 不支持 INPLACE 算法，不能与其他 ALTER 合并
6. **向量索引不能设为 INVISIBLE**
7. **最大维度 16383**（`MAX_DIMENSIONS = 16383`）
8. **ef_search 最大 10000**（`max_ef = 10000`）

---

## 九、使用示例

```sql
-- 启用向量功能
SET GLOBAL vidx_disabled = OFF;

-- 创建带向量列和向量索引的表
CREATE TABLE items (
    id INT PRIMARY KEY,
    name VARCHAR(255),
    embedding VECTOR(128)    -- 128维向量
);

CREATE VECTOR INDEX vidx_emb ON items(embedding) M=8 DISTANCE=COSINE;

-- 插入数据（使用 VEC_FromText 转换）
INSERT INTO items VALUES (1, 'item1',
    VEC_FromText('[0.1, 0.2, 0.3, ...]'));

-- ANN 查询（自动使用向量索引）
SELECT id, name,
    VEC_DISTANCE(embedding, VEC_FromText('[0.1, 0.2, ...]')) AS dist
FROM items
ORDER BY dist
LIMIT 10;

-- 强制使用向量索引
SELECT * FROM items FORCE INDEX (vidx_emb)
ORDER BY VEC_DISTANCE(embedding, VEC_FromText('[...]'))
LIMIT 10;

-- 查看向量值
SELECT id, VEC_ToText(embedding) AS vec_str FROM items LIMIT 5;

-- 调整搜索精度（ef_search 越大，精度越高，速度越慢）
SET vidx_hnsw_ef_search = 100;
```

---

## 十、源码文件索引

| 文件 | 行数 | 职责 |
|------|------|------|
| `sql/vidx/vidx_hnsw.cc` | 1551 | HNSW 核心算法：FVector, FVectorNode, MHNSW_Share, MHNSW_Trx, search_layer, mhnsw_insert, mhnsw_read_first/next |
| `sql/vidx/vidx_index.cc` | 1162 | 插件注册、DDL管理、DML钩子、优化器集成、TABLE 方法实现 |
| `sql/vidx/vidx_func.cc` | ~300 | 距离函数实现、VEC_FromText/ToText、向量列 DDL 改写 |
| `sql/vidx/vidx_field.cc` | ~100 | Field_vector 存储相关实现 |
| `include/vidx/vidx_hnsw.h` | 56 | HNSW 公共接口声明 |
| `include/vidx/vidx_common.h` | 70 | 公共常量（维度上限、M范围、距离类型名） |
| `include/vidx/vidx_index.h` | 143 | DDL/DML 管理接口声明 |
| `include/vidx/vidx_func.h` | 130 | 向量 SQL 函数类定义 |
| `include/vidx/vidx_field.h` | 75 | Field_vector 类定义 |
| `include/vidx/bloom_filters.h` | ~300 | SIMD 加速的 Bloom filter（基于 AVX2/AVX512） |
| `include/vidx/sql_queue.h` | - | 优先队列（min-heap/max-heap）用于搜索 |
| `include/vidx/sql_hset.h` | - | 哈希集合（node_cache 使用） |
| `include/vidx/SIMD.h` | - | SIMD 编译期特性检测与宏定义 |
