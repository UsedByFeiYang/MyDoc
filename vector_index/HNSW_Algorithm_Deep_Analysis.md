# HNSW 算法深度解读

## 论文信息
**标题**: Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs  
**作者**: Yu A. Malkov, D.A. Yashunin  
**发表**: 2016年, IEEE Transactions on Pattern Analysis and Machine Intelligence

---

## 目录
1. [背景与问题定义](#背景与问题定义)
2. [核心思想](#核心思想)
3. [算法详细解读](#算法详细解读)
4. [具体示例演示](#具体示例演示)
5. [参数选择指南](#参数选择指南)
6. [实现建议](#实现建议)

---

## 背景与问题定义

### 近似最近邻搜索 (ANN)

**问题定义**: 给定一个查询向量 q，在数据集 D = {v₁, v₂, ..., vₙ} 中找到距离 q 最近的 k 个向量。

**精确搜索的问题**:
- 时间复杂度 O(n)，对于大规模数据集（百万/亿级）太慢
- 空间复杂度高，难以存储所有距离矩阵

**近似搜索的目标**:
- 在可接受的误差范围内，大幅降低搜索时间
- 时间复杂度目标: O(log n) 或更低

### NSW (Navigable Small World) 图

HNSW 的前身是 NSW 图，它是一种特殊的图结构：

```
NSW 图特点:
- 每个节点代表一个数据点
- 节点之间有边连接，边表示"相似"关系
- 图具有"小世界"特性: 平均路径长度短
- 可以通过贪心搜索快速逼近目标
```

**NSW 的问题**:
- 搜索效率随数据规模增长而下降
- 高维数据下贪心搜索容易陷入局部最优
- 无法有效处理不同尺度的距离

---

## 核心思想

### HNSW 的创新点

HNSW 通过 **分层结构** 解决 NSW 的缺陷：

```
┌─────────────────────────────────────────────────────────────┐
│                    Layer 2 (最稀疏)                          │
│  只有少数节点，长距离连接，用于快速跳跃                        │
│                                                              │
│         ●───────────────────────●                           │
│              │                   │                           │
│              │                   │                           │
└──────────────│───────────────────│───────────────────────────┘
               │                   │
┌──────────────│───────────────────│───────────────────────────┐
│              │                   │                           │
│         ●────●                   ●                           │
│    Layer 1 (中等密度)                                         │
│    更多节点，中等距离连接                                      │
│                                                              │
│         ●───●───●───●───●                                    │
└─────────────────────────────────────────────────────────────┘
               │
┌──────────────│───────────────────────────────────────────────┐
│              ●                                               │
│    Layer 0 (最密集)                                          │
│    所有节点，短距离连接，精细搜索                              │
│                                                              │
│    ●──●──●──●──●──●──●──●──●──●──●──●──●                    │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**核心思想**:
1. **分层导航**: 从高层（稀疏）开始，快速逼近目标区域
2. **逐层细化**: 每下降一层，搜索范围缩小，精度提高
3. **多尺度连接**: 高层连接跨越远距离，低层连接覆盖近距离

### 关键概念

#### 1. 层级分配 (Layer Assignment)

每个节点被分配到一个最大层级 L，该节点存在于 Layer 0 到 Layer L 的所有层中。

**层级计算公式**:
```
L = floor(-ln(random()) * mL)
```

其中:
- `random()` 是 [0, 1) 之间的均匀随机数
- `mL` 是层级因子，通常 `mL = 1/ln(M)`
- `M` 是每层的最大连接数

**示例**:
```
假设 M = 16, mL = 1/ln(16) ≈ 0.36

节点 A: random() = 0.8
  L = floor(-ln(0.8) * 0.36) = floor(0.223 * 0.36) = floor(0.08) = 0
  → 节点 A 只存在于 Layer 0

节点 B: random() = 0.1
  L = floor(-ln(0.1) * 0.36) = floor(2.303 * 0.36) = floor(0.83) = 0
  → 节点 B 只存在于 Layer 0

节点 C: random() = 0.01
  L = floor(-ln(0.01) * 0.36) = floor(4.605 * 0.36) = floor(1.66) = 1
  → 节点 C 存在于 Layer 0 和 Layer 1

节点 D: random() = 0.001
  L = floor(-ln(0.001) * 0.36) = floor(6.908 * 0.36) = floor(2.49) = 2
  → 节点 D 存在于 Layer 0, 1, 2
```

**层级分布特点**:
- 约 1/mL 的节点存在于 Layer 1
- 约 1/mL² 的节点存在于 Layer 2
- 大多数节点只存在于 Layer 0
- 高层节点稀疏，适合快速跳跃

#### 2. 连接数限制 (Connection Limits)

每层的最大连接数:
- **Layer 0**: M₀ = 2M (双倍，因为需要更密集的连接)
- **其他层**: Mₗ = M

**示例** (M = 16):
```
Layer 0: 每个节点最多 32 个邻居
Layer 1: 每个节点最多 16 个邻居
Layer 2: 个节点最多 16 个邻居
...
```

#### 3. 入口节点 (Entry Point)

搜索从最高层的某个节点开始，这个节点称为入口节点 (ep)。

通常选择:
- 插入第一个节点时，该节点成为入口节点
- 后续如果插入节点的层级更高，更新入口节点

---

## 算法详细解读

### 算法 1: INSERT (插入节点)

```
算法: INSERT(hnsw, q, M, M₀, mL, ef)
输入: 
  hnsw - HNSW 图结构
  q - 待插入的新节点（向量）
  M - 每层最大连接数（Layer 0 除外）
  M₀ - Layer 0 最大连接数 (= 2M)
  mL - 层级因子 (= 1/ln(M))
  ef - 构建时的动态候选列表大小

输出: 无（更新 hnsw 结构）

步骤:
1. 计算新节点的层级
   L = floor(-ln(unif(0,1)) * mL)
   
2. 获取入口节点
   ep = get_entry_point(hnsw)
   
3. 如果图为空（首次插入）
   set_entry_point(hnsw, q, L)
   return
   
4. 从最高层向下搜索到 L+1 层
   // 目的: 找到每层的最近节点作为下一层的入口
   for lc from Lmax down to L+1:
     W = SEARCH_LAYER(q, ep, ef=1, lc)  // 只找最近的1个
     ep = nearest element from W
     
5. 从 L 层向下搜索并建立连接
   for lc from L down to 0:
     W = SEARCH_LAYER(q, ep, ef, lc)
     // 选择邻居
     neighbors = SELECT_NEIGHBORS(q, W, Mₗ)
     
     // 建立双向连接
     for neighbor in neighbors:
       add_bidirectional_connection(q, neighbor, lc)
       
       // 如果邻居连接数超限，需要裁剪
       if neighbor.connections[lc].size > Mₗ:
         new_conn = SELECT_NEIGHBORS(neighbor, 
                                     neighbor.connections[lc], 
                                     Mₗ)
         neighbor.connections[lc] = new_conn
     
     // 设置下一层的入口
     ep = W
     
6. 如果新节点层级 > 当前最高层
   set_entry_point(hnsw, q, L)
```

### 算法 2: SEARCH_LAYER (层级搜索)

```
算法: SEARCH_LAYER(q, ep, ef, lc)
输入:
  q - 查询向量
  ep - 入口点集合
  ef - 返回的最近邻数量
  lc - 当前层级

输出:
  W - ef 个最近邻节点集合

步骤:
1. 初始化
   V = empty set  // 已访问集合
   C = empty priority queue (max heap)  // 候选队列（按距离排序）
   W = empty priority queue (min heap)  // 结果队列（按距离排序，保留最近的）
   
   for e in ep:
     d = distance(q, e)
     C.push(e, d)  // 候选队列：距离大的在顶部（便于取出最远的）
     W.push(e, d)  // 结果队列：距离小的在顶部（便于检查最远的）
     V.add(e)
     
2. 贪心搜索
   while C is not empty:
     // 取出候选中距离查询最近的节点
     c = C.pop_nearest()
     
     // 取出结果中距离查询最远的节点
     f = W.pop_furthest()
     
     // 如果候选最近点比结果最远点还远，搜索结束
     if distance(q, c) > distance(q, f):
       break  // 所有候选都比结果中最差的更差，无需继续
       
     // 遍历 c 的邻居
     for e in c.connections[lc]:
       if e not in V:
         V.add(e)
         d = distance(q, e)
         
         // 如果比结果中最差的更近，加入候选和结果
         f = W.furthest()
         if d < distance(q, f) or W.size < ef:
           C.push(e, d)
           W.push(e, d)
           
           // 如果结果超过 ef，移除最远的
           if W.size > ef:
             W.pop_furthest()
             
3. 返回结果
   return W  // 包含 ef 个最近邻
```

**关键理解**:
- `C` 是候选队列，存储待探索的节点
- `W` 是结果队列，存储已找到的最近邻
- 搜索终止条件: 候选中最近的节点比结果中最远的节点还远

### 算法 3: SELECT_NEIGHBORS (邻居选择)

论文提出了两种邻居选择策略：

#### 简单选择 (SELECT-NEIGHBORS-SIMPLE)

```
算法: SELECT_NEIGHBORS_SIMPLE(q, C, M)
输入:
  q - 目标节点
  C - 候选集合
  M - 最大邻居数

输出:
  R - 选出的 M 个最近邻居

步骤:
1. 从 C 中选择距离 q 最近的 M 个节点
   R = M nearest elements from C
   return R
```

#### 启发式选择 (SELECT-NEIGHBORS-HEURISTIC)

```
算法: SELECT_NEIGHBORS_HEURISTIC(q, C, M, lc, extendCandidates, keepPruned)
输入:
  q - 目标节点
  C - 候选集合
  M - 最大邻居数
  lc - 当前层级
  extendCandidates - 是否扩展候选（建议 true）
  keepPruned - 是否保留被裁剪的候选（建议 true）

输出:
  R - 选出的邻居集合

步骤:
1. 初始化
   R = empty
   W = C sorted by distance to q
   
2. 如果 extendCandidates = true
   // 扩展候选：加入候选的邻居
   for e in C:
     for e_adj in e.connections[lc]:
       if e_adj not in W:
         d = distance(q, e_adj)
         W.push(e_adj, d)
         
3. 启发式选择
   while W is not empty and R.size < M:
     e = W.pop_nearest()
     
     // 检查 e 是否与已选邻居过于接近
     good = true
     for r in R:
       if distance(e, r) < distance(q, e):
         good = false
         break  // e 与某个已选邻居的距离比到 q 更近，跳过
         
     if good:
       R.add(e)
       
4. 如果 keepPruned = true 且 R.size < M
   // 补充被裁剪的候选
   while W is not empty and R.size < M:
     R.add(W.pop_nearest())
     
5. return R
```

**启发式选择的核心思想**:

```
问题: 简单选择可能导致邻居之间过于相似

示例:
假设 q = [1, 1]
候选: 
  a = [1.1, 1.1]  距离 q: 0.14
  b = [1.2, 1.2]  距离 q: 0.28
  c = [1.05, 1.05] 距离 q: 0.07

简单选择 (M=2): 选 c 和 a
  但 c 和 a 距离只有 0.07，非常接近
  如果从 c 出发，很可能直接跳到 a，搜索路径冗余

启发式选择:
  先选 c (最近的)
  检查 a: distance(a, c) = 0.07 < distance(a, q) = 0.14
    → a 与 c 太接近，跳过
  检查 b: distance(b, c) = 0.21 > distance(b, q) = 0.28
    → b 与 c 不太接近，选 b
  最终选 c 和 b，邻居分布更均匀
```

### 算法 4: K-NN SEARCH (搜索)

```
算法: KNN_SEARCH(hnsw, q, K, ef)
输入:
  hnsw - HNSW 图结构
  q - 查询向量
  K - 返回的最近邻数量
  ef - 搜索时的动态候选列表大小

输出:
  W - K 个最近邻节点集合

步骤:
1. 获取入口节点
   ep = get_entry_point(hnsw)
   Lmax = ep.layer
   
2. 从最高层向下搜索
   for lc from Lmax down to 1:
     W = SEARCH_LAYER(q, ep, ef=1, lc)
     ep = nearest element from W
     
3. 在 Layer 0 搜索
   W = SEARCH_LAYER(q, ep, ef, lc=0)
   
4. 返回最近的 K 个
   return K nearest elements from W
```

**关键参数 ef**:
- ef 越大，搜索越精确，但速度越慢
- ef ≥ K，否则可能找不到 K 个结果
- 论文建议: ef = K 时召回率约 70-80%，ef = 2K 时召回率约 90-95%

---

## 具体示例演示

### 示例 1: 构建过程

假设我们有 5 个二维向量，按顺序插入：

```
数据点:
  A = (0, 0)
  B = (1, 0)
  C = (0, 1)
  D = (1, 1)
  E = (0.5, 0.5)

参数: M = 4, M₀ = 8, mL = 0.36
```

#### 步骤 1: 插入 A

```
1. 计算层级
   random() = 0.01
   L = floor(-ln(0.01) * 0.36) = floor(1.66) = 1
   
   A 存在于 Layer 0 和 Layer 1

2. 图为空，设置入口节点
   ep = A
   Lmax = 1
   
3. A 没有邻居（首个节点）

图状态:
  Layer 1: A (无邻居)
  Layer 0: A (无邻居)
```

#### 步骤 2: 插入 B

```
1. 计算层级
   random() = 0.5
   L = floor(-ln(0.5) * 0.36) = floor(0.25) = 0
   
   B 只存在于 Layer 0

2. 从 Layer 1 搜索到 Layer 0+1
   lc = 1: SEARCH_LAYER(B, ep=A, ef=1, lc=1)
     W = {A}
     ep = A
     
3. 在 Layer 0 搜索并建立连接
   lc = 0: SEARCH_LAYER(B, ep=A, ef=10, lc=0)
     W = {A}  (A 是唯一节点)
     
   neighbors = SELECT_NEIGHBORS(B, {A}, M₀=8)
     = {A}
     
   建立连接:
     B.neighbors[0] = {A}
     A.neighbors[0] = {B}  (A 原本无邻居)

图状态:
  Layer 1: A (无邻居)
  Layer 0: A ←→ B
```

#### 步骤 3: 插入 C

```
1. 计算层级
   random() = 0.6
   L = floor(-ln(0.6) * 0.36) = floor(0.15) = 0
   
   C 只存在于 Layer 0

2. 从 Layer 1 搜索
   lc = 1: SEARCH_LAYER(C, ep=A, ef=1, lc=1)
     W = {A}
     ep = A
     
3. 在 Layer 0 搜索并建立连接
   lc = 0: SEARCH_LAYER(C, ep=A, ef=10, lc=0)
     从 A 开始，A 的邻居是 B
     访问 A: d(C, A) = 1.0
     访问 B: d(C, B) = √2 ≈ 1.41
     W = {A, B}  (按距离排序)
     
   neighbors = SELECT_NEIGHBORS(C, {A, B}, M₀=8)
     = {A, B}  (都选，因为邻居数未超限)
     
   建立连接:
     C.neighbors[0] = {A, B}
     A.neighbors[0] = {B, C}
     B.neighbors[0] = {A, C}

图状态:
  Layer 1: A (无邻居)
  Layer 0: 
    A ←→ B, A ←→ C
    B ←→ C
    (三角形连接)
```

#### 步骤 4: 插入 D

```
1. 计算层级
   random() = 0.7
   L = floor(-ln(0.7) * 0.36) = floor(0.11) = 0
   
   D 只存在于 Layer 0

2. 从 Layer 1 搜索
   lc = 1: SEARCH_LAYER(D, ep=A, ef=1, lc=1)
     W = {A}
     ep = A
     
3. 在 Layer 0 搜索并建立连接
   lc = 0: SEARCH_LAYER(D, ep=A, ef=10, lc=0)
     从 A 开始
     访问 A: d(D, A) = √2 ≈ 1.41
     访问 B: d(D, B) = 1.0
     访问 C: d(D, C) = 1.0
     W = {B, C, A}  (按距离排序)
     
   neighbors = SELECT_NEIGHBORS(D, {B, C, A}, M₀=8)
     = {B, C, A}
     
   建立连接:
     D.neighbors[0] = {B, C, A}
     B.neighbors[0] = {A, C, D}
     C.neighbors[0] = {A, B, D}
     A.neighbors[0] = {B, C, D}

图状态:
  Layer 1: A (无邻居)
  Layer 0: 四个节点全连接
```

#### 步骤 5: 插入 E (中心点)

```
1. 计算层级
   random() = 0.02
   L = floor(-ln(0.02) * 0.36) = floor(1.39) = 1
   
   E 存在于 Layer 0 和 Layer 1

2. 从 Layer 1 搜索到 Layer 1+1 = 2
   当前最高层 Lmax = 1
   无需搜索更高层
   
3. 在 Layer 1 搜索并建立连接
   lc = 1: SEARCH_LAYER(E, ep=A, ef=10, lc=1)
     W = {A}  (只有 A 在 Layer 1)
     
   neighbors = SELECT_NEIGHBORS(E, {A}, M=4)
     = {A}
     
   建立连接:
     E.neighbors[1] = {A}
     A.neighbors[1] = {E}  (A 原本无 Layer 1 邻居)

4. 在 Layer 0 搜索并建立连接
   lc = 0: SEARCH_LAYER(E, ep=A, ef=10, lc=0)
     从 A 开始，遍历 A 的邻居
     访问 A: d(E, A) = 0.71
     访问 B: d(E, B) = 0.71
     访问 C: d(E, C) = 0.71
     访问 D: d(E, D) = 0.71
     W = {A, B, C, D}  (距离相同)
     
   neighbors = SELECT_NEIGHBORS(E, {A, B, C, D}, M₀=8)
     = {A, B, C, D}
     
   建立连接:
     E.neighbors[0] = {A, B, C, D}
     A.neighbors[0] = {B, C, D, E}
     B.neighbors[0] = {A, C, D, E}
     C.neighbors[0] = {A, B, D, E}
     D.neighbors[0] = {A, B, C, E}

5. 更新入口节点（E 的层级 = 1，与 A 相同）
   不需要更新

最终图状态:
  Layer 1: A ←→ E
  Layer 0: 五个节点全连接
```

### 示例 2: 搜索过程

假设查询 q = (0.3, 0.3)，找最近的 2 个节点 (K=2, ef=5)

```
1. 从入口节点开始
   ep = A 或 E (假设是 E，因为 E 在 Layer 1)
   Lmax = 1
   
2. Layer 1 搜索
   SEARCH_LAYER(q, ep=E, ef=1, lc=1)
   
   初始化:
     V = {E}
     C = {E}  (d(q, E) = 0.28)
     W = {E}
     
   搜索:
     取出 E，遍历 E 的 Layer 1 邻居 {A}
     d(q, A) = 0.42
     加入候选和结果
     
   结果: W = {E, A}
   ep = E (最近的)
   
3. Layer 0 搜索
   SEARCH_LAYER(q, ep=E, ef=5, lc=0)
   
   初始化:
     V = {E}
     C = {E}  (d(q, E) = 0.28)
     W = {E}
     
   搜索过程:
     第1轮: 取出 E
       邻居: {A, B, C, D}
       d(q, A) = 0.42 → 加入
       d(q, B) = 0.72 → 加入
       d(q, C) = 0.72 → 加入
       d(q, D) = 1.01 → 加入
       W = {E, A, B, C, D}  (已满 5 个)
       
     第2轮: 取出 A (候选中最近的)
       邻居: {B, C, D, E} (都已访问)
       无新候选
       
     第3轮: 取出 B
       邻居: {A, C, D, E} (都已访问)
       无新候选
       
     ... 类似 C, D
     
   最终: W = {E, A, B, C, D}
   
4. 返回最近的 K=2 个
   结果: {E, A}
   
   实际距离:
     d(q, E) = 0.28
     d(q, A) = 0.42
```

### 示例 3: 启发式邻居选择

假设目标节点 q = (5, 5)，候选集合:

```
候选:
  a = (5.1, 5.1)  d(q, a) = 0.14
  b = (5.2, 5.2)  d(q, b) = 0.28
  c = (6, 5)      d(q, c) = 1.0
  d = (5, 6)      d(q, d) = 1.0
  e = (7, 7)      d(q, e) = 2.83

M = 3 (最多选 3 个邻居)
```

#### 简单选择结果:
```
选最近的 3 个: {a, b, c}
问题: a 和 b 距离只有 0.14，非常接近
      从 a 出发很容易直接跳到 b，路径冗余
```

#### 启发式选择过程:
```
1. W = {a, b, c, d, e} (按距离排序)

2. 选择 a
   R = {a}
   
3. 检查 b
   d(b, a) = 0.14 < d(b, q) = 0.28
   → b 与 a 太接近，跳过
   
4. 检查 c
   d(c, a) = 1.14 > d(c, q) = 1.0
   → c 与 a 不太接近，选 c
   R = {a, c}
   
5. 检查 d
   d(d, a) = 1.14 > d(d, q) = 1.0
   d(d, c) = 1.41 > d(d, q) = 1.0
   → d 与 a, c 都不太接近，选 d
   R = {a, c, d}
   
6. R 已满 (M=3)，结束

结果: {a, c, d}
邻居分布更均匀，覆盖不同方向
```

---

## 参数选择指南

### M (每层最大连接数)

```
推荐值: 16-48

影响:
- M 越大，图连通性越好，搜索精度越高
- M 越大，内存占用越大，插入速度越慢
- M 太小可能导致图不连通，搜索失败

经验:
- 低维数据 (d < 100): M = 16
- 中维数据 (d = 100-500): M = 24-32
- 高维数据 (d > 500): M = 48
```

### ef_construction (构建时的 ef)

```
推荐值: 100-200

影响:
- ef 越大，构建时搜索越精确，图质量越好
- ef 越大，构建时间越长

经验:
- ef_construction = 2M 是较好的起点
- 追求高质量图: ef_construction = 200-400
```

### ef_search (搜索时的 ef)

```
推荐值: 根据召回率需求调整

召回率 vs ef:
- ef = K: 召回率约 70-80%
- ef = 2K: 召回率约 90-95%
- ef = 4K: 召回率约 98-99%

经验:
- 先用 ef = K 测试召回率
- 如果召回率不够，逐步增加 ef
```

### mL (层级因子)

```
推荐值: 1/ln(M)

影响:
- mL 越大，高层节点越多，搜索跳跃更快
- mL 太大可能导致高层过于密集，失去分层优势

经验:
- 使用论文推荐值 mL = 1/ln(M)
- 不建议调整
```

---

## 实现建议

### 数据结构设计

```cpp
// 节点结构
struct Node {
  int id;                    // 节点 ID
  float* vector;             // 向量数据
  int max_layer;             // 该节点的最大层级
  vector<vector<int>> neighbors;  // 每层的邻居列表
};

// HNSW 图结构
struct HNSW {
  int M;                     // 每层最大连接数
  int M0;                    // Layer 0 最大连接数 (= 2M)
  float mL;                  // 层级因子
  int max_layer;             // 图的最高层级
  int entry_point;           // 入口节点 ID
  unordered_map<int, Node> nodes;  // 所有节点
};
```

### 关键实现要点

1. **距离计算优化**
```cpp
// 使用 SIMD 加速
float distance_simd(float* v1, float* v2, int dim) {
  // AVX2 实现
  __m256 sum = _mm256_setzero_ps();
  for (int i = 0; i < dim; i += 8) {
    __m256 a = _mm256_loadu_ps(v1 + i);
    __m256 b = _mm256_loadu_ps(v2 + i);
    __m256 diff = _mm256_sub_ps(a, b);
    sum = _mm256_fmadd_ps(diff, diff, sum);  // diff * diff + sum
  }
  // 横向求和
  float result[8];
  _mm256_storeu_ps(result, sum);
  return sqrt(result[0] + result[1] + ... + result[7]);
}
```

2. **优先队列实现**
```cpp
// 候选队列: 最小堆（距离小的在顶部）
struct CandidateQueue {
  priority_queue<pair<float, int>, 
                 vector<pair<float, int>>,
                 greater<pair<float, int>>> pq;
  
  void push(int node, float dist) { pq.push({dist, node}); }
  int pop_nearest() { return pq.top().second; pq.pop(); }
  bool empty() { return pq.empty(); }
};

// 结果队列: 最大堆（距离大的在顶部，便于检查最远的）
struct ResultQueue {
  priority_queue<pair<float, int>> pq;
  int ef;
  
  void push(int node, float dist) {
    pq.push({dist, node});
    if (pq.size() > ef) pq.pop();  // 移除最远的
  }
  int furthest() { return pq.top().second; }
  float furthest_dist() { return pq.top().first; }
};
```

3. **并发控制**
```cpp
// 插入时使用锁
mutex node_locks[MAX_NODES];  // 每个节点一个锁

void add_connection(int node1, int node2, int layer) {
  // 先锁 ID 小的，避免死锁
  if (node1 < node2) {
    node_locks[node1].lock();
    node_locks[node2].lock();
  } else {
    node_locks[node2].lock();
    node_locks[node1].lock();
  }
  
  nodes[node1].neighbors[layer].push_back(node2);
  nodes[node2].neighbors[layer].push_back(node1);
  
  node_locks[node1].unlock();
  node_locks[node2].unlock();
}
```

4. **内存优化**
```cpp
// 邻居列表使用固定大小数组
struct Node {
  int neighbors[MAX_LAYER][2*M];  // 预分配
  int neighbor_count[MAX_LAYER];  // 实际数量
};

// 或者使用压缩存储
struct Node {
  uint16_t* neighbors;  // 假设节点 ID < 65536
  uint8_t neighbor_count[MAX_LAYER];
};
```

### 性能优化建议

1. **批量插入**: 先收集所有节点，再批量构建图
2. **预计算距离**: 对于静态数据，预计算常用距离
3. **缓存友好**: 将邻居列表连续存储
4. **并行搜索**: 多线程同时搜索不同路径

---

## 总结

HNSW 算法的核心优势:

1. **分层导航**: 高层快速跳跃，低层精细搜索
2. **启发式邻居选择**: 避免冗余连接，提高搜索效率
3. **动态候选列表**: ef 参数平衡精度和速度
4. **增量构建**: 支持动态插入新节点

适用场景:
- 大规模向量搜索（百万级以上）
- 高维数据（100-1000 维）
- 需要高召回率的近似搜索
- 动态更新的数据集

---

## 参考资料

1. 论文原文: "Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs"
2. AliSQL 实现: 见 AliSQL_Vector_Index_Analysis.md
3. faiss 库实现: https://github.com/facebookresearch/faiss
4. hnswlib 实现: https://github.com/nmslib/hnswlib