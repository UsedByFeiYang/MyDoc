# AliSQL 向量索引实现分析

本文档详细分析了 AliSQL 项目中向量搜索的实现，包括向量索引创建、数据操作、SIMD优化、缓存机制、优化器选择策略以及完整的词法/语法解析流程。

---

## 目录

1. [向量索引创建过程](#1-向量索引创建过程)
2. [Insert/Update/Select 过程](#2-insertupdateselect-过程)
3. [SIMD 优化实现](#3-simd-优化实现)
4. [两个缓存的作用](#4-两个缓存的作用)
5. [优化器如何选择向量索引](#5-优化器如何选择向量索引)
6. [COSINE_DISTANCE 与 EUCLIDEAN_DISTANCE 完整调用过程](#6-cosine_distance-与-euclidean_distance-完整调用过程)

---

## 1. 向量索引创建过程

### 1.1 SQL 语法

```sql
CREATE VECTOR INDEX idx_embedding ON embeddings(embedding)
    [WITH (M=16, DISTANCE=COSINE)];
```

### 1.2 创建流程

```
CREATE VECTOR INDEX 语句处理流程:
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  1. SQL 解析                                                        │
│     sql/sql_yacc.yy:3707-3710                                      │
│     └─> PT_create_index_stmt(KEYTYPE_VECTOR, ...)                  │
│                                                                     │
│  2. 执行创建语句                                                     │
│     sql/sql_cmd_ddl_table.cc                                       │
│     └─> PT_create_index_stmt::execute()                            │
│         └─> create_index()                                         │
│             └─> ha_create_index()                                  │
│                                                                     │
│  3. 存储引擎处理                                                     │
│     sql/handler.cc                                                 │
│     └─> handler::create_index()                                    │
│         └─> ha_innodb::create_index()                              │
│                                                                     │
│  4. 向量索引初始化                                                   │
│     sql/vidx/vidx_index.cc                                         │
│     └─> MHNSW_Share::init()                                        │
│         ├─> 解析索引参数 (M, DISTANCE)                              │
│         ├─> 初始化 HNSW 图结构                                      │
│         ├─> 创建图存储表 (hidden table)                             │
│         └─> 设置向量维度和距离类型                                   │
│                                                                     │
│  5. 元数据持久化                                                     │
│     storage/innobase/handler/ha_innodb.cc                          │
│     └─> 将索引信息写入 DD (Data Dictionary)                         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 关键数据结构

```cpp
// sql/vidx/vidx_hnsw.cc
class MHNSW_Share {
  // HNSW 算法参数
  uint M;                 // 每层最大连接数
  uint efConstruction;    // 构建时的搜索范围
  
  // 距离类型
  distance_kind metric;   // EUCLIDEAN 或 COSINE
  
  // 图结构
  FVectorNode *root;      // 入口节点
  hash_table node_cache;  // gref -> FVectorNode 映射
  
  // 缓存控制
  size_t max_cache_size;  // 最大缓存大小
  mysql_mutex_t cache_lock;
};
```

### 1.4 隐藏表结构

向量索引创建时会生成一个隐藏表来存储 HNSW 图数据：

```sql
-- 隐藏表命名规则: <table_name>_vidx_<index_name>
CREATE TABLE embeddings_vidx_idx_embedding (
    gref BINARY(8),           -- 主表记录引用 (row_id)
    hlindex INT,              -- 节点在图中的层级索引
    layer TINYINT,            -- 节点所在层
    neighbors BLOB,           -- 邻居节点列表
    vec BLOB                  -- 量化后的向量数据
) ENGINE=InnoDB;
```

---

## 2. Insert/Update/Select 过程

### 2.1 INSERT 过程

```
INSERT INTO embeddings(id, content, embedding) 
VALUES (1, 'text', '[0.1, 0.2, ...]');
```

```
INSERT 处理流程:
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  1. 主表写入                                                        │
│     ha_innodb::write_row()                                         │
│     └─> 写入 InnoDB 主表                                            │
│     └─> 获取 row_id (tref)                                         │
│                                                                     │
│  2. 向量索引更新                                                     │
│     handler::secondary_engine_write_row()                          │
│     └─> mhnsw_write_row()                                          │
│                                                                     │
│  3. 向量量化                                                        │
│     FVector::create(metric, mem, vec, len)                         │
│     ├─> 计算缩放因子 scale                                          │
│     ├─> float -> int16_t 量化                                      │
│     ├─> 计算 abs2 (平方和的一半)                                    │
│     └─> COSINE: 归一化 abs2 = 0.5                                  │
│                                                                     │
│  4. HNSW 图插入                                                     │
│     MHNSW_Trx::insert()                                            │
│     ├─> 确定节点层级 (随机算法)                                      │
│     ├─> search_layer() 找到最近邻居                                 │
│     ├─> connect_neighbors() 建立连接                                │
│     └─> 更新入口节点 (如果需要)                                      │
│                                                                     │
│  5. 图数据持久化                                                     │
│     FVectorNode::save(graph_table)                                 │
│     └─> 写入隐藏表                                                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 UPDATE 过程

```
UPDATE embeddings SET embedding = '[0.3, 0.4, ...]' WHERE id = 1;
```

```
UPDATE 处理流程:
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  1. 定位旧记录                                                      │
│     ha_innodb::index_read()                                        │
│     └─> 获取旧 tref                                                 │
│                                                                     │
│  2. 删除旧向量节点                                                   │
│     mhnsw_delete_row(old_tref)                                     │
│     ├─> 从 node_cache 找到旧节点                                    │
│     ├─> 标记为 deleted                                              │
│     └─> 断开邻居连接                                                │
│                                                                     │
│  3. 更新主表                                                        │
│     ha_innodb::update_row()                                        │
│     └─> 写入新向量值                                                │
│                                                                     │
│  4. 插入新向量节点                                                   │
│     mhnsw_write_row(new_tref, new_vec)                             │
│     └─> 同 INSERT 流程                                              │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.3 SELECT 过程 (向量搜索)

```sql
SELECT id, content, 
       COSINE_DISTANCE(embedding, '[0.1, 0.2, ...]') as distance
FROM embeddings
ORDER BY distance
LIMIT 10;
```

```
SELECT 处理流程:
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  1. 优化器分析                                                      │
│     test_if_cheaper_vector_ordering()                              │
│     ├─> 检测 ORDER BY VEC_DISTANCE                                 │
│     ├─> 找到向量索引                                                │
│     ├─> 比较成本，选择向量索引                                      │
│     └─> 设置 tab->set_index(idx)                                   │
│                                                                     │
│  2. 向量索引扫描                                                    │
│     mhnsw_read_first()                                             │
│     ├─> 解析查询向量                                                │
│     ├─> FVector::create() 量化查询向量                              │
│     └─> search_layer() HNSW 图搜索                                  │
│                                                                     │
│  3. HNSW 图搜索算法                                                 │
│     search_layer(entry_point, target, ef)                          │
│     ├─> 初始化候选队列和结果集合                                    │
│     ├─> 从入口节点开始遍历                                          │
│     ├─> 计算距离: node->distance_to(target)                        │
│     │   └─> SIMD 加速点积计算                                      │
│     ├─> 使用 Bloom Filter 过滤已访问节点                            │
│     └─> 返回最近的 ef 个候选                                        │
│                                                                     │
│  4. 结果返回                                                        │
│     mhnsw_read_next()                                              │
│     ├─> 从结果队列取下一个节点                                      │
│     ├─> 通过 tref 定位主表记录                                      │
│     └─> 返回完整行数据                                              │
│                                                                     │
│  5. 距离计算                                                        │
│     Item_func_vec_distance_cosine::val_real()                      │
│     └─> calc_distance_cosine(v1, v2, dims)                         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. SIMD 优化实现

### 3.1 量化策略

AliSQL 使用 int16_t 量化来减少内存占用并利用 SIMD 指令：

```cpp
// sql/vidx/vidx_hnsw.cc
struct FVector {
  float abs2, scale;    // abs2: 向量平方和的一半, scale: 量化缩放因子
  int16_t dims[4];      // 量化后的向量维度

  static const FVector *create(distance_kind metric, void *mem, 
                               const void *src, size_t src_len) {
    float scale = 0, *v = (float *)src;
    size_t vec_len = src_len / sizeof(float);
    
    // 1. 计算最大绝对值
    for (size_t i = 0; i < vec_len; i++)
      scale = std::max(scale, std::abs(get_float(v + i)));

    FVector *vec = align_ptr(mem);
    vec->scale = scale ? scale / 32767 : 1;  // 缩放到 int16 范围
    
    // 2. 量化: float -> int16_t
    for (size_t i = 0; i < vec_len; i++)
      vec->dims[i] = static_cast<int16_t>(
          std::round(get_float(v + i) / vec->scale));

    // 3. COSINE 归一化
    if (metric == COSINE) {
      if (vec->abs2 > 0.0f) 
        vec->scale /= std::sqrt(2 * vec->abs2);
      vec->abs2 = 0.5f;  // 归一化后固定为 0.5
    }
    return vec;
  }
};
```

**量化原理**：
- 原始向量: float (4字节/维度)
- 量化向量: int16_t (2字节/维度)
- 内存节省: 50%
- SIMD 加速: 一次处理更多数据

### 3.2 AVX2 实现 (x86)

```cpp
// sql/vidx/vidx_hnsw.cc
#ifdef AVX2_IMPLEMENTATION
static float dot_product(const int16_t *v1, const int16_t *v2, size_t len) {
  typedef float v8f __attribute__((vector_size(32)));  // 8个float的向量类型
  union {
    v8f v;
    __m256 i;
  } tmp;
  __m256i *p1 = (__m256i *)v1;
  __m256i *p2 = (__m256i *)v2;
  v8f d = {0};
  
  // 每次处理16个int16_t（256位）
  for (size_t i = 0; i < (len + 15) / 16; p1++, p2++, i++) {
    // _mm256_madd_epi16: 同时做乘法和加法
    // 16个int16相乘，得到8个int32的和
    tmp.i = _mm256_cvtepi32_ps(_mm256_madd_epi16(*p1, *p2));
    d += tmp.v;
  }
  // 水平求和8个float
  return d[0] + d[1] + d[2] + d[3] + d[4] + d[5] + d[6] + d[7];
}
#endif
```

### 3.3 AVX512 实现

```cpp
#ifdef AVX512_IMPLEMENTATION
static float dot_product(const int16_t *v1, const int16_t *v2, size_t len) {
  __m512i *p1 = (__m512i *)v1;
  __m512i *p2 = (__m512i *)v2;
  __m512 d = _mm512_setzero_ps();
  
  // 每次处理32个int16_t（512位）
  for (size_t i = 0; i < (len + 31) / 32; p1++, p2++, i++)
    d = _mm512_add_ps(d, _mm512_cvtepi32_ps(_mm512_madd_epi16(*p1, *p2)));
  
  return _mm512_reduce_add_ps(d);  // AVX512的水平求和指令
}
#endif
```

### 3.4 NEON 实现 (ARM)

```cpp
#ifdef NEON_IMPLEMENTATION
static float dot_product(const int16_t *v1, const int16_t *v2, size_t len) {
  int64_t d = 0;
  for (size_t i = 0; i < (len + 7) / 8; i++) {
    int16x8_t p1 = vld1q_s16(v1);  // 加载8个int16_t
    int16x8_t p2 = vld1q_s16(v2);
    // vmull_s16: 低4个元素乘法
    // vmull_high_s16: 高4个元素乘法
    // vaddlvq_s32: 水平求和4个int32
    d += vaddlvq_s32(vmull_s16(vget_low_s16(p1), vget_low_s16(p2))) +
         vaddlvq_s32(vmull_high_s16(p1, p2));
    v1 += 8;
    v2 += 8;
  }
  return static_cast<float>(d);
}
#endif
```

### 3.5 SIMD 选择机制

```cpp
// sql/vidx/vidx_hnsw.cc
// 编译时根据 CPU 支持选择最优实现
#if defined(__AVX512F__)
  #define AVX512_IMPLEMENTATION
#elif defined(__AVX2__)
  #define AVX2_IMPLEMENTATION  
#elif defined(__ARM_NEON)
  #define NEON_IMPLEMENTATION
#else
  #define DEFAULT_IMPLEMENTATION
#endif
```

### 3.6 性能对比

| SIMD 类型 | 每次处理元素数 | 相对性能 |
|-----------|---------------|----------|
| DEFAULT (无SIMD) | 1 | 1x |
| AVX2 | 16 | ~8x |
| AVX512 | 32 | ~16x |
| NEON | 8 | ~4x |

---

## 4. 两个缓存的作用

### 4.1 MHNSW_Share (共享缓存)

```cpp
// sql/vidx/vidx_hnsw.cc
class MHNSW_Share {
  // 存储在 TABLE_SHARE::mem_root
  // 生命周期：与表定义相同
  
  FVectorNode *root;           // 入口节点（最高层节点）
  hash_table node_cache;       // gref -> FVectorNode 映射
  mysql_mutex_t cache_lock;    // 缓存锁
  size_t max_cache_size;       // 最大缓存大小
  uint ref_count;              // 引用计数
};
```

**作用**：
1. **存储完整的 HNSW 图结构**：所有节点和连接关系
2. **跨线程共享**：多个查询线程可以并发访问
3. **内存管理**：限制缓存大小，防止内存溢出
4. **持久化协调**：脏数据刷盘时使用引用计数保护

### 4.2 MHNSW_Trx (事务缓存)

```cpp
// sql/vidx/vidx_hnsw.cc
class MHNSW_Trx {
  // 存储在 THD::mem_root
  // 生命周期：单个事务
  
  MHNSW_Share *share;          // 引用共享缓存
  hash_table trx_cache;        // 本事务修改的节点
  mem_root deque;              // 待刷新节点队列
  bool modified;               // 是否有修改
};
```

**作用**：
1. **事务隔离**：本事务修改的节点不立即写入共享缓存
2. **批量刷新**：事务提交时统一刷新到共享缓存和磁盘
3. **回滚支持**：事务失败时可以丢弃 trx_cache
4. **减少锁竞争**：事务期间不需要频繁获取 cache_lock

### 4.3 缓存交互流程

```
缓存交互流程示例:
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  事务开始:                                                          │
│  THD->mem_root 上创建 MHNSW_Trx                                    │
│  trx_cache = 空                                                    │
│                                                                     │
│  INSERT 操作:                                                       │
│  1. 创建新 FVectorNode (在 trx_cache)                              │
│  2. 搜索邻居 (先查 trx_cache，再查 share->node_cache)              │
│  3. 建立连接 (只修改 trx_cache 中的节点)                            │
│                                                                     │
│  SELECT 操作:                                                       │
│  1. 搜索时先查 trx_cache (看到本事务的修改)                         │
│  2. 再查 share->node_cache (看到已提交的数据)                       │
│  3. 合并结果                                                        │
│                                                                     │
│  事务提交:                                                          │
│  1. 获取 share->cache_lock                                         │
│  2. 将 trx_cache 合并到 share->node_cache                          │
│  3. 刷新脏节点到磁盘                                                │
│  4. 释放 cache_lock                                                │
│  5. 释放 trx_cache                                                 │
│                                                                     │
│  事务回滚:                                                          │
│  1. 直接丢弃 trx_cache                                             │
│  2. 不影响 share->node_cache                                       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 5. 优化器如何选择向量索引

### 5.1 检测条件

```cpp
// sql/sql_optimizer.cc
bool test_if_cheaper_vector_ordering(QEP_TAB *tab, ORDER *order, 
                                     ha_rows limit) {
  // 1. 检查 ORDER BY 是否是 VEC_DISTANCE 函数
  Item *order_item = order->item[0];
  if (!check_item_func_vec_distance(order_item))
    return false;
  
  // 2. 检查是否有向量索引
  Item_func_vec_distance *vec_func = (Item_func_vec_distance *)order_item;
  int key_no = vec_func->get_key();  // 获取向量索引编号
  if (key_no < 0)
    return false;
  
  // 3. 检查距离类型是否匹配
  KEY *key_info = tab->table->key_info + key_no;
  if (key_info->vector_distance != vec_func->kind)
    return false;
  
  // 4. 检查是否是 ASC 排序（向量搜索需要升序）
  if (!order->direction.is_ascending())
    return false;
  
  // 5. 成本估算
  // 向量索引成本 ≈ log(N) 的图搜索成本
  // 全表扫描成本 ≈ N * 距离计算成本
  double vector_cost = estimate_hnsw_search_cost(tab, limit);
  double scan_cost = estimate_full_scan_cost(tab);
  
  return vector_cost < scan_cost;
}
```

### 5.2 选择流程

```
优化器选择向量索引流程:
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  SQL: SELECT * FROM t ORDER BY VEC_DISTANCE(vec, '[...]') LIMIT 10 │
│                                                                     │
│  1. 解析 ORDER BY                                                   │
│     ├─> order->item[0] = Item_func_vec_distance                    │
│     └─> order->direction = ASC                                     │
│                                                                     │
│  2. get_best_ordering_index()                                      │
│     └─> test_if_cheaper_vector_ordering()                          │
│                                                                     │
│  3. 检查向量索引                                                    │
│     ├─> vec_func->get_key()                                        │
│     │   ├─> 检查 args[0] 是否是向量列                               │
│     │   ├─> 检查 args[1] 是否是常量                                 │
│     │   └─> 查找该列上的向量索引                                    │
│     │       for (i = keys; i < total_keys; i++)                    │
│     │         if (key_info[i].flags & HA_VECTOR)                   │
│     │           if (field->key_start.is_set(i))                    │
│     │             return i;                                        │
│     │                                                              │
│     ├─> 检查距离类型匹配                                            │
│     │   key_info->vector_distance == vec_func->kind                │
│     │                                                              │
│     └─> 成本比较                                                    │
│         vector_cost = HNSW 搜索成本                                │
│         scan_cost = 全表扫描 + 排序成本                            │
│                                                                     │
│  4. 设置执行计划                                                    │
│     ├─> tab->set_index(key_no)                                     │
│     ├─> tab->set_vec_func(vec_func)                                │
│     ├─> vec_func->set_limit(limit)                                 │
│     └─> 使用向量索引扫描                                            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.3 成本估算公式

```
向量索引成本:
  cost = M * log(N) * distance_compute_cost
  其中:
    M = 每层最大连接数
    N = 总记录数
    distance_compute_cost = 单次距离计算成本

全表扫描成本:
  cost = N * distance_compute_cost + sort_cost(N, limit)
```

---

## 6. COSINE_DISTANCE 与 EUCLIDEAN_DISTANCE 完整调用过程

### 6.1 示例 SQL

```sql
-- 创建向量表
CREATE TABLE embeddings (
    id INT PRIMARY KEY,
    content TEXT,
    embedding VECTOR(768)
) ENGINE=InnoDB;

-- 创建向量索引
CREATE VECTOR INDEX idx_embedding ON embeddings(embedding);

-- 使用余弦距离查询
SELECT id, content,
       COSINE_DISTANCE(embedding, '[0.1, 0.2, ...]') as distance
FROM embeddings
ORDER BY distance
LIMIT 10;
```

### 6.2 词法解析

**关键字定义** (`sql/lex.h`):
```cpp
{SYM("VECTOR", VECTOR_SYM)},
{SYM("DISTANCE", DISTANCE_SYM)},
{SYM("EUCLIDEAN", EUCLIDEAN_SYM)},
{SYM("COSINE", COSINE_SYM)},
```

**词法解析流程**:
```
输入: "COSINE_DISTANCE(embedding, '[0.1, 0.2, ...]')"

Token 序列:
[IDENT("COSINE_DISTANCE"), '(' , IDENT("embedding"), ',', TEXT_STRING, ')']
```

### 6.3 语法解析

**语法规则** (`sql/sql_yacc.yy`):
```cpp
// 函数调用规则
function_call_generic:
    | ident '(' opt_udf_expr_list ')'
        { $$= NEW_PTN PTI_function_call_generic_ident_sys(@1, $1, $3); }

// 向量索引选项
vector_distance_name:
    | EUCLIDEAN_SYM { $$= 0; }
    | COSINE_SYM { $$= 1; }
```

### 6.4 函数注册

**函数注册表** (`sql/item_create.cc`):
```cpp
{"VEC_DISTANCE", SQL_FN(vidx::Item_func_vec_distance, 2)},
{"VEC_DISTANCE_EUCLIDEAN", SQL_FN(vidx::Item_func_vec_distance_euclidean, 2)},
{"VEC_DISTANCE_COSINE", SQL_FN(vidx::Item_func_vec_distance_cosine, 2)},
{"VEC_FROMTEXT", SQL_FN(vidx::Item_func_vec_fromtext, 1)},
{"VEC_TOTEXT", SQL_FN(vidx::Item_func_vec_totext, 1)},
{"VECTOR_DIM", SQL_FN(vidx::Item_func_vector_dim, 1)},
```

### 6.5 函数类定义

**类继承结构** (`include/vidx/vidx_func.h`):
```cpp
namespace vidx {

enum distance_kind { EUCLIDEAN, COSINE, AUTO };

class Item_func_vec_distance : public Item_real_func {
 public:
  Item_func_vec_distance(const POS &pos, Item *a, Item *b, distance_kind c)
      : Item_real_func(pos, a, b), kind(c) {}

  bool resolve_type(THD *thd) override;
  double val_real() override;
  enum Functype functype() const override { return VECTOR_DISTANCE_FUNC; }

 private:
  distance_kind kind;
  double (*calc_distance_func)(float *v1, float *v2, size_t v_len);
};

class Item_func_vec_distance_euclidean final : public Item_func_vec_distance {
 public:
  Item_func_vec_distance_euclidean(const POS &pos, Item *a, Item *b)
      : Item_func_vec_distance(pos, a, b, distance_kind::EUCLIDEAN) {}
};

class Item_func_vec_distance_cosine final : public Item_func_vec_distance {
 public:
  Item_func_vec_distance_cosine(const POS &pos, Item *a, Item *b)
      : Item_func_vec_distance(pos, a, b, distance_kind::COSINE) {}
};

}  // namespace vidx
```

### 6.6 距离计算实现

**欧几里得距离** (`sql/vidx/vidx_func.cc`):
```cpp
static double calc_distance_euclidean(float *v1, float *v2, size_t v_len) {
  double d = 0;
  for (size_t i = 0; i < v_len; i++, v1++, v2++) {
    double dist = get_float(v1) - get_float(v2);
    d += dist * dist;
  }
  return sqrt(d);
}
```

**余弦距离**:
```cpp
static double calc_distance_cosine(float *v1, float *v2, size_t v_len) {
  double dotp = 0, abs1 = 0, abs2 = 0;
  for (size_t i = 0; i < v_len; i++, v1++, v2++) {
    float f1 = get_float(v1), f2 = get_float(v2);
    abs1 += f1 * f1;
    abs2 += f2 * f2;
    dotp += f1 * f2;
  }
  return 1 - dotp / sqrt(abs1 * abs2);
}
```

### 6.7 完整调用流程图

```
用户输入: SELECT id, COSINE_DISTANCE(embedding, '[0.1,0.2,...]') 
         FROM embeddings ORDER BY distance LIMIT 10;

┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                              完整调用流程                                                    │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  1. 词法解析 (sql/sql_scanner.ll + sql/lex.h)                                               │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐   │
│  │ 输出 Token 序列:                                                                    │   │
│  │ [IDENT("COSINE_DISTANCE"), '(' , IDENT("embedding"), ',', TEXT_STRING("..."), ')'] │   │
│  └─────────────────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                                      │
│                                      ▼                                                      │
│  2. 语法解析 (sql/sql_yacc.yy)                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐   │
│  │ 创建解析树节点:                                                                      │   │
│  │ PTI_function_call_generic_ident_sys(                                                │   │
│  │     ident = {"COSINE_DISTANCE"},                                                    │   │
│  │     args = [PTI_simple_ident("embedding"), PTI_text_string("[...]")]               │   │
│  │ )                                                                                   │   │
│  └─────────────────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                                      │
│                                      ▼                                                      │
│  3. 解析树处理 (sql/parse_tree_items.cc)                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐   │
│  │ PTI_function_call_generic_ident_sys::itemize()                                      │   │
│  │ ├─> find_native_function_builder("COSINE_DISTANCE")                                │   │
│  │ │   └─> 找到: SQL_FN(vidx::Item_func_vec_distance_cosine, 2)                       │   │
│  │ ├─> builder->create_func(thd, ident, args)                                         │   │
│  │ │   └─> new Item_func_vec_distance_cosine(POS(), args[0], args[1])                 │   │
│  │ └─> 返回 Item_func_vec_distance_cosine 对象                                         │   │
│  └─────────────────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                                      │
│                                      ▼                                                      │
│  4. 类型解析 (sql/vidx/vidx_func.cc)                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐   │
│  │ Item_func_vec_distance::resolve_type(thd)                                           │   │
│  │ kind = COSINE                                                                        │   │
│  │ calc_distance_func = calc_distance_cosine                                           │   │
│  └─────────────────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                                      │
│                                      ▼                                                      │
│  5. 优化器处理 (sql/sql_optimizer.cc)                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐   │
│  │ test_if_cheaper_vector_ordering()                                                   │   │
│  │ ├─> 找到向量索引 idx_embedding                                                      │   │
│  │ ├─> 设置 tab->set_index(idx_embedding)                                             │   │
│  │ ├─> 设置 tab->set_vec_func(Item_func_vec_distance_cosine)                          │   │
│  │ └─> 设置 limit = 10                                                                │   │
│  └─────────────────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                                      │
│                                      ▼                                                      │
│  6. 执行阶段 (sql/vidx/vidx_hnsw.cc)                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐   │
│  │ mhnsw_read_first()                                                                  │   │
│  │ ├─> FVector::create(COSINE, mem, vec, 768)                                         │   │
│  │ │   ├─> 量化: float -> int16_t                                                     │   │
│  │ │   └─> 归一化: abs2 = 0.5                                                         │   │
│  │ ├─> search_layer(entry_point, target, ef=10)                                       │   │
│  │ │   ├─> node->distance_to(target)                                                  │   │
│  │ │   │   └─> return 0.5 + 0.5 - scale*target->scale*dot_product()                  │   │
│  │ │   │   └─> = 1 - dot_product (归一化后的余弦距离)                                  │   │
│  │ │   ├─> SIMD 加速点积计算                                                          │   │
│  │ │   └─> 返回最近的 10 个节点                                                        │   │
│  │ └─> 返回第一个结果行                                                                 │   │
│  └─────────────────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                                      │
│                                      ▼                                                      │
│  7. 距离计算 (sql/vidx/vidx_func.cc)                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐   │
│  │ Item_func_vec_distance_cosine::val_real()                                           │   │
│  │ ├─> args[0]->val_str() -> 获取 embedding 列值                                       │   │
│  │ ├─> args[1]->val_str() -> 获取查询向量                                              │   │
│  │ ├─> calc_distance_cosine(v1, v2, 768)                                              │   │
│  │ │   return 1 - dotp / sqrt(abs1 * abs2)                                            │   │
│  │ └─> 返回距离值 (DOUBLE)                                                             │   │
│  └─────────────────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                                      │
│                                      ▼                                                      │
│  8. 返回结果                                                                                │
│  ┌─────────────────────────────────────────────────────────────────────────────────────┐   │
│  │ 返回 10 行结果:                                                                      │   │
│  │ id | content | distance                                                            │   │
│  │ 1  | "..."   | 0.023                                                               │   │
│  │ 2  | "..."   | 0.045                                                               │   │
│  │ ...                                                                                 │   │
│  └─────────────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 关键文件总结

| 文件 | 作用 |
|------|------|
| `sql/lex.h` | 词法关键字定义 |
| `sql/sql_yacc.yy` | 语法规则定义 |
| `sql/item_create.cc` | 函数注册表 |
| `sql/parse_tree_items.cc` | 解析树处理 |
| `include/vidx/vidx_func.h` | 函数类定义 |
| `sql/vidx/vidx_func.cc` | 函数实现 |
| `sql/vidx/vidx_hnsw.cc` | HNSW 索引实现 |
| `sql/vidx/vidx_index.cc` | 向量索引接口 |
| `include/vidx/vidx_field.h` | 向量字段定义 |
| `include/vidx/SIMD.h` | SIMD 优化实现 |

---

## 参考资料

- [AliSQL GitHub](https://github.com/alibaba/AliSQL)
- [HNSW 算法论文](https://arxiv.org/abs/1603.09320)
- [MySQL 8.0 官方文档](https://dev.mysql.com/doc/refman/8.0/en/)