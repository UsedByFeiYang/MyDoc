# `calc_row_difference`、Undo 与虚拟列回滚实现笔记

本文以 MySQL Bug #120176 的复现用例为主线，串起来看下面几件事：

- `ha_innobase::update_row()` 如何进入 InnoDB 更新路径
- `calc_row_difference()` 如何判断哪些列“真的改了”
- update undo 里到底记录了什么
- 有虚拟列时，为什么除了“更新字段”还要额外记录旧虚拟列值
- statement rollback 时，InnoDB 如何根据 undo 重建旧的二级索引记录
- Bug #120176 为什么会在回滚阶段崩溃

## 1. 复现用例

```sql
set sql_mode="";
create database test;
use test;

create table t18 (
  c5 int not null,
  primary key (c5)
) engine=InnoDB;

create table t21 (
  c1 int not null,
  c2 int default null,
  key i15 ((c2 div 1), (floor(c1))) using btree,
  constraint t21_ibfk_1 foreign key (c2)
    references t18 (c5) on update set null
) engine=InnoDB;

insert into t18(c5) values (1), (2);
insert into t21(c2) values (1);

update t18 set c5 = 3;
```

这个 `UPDATE` 没有 `WHERE`，因此会依次尝试：

1. 把 `t18.c5 = 1` 更新成 `3`
2. 把 `t18.c5 = 2` 更新成 `3`

第二步会撞上主键冲突，因此整条语句必须回滚。

子表 `t21` 因为外键 `ON UPDATE SET NULL`，在第一步父表更新时会发生级联更新：

```text
t21: (c1=0, c2=1)  ->  (c1=0, c2=NULL)
```

而这正好会影响虚拟索引 `i15`。

## 2. 例子里的“索引可见值”

`t21` 上的虚拟索引表达式有两个：

- `vcol_0 = c2 DIV 1`
- `vcol_1 = FLOOR(c1)`

这条子记录在不同阶段的值如下：

| 阶段 | `c1` | `c2` | `vcol_0 = c2 DIV 1` | `vcol_1 = FLOOR(c1)` |
| --- | --- | --- | --- | --- |
| 语句开始前 | `0` | `1` | `1` | `0` |
| 级联更新后 | `0` | `NULL` | `NULL` | `0` |
| 语句回滚后应恢复到 | `0` | `1` | `1` | `0` |

关键点在于：

- `vcol_0` 确实变了
- `vcol_1` 没变
- 但是 `vcol_1` 仍然属于同一个虚拟索引键的一部分

因此 rollback 重建旧索引项时，`vcol_1` 仍然必须是可用的。

## 3. 正向更新主链路

核心调用链可以先记成这样：

```text
SQL UPDATE
  -> ha_innobase::update_row()
     -> calc_row_difference()
        -> 生成 upd_t / old_vrow / old_v_val
     -> row_update_for_mysql()
        -> 更新 clustered index / secondary index
        -> 写 update undo
```

几个关键入口：

- `storage/innobase/handler/ha_innodb.cc`
  - `ha_innobase::update_row()`
  - `calc_row_difference()`
- `storage/innobase/row/row0mysql.cc`
  - `row_update_for_mysql()`
- `storage/innobase/trx/trx0rec.cc`
  - `trx_undo_report_row_operation()`

## 4. `calc_row_difference()` 的职责

`calc_row_difference()` 的工作不是“简单比较两个 MySQL row buffer 是否不同”，而是：

1. 逐列遍历 old/new row
2. 按列类型把值整理成可比较的形式
3. 判定哪些列真的改了
4. 把这些变化写入 InnoDB 的 update vector `upd_t`
5. 如果表里有已索引虚拟列，把回滚和索引维护所需的旧虚拟列值也准备好

函数位置：

- `storage/innobase/handler/ha_innodb.cc`

### 4.1 它怎么判断“这一列改了”

判断条件本身很直接：

```cpp
if (o_len != n_len || (o_len != UNIV_SQL_NULL && o_len != 0 &&
                       0 != memcmp(o_ptr, n_ptr, o_len))) {
```

也就是：

- 长度不同，算修改
- 长度相同但内容不同，算修改
- 两边都是 `NULL`，不算修改
- 两边都是 0 长度，不再做 `memcmp`

但在比较前，它会先按类型处理：

- `BLOB/GEOMETRY/POINT`：先解引用到真实 payload
- `true VARCHAR`：先读出真实长度和内容
- nullable 列：先转成 `UNIV_SQL_NULL` 语义
- multi-value 虚拟列：先解析出多值，再做差异计算

所以它比较的不是“原始行内存”，而是“按 InnoDB 语义整理后的列值”。

### 4.2 普通列和虚拟列有何不同

普通列：

- 如果变了，就往 `uvect->fields[]` 里放一个 `upd_field_t`
- `new_val` 记录新值
- `field_no` 记录它在 clustered index 里的列位置

虚拟列：

- 只有“已索引虚拟列”或者 online DDL 正在物化的虚拟列才参与后续维护
- 如果变了，会在 `upd_field_t` 里记录：
  - `new_val`
  - `old_v_val`
- 即使没变，只要它属于 indexed virtual column，通常也会进入 `uvect->old_vrow`

这就是理解后续 undo/rollback 的关键。

## 5. `old_v_val` 和 `old_vrow` 的区别

这两个名字很像，但职责不同。

### 5.1 `old_v_val`

`old_v_val` 是“单个已更新虚拟列”的旧值。

它挂在某个 `upd_field_t` 上，按“字段”粒度保存。

以本例为例，`vcol_0` 发生了变化：

```text
vcol_0: 1 -> NULL
```

那么对应的 `upd_field_t` 里会包含：

```text
new_val   = NULL
old_v_val = 1
```

### 5.2 `old_vrow`

`old_vrow` 是“整行已索引虚拟列的旧视图”。

它不是只记录更新过的虚拟列，而是记录 rollback 或索引维护所需的所有旧虚拟列值。

在本例里，`old_vrow` 里应该至少有：

```text
old_vrow[vcol_0] = 1
old_vrow[vcol_1] = 0
```

注意：

- `vcol_0` 是“已更新的虚拟列”
- `vcol_1` 是“未更新但仍属于虚拟索引键”的虚拟列

这两者都可能在回滚构造旧二级索引记录时被需要。

### 5.3 一张图看懂

```text
upd_t
├─ fields[]
│  ├─ c2         -> new_val = NULL
│  └─ vcol_0     -> new_val = NULL
│                 -> old_v_val = 1
└─ old_vrow
   ├─ vcol_0 = 1
   └─ vcol_1 = 0
```

一句话概括：

- `old_v_val`：给“更新过的虚拟列”用
- `old_vrow`：给“整条记录的旧虚拟列视图”用

## 6. update undo 里记录了什么

update undo 不是整行镜像，而是一份“足够恢复旧版本”的差量数据。

可以把它概括成：

```text
undo record
├─ general header
├─ old trx_id / old roll_ptr / info_bits
├─ row reference
├─ update vector
│  ├─ 普通列的 new_val
│  ├─ 虚拟列的 new_val
│  └─ 虚拟列的 old_v_val
└─ unchanged but indexed virtual columns
   └─ 从 update->old_vrow 额外写入
```

本例里，undo 至少需要表达出下面这些信息：

| 类别 | 记录的值 |
| --- | --- |
| 普通列更新 | `c2.new_val = NULL` |
| 虚拟列更新 | `vcol_0.new_val = NULL` |
| 虚拟列旧值 | `vcol_0.old_v_val = 1` |
| 额外虚拟列旧值 | `vcol_1 = 0` |
| 系统列 | `trx_id`、`roll_ptr` |

### 6.1 为什么光有 `old_v_val` 还不够

因为 rollback 构造旧 secondary index entry 时，需要的是“完整索引键”。

本例中的旧索引键是：

```text
(c2 DIV 1, FLOOR(c1)) = (1, 0)
```

如果 undo 里只有：

```text
vcol_0.old_v_val = 1
```

却没有：

```text
vcol_1 = 0
```

那么回滚路径就没法完整重建旧索引项。

## 7. rollback 主链路

回滚可以先记成这条链：

```text
statement rollback
  -> row_undo_mod()
     -> row_undo_mod_parse_undo_rec()
        -> trx_undo_update_rec_get_update()
        -> row_upd_replace_vcol()
     -> row_undo_mod_upd_exist_sec()
        -> row_build_index_entry(node->row)
        -> row_build_index_entry(node->undo_row)
     -> row_undo_mod_clust()
```

重点是：secondary index 的回滚发生在 clustered index 回滚之前。

## 8. rollback 时虚拟列怎么补回去

`row_upd_replace_vcol()` 会从两个地方回填虚拟列：

### 8.1 已更新的虚拟列：从 `old_v_val` 回填

对于本次真的更新过的虚拟列，回滚时直接用 `upd_field->old_v_val`。

本例中：

```text
vcol_0 <- old_v_val = 1
```

### 8.2 未更新但已索引的虚拟列：从 undo 额外区回填

对于没有出现在 `fields[]` 里的 indexed virtual column，`row_upd_replace_vcol()`
会继续读取 undo record 后半段保存的“unchanged but indexed virtual columns”。

本例中：

```text
vcol_1 <- undo extra vcol data = 0
```

### 8.3 一张图看懂回填过程

```text
undo record
├─ update fields
│  └─ vcol_0.old_v_val = 1
└─ extra indexed vcols
   └─ vcol_1 = 0

row_upd_replace_vcol()
├─ 先回填更新过的虚拟列
│  └─ vcol_0 <- 1
└─ 再回填未更新但索引需要的虚拟列
   └─ vcol_1 <- 0
```

补完以后，回滚路径应该得到一份可用于构造旧索引项的虚拟列视图：

```text
undo_row virtual view
├─ vcol_0 = 1
└─ vcol_1 = 0
```

## 9. 二级索引回滚时到底在做什么

`row_undo_mod_upd_exist_sec()` 会对每个相关 secondary index 做两件事：

1. 基于“当前版本行”构造当前索引项，删除它或撤销其状态
2. 基于“旧版本行”构造旧索引项，重新插回去或去掉 delete-mark

可以抽象成：

```text
secondary index rollback
├─ current entry = row_build_index_entry(node->row)
│  └─ 表示语句失败前那份“新版本”的索引项
└─ old entry = row_build_index_entry(node->undo_row)
   └─ 表示应该恢复回去的“旧版本”索引项
```

在本例里，旧索引项必须是：

```text
(c2 DIV 1, FLOOR(c1)) = (1, 0)
```

只有这样，rollback 才能正确恢复原来的 `i15` 记录。

## 10. Bug #120176 为什么会崩

问题的本质不是“更新过的虚拟列没记住”，而是：

> 同一个虚拟索引里，那些未变化但仍然参与索引键构造的虚拟列，
> 没有被完整地带到 rollback 所需的数据里。

对应到本例：

- `vcol_0 = c2 DIV 1` 确实受级联更新影响
- `vcol_1 = FLOOR(c1)` 没有变化
- 但 rollback 恢复旧 `i15` 记录时，`vcol_1` 仍然必须是 `0`

如果 cascade update 路径只准备了“直接受 FK 影响的虚拟列”，而没有把
整套 indexed virtual columns 都物化出来，那么 rollback 构造 `undo_row`
时就会不完整。

后果就是：

1. `row_build_index_entry(node->undo_row, ...)` 构造出的旧索引项不对
2. 二级索引回滚时找不到应该恢复的记录
3. 最终在更深层的 record/index 处理里触发断言或崩溃

## 11. 为什么修复点会落在 `row0ins.cc`

虽然崩溃发生在 rollback 阶段，但 bug 根因出在更前面的“级联更新准备阶段”。

修复思路是：

- 不再只物化“外键直接影响到的虚拟列”
- 而是把所有 rollback / 索引维护需要的 indexed virtual columns 都准备好

这样在后续：

- `calc_row_difference()`
- undo 写入
- rollback 解析
- `row_upd_replace_vcol()`
- `row_build_index_entry()`

这整条链上，都会拿到足够完整的数据。

## 12. 一页速记图

```text
父表 UPDATE
  -> 子表 FK ON UPDATE SET NULL
     -> child row: (c1=0, c2=1) -> (c1=0, c2=NULL)
     -> 虚拟索引键: (1,0) -> (NULL,0)
     -> calc_row_difference()
        ├─ fields[]:
        │  ├─ c2.new_val = NULL
        │  └─ vcol_0.new_val = NULL
        │     vcol_0.old_v_val = 1
        └─ old_vrow:
           ├─ vcol_0 = 1
           └─ vcol_1 = 0
     -> 写 update undo
        ├─ updated fields
        └─ extra indexed virtual cols
  -> 父表第二行更新撞主键
  -> statement rollback
     -> 读 undo
     -> row_upd_replace_vcol()
        ├─ vcol_0 <- old_v_val
        └─ vcol_1 <- extra undo vcol data
     -> row_build_index_entry(node->undo_row)
        -> 重建旧索引键 (1,0)
     -> rollback 成功
```

## 13. 调试建议

如果想跟一次完整数据流，推荐断点按这个顺序下：

1. `ha_innobase::update_row()`
2. `calc_row_difference()`
3. `trx_undo_report_row_operation()`
4. `trx_undo_update_rec_get_update()`
5. `row_upd_replace_vcol()`
6. `row_undo_mod_upd_exist_sec()`
7. `row_build_index_entry_low()`

调试时重点观察：

- `uvect->fields[]`
- `uvect->old_vrow`
- `upd_field->old_v_val`
- `node->row`
- `node->undo_row`
- 构造 `i15` 索引项时两个虚拟表达式的实际值

## 14. 最后的理解方式

如果只记一句话，可以记这个：

> `calc_row_difference()` 不只是找出“哪些列被更新了”，
> 它还要为后续 secondary index 维护和 undo rollback 准备一份“足够重建旧索引键”的数据。

而在有虚拟列索引时，这份“足够的数据”不仅包括更新过的虚拟列旧值，
还包括那些没有变化、但仍然属于虚拟索引键一部分的虚拟列旧值。

## 15. 更完整的时序图

下面这张图把“父表更新触发子表级联更新，随后语句失败进入 rollback”的关键阶段串在一起。

```text
+------------------+      +--------------------+      +--------------------+
| SQL layer        |      | InnoDB update path |      | InnoDB rollback    |
+------------------+      +--------------------+      +--------------------+
         |                            |                           |
         | UPDATE t18 SET c5 = 3      |                           |
         |--------------------------->|                           |
         |                            | update parent row 1->3    |
         |                            |-------------------------->|
         |                            | cascade child c2: 1->NULL |
         |                            | calc_row_difference()     |
         |                            |   fields[]                |
         |                            |   old_v_val               |
         |                            |   old_vrow                |
         |                            | write update undo         |
         |                            |-------------------------->|
         |                            | continue parent row 2->3  |
         |                            | duplicate key on PRIMARY  |
         |<---------------------------|                           |
         | statement must rollback    |                           |
         |--------------------------->|                           |
         |                            | parse undo                |
         |                            | trx_undo_update_rec_get_update()
         |                            | row_upd_replace_vcol()    |
         |                            |   vcol_0 <- old_v_val     |
         |                            |   vcol_1 <- undo extra    |
         |                            | row_undo_mod_upd_exist_sec()
         |                            | row_build_index_entry()   |
         |                            | rebuild old key (1,0)     |
         |                            | row_undo_mod_clust()      |
         |<---------------------------| rollback complete         |
```

如果只盯本例的 child row，可以把状态变化压缩成下面这一条：

```text
(c1=0, c2=1,  vkey=(1,0))
   -> cascade update
(c1=0, c2=NULL, vkey=(NULL,0))
   -> duplicate key on parent
   -> statement rollback
(c1=0, c2=1,  vkey=(1,0))
```

## 16. 按断点顺序的调试手册

下面这节的目标不是覆盖所有细节，而是告诉你“每一站最值得看什么变量”。

### 16.1 断点 1：`ha_innobase::update_row()`

目标：

- 确认现在处理的是哪一条 child row
- 确认 `old_row` 和 `new_row` 已经进入 handler 层

建议观察：

- `old_row`
- `new_row`
- `m_prebuilt->table->name`
- `uvect`

你在这里主要是确认：

```text
old_row: c1=0, c2=1
new_row: c1=0, c2=NULL
```

### 16.2 断点 2：`calc_row_difference()`

目标：

- 看哪些列被判定为“已修改”
- 看虚拟列旧值是如何进入 `old_v_val` / `old_vrow` 的

建议观察：

- `i`
- `field->field_name`
- `is_virtual`
- `o_len`
- `n_len`
- `ufield->new_val`
- `ufield->old_v_val`
- `uvect->old_vrow`
- `n_changed`
- `num_v`

看点：

1. 到普通列 `c2` 时，应当进入“字段已变更”的分支
2. 到虚拟列 `vcol_0 = c2 DIV 1` 时，应当看到：
   - `new_val = NULL`
   - `old_v_val = 1`
3. 到虚拟列 `vcol_1 = FLOOR(c1)` 时，它虽然没变，但应进入
   “未更新但已索引虚拟列仍写入 `old_vrow`”的路径

你可以把期望状态记成：

```text
fields[]:
  c2.new_val = NULL
  vcol_0.new_val = NULL
  vcol_0.old_v_val = 1

old_vrow:
  vcol_0 = 1
  vcol_1 = 0
```

### 16.3 断点 3：`trx_undo_report_row_operation()`

目标：

- 确认 update undo 已经开始写
- 确认这次写入的 `update` 正是刚才那份 `uvect`

建议观察：

- `op_type`
- `update`
- `rec`
- `roll_ptr`

这里最值得确认的是：

- 这是一次 `TRX_UNDO_MODIFY_OP`
- `update` 里已经带上虚拟列相关信息

### 16.4 断点 4：`trx_undo_update_rec_get_update()`

目标：

- 看 undo 是如何被重新解析成 update vector 的
- 看虚拟列的 `old_v_val` 如何被恢复出来

建议观察：

- `n_fields`
- `field_no`
- `is_virtual`
- `upd_field->new_val`
- `upd_field->old_v_val`
- `ptr`

这里可以重点看：

1. 普通列的 `new_val` 是直接从 undo 里读出来的
2. 虚拟列在读完 `new_val` 后，还会再读一份 `old_v_val`

也就是说，rollback 使用的 `update` 不是当时内存里的原对象，而是“undo 解析重建出来”的对象。

### 16.5 断点 5：`row_upd_replace_vcol()`

目标：

- 看 rollback 如何把虚拟列值重新灌进 `row` / `undo_row`

建议观察：

- `upd_new`
- `row`
- `undo_row`
- `col_no`
- `upd_field`
- `dfield`
- `ptr`

最关键的两个阶段：

1. 从 `upd_field->old_v_val` 回填“已更新的虚拟列”
2. 从 undo extra vcol data 回填“未更新但已索引的虚拟列”

本例中的期望是：

```text
vcol_0 <- 1   // from old_v_val
vcol_1 <- 0   // from extra undo vcol payload
```

### 16.6 断点 6：`row_undo_mod_upd_exist_sec()`

目标：

- 看 secondary index rollback 是否正在用“当前版本 entry”和“旧版本 entry”做撤销

建议观察：

- `index->name`
- `entry`
- `node->row`
- `node->undo_row`
- `node->update`

这里要建立一个非常重要的认识：

- `row_build_index_entry(node->row, ...)`
  代表当前版本 secondary index entry
- `row_build_index_entry(node->undo_row, ...)`
  代表应该恢复回去的旧 secondary index entry

### 16.7 断点 7：`row_build_index_entry_low()`

目标：

- 最终确认构造索引项时，虚拟列表达式到底拿到了什么值

建议观察：

- `index->name`
- `i`
- `col->is_virtual()`
- `dfield2`
- `dfield_get_data(dfield2)`
- `dfield_get_len(dfield2)`

对于 `i15`，你最想确认的是：

```text
old entry for rollback:
  (c2 DIV 1, FLOOR(c1)) = (1, 0)
```

如果这里看到的不是 `(1, 0)`，那前面的虚拟列恢复一定有缺口。

## 17. 推荐的单步阅读顺序

如果你准备边断点边读源码，我建议按下面顺序来，不要一开始就同时啃所有文件。

### 第一轮：先只看“正向更新”

1. `ha_innobase::update_row()`
2. `calc_row_difference()`
3. `trx_undo_report_row_operation()`

这一轮的目标只有一个：

> 搞清楚 `upd_t` 是怎么被填出来的。

### 第二轮：再看“undo 是怎么被重新读回来”

1. `trx_undo_update_rec_get_update()`
2. `row_upd_replace_vcol()`

这一轮只盯：

- `old_v_val`
- `old_vrow`
- extra undo vcol payload

### 第三轮：最后看“索引是怎么恢复的”

1. `row_undo_mod_upd_exist_sec()`
2. `row_build_index_entry_low()`

这一轮只回答一个问题：

> rollback 构造出来的旧索引键，和语句执行前的旧索引键是不是同一份。

## 18. 调试时最容易混淆的 4 个点

### 18.1 `new_val` 在 rollback 里不代表“回滚后的值”

`upd_field->new_val` 记录的是“当时 update 想写进去的新值”，不是“rollback 后的值”。

对本例来说：

```text
vcol_0.new_val = NULL
```

这表示正向更新时要把它改成 `NULL`，而 rollback 恢复旧值时真正要看的，是：

```text
vcol_0.old_v_val = 1
```

### 18.2 `old_v_val` 不是完整旧虚拟行

它只覆盖“这次真的更新过的虚拟列”。

所以在本例里：

- `old_v_val` 能帮你恢复 `vcol_0`
- 但不能单独帮你恢复 `vcol_1`

### 18.3 `old_vrow` 不是 undo 的完整替代品

`old_vrow` 是正向更新阶段准备出来的旧虚拟列视图；真正 rollback 时，仍然要依赖 undo 把这些信息重新读回来。

也就是说：

```text
old_vrow -> 帮助写 undo
undo     -> 帮助 rollback 重建 old view
```

### 18.4 崩溃点不一定就是根因点

`bug120176` 的断言/崩溃是在 rollback 深处出现的，但根因其实更早，在“级联更新准备虚拟列数据”这一步就埋下了。

所以调试这类问题时，最好把正向更新和回滚两边都走一遍，不然很容易只盯到崩溃点，而忽略更前面的数据缺失。

## 19. 外键级联更新路径的详细处理逻辑

前面的章节更多是从 `calc_row_difference()` 和 undo/rollback 的角度看问题。  
如果从“为什么修复点落在 `row0ins.cc`”这个问题往回看，就必须把外键级联更新的处理链单独拎出来。

### 19.1 先记住一个关键事实

在 InnoDB 里：

- 外键列本身不能是虚拟列
- 但是“依赖外键列的虚拟列”完全可能存在
- 而且这些虚拟列还可能参与 secondary index

本例就是这种情况：

- FK 列：`t21.c2`
- 虚拟索引表达式之一：`c2 DIV 1`

也就是说：

> FK 动作直接更新的是普通列 `c2`，  
> 但 secondary index 维护和 rollback 恢复时，受影响的其实是“依赖 `c2` 的虚拟索引键”。

### 19.2 外键级联更新主链路

从父表更新触发 child table 级联更新时，大致会走这条路径：

```text
parent UPDATE
  -> row_ins_check_foreign_constraint()
     -> row_ins_foreign_check_on_constraint()
        -> 为 child row 加锁
        -> 构造 cascade->update
           -> row_ins_cascade_calc_update_vec()
           -> row_ins_foreign_fill_virtual()
        -> row_update_cascade_for_mysql()
           -> child row 真正进入 InnoDB update
              -> calc_row_difference()
              -> 写 undo
```

这里最重要的函数是：

- `row_ins_foreign_check_on_constraint()`
- `row_ins_cascade_calc_update_vec()`
- `row_ins_foreign_fill_virtual()`

它们都在 [row0ins.cc](/d:/Project/mysql-server/storage/innobase/row/row0ins.cc)。

### 19.3 `row_ins_foreign_check_on_constraint()` 在做什么

这个函数是父表 delete/update 遇到 `ON DELETE` / `ON UPDATE` 动作时的总入口。  
对本例来说，命中的是：

- 父表 `UPDATE`
- 外键动作 `ON UPDATE SET NULL`

它会先做几件基础工作：

1. 定位 child table 上匹配的记录
2. 给 child clustered record 加 `X` 锁
3. 创建或复用 `cascade` update node
4. 根据外键动作构造 child side 的 `cascade->update`
5. 再调用 `row_update_cascade_for_mysql()` 真正去更新 child row

可以把它理解成：

> 这一步还没有真正把 child row 改掉，  
> 它先在 FK 层把“child row 应该怎么改”准备成一份 update vector。

### 19.4 `ON UPDATE SET NULL` 时，普通列 update vector 怎么构造

对于 `ON UPDATE SET NULL`，`row_ins_foreign_check_on_constraint()` 会直接把 FK 对应的 child 列设成 `NULL`。

本例中就是：

```text
t21.c2 <- NULL
```

这部分逻辑在 `row_ins_foreign_check_on_constraint()` 里相对直接：

```text
for each FK column:
  ufield->field_no = child clustered col position
  ufield->new_val  = SQL NULL
```

所以在 FK 层最初构造出来的 `cascade->update`，只会显式包含：

```text
c2.new_val = NULL
```

这很合理，因为真正被 referential action 直接修改的只有 child table 的 FK 列。

### 19.5 `ON UPDATE CASCADE` 时，普通列 update vector 怎么构造

如果是 `ON UPDATE CASCADE`，则会走 `row_ins_cascade_calc_update_vec()`。

这个函数的职责是：

1. 遍历外键列映射关系
2. 在 parent update vector 里找到对应父列的新值
3. 把这些新值拷贝到 child table 的 update vector
4. 做一些合法性检查

包括：

- child 列是否允许 `NULL`
- 新值是否能放进 child 列长度
- 固定长度字符列是否需要补齐空格
- FTS Doc ID 是否需要联动更新

一个很关键的细节是：  
它只处理“普通列”的级联值传递，看到 parent update field 是虚拟列会直接跳过。

所以这层逻辑的边界非常清楚：

- 它负责 FK 基础列
- 不负责虚拟列表达式的派生值

### 19.6 为什么还需要 `row_ins_foreign_fill_virtual()`

因为单靠 FK 基础列 update vector 不够。

在本例中，FK 层只知道：

```text
c2: 1 -> NULL
```

但真正参与 secondary index `i15` 的索引键是：

```text
(c2 DIV 1, FLOOR(c1))
```

也就是说，一旦 child row 后面真的进入 InnoDB update / undo / rollback，
仅有 `c2.new_val = NULL` 还不够，系统还需要知道：

- 受 FK 影响的虚拟列旧值和新值
- 同一个虚拟索引键里，未变化但 rollback 仍需要的其它虚拟列值

`row_ins_foreign_fill_virtual()` 的作用，就是在 FK 层把这部分信息提前补进 `cascade->update`。

### 19.7 `row_ins_foreign_fill_virtual()` 原本的处理思路

这个函数会先基于 child 当前 clustered record 构造一份旧行视图：

```text
update->old_vrow = row_build(...)
```

然后再为相关虚拟列填充：

- `upd_field->old_v_val`
- `upd_field->new_val`

可以抽象成：

```text
old child rec
  -> build old_vrow
  -> for each selected virtual column:
       old_v_val = old computed value
       new_val   = new computed value if FK base col changed
                 = old value otherwise
```

其中一个重要判断是：

- 如果虚拟列的 base column 不在 FK 影响范围内
  - `new_val = old value`
- 如果虚拟列依赖 FK base column
  - 重新计算 `new_val`

这套设计本身没有问题，它试图把“受 FK 影响的虚拟列变化”也编码进 update vector。

### 19.8 Bug #120176 暴露出的缺口

问题不在于这套机制完全没有考虑虚拟列，而在于它“选中的虚拟列集合太窄了”。

旧逻辑大致相当于：

```text
只处理 foreign->v_cols 里的虚拟列
```

而 `foreign->v_cols` 的语义更接近：

> 与这个 foreign key 直接相关的虚拟列集合

这在很多场景下够用，但对本例不够。

因为本例的虚拟索引键是：

```text
(c2 DIV 1, FLOOR(c1))
```

其中：

- `c2 DIV 1` 直接依赖 FK 列 `c2`
- `FLOOR(c1)` 不依赖 FK 列

旧逻辑容易只准备前者，而遗漏后者。  
但 rollback 恢复旧 secondary index entry 时，真正需要的是完整旧键：

```text
(1, 0)
```

不是只要第一列 `1` 就够了。

### 19.9 用本例看 FK 层数据准备为什么会不完整

站在 FK 处理层看，这条 child row 的变化像这样：

```text
child clustered cols
  c1 = 0
  c2 = 1

FK action
  c2 -> NULL
```

如果只从 FK 影响范围出发，系统很容易认为：

```text
受影响的虚拟列只有 c2 DIV 1
```

于是就准备：

```text
vcol_0.old_v_val = 1
vcol_0.new_val   = NULL
```

却没有把：

```text
vcol_1 = FLOOR(c1) = 0
```

这份“虽然没变，但仍属于同一虚拟索引键”的值一起完整带下去。

这就会导致：

```text
FK prepare stage   -> 数据不完整
calc_row_difference -> 能看到的旧虚拟列视图不完整
undo                -> 记录不完整
rollback            -> 无法重建完整旧 secondary key
```

### 19.10 一张 FK 视角的示意图

```text
parent row update
  t18.c5: 1 -> 3
        |
        v
foreign key action on child
  t21.c2: 1 -> NULL
        |
        v
FK layer builds cascade->update
  ordinary FK fields:
    c2.new_val = NULL
        |
        v
FK layer fills virtual-column info
  needed for indexed virtual keys:
    vcol_0.old_v_val = 1
    vcol_0.new_val   = NULL
    vcol_1.old view  = 0   <-- rollback 仍然需要
        |
        v
child row enters normal update / undo / rollback path
```

## 20. 这次修复方案和外键逻辑的关系

前面说过，修复点落在 `row0ins.cc`，不是因为崩溃发生在这里，而是因为这里是“FK 级联更新准备数据”的源头。

### 20.1 修复前的思路

修复前的思路更偏向：

```text
只把 foreign key 直接影响到的虚拟列补进 cascade->update
```

这意味着系统关注的是：

- 哪些虚拟列依赖 FK base column

但没有充分覆盖：

- 哪些虚拟列虽然没变，却仍然属于某个 indexed virtual key

### 20.2 修复后的核心思路

修复后的思路应该改成：

```text
只要 child table 上存在 rollback / index maintenance 需要的
indexed virtual columns，就都要在 FK prepare 阶段被物化出来
```

也就是从“FK 直接影响范围”切换到“索引恢复所需范围”。

换句话说，判断标准不再只是：

```text
这个虚拟列是不是 foreign->v_cols 的成员
```

而更应该是：

```text
这个虚拟列是否属于 indexed virtual column，
或者在线 DDL / 索引维护 / rollback 构造旧 entry 时会被需要
```

### 20.3 为什么这样修就能堵住 bug

因为只要 FK 层在准备 `cascade->update` 时，把完整的 indexed virtual old view 都带下去，后面整条链都会变得完整：

```text
row_ins_foreign_fill_virtual()
  -> cascade->update / old_vrow 完整
  -> child update path 完整
  -> undo 完整
  -> row_upd_replace_vcol() 可完整恢复
  -> row_build_index_entry(node->undo_row) 可重建旧键
  -> rollback 不再崩
```

对本例来说，修复后 FK 层需要准备出来的最小充分信息就是：

```text
c2.new_val   = NULL
vcol_0.old   = 1
vcol_0.new   = NULL
vcol_1.old   = 0
```

这样 rollback 才能恢复出：

```text
(c2 DIV 1, FLOOR(c1)) = (1, 0)
```

### 20.4 从源码阅读角度，应该重点看哪几处

如果你想把“FK 处理逻辑”和“undo/rollback 逻辑”连起来读，建议按这个顺序：

1. `row_ins_foreign_check_on_constraint()`
2. `row_ins_cascade_calc_update_vec()`
3. `row_ins_foreign_fill_virtual()`
4. `row_update_cascade_for_mysql()`
5. `calc_row_difference()`
6. `trx_undo_report_row_operation()`
7. `trx_undo_update_rec_get_update()`
8. `row_upd_replace_vcol()`
9. `row_undo_mod_upd_exist_sec()`

这样你会更容易看清楚：

> 这不是一个“rollback 层单点出错”的 bug，  
> 而是一个“FK prepare 阶段准备出的虚拟列信息不够完整，最终在 rollback 层爆炸”的 bug。
