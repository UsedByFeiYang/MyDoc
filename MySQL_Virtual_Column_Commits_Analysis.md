# MySQL 8.0.41+ 虚拟列相关Commit分析报告

## 概述

本文档分析了从 MySQL 8.0.41 版本开始到现在，与虚拟列（Virtual Column / Generated Column）相关的所有 commit。

**分析时间范围**: MySQL 8.0.41 → 当前 HEAD  
**生成日期**: 2026年4月19日  
**相关版本标签**: mysql-8.0.41, mysql-8.0.42, mysql-8.0.43, mysql-8.0.44, mysql-8.0.45

---

## Commit 列表与分析

### 1. Bug#37602657 - virtual index corruption

**Commit Hash**: `f97c71a08fb`  
**Author**: Andrzej Jarzabek  
**Date**: 2025-06-12

**问题描述**:
在某些场景下，基于虚拟列的二级索引字段会被随机值或无效值破坏。

**根本原因**:
当对基于虚拟列的二级索引进行 inplace 更新时，更新向量（update vector）的格式混淆了 - `field_no` 成员包含实际的字段编号，但某些代码期望它是虚拟列编号。

"虚拟字段更新"的 `field_no` 替代语义仅对聚簇索引的更新向量有意义。聚簇索引没有虚拟列的字段。当由于各种原因需要更新向量包含虚拟列值更新的信息时，我们创建"虚拟字段更新"，这些更新实际上不更新任何字段。

**修复方案**:
只有聚簇索引的更新向量才使用带有替代语义的"虚拟字段更新"。对于二级索引中物化虚拟列值的更新，将其作为常规字段更新处理。

**影响文件** (14个文件, +153/-119行):
- `storage/innobase/btr/btr0cur.cc`
- `storage/innobase/data/data0data.cc`
- `storage/innobase/dict/dict0dict.cc`
- `storage/innobase/handler/ha_innodb.cc`
- `storage/innobase/ibuf/ibuf0ibuf.cc`
- `storage/innobase/include/data0data.h`
- `storage/innobase/include/dict0mem.h`
- `storage/innobase/include/row0upd.h`
- `storage/innobase/include/row0upd.ic`
- `storage/innobase/pars/pars0pars.cc`
- `storage/innobase/row/row0ins.cc`
- `storage/innobase/row/row0row.cc`
- `storage/innobase/row/row0upd.cc`
- `storage/innobase/trx/trx0rec.cc`

---

### 2. Bug#37324137 - MySQL error when optimizing and updating table simultaneously

**Commit Hash**: `0291bd98b95`  
**Author**: Krystian Warzocha  
**Date**: 2025-06-30

**问题描述**:
当对含有虚拟列（这些虚拟列不属于任何索引）的表进行并发 OPTIMIZE 和 UPDATE 操作时，会发生错误。

**根本原因**:
在表重建过程中，undo log 中值的序列化和反序列化不一致。不属于任何索引的虚拟列值不会被序列化到 undo log，但在表重建期间从 undo log 读取时，错误地尝试读取这些不应该读取的虚拟列值。

**修复方案**:
在全表重建期间，不再尝试从 undo log 读取不属于任何索引的虚拟列值。

**影响文件** (+563/-13行):
- `mysql-test/suite/innodb/r/undo_with_virtual_no_idx.result` (新增测试)
- `mysql-test/suite/innodb/t/undo_with_virtual_no_idx.test` (新增测试)
- `storage/innobase/row/row0log.cc`

---

### 3. Bug#37478594 - Virtual column value may be incorrectly set to NULL on cascade update

**Commit Hash**: `fc4b73f4002` (主修复), `045459cf83e` 和 `8e01805f1a1` (post-push fixes)  
**Author**: Andrzej Jarzabek  
**Date**: 2025-02-11 (主修复), 2025-06-10 (post-push fixes)

**问题描述**:
当 ON DELETE SET NULL 级联更新子表中的行，将虚拟列所基于的所有列设置为 NULL 时，可能发生以下两种情况之一：
1. 虚拟字段在更新中被设置为 NULL，而不是重新计算
2. 虚拟字段的值根本没有重新计算

这会导致基于虚拟字段的索引损坏。

**根本原因**:
对于 ON DELETE SET NULL 和 ON UPDATE SET NULL 操作，虚拟字段计算尝试使用父表更新向量来确定哪些基础字段应该被视为 NULL。但在 ON DELETE SET NULL 场景中，父行已被删除，因此没有有意义的更新向量。此外，当虚拟列基于 FK 的所有列时，被作为特殊情况处理 - 虚拟字段被"计算"为 NULL 而不实际计算底层表达式。如果表达式的值在所有列操作数为 NULL 时为 NULL，这恰好是正确的 - 但显然并非所有表达式都是如此。

**修复方案**:
当虚拟字段作为外键操作的一部分进行计算时，子表非虚拟列的所有更新在该点已经知道。不再查找父表的更新，而是直接使用这些更新进行计算。这可以统一应用于所有 FK 更新操作：ON DELETE SET NULL、ON UPDATE CASCADE 和 ON UPDATE SET NULL。

**影响文件** (25个文件, +375/-382行):
- `storage/innobase/btr/btr0sea.cc`
- `storage/innobase/data/data0data.cc`
- `storage/innobase/ddl/ddl0builder.cc`
- `storage/innobase/dict/dict0dict.cc`
- `storage/innobase/handler/ha_innodb.cc`
- `storage/innobase/include/data0data.h`
- `storage/innobase/include/dict0dict.h`
- `storage/innobase/include/dict0dict.ic`
- `storage/innobase/include/rem0rec.ic`
- `storage/innobase/include/row0mysql.h`
- `storage/innobase/include/row0upd.h`
- `storage/innobase/include/row0upd.ic`
- `storage/innobase/lob/lob0lob.cc`
- `storage/innobase/lob/lob0update.cc`
- `storage/innobase/lob/zlob0update.cc`
- `storage/innobase/mtr/mtr0log.cc`
- `storage/innobase/rem/rec.h`
- `storage/innobase/rem/rem0rec.cc`
- `storage/innobase/row/row0ins.cc`
- `storage/innobase/row/row0log.cc`
- `storage/innobase/row/row0row.cc`
- `storage/innobase/row/row0sel.cc`
- `storage/innobase/row/row0upd.cc`
- `storage/innobase/row/row0vers.cc`
- `storage/innobase/trx/trx0rec.cc`

---

### 4. WL#16995 - Error code for generated column evaluation failure

**Commit Hash**: `e8b6e25751d` 和 `3a9a155d46d`  
**Author**: Stella Giannakopoulou  
**Date**: 2025-10-15

**描述**:
这是一个 Work Log，为生成列评估失败添加了新的错误代码。

**影响文件**:
- `share/messages_to_clients.txt` (+2行)

**说明**:
这个 commit 主要是添加错误消息定义，是更大功能的一部分。

---

### 5. WL#17016 - InnoDB: Support tables with generated columns in bulk load component

**Commit Hash**: `7340ff155d1`  
**Author**: Annamalai Gurusami  
**Date**: 2025-09-20

**描述**:
为 InnoDB 批量加载组件添加对生成列（包括存储生成列和虚拟生成列）表的支持。数据验证已完成。

**影响文件** (15个文件, +547/-50行):
- `include/mysql/components/services/bulk_data_service.h`
- `sql/check_stack.cc`
- `sql/handler.cc`
- `sql/handler.h`
- `sql/server_component/bulk_data_service.cc`
- `storage/innobase/btr/btr0mtib.cc`
- `storage/innobase/ddl/ddl0bulk.cc`
- `storage/innobase/handler/ha_innodb.cc`
- `storage/innobase/handler/handler0alter.cc`
- `storage/innobase/include/btr0mtib.h`
- `storage/innobase/include/db0err.h`
- `storage/innobase/include/ddl0bulk.h`
- `storage/innobase/include/dict0mem.h`
- `storage/innobase/include/row0mysql.h`
- `storage/innobase/ut/ut0ut.cc`

**功能说明**:
这是一个重要的功能增强，使得批量加载操作能够正确处理包含生成列的表。

---

### 6. Bug#35451459 - MySQL Server crashes when executing query

**Commit Hash**: `e28ccd48a6e`  
**Author**: Ayush Gupta  
**Date**: 2025-07-07

**问题描述**:
MySQL Server 在执行查询时崩溃。

**根本原因**:
`unpack_partition_info()` 在分配位图之前被调用，但需要在生成列之前完成，因为生成列会调用 `fix_fields`，而函数可能需要访问位图。

这是 Bug#33142135 修复后的回归问题。该问题在 9.3.0 中已修复，但未移植到 8.0+ 版本。此修复是 Bug#35044654 的反向移植。

**修复方案**:
调整 `sql/table.cc` 中分区信息处理和位图分配的顺序。

**影响文件**:
- `mysql-test/r/partition_error.result`
- `mysql-test/t/partition_error.test`
- `sql/table.cc`

---

### 7. Bug#30453221 - CHECK CONSTRAINTS CHARACTER SET MISMATCH

**Commit Hash**: `99a05bf40ed`  
**Author**: Dag Wanvik  
**Date**: 2025-03-28

**问题描述**:
CREATE TABLE 语句包含生成列表达式（如 CHECK 约束表达式），当引用非 ASCII 标识符时，如果当前客户端字符集与 UTF-8 不兼容（例如 GBK），会导致语法错误。

**根本原因**:
在执行包含表达式（如 CHECK 约束）的 CREATE TABLE 语句时，MySQL 从 AST 重建该约束表达式的源字符串，然后立即重新解析重建的表达式源。重建的表达式字符串使用 utf8mb3 编码，但使用当前客户端字符集重新解析。如果重建的表达式字符串包含在当前客户端字符集中无效的字节序列，就会导致失败。

**修复方案**:
在重新解析生成列表达式的重建源字符串时，用系统字符集 (utf8mb3) 替换当前客户端字符集。

同时修复了一个副作用：为 `Item_func_set_collation` 实现了专门的 `Item::eq` 方法，只检查解析后的 collation 是否相同，而不关心 collation 字符串本身的字符集。

**影响文件** (7个文件, +107/-6行):
- `mysql-test/r/check_constraints.result`
- `mysql-test/r/functional_index.result`
- `mysql-test/t/check_constraints.test`
- `mysql-test/t/functional_index.test`
- `sql/item_strfunc.cc`
- `sql/item_strfunc.h`
- `sql/table.cc`

---

### 8. Bug#37523857 - The table access service crashes inserting on a table with functional columns

**Commit Hash**: `c5ab0637682`  
**Author**: Georgi Kodinov  
**Date**: 2025-01-28

**问题描述**:
表访问服务（table access service）在向具有功能列（可见或隐藏）的表插入数据时崩溃。

**修复方案**:
为表访问服务当前不支持的一些表功能添加检查：
- 写入具有生成列的表
- 写入具有触发器的表

**影响文件**:
- `sql/server_component/table_access_service.cc` (+38行)

---

### 9. Bug#35044654 - partition by default crashes in bitmap_set_bit

**Commit Hash**: `f125a68e9b4`  
**Author**: Volodymyr Verovkin  
**Date**: 2025-02-10

**问题描述**:
在 `bitmap_set_bit` 中崩溃，与分区默认值相关。

**根本原因**:
`unpack_partition_info()` 在分配位图之前被调用，但需要在生成列之前完成，因为生成列会调用 `fix_fields`，而函数可能需要访问位图。

**修复方案**:
调整 `sql/table.cc` 中的处理顺序，确保位图在生成列处理之前分配。

**影响文件**:
- `sql/table.cc` (+33/-28行)

---

## 统计摘要

| 类别 | 数量 |
|------|------|
| Bug 修复 | 7 |
| 功能增强 (Work Log) | 2 |
| 总 Commit 数 | 12 |
| 影响的 InnoDB 文件 | 约 25 |
| 影响的 SQL 层文件 | 约 10 |

**注意**: 已排除 NDB 存储引擎相关的 commit (Bug#38593818)

---

## 按版本归类

| 版本 | 相关 Commit |
|------|-------------|
| 8.0.42 | Bug#37523857, Bug#35044654, Bug#37478594 |
| 8.0.43 | Bug#37602657, Bug#37324137, Bug#30453221, Bug#35451459 |
| 8.0.44+ | WL#17016, WL#16995 |

---

## 主要技术改进总结

1. **虚拟列索引完整性修复**: 修复了虚拟列二级索引可能损坏的严重问题 (Bug#37602657)

2. **外键级联操作修复**: 修复了 ON DELETE SET NULL 场景下虚拟列值计算错误的问题 (Bug#37478594)

3. **并发操作稳定性**: 修复了 OPTIMIZE 和 UPDATE 并发执行时的冲突问题 (Bug#37324137)

4. **字符集兼容性**: 解决了非 ASCII 标识符与 CHECK 约束的字符集兼容问题 (Bug#30453221)

5. **功能增强**: 
   - 批量加载支持生成列表 (WL#17016)
   - 生成列评估失败的错误码 (WL#16995)

6. **回归修复**: 多个回归问题的修复，确保之前版本的功能在新版本中正常工作

---

## 参考链接

- MySQL Bug Database: https://bugs.mysql.com/
- MySQL Work Logs: https://dev.mysql.com/worklog/