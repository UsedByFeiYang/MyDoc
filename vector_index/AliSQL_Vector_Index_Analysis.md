# AliSQL 向量索引实现深度分析

## 目录
1. [概述](#概述)
2. [整体架构](#整体架构)
3. [核心数据结构](#核心数据结构)
4. [向量字段类型实现](#向量字段类型实现)
5. [HNSW 算法实现](#hnsw-算法实现)
6. [距离计算与 SIMD 优化](#距离计算与-simd-优化)
7. [SQL 函数实现](#sql-函数实现)
8. [辅助表结构](#辅助表结构)
9. [DDL 处理流程](#ddl-处理流程)
10. [DML 操作流程](#dml-操作流程)
11. [查询优化与执行](#查询优化与执行)
12. [在社区版 MySQL 上重新实现的步骤](#在社区版-mysql-上重新实现的步骤)

---

## 概述

AliSQL 的向量索引功能基于 **HNSW (Hierarchical Navigable Small World)** 算法实现，这是一种高效的近似最近邻搜索算法。该实现支持：

- **向量数据类型**: `vector(N)`，N 为维度数（最大 16383）
- **距离度量**: 欧几里得距离 (EUCLIDEAN) 和余弦距离 (COSINE)
- **SQL 函数**: `VEC_DISTANCE`, `VEC_FromText`, `VEC_ToText`, `vector_dim`
- **索引创建**: 通过 `CREATE INDEX` 语法创建向量索引

### 核心文件结构

```
include/vidx/
├── vidx_common.h      # 常量定义和通用工具
├── vidx_field.h       # 向量字段类型定义
├── vidx_func.h        # 向量相关 SQL 函数定义
├── vidx_index.h       # 向量索引管理接口
├── vidx_hnsw.h        # HNSW 算法接口
├── SIMD.h             # SIMD 优化支持
├── bloom_filters.h    # Bloom 过滤器
├── hash.h             # 哈希表
├── my_atomic_wrapper.h # 原子操作包装
├── sql_hset.h         # SQL 哈希集合
└── sql_queue.h        # SQL 队列

sql/vidx/
├── vidx_field.cc      # 向量字段实现
├── vidx_func.cc       # 向量函数实现
├── vidx_index.cc      # 向量索引管理实现
└── vidx_hnsw.cc       # HNSW 算法核心实现
```

---

## 整体架构

AliSQL 向量索引采用 **辅助表 (Auxiliary Table)** 架构：

```
┌─────────────────────────────────────────────────────────────┐
│                      用户主表 (Base Table)                    │
│  ┌─────────┬─────────┬─────────────────────────────────────┐│
│  │   id    │  name   │        embedding (vector(128))       ││
│  │  INT    │ VARCHAR │        VARBINARY(512)                ││
│  └─────────┴─────────┴─────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
                              │
                              │ 关联 (通过 tref)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              向量索引辅助表 (HNSW Graph Table)                │
│  ┌─────────┬─────────┬─────────┬───────────────────────────┐│
│  │  layer  │  tref   │   vec   │        neighbors          ││
│  │ TINYINT │VARBINARY│  BLOB   │          BLOB             ││
│  │  (层级) │(主表引用)│(向量数据)│    (邻居节点列表)          ││
│  └─────────┴─────────┴─────────┴───────────────────────────┘│
│  索引: IDX_TREF (tref), IDX_LAYER (layer)                   │
└─────────────────────────────────────────────────────────────┘
```

### 关键设计点

1. **辅助表命名**: `vidx_<table_id_hex>_<index_num>`
2. **辅助表隐藏**: `dd::Abstract_table::HT_HIDDEN_HLINDEX`
3. **元数据存储**: 在 DD (Data Dictionary) 表的 options 中存储向量索引配置
4. **内存缓存**: HNSW 图节点缓存在 `MHNSW_Share` 对象中

---

## 核心数据结构

### 1. FVector - 向量数据结构

```cpp
#pragma pack(push, 1)
struct FVector {
  static constexpr size_t data_header = sizeof(float);  // 4 bytes
  static constexpr size_t alloc_header = data_header + sizeof(float);  // 8 bytes

  float abs2, scale;      // abs2: 向量模平方/2, scale: 量化缩放因子
  int16_t dims[4];        // 量化后的维度值 (int16 代替 float，节省空间)

  uchar *data() const { return (uchar *)(&scale); }

  // 数据大小计算: header + n * 2 bytes (int16)
  static size_t data_size(size_t n) { return data_header + n * 2; }

  // 创建向量对象 (从原始 float 数据)
  static const FVector *create(distance_kind metric, void *mem, 
                                const void *src, size_t src_len) {
    float scale = 0, *v = (float *)src;
    size_t vec_len = src_len / sizeof(float);
    
    // 找最大绝对值，用于量化缩放
    for (size_t i = 0; i < vec_len; i++)
      scale = std::max(scale, std::abs(get_float(v + i)));

    FVector *vec = align_ptr(mem);
    vec->scale = scale ? scale / 32767 : 1;  // 缩放因子
    
    // 将 float 量化为 int16
    for (size_t i = 0; i < vec_len; i++)
      vec->dims[i] = static_cast<int16_t>(std::round(get_float(v + i) / vec->scale));
    
    vec->postprocess(vec_len);
    
    // COSINE 距离的特殊处理: 归一化
    if (metric == COSINE) {
      if (vec->abs2 > 0.0f) vec->scale /= std::sqrt(2 * vec->abs2);
      vec->abs2 = 0.5f;  // 归一化后模平方固定为 0.5
    }
    return vec;
  }

  // 计算距离
  float distance_to(const FVector *other, size_t vec_len) const {
    return abs2 + other->abs2 -
           scale * other->scale * dot_product(dims, other->dims, vec_len);
  }
};
#pragma pack(pop)
```

**关键设计思想**:
- 使用 **int16 量化** 代替 float，将存储空间减半
- 预计算 `abs2` (模平方/2)，加速距离计算
- COSINE 距离通过归一化预处理，使距离计算简化为点积

### 2. FVectorNode - HNSW 图节点

```cpp
#pragma pack(push, 1)
class FVectorNode {
 private:
  MHNSW_Share *ctx;       // 所属图上下文

 public:
  const FVector *vec = nullptr;     // 向量数据
  Neighborhood *neighbors = nullptr; // 邻居列表 (每层一个)
  uint8_t max_layer;                 // 该节点的最大层级
  bool stored : 1;   // 是否已存储到辅助表
  bool deleted : 1;  // 是否被标记删除

  // 从辅助表加载节点
  int load(TABLE *graph);
  int load_from_record(TABLE *graph);
  
  // 保存节点到辅助表
  int save(TABLE *graph);
  
  // 计算到其他向量的距离
  float distance_to(const FVector *other) const;
  
  // 内存布局: FVectorNode + gref + tref + FVector
  uchar *gref() const;  // 辅助表中的记录位置
  uchar *tref() const;  // 主表中的记录位置
};
#pragma pack(pop)
```

### 3. Neighborhood - 邓居数组

```cpp
struct Neighborhood {
  FVectorNode **links;  // 邓居节点指针数组
  size_t num;           // 当前邻居数量

  FVectorNode **init(FVectorNode **ptr, size_t n) {
    num = 0;
    links = ptr;
    n = MY_ALIGN(n, 8);  // 对齐到 8 (用于 SIMD Bloom Filter)
    bzero(ptr, n * sizeof(*ptr));
    return ptr + n;
  }
};
```

### 4. MHNSW_Share - 共享图上下文

```cpp
class MHNSW_Share {
  mysql_mutex_t cache_lock;      // 节点缓存锁
  mysql_mutex_t node_lock[8];    // 分区节点锁 (减少锁竞争)
  mysql_rwlock_t commit_lock;    // 提交锁 (事务支持)

  std::atomic<uint> refcnt{0};   // 引用计数
  MEM_ROOT root;                 // 内存分配器
  Hash_set<FVectorNode> node_cache;  // gref -> FVectorNode 映射

 public:
  ulonglong version = 0;         // 版本号 (用于并发控制)
  size_t vec_len = 0;            // 向量维度数
  size_t byte_len = 0;           // 向量字节长度
  FVectorNode *start = nullptr;  // 入口节点 (最高层)

  const uint tref_len;           // 主表引用长度
  const uint gref_len;           // 辅助表引用长度
  const uint M;                  // HNSW 参数 M (每层最大连接数)
  distance_kind metric;          // 距离度量类型

  // 获取/创建节点
  FVectorNode *get_node(const void *gref);
  
  // 每层最大邻居数
  uint max_neighbors(size_t layer) const {
    return (layer ? 1 : 2) * M;  // 第0层是 2M，其他层是 M
  }
};
```

### 5. MHNSW_Trx - 事务级上下文

```cpp
class MHNSW_Trx : public MHNSW_Share {
 public:
  MDL_ticket *table_id;          // 表的 MDL 锁票据
  bool list_of_nodes_is_lost = false;
  MHNSW_Trx *next = nullptr;     // 链表 (一个 THD 可有多个 trx)

  // 事务提交时合并到共享上下文
  static int do_commit(handlerton *, THD *thd, bool all);
  // 事务回滚时丢弃
  static int do_rollback(handlerton *, THD *thd, bool all);
};
```

---

## 向量字段类型实现

### Field_vector 类定义

```cpp
// include/vidx/vidx_field.h
class Field_vector : public Field_varstring {
 public:
  // 维度字节大小计算
  static uint32 dimension_bytes(uint32 dimensions) {
    return VECTOR_PRECISION * dimensions;  // 4 bytes * dimensions
  }

  Field_vector(uchar *ptr_arg, uint32 len_arg, uint length_bytes_arg,
               uchar *null_ptr_arg, uchar null_bit_arg, uchar auto_flags_arg,
               const char *field_name_arg, TABLE_SHARE *share)
      : Field_varstring(ptr_arg, len_arg, length_bytes_arg, null_ptr_arg,
                        null_bit_arg, auto_flags_arg, field_name_arg, share,
                        &my_charset_bin) {}

  // 获取维度数
  uint32 get_dimensions() const;

  // SQL 类型显示: /*!99999 vector(128) */ varbinary(512)
  void sql_type(String &res) const final {
    const CHARSET_INFO *cs = res.charset();
    size_t length = cs->cset->snprintf(
        cs, res.ptr(), res.alloced_length(),
        RDS_COMMENT_VIDX_START "vector(%u)" RDS_COMMENT_VIDX_END
                               " varbinary(%u)",
        get_dimensions(), VECTOR_PRECISION * get_dimensions());
    res.length(length);
  }

  // 存储验证
  type_conversion_status store(const char *from, size_t length,
                                const CHARSET_INFO *cs) final;
  
  // 标识为向量字段
  bool is_vector() const final { return true; }
};
```

### 存储验证实现

```cpp
// sql/vidx/vidx_field.cc
type_conversion_status Field_vector::store(const char *from, size_t length,
                                            const CHARSET_INFO *cs) {
  // 1. 验证字符集必须是 binary
  if (cs != &my_charset_bin) {
    push_warning_printf(thd, Sql_condition::SL_WARNING, 
                        ER_TRUNCATED_WRONG_VALUE_FOR_FIELD, ...);
  }

  // 2. 验证长度匹配
  if (length != field_length) {
    my_error(ER_DATA_INCOMPATIBLE_WITH_VECTOR, MYF(0), "string", length,
             get_dimensions());
    return TYPE_ERR_BAD_VALUE;
  }

  // 3. 计算维度
  uint32 dimensions = get_dimensions_low(length, VECTOR_PRECISION);
  if (dimensions == UINT_MAX32 || dimensions > get_dimensions()) {
    return TYPE_ERR_BAD_VALUE;
  }

  // 4. 验证每个维度值有效 (非 NaN/Inf)
  float abs2 = 0.0f;
  for (uint32 i = 0; i < dimensions; i++) {
    float to_store;
    memcpy(&to_store, from + sizeof(float) * i, sizeof(float));
    if (std::isnan(to_store) || std::isinf(to_store)) {
      my_error(ER_TRUNCATED_WRONG_VALUE_FOR_FIELD, ...);
      return TYPE_ERR_BAD_VALUE;
    }
    abs2 += to_store * to_store;
  }

  // 5. 验证模平方有限
  if (!std::isfinite(abs2)) {
    return TYPE_ERR_BAD_VALUE;
  }

  return Field_varstring::store(from, length, cs);
}
```

---

## HNSW 算法实现

### HNSW 核心参数

```cpp
// vidx_common.h
namespace hnsw {
static constexpr uint M_DEF = 6;      // 默认 M 值
static constexpr uint M_MAX = 200;    // 最大 M 值
static constexpr uint M_MIN = 3;      // 最小 M 值
}

// vidx_hnsw.cc
static constexpr float NEAREST = -1.0f;  // 搜索最近邻的阈值
static constexpr float alpha = 1.1f;     // 邻居选择参数
static constexpr uint ef_construction = 10;  // 构建时的 ef
static constexpr uint max_ef = 10000;    // 最大 ef (搜索时)
```

### 向量插入流程 (mhnsw_insert)

```cpp
int mhnsw_insert(TABLE *table, KEY *keyinfo) {
  THD *thd = table->in_use;
  TABLE *graph = table->hlindex;  // 辅助表
  
  // 1. 获取向量数据
  Field *vec_field = keyinfo->key_part->field;
  String buf, *res = vec_field->val_str(&buf);
  
  // 2. 获取主表记录位置 (作为 tref)
  table->file->position(table->record[0]);
  
  // 3. 获取/创建图上下文
  MHNSW_Share *ctx;
  int err = MHNSW_Share::acquire(&ctx, table, true);
  
  // 4. 首次插入 (图为空)
  if (err == HA_ERR_END_OF_FILE) {
    ctx->set_lengths(res->length());
    FVectorNode *target = new (ctx->alloc_node())
        FVectorNode(ctx, table->file->ref, 0, res->ptr());
    target->save(graph);
    ctx->start = target;  // 设置入口节点
    return 0;
  }

  // 5. 计算目标节点的层级 (随机)
  const double NORMALIZATION_FACTOR = 1 / std::log(ctx->M);
  double log = -std::log(my_rnd(&thd->rand)) * NORMALIZATION_FACTOR;
  uint8_t target_layer = std::min<uint8_t>(
      static_cast<uint8_t>(std::floor(log)), max_layer + 1);

  // 6. 创建目标节点
  FVectorNode *target = new (ctx->alloc_node())
      FVectorNode(ctx, table->file->ref, target_layer, res->ptr());

  // 7. 从最高层向下搜索，找到插入位置
  Neighborhood candidates;
  candidates.init(...);
  candidates.links[candidates.num++] = ctx->start;

  // 从入口层到目标层+1: 贪心搜索找最近节点
  for (cur_layer = max_layer; cur_layer > target_layer; cur_layer--) {
    search_layer(ctx, graph, target->vec, NEAREST, 1, cur_layer, 
                 &candidates, false);
  }

  // 从目标层到第0层: 搜索并连接邻居
  for (cur_layer = target_layer; cur_layer >= 0; cur_layer--) {
    uint max_neighbors = ctx->max_neighbors(cur_layer);
    search_layer(ctx, graph, target->vec, NEAREST, max_neighbors,
                 cur_layer, &candidates, true);
    
    // 选择邻居
    select_neighbors(ctx, graph, cur_layer, *target, candidates, 
                     nullptr, max_neighbors);
  }

  // 8. 保存节点
  target->save(graph);

  // 9. 如果新节点层级更高，更新入口节点
  if (target_layer > max_layer) ctx->start = target;

  // 10. 更新反向连接 (邻居指向新节点)
  for (cur_layer = target_layer; cur_layer >= 0; cur_layer--) {
    update_second_degree_neighbors(ctx, graph, cur_layer, target);
  }

  return 0;
}
```

### 层级搜索 (search_layer)

```cpp
static int search_layer(MHNSW_Share *ctx, TABLE *graph, 
                         const FVector *target,
                         float threshold, uint result_size, 
                         size_t layer,
                         Neighborhood *inout, bool construction) {
  MEM_ROOT *root = graph->in_use->mem_root;
  
  // 1. 初始化优先队列
  Queue<Visited> candidates, best;  // candidates: 待访问, best: 结果
  uint ef = result_size;
  
  if (construction) {
    ef = std::max(ef_construction, ef);  // 构建时使用更大的 ef
  } else {
    ef = std::max(get_ef_search(graph->in_use), ef);  // 搜索时
  }

  // 2. 初始化已访问集合 (Bloom Filter)
  const uint est_size = ...;  // 估计大小
  VisitedSet visited(root, target, est_size);

  // 3. 从入口节点开始
  for (size_t i = 0; i < inout->num; i++) {
    Visited *v = visited.create(inout->links[i]);
    candidates.safe_push(v);
    best.push(v);
  }

  // 4. 贪心搜索
  float furthest_best = generous_furthest(best, max_distance, generosity);
  
  while (candidates.elements()) {
    const Visited &cur = *candidates.pop();
    
    // 如果当前距离超过阈值，停止
    if (cur.distance_to_target > furthest_best && best.is_full())
      break;

    // 遍历当前节点的邻居
    Neighborhood &neighbors = cur.node->neighbors[layer];
    for (FVectorNode **links = neighbors.links; links < end; links += 8) {
      uint8_t res = visited.seen(links);  // Bloom Filter 检查
      if (res == 0xff) continue;  // 全部已访问

      for (size_t i = 0; i < 8; i++) {
        if (res & (1 << i)) continue;  // 已访问
        
        links[i]->load(graph);  // 按需加载
        Visited *v = visited.create(links[i]);
        
        if (v->distance_to_target <= threshold) continue;
        
        // 加入候选队列
        candidates.safe_push(v);
        
        // 更新最佳结果队列
        if (!best.is_full()) {
          best.push(v);
          furthest_best = generous_furthest(best, max_distance, generosity);
        } else if (v->distance_to_target < furthest_best) {
          if (v->distance_to_target < best.top()->distance_to_target) {
            best.replace_top(v);
            furthest_best = generous_furthest(best, max_distance, generosity);
          }
        }
      }
    }
  }

  // 5. 返回结果
  inout->num = best.elements();
  for (; best.elements();)
    inout->links[--inout->num] = best.pop()->node;

  return 0;
}
```

### 邻居选择 (select_neighbors)

```cpp
static int select_neighbors(MHNSW_Share *ctx, TABLE *graph, size_t layer,
                            FVectorNode &target, 
                            const Neighborhood &candidates,
                            FVectorNode *extra_candidate,
                            size_t max_neighbor_connections) {
  Queue<Visited> pq;  // 工作队列
  pq.init(max_ef, false, Visited::cmp);

  MEM_ROOT *root = graph->in_use->mem_root;
  Neighborhood &neighbors = target.neighbors[layer];

  // 1. 将候选节点加入队列
  for (size_t i = 0; i < candidates.num; i++) {
    FVectorNode *node = candidates.links[i];
    node->load(graph);
    pq.push(new (root) Visited(node, node->distance_to(target.vec)));
  }
  if (extra_candidate)
    pq.push(new (root) Visited(extra_candidate, ...));

  // 2. 选择邻居 (启发式算法)
  neighbors.num = 0;
  Visited **discarded = ...;  // 被丢弃的候选
  size_t discarded_num = 0;

  while (pq.elements() && neighbors.num < max_neighbor_connections) {
    Visited *vec = pq.pop();
    FVectorNode *node = vec->node;
    
    // 启发式: 如果新节点太接近已选邻居，丢弃
    const float target_dista = 
        std::max(32 * FLT_EPSILON, vec->distance_to_target / alpha);
    bool discard = false;
    for (size_t i = 0; i < neighbors.num; i++)
      if (node->distance_to(neighbors.links[i]->vec) <= target_dista) {
        discard = true;
        break;
      }
    
    if (!discard)
      target.push_neighbor(layer, node);
    else if (discarded_num + neighbors.num < max_neighbor_connections)
      discarded[discarded_num++] = vec;
  }

  // 3. 如果邻居不足，补充被丢弃的候选
  for (size_t i = 0; 
       i < discarded_num && neighbors.num < max_neighbor_connections; i++)
    target.push_neighbor(layer, discarded[i]->node);

  return 0;
}
```

### 向量搜索流程 (mhnsw_read_first / mhnsw_read_next)

```cpp
int mhnsw_read_first(TABLE *table, KEY *, Item *dist) {
  THD *thd = table->in_use;
  TABLE *graph = table->hlindex;
  auto *fun = static_cast<Item_func_vec_distance *>(dist->real_item());
  ulonglong limit = fun->get_limit();  // LIMIT 值

  // 1. 获取查询向量
  String buf, *res = fun->get_const_arg()->val_str(&buf);

  // 2. 获取图上下文
  MHNSW_Share *ctx;
  MHNSW_Share::acquire(&ctx, table, false);

  // 3. 初始化候选集合
  Neighborhood candidates;
  candidates.links[candidates.num++] = ctx->start;  // 从入口开始

  // 4. 创建查询向量对象
  auto target = FVector::create(ctx->metric, ..., res->ptr(), res->length());

  // 5. 从最高层向下搜索
  for (size_t cur_layer = max_layer; cur_layer > 0; cur_layer--) {
    search_layer(ctx, graph, target, NEAREST, 1, cur_layer, &candidates, false);
  }

  // 6. 在第0层搜索 limit 个结果
  search_layer(ctx, graph, target, NEAREST, limit, 0, &candidates, false);

  // 7. 创建搜索上下文 (保存结果)
  auto result = new (thd->mem_root) Search_context(&candidates, ctx, target);
  graph->context = result;

  return mhnsw_read_next(table);  // 返回第一个结果
}

int mhnsw_read_next(TABLE *table) {
  auto result = static_cast<Search_context *>(table->hlindex->context);

  // 1. 如果还有结果，返回下一个
  if (result->pos < result->found.num) {
    uchar *ref = result->found.links[result->pos++]->tref();
    return table->file->ha_rnd_pos(table->record[0], ref);  // 定位主表记录
  }

  // 2. 如果结果耗尽，继续搜索更多
  if (!result->found.num) return HA_ERR_END_OF_FILE;

  // 3. 检查版本变化 (并发修改)
  if (ctx->version != result->ctx_version) {
    // 重新加载节点...
  }

  // 4. 继续搜索
  float new_threshold = 
      result->found.links[result->found.num - 1]->distance_to(result->target);
  
  search_layer(ctx, graph, result->target, result->threshold,
               result->pos, 0, &result->found, false);
  
  result->pos = 0;
  result->threshold = new_threshold + FLT_EPSILON;
  
  return mhnsw_read_next(table);
}
```

---

## 距离计算与 SIMD 优化

### 欧几里得距离

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

### 余弦距离

```cpp
static double calc_distance_cosine(float *v1, float *v2, size_t v_len) {
  double dotp = 0, abs1 = 0, abs2 = 0;
  for (size_t i = 0; i < v_len; i++, v1++, v2++) {
    float f1 = get_float(v1), f2 = get_float(v2);
    abs1 += f1 * f1;
    abs2 += f2 * f2;
    dotp += f1 * f2;
  }
  return 1 - dotp / sqrt(abs1 * abs2);  // 1 - cosine_similarity
}
```

### SIMD 优化点积计算

AliSQL 使用 **int16 量化** 配合 SIMD 指令加速点积计算：

```cpp
// AVX2 实现 (x86)
#ifdef AVX2_IMPLEMENTATION
AVX2_IMPLEMENTATION
static float dot_product(const int16_t *v1, const int16_t *v2, size_t len) {
  typedef float v8f __attribute__((vector_size(32)));  // 8 floats
  union { v8f v; __m256 i; } tmp;
  
  __m256i *p1 = (__m256i *)v1;
  __m256i *p2 = (__m256i *)v2;
  v8f d = {0};
  
  // 每次处理 16 个 int16 (256 bits)
  for (size_t i = 0; i < (len + 15) / 16; p1++, p2++, i++) {
    // _mm256_madd_epi16: 同时做乘法和加法
    // int16 * int16 -> int32, 然后相邻两个 int32 相加
    tmp.i = _mm256_cvtepi32_ps(_mm256_madd_epi16(*p1, *p2));
    d += tmp.v;
  }
  
  // 横向求和
  return d[0] + d[1] + d[2] + d[3] + d[4] + d[5] + d[6] + d[7];
}
#endif

// AVX512 实现
#ifdef AVX512_IMPLEMENTATION
AVX512_IMPLEMENTATION
static float dot_product(const int16_t *v1, const int16_t *v2, size_t len) {
  __m512i *p1 = (__m512i *)v1;
  __m512i *p2 = (__m512i *)v2;
  __m512 d = _mm512_setzero_ps();
  
  // 每次处理 32 个 int16 (512 bits)
  for (size_t i = 0; i < (len + 31) / 32; p1++, p2++, i++)
    d = _mm512_add_ps(d, _mm512_cvtepi32_ps(_mm512_madd_epi16(*p1, *p2)));
  
  return _mm512_reduce_add_ps(d);  // 横向求和
}
#endif

// ARM NEON 实现
#ifdef NEON_IMPLEMENTATION
static float dot_product(const int16_t *v1, const int16_t *v2, size_t len) {
  int64_t d = 0;
  for (size_t i = 0; i < (len + 7) / 8; i++) {
    int16x8_t p1 = vld1q_s16(v1);
    int16x8_t p2 = vld1q_s16(v2);
    // vmull_s16: 低 4 个 int16 相乘 -> 4 个 int32
    // vmull_high_s16: 高 4 个 int16 相乘 -> 4 个 int32
    d += vaddlvq_s32(vmull_s16(vget_low_s16(p1), vget_low_s16(p2))) +
         vaddlvq_s32(vmull_high_s16(p1, p2));
    v1 += 8;
    v2 += 8;
  }
  return static_cast<float>(d);
}
#endif
```

### 量化距离计算

```cpp
// FVector::distance_to - 使用量化数据计算距离
float distance_to(const FVector *other, size_t vec_len) const {
  // 距离公式: ||v1||² + ||v2||² - 2 * (v1 · v2)
  // 由于量化: 实际距离 = abs2 + other->abs2 - scale * other->scale * dot_product
  return abs2 + other->abs2 -
         scale * other->scale * dot_product(dims, other->dims, vec_len);
}
```

**优化效果**:
- int16 存储 vs float: **空间减半**
- SIMD 点积: **8-32x 并行计算**
- 预计算 abs2: **减少重复计算**

---

## SQL 函数实现

### VEC_DISTANCE 函数

```cpp
class Item_func_vec_distance : public Item_real_func {
 public:
  Item_func_vec_distance(const POS &pos, Item *a, Item *b)
      : Item_real_func(pos, a, b), kind(AUTO) {}

  // 函数名
  const char *func_name() const override {
    static LEX_CSTRING name[3] = {
      {"VEC_DISTANCE_EUCLIDEAN", ...},
      {"VEC_DISTANCE_COSINE", ...},
      {"VEC_DISTANCE", ...}
    };
    return name[kind].str;
  }

  // 类型解析: 确定距离类型
  bool resolve_type(THD *thd) override {
    switch (kind) {
      case EUCLIDEAN:
        calc_distance_func = calc_distance_euclidean;
        break;
      case COSINE:
        calc_distance_func = calc_distance_cosine;
        break;
      case AUTO:
        // 自动检测: 根据向量索引的距离类型
        for (uint fno = 0; fno < 2; fno++) {
          if (args[fno]->type() == Item::FIELD_ITEM) {
            Field *f = ((Item_field *)args[fno])->field;
            KEY *key_info = f->table->s->key_info;
            for (uint i = f->table->s->keys; 
                 i < f->table->s->total_keys; i++) {
              if (f->key_start.is_set(i)) {
                kind = mhnsw_uses_distance(key_info + i);
                return resolve_type(thd);
              }
            }
          }
        }
        my_error(ER_VEC_DISTANCE_TYPE, MYF(0));
        return true;
    }
    return Item_real_func::resolve_type(thd);
  }

  // 计算距离值
  double val_real() override {
    String tmp1, tmp2;
    String *r1 = args[0]->val_str(&tmp1);
    String *r2 = args[1]->val_str(&tmp2);

    // 验证维度匹配
    if (!r1 || !r2 || r1->length() != r2->length() ||
        r1->length() % sizeof(float)) {
      null_value = true;
      return 0;
    }

    return calc_distance_func((float *)r1->ptr(), (float *)r2->ptr(),
                              r1->length() / sizeof(float));
  }

  // 获取可用的向量索引
  int get_key();

 private:
  distance_kind kind;
  double (*calc_distance_func)(float *, float *, size_t);
  ha_rows m_limit = 0;
  Item_field *field_arg = nullptr;
  Item *const_arg = nullptr;
};
```

### VEC_FromText 函数

```cpp
class Item_func_vec_fromtext : public Item_str_func {
 public:
  bool resolve_type(THD *thd) override {
    // 返回类型: vector(MAX_DIMENSIONS)
    set_data_type_vector(Field_vector::dimension_bytes(MAX_DIMENSIONS));
    return false;
  }

  String *val_str(String *str) override {
    String *res = args[0]->val_str(str);
    
    uint32 output_dims = MAX_DIMENSIONS;
    buffer.mem_realloc(Field_vector::dimension_bytes(output_dims));
    
    // 解析文本格式: "[1.0, 2.0, 3.0, ...]"
    bool err = from_string_to_vector(res->ptr(), res->length(), 
                                      buffer.ptr(), &output_dims);
    if (err) {
      my_error(ER_TO_VECTOR_CONVERSION, ...);
      return error_str();
    }
    
    buffer.length(Field_vector::dimension_bytes(output_dims));
    return &buffer;
  }
};

// 文本解析实现
static bool from_string_to_vector(const char *input, uint32_t input_len,
                                   char *output, uint32_t *max_output_dims) {
  // 验证格式: 必须以 '[' 开始，']' 结束
  if (input[0] != '[' || input[input_len - 1] != ']') {
    return true;
  }

  const char *end = input + input_len - 1;
  input = input + 1;
  uint32_t dim = 0;
  
  // 解析每个浮点数
  for (float fnum = strtof(input, &end); input != end;
       fnum = strtof(input, &end)) {
    input = end;
    
    // 验证范围
    if (errno == ERANGE || dim >= *max_output_dims || 
        std::isnan(fnum) || std::isinf(fnum)) {
      return true;
    }
    
    // 存储为 float
    memcpy(output + dim * sizeof(float), &fnum, sizeof(float));
    
    // 处理分隔符
    if (*input == ',') {
      input++;
      dim++;
    } else if (*input == ']' && input == end) {
      dim++;
      break;
    } else {
      return true;
    }
  }

  *max_output_dims = dim;
  return false;
}
```

### VEC_ToText 函数

```cpp
class Item_func_vec_totext : public Item_str_func {
 public:
  bool resolve_type(THD *thd) override {
    // 返回类型: VARCHAR
    set_data_type_string(Item_func_vec_totext::max_output_bytes);
    return false;
  }

  String *val_str(String *str) override {
    String *res = args[0]->val_str(str);
    
    // 将二进制向量转换为文本格式 "[1.0, 2.0, 3.0, ...]"
    if (from_vector_to_string(res, VECTOR_PRECISION, &my_charset_numeric, &buffer)) {
      my_error(ER_VECTOR_BINARY_FORMAT_INVALID, MYF(0));
      return error_str();
    }
    
    return &buffer;
  }
};

// 向量转文本实现
static bool from_vector_to_string(String *input, const uint32 precision,
                                   CHARSET_INFO *cs, String *output) {
  const uint32 input_dims = get_dimensions_low(input->length(), precision);
  
  output->length(0);
  output->set_charset(cs);
  output->reserve(input_dims * (MAX_FLOAT_STR_LENGTH + 1) + 2);
  
  output->append('[');
  
  auto ptr = (const uchar *)input->ptr();
  for (size_t i = 0; i < input_dims; i++) {
    if (i != 0) output->append(',');
    
    float val = float4get(ptr);
    if (std::isinf(val))
      output->append(val < 0 ? "-Inf" : "Inf");
    else if (std::isnan(val))
      output->append("NaN");
    else {
      char buf[MAX_FLOAT_STR_LENGTH + 1];
      size_t len = my_gcvt(val, MY_GCVT_ARG_FLOAT, MAX_FLOAT_STR_LENGTH, buf, 0);
      output->append(buf, len);
    }
    
    ptr += precision;
  }
  
  output->append(']');
  return false;
}
```

### vector_dim 函数

```cpp
class Item_func_vector_dim : public Item_int_func {
 public:
  longlong val_int() override {
    String *res = args[0]->val_str(&value);
    
    uint32 dimensions = get_dimensions_low(res->length(), VECTOR_PRECISION);
    if (dimensions == UINT_MAX32) {
      my_error(ER_TO_VECTOR_CONVERSION, ...);
      return error_int();
    }
    
    return (longlong)dimensions;
  }
};
```

---

## 辅助表结构

### 辅助表创建流程

```cpp
// sql/vidx/vidx_index.cc
static TABLE *create_hlindex_table(THD *thd, TABLE_SHARE *share,
                                    KEY *keyinfo, std::string &error_message) {
  // 1. 构建辅助表名
  const char *hlindex_name = build_name(thd, share->table_id, vidx_num, error_message);

  // 2. 构建建表 SQL
  String sql;
  sql.append("CREATE TABLE ");
  sql.append(db_name);
  sql.append(".");
  sql.append(hlindex_name);
  sql.append(" (");
  sql.append("layer TINYINT NOT NULL, ");      // 层级
  sql.append("tref VARBINARY(");
  sql.append(std::to_string(get_tref_len(table)));
  sql.append(") NOT NULL, ");                  // 主表引用
  sql.append("vec BLOB NOT NULL, ");           // 向量数据
  sql.append("neighbors BLOB NOT NULL");       // 邓居列表
  sql.append(") ENGINE=InnoDB");

  // 3. 执行建表
  if (mysql_real_query(thd, sql.ptr(), sql.length())) {
    error_message = "Failed to create vector index table.";
    return nullptr;
  }

  // 4. 创建索引
  // IDX_TREF: 用于通过主表记录查找向量节点
  sql.clear();
  sql.append("CREATE INDEX IDX_TREF ON ");
  sql.append(hlindex_name);
  sql.append("(tref)");
  mysql_real_query(thd, sql.ptr(), sql.length());

  // IDX_LAYER: 用于按层级遍历
  sql.clear();
  sql.append("CREATE INDEX IDX_LAYER ON ");
  sql.append(hlindex_name);
  sql.append("(layer)");
  mysql_real_query(thd, sql.ptr(), sql.length());

  // 5. 设置隐藏属性
  dd::Table *dd_table;
  thd->dd_client()->acquire_for_modification(db_name, hlindex_name, &dd_table);
  dd_table->set_hidden(dd::Abstract_table::HT_HIDDEN_HLINDEX);
  
  // 6. 存储元数据
  dd_table->options().set("distance", distance_names[keyinfo->vector_distance]);
  dd_table->options().set("M", std::to_string(keyinfo->vector_M));
  
  thd->dd_client()->update(dd_table);

  return open_table(thd, db_name, hlindex_name);
}
```

### 辅助表字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `layer` | TINYINT | 节点所在层级 (0-255) |
| `tref` | VARBINARY(N) | 主表记录位置引用 (用于回表) |
| `vec` | BLOB | 量化后的向量数据 (int16 格式) |
| `neighbors` | BLOB | 邓居节点引用列表 (每层一个数组) |

### 辅助表索引

| 索引名 | 字段 | 用途 |
|--------|------|------|
| `IDX_TREF` | tref | 主表 DELETE/UPDATE 时定位向量节点 |
| `IDX_LAYER` | layer | 按层级遍历图 (找入口节点等) |
| `PRIMARY` | (隐式) | 内部 row_id |

---

## DDL 处理流程

### CREATE TABLE with vector column

```cpp
// sql/vidx/vidx_index.cc

// 1. SQL 解析阶段: 重写 SQL
static void rewrite_sql_of_vector_column(THD *thd) {
  std::string query_str = to_string(thd->query());
  
  // 将 vector(N) 替换为 /*!99999 vector(N) */ varbinary(4*N)
  // 这样在社区版 MySQL 上也能执行 (忽略注释)
  rewrite_sql(thd, sql_regex_replacement(query_str,
      std::regex{R"(\bvector\s*\(\s*(\d+)\s*\))", std::regex_constants::icase},
      replacement_vector));
}

static std::string replacement_vector(const std::string &catching) {
  return std::string(RDS_COMMENT_VIDX_START "vector(") + catching +
         std::string(")" RDS_COMMENT_VIDX_END " varbinary(") +
         std::to_string(4 * stoi(catching)) + std::string(")");
}

// 示例:
// CREATE TABLE t1 (id INT, emb vector(128));
// 被重写为:
// CREATE TABLE t1 (id INT, emb /*!99999 vector(128) */ varbinary(512));
```

### CREATE VECTOR INDEX

```cpp
// 语法: CREATE VECTOR INDEX idx_name ON table(column) 
//       WITH (distance='euclidean', M=6);

// 1. 解析阶段
bool Sql_cmd_create_index::prepare(THD *thd) {
  if (keyinfo->flags & HA_VECTOR) {
    // 验证向量索引参数
    if (!keyinfo->vector_distance)  // 默认使用系统变量
      keyinfo->vector_distance = THDVAR(thd, default_distance);
    if (!keyinfo->vector_M)
      keyinfo->vector_M = THDVAR(thd, hnsw_default_m);
    
    // 验证字段是向量类型
    Field *field = keyinfo->key_part->field;
    if (!field->is_vector()) {
      my_error(ER_VECTOR_INDEX_ON_NON_VECTOR_COLUMN, ...);
      return true;
    }
  }
  return false;
}

// 2. 执行阶段
bool Sql_cmd_create_index::execute(THD *thd) {
  if (keyinfo->flags & HA_VECTOR) {
    // 创建辅助表
    TABLE *hlindex_table = create_hlindex_table(thd, table->s, keyinfo);
    
    // 在主表 DD 中记录向量索引信息
    dd::String_type hlindexes;
    dd_table_get_hlindexes(dd_table, &hlindexes);
    hlindexes.append(hlindex_name);
    dd_table_set_hlindexes(dd_table, hlindexes);
    
    // 标记索引为 HA_VECTOR 类型
    keyinfo->flags |= HA_VECTOR;
    table->s->total_keys++;  // 额外的隐藏索引
    
    // 构建向量索引 (遍历主表数据)
    build_vector_index(thd, table, hlindex_table, keyinfo);
  }
  return false;
}

// 3. 构建向量索引
static int build_vector_index(THD *thd, TABLE *table, TABLE *graph, KEY *keyinfo) {
  // 遍历主表所有记录，插入到 HNSW 图
  table->file->ha_rnd_init(true);
  while (!table->file->ha_rnd_next(table->record[0])) {
    mhnsw_insert(table, keyinfo);
  }
  table->file->ha_rnd_end();
  return 0;
}
```

### ALTER TABLE ADD VECTOR INDEX

```cpp
// 语法: ALTER TABLE t1 ADD VECTOR INDEX idx(emb) WITH (distance='cosine');

// 处理流程与 CREATE VECTOR INDEX 类似
// 但需要处理并发 DML 的情况
```

### DROP VECTOR INDEX

```cpp
// 语法: ALTER TABLE t1 DROP VECTOR INDEX idx;

bool Sql_cmd_alter_table::execute(THD *thd) {
  if (drop_vector_index) {
    // 1. 获取辅助表名
    dd::String_type hlindexes;
    dd_table_get_hlindexes(dd_table, &hlindexes);
    
    // 2. 删除辅助表
    for (const char *hlindex_name : parse_hlindexes(hlindexes)) {
      drop_table(thd, db_name, hlindex_name);
    }
    
    // 3. 更新主表 DD
    dd_table_set_hlindexes(dd_table, "");
    
    // 4. 清理内存缓存
    MHNSW_Share::reset(table->s);
  }
  return false;
}
```

---

## DML 操作流程

### INSERT 流程

```cpp
// sql/vidx/vidx_index.cc

// handler 层钩子
int ha_innodb::write_row(uchar *buf) {
  // 1. 正常写入主表
  int err = innodb_write_row(buf);
  if (err) return err;
  
  // 2. 如果有向量索引，插入到 HNSW 图
  for (uint i = table->s->keys; i < table->s->total_keys; i++) {
    KEY *keyinfo = &table->s->key_info[i];
    if (keyinfo->flags & HA_VECTOR) {
      err = mhnsw_insert(table, keyinfo);
      if (err) {
        // 回滚主表写入
        delete_row(buf);
        return err;
      }
    }
  }
  return 0;
}
```

### DELETE 流程

```cpp
int ha_innodb::delete_row(const uchar *buf) {
  // 1. 获取主表记录位置
  position(buf);
  
  // 2. 如果有向量索引，标记删除向量节点
  for (uint i = table->s->keys; i < table->s->total_keys; i++) {
    KEY *keyinfo = &table->s->key_info[i];
    if (keyinfo->flags & HA_VECTOR) {
      // 通过 tref 找到向量节点
      TABLE *graph = table->hlindex;
      uchar *tref = table->file->ref;
      
      // 在辅助表中查找并标记删除
      graph->file->ha_index_init(IDX_TREF, 1);
      if (!graph->file->ha_index_read_map(graph->record[0], tref, ...)) {
        // 标记 deleted = true (软删除)
        Field *deleted_field = graph->field[...];
        deleted_field->store(true);
        graph->file->ha_update_row(graph->record[0], graph->record[1]);
      }
      graph->file->ha_index_end();
    }
  }
  
  // 3. 删除主表记录
  return innodb_delete_row(buf);
}
```

### UPDATE 流程

```cpp
int ha_innodb::update_row(const uchar *old_buf, uchar *new_buf) {
  // 1. 检查向量字段是否变化
  for (uint i = table->s->keys; i < table->s->total_keys; i++) {
    KEY *keyinfo = &table->s->key_info[i];
    if (keyinfo->flags & HA_VECTOR) {
      Field *vec_field = keyinfo->key_part->field;
      
      // 比较新旧向量值
      if (vec_field->cmp(old_buf, new_buf) != 0) {
        // 向量变化: 删除旧节点，插入新节点
        delete_vector_node(table, keyinfo, old_buf);
        mhnsw_insert(table, keyinfo);
      }
    }
  }
  
  // 2. 更新主表记录
  return innodb_update_row(old_buf, new_buf);
}
```

---

## 查询优化与执行

### ORDER BY VEC_DISTANCE LIMIT N 优化

```cpp
// sql/sql_select.cc

// 1. 优化器识别向量索引可用
bool JOIN::optimize() {
  for (ORDER *order = order_list.first; order; order = order->next) {
    Item *item = *order->item;
    
    // 检查是否是 VEC_DISTANCE 函数
    if (item->type() == Item::FUNC_ITEM &&
        ((Item_func *)item)->functype() == Item_func::VEC_DISTANCE) {
      auto *dist_func = (Item_func_vec_distance *)item;
      
      // 检查是否有可用的向量索引
      int key = dist_func->get_key();
      if (key >= 0) {
        // 使用向量索引访问路径
        AccessPath *path = create_vector_index_access_path(join, key, dist_func);
        
        // 设置 LIMIT 作为搜索参数
        dist_func->set_limit(select_limit);
        
        return true;
      }
    }
  }
  return false;
}

// 2. 创建向量索引访问路径
AccessPath *create_vector_index_access_path(JOIN *join, int key, 
                                             Item_func_vec_distance *dist) {
  AccessPath *path = new AccessPath;
  path->type = AccessPath::VECTOR_INDEX_SCAN;
  path->vector_index_info = {
    .table = join->tables[0]->table,
    .key = key,
    .distance_func = dist,
    .limit = join->select_limit
  };
  return path;
}

// 3. 执行向量索引扫描
int join_read_vector_index_next(JOIN_TAB *tab) {
  TABLE *table = tab->table;
  
  if (!tab->vector_index_started) {
    // 首次调用: 初始化搜索
    int err = mhnsw_read_first(table, tab->keyinfo, tab->distance_item);
    tab->vector_index_started = true;
    return err;
  }
  
  // 后续调用: 获取下一个结果
  return mhnsw_read_next(table);
}
```

### 执行流程图

```
┌─────────────────────────────────────────────────────────────┐
│  SELECT * FROM t1                                            │
│  ORDER BY VEC_DISTANCE(emb, VEC_FromText('[1,2,3,...]'))    │
│  LIMIT 10;                                                   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  优化器阶段                                                   │
│  1. 识别 ORDER BY 包含 VEC_DISTANCE                          │
│  2. 检查 emb 字段是否有向量索引                               │
│  3. 创建 VECTOR_INDEX_SCAN 访问路径                          │
│  4. 设置 ef_search = max(LIMIT, @@hnsw_ef_search)           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  执行阶段                                                     │
│  1. mhnsw_read_first():                                      │
│     - 获取查询向量                                            │
│     - 从最高层向下贪心搜索                                    │
│     - 在第0层搜索 ef_search 个候选                            │
│                                                              │
│  2. mhnsw_read_next() [循环]:                                │
│     - 通过 tref 回表获取主表记录                              │
│     - 计算精确距离                                            │
│     - 返回给客户端                                            │
│     - 直到返回 LIMIT 个结果                                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 在社区版 MySQL 上重新实现的步骤

### 第一阶段: 基础框架搭建

1. **创建目录结构**
```bash
mkdir -p include/vidx sql/vidx
```

2. **定义常量和基础类型** (`include/vidx/vidx_common.h`)
```cpp
// 基础常量
#define VECTOR_PRECISION 4  // float = 4 bytes
#define MAX_DIMENSIONS 16383

// 距离类型枚举
enum distance_kind {
  EUCLIDEAN = 0,
  COSINE = 1,
  AUTO = 2
};

// 向量索引标志
#define HA_VECTOR (1ULL << 30)
```

3. **实现向量字段类型** (`include/vidx/vidx_field.h`, `sql/vidx/vidx_field.cc`)
- 继承 `Field_varstring`
- 实现 `store()` 验证逻辑
- 实现 `sql_type()` 显示格式
- 添加 `is_vector()` 标识方法

### 第二阶段: SQL 函数实现

4. **实现 VEC_FromText** (`sql/vidx/vidx_func.cc`)
- 继承 `Item_str_func`
- 解析 `[f1, f2, f3, ...]` 格式
- 返回二进制向量数据

5. **实现 VEC_ToText**
- 将二进制向量转换为文本格式

6. **实现 VEC_DISTANCE**
- 继承 `Item_real_func`
- 实现欧几里得和余弦距离计算
- 支持 AUTO 模式自动检测

7. **实现 vector_dim**
- 返回向量维度数

### 第三阶段: 辅助表管理

8. **实现辅助表创建** (`sql/vidx/vidx_index.cc`)
- 建表 SQL 生成
- 索引创建
- DD 元数据存储

9. **实现辅助表打开/关闭**
- 与主表关联
- 缓存管理

### 第四阶段: HNSW 算法实现

10. **实现 FVector 数据结构** (`sql/vidx/vidx_hnsw.cc`)
- int16 量化
- 距离计算

11. **实现 FVectorNode**
- 邻居管理
- 加载/保存

12. **实现 MHNSW_Share**
- 共享缓存
- 锁管理

13. **实现核心算法**
- `mhnsw_insert()`: 向量插入
- `search_layer()`: 层级搜索
- `select_neighbors()`: 邓居选择
- `mhnsw_read_first/next()`: 向量搜索

### 第五阶段: DDL/DML 集成

14. **修改 SQL 解析器**
- 支持 `vector(N)` 类型语法
- 支持 `CREATE VECTOR INDEX` 语法
- 支持 `WITH (distance='...', M=...)` 参数

15. **修改 CREATE TABLE 流程**
- SQL 重写 (vector -> varbinary)
- 字段类型映射

16. **修改 CREATE INDEX 流程**
- 识别 VECTOR INDEX
- 创建辅助表
- 构建初始索引

17. **修改 handler 接口**
- `write_row()` 钩子
- `delete_row()` 钩子
- `update_row()` 钩子

### 第六阶段: 查询优化

18. **修改优化器**
- 识别 `ORDER BY VEC_DISTANCE`
- 创建向量索引访问路径

19. **修改执行器**
- 实现向量索引扫描执行逻辑

### 第七阶段: SIMD 优化

20. **实现 SIMD 点积计算** (`include/vidx/SIMD.h`)
- AVX2 版本
- AVX512 版本
- ARM NEON 版本

### 第八阶段: 测试与完善

21. **编写测试用例**
```sql
-- 基础功能测试
CREATE TABLE t1 (id INT PRIMARY KEY, emb vector(128));
INSERT INTO t1 VALUES (1, VEC_FromText('[1,2,3,...]'));
CREATE VECTOR INDEX idx_emb ON t1(emb) WITH (distance='euclidean');
SELECT * FROM t1 ORDER BY VEC_DISTANCE(emb, VEC_FromText('[...]')) LIMIT 10;

-- 边界测试
-- 大维度向量
-- 空向量
-- NaN/Inf 处理
-- 并发插入
```

22. **性能优化**
- 内存管理优化
- 锁粒度优化
- 缓存策略优化

---

## 关键实现要点总结

### 1. 向量存储格式
- 使用 **int16 量化** 节省存储空间
- 预计算 **abs2** 加速距离计算
- COSINE 距离使用 **归一化预处理**

### 2. HNSW 算法要点
- **层级随机生成**: `layer = floor(-ln(random) / ln(M))`
- **第0层邻居数**: 2M，其他层 M
- **启发式邻居选择**: 避免过于密集的连接
- **贪心搜索**: 从高层向下逐层逼近

### 3. 辅助表设计
- **隐藏表**: 不出现在 `SHOW TABLES` 中
- **tref 关联**: 通过主表记录位置关联
- **软删除**: DELETE 只标记 deleted=true

### 4. 事务支持
- **MHNSW_Trx**: 事务级缓存
- **提交合并**: commit 时合并到共享缓存
- **回滚丢弃**: rollback 时丢弃事务缓存

### 5. 查询优化
- **ORDER BY VEC_DISTANCE LIMIT N**: 使用向量索引
- **ef_search**: 控制搜索精度/速度平衡
- **回表**: 通过 tref 定位主表记录

---

## 参考资料

1. AliSQL 官方文档: https://developer.aliyun.com/article/1689573
2. HNSW 论文: "Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs"
3. MariaDB 向量索引实现 (MDEV-35922)
4. MySQL 8.0 Data Dictionary 设计
