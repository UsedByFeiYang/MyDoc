# MySQL/InnoDB 会话总结

## 会话范围

本次会话围绕 `mysql-server` 源码做了两条主线分析：

1. `VARCHAR` 在 MySQL/InnoDB 中的存储、读取，以及为什么跨越 `255` 字节扩展不能走 `INPLACE`。
2. `ALTER TABLE` 从 parser 到 `mysql_alter_table()`、`mysql_inplace_alter_table()`，以及 InnoDB `prepare/inplace/commit` 三阶段执行流程。

另外还确认了当前工作目录是 `D:\Project\mysql-server`，并尝试启动了本机 `Word`。

## 一、VARCHAR 的存储与读取

### 1. MySQL 层的 true VARCHAR

在 server 行缓冲里，`VARCHAR` 采用“长度前缀 + payload”的形式：

- 长度前缀为 `1` 或 `2` 字节
- 写长度前缀的代码在 `storage/innobase/row/row0mysql.cc` 的 `row_mysql_store_true_var_len()`
- 读长度前缀的代码在同文件的 `row_mysql_read_true_varchar()`

如果列定义超过 `255`，数据字典层会把它标记为 `DATA_LONG_TRUE_VARCHAR`，表示它是“2 字节长度前缀的 true varchar”。

### 2. InnoDB 页内记录的 VARCHAR

真正落到 InnoDB record 时，并不会把 MySQL 的那段长度前缀原样存进去。

写入时：

- 先从 MySQL 行缓冲中读出真实长度和真实 payload
- 再把 payload 放进 InnoDB 的字段内容区
- 字段长度放到 compact/dynamic record 的变长字段长度区里

对应逻辑主要在：

- `storage/innobase/row/row0mysql.cc`
- `storage/innobase/rem/rem0rec.cc`

### 3. Dynamic/Compact record 中变长列长度的规则

`DYNAMIC` 与 `COMPACT` 共用 new-style record 编码逻辑：

- 最大长度 `<=255`：长度总是按 `1` 字节编码
- 最大长度 `>255`：长度进入“大变长列”规则，可能用 `1` 或 `2` 字节
- 外部存储时还会带 extern 标志

这部分核心代码：

- 写入：`rec_convert_dtuple_to_rec_comp()`
- 读取偏移：`rec_get_offsets()`

### 4. 以 INSERT 为例

`INSERT` 路径的关键理解是：先把 MySQL 格式拆开，再转成 InnoDB tuple。

大致过程：

1. `ha_innobase::write_row()`
2. `row_insert_for_mysql()`
3. `row_mysql_convert_row_to_innobase()`
4. 若为 true varchar，调用 `row_mysql_read_true_varchar()` 读掉前缀
5. 只把 payload 和真实长度放入 `dfield`
6. `rec_convert_dtuple_to_rec_comp()` 把长度写入 record header 的 var-len 区，把 payload 写入数据区

### 5. 以 SELECT 为例

`SELECT` 路径会把 InnoDB record 重新还原成 MySQL 行格式：

1. `row_search_mvcc()`
2. `row_sel_store_mysql_rec()`
3. `row_sel_store_mysql_field()`
4. `row_sel_field_store_in_mysql_format()`
5. 若目标列是 true varchar，则再次调用 `row_mysql_store_true_var_len()` 补回 `1/2` 字节前缀

因此：

- InnoDB 内部存的是“长度区 + payload”
- 返回 MySQL 层时又恢复成“长度前缀 + payload”

## 二、为什么 VARCHAR 跨 255 字节不能 INPLACE

结论是：这和 InnoDB/MySQL 对 `VARCHAR` 的存储表示直接相关。

### 1. SQL 层显式设置了 256 byte barrier

`sql/field.cc` 中的 `length_prevents_inplace()` 明确规定：

- 不能缩短长度
- 不能跨越 `256 byte row format barrier`

也就是：

- `VARCHAR(100) -> VARCHAR(200)` 仍可能被视为兼容
- `VARCHAR(255) -> VARCHAR(256)` 直接被判定为不兼容

### 2. 根因是存储契约改变了

跨越 `255/256` 分界后，至少有两套编码规则发生变化：

1. MySQL 行缓冲的长度前缀从 `1` 字节变成 `2` 字节
2. InnoDB record header 对 var-len 列的编码类别也从“小变长列”切到“大变长列”

因此这不是简单 metadata 扩容，而是“旧行的物理解释方式变了”。

### 3. 进入 ALTER 框架后的后果

`Field_varstring::is_equal()` 在跨越这个边界时不会返回 `IS_EQUAL_PACK_LENGTH`，而是返回 `IS_EQUAL_NO`。

后续：

- SQL 层把它当作 `ALTER_STORED_COLUMN_TYPE`
- InnoDB 只允许 `ALTER_COLUMN_EQUAL_PACK_LENGTH` 进入 no-rebuild/inplace 集合
- 因此最终只能落到 `COPY`

## 三、ALTER TABLE 从 parser 到 mysql_alter_table()

### 1. 词法与语法

`dispatch_sql_command()` 会调用 `parse_sql()`，后者进入 parser。

`ALTER TABLE` 的 grammar 入口在 `sql/sql_yacc.yy`：

- `ALTER TABLE table_ident opt_alter_table_actions`
- `ALTER TABLE table_ident standalone_alter_table_action`

grammar 的职责主要是构造 parse tree 节点：

- `PT_alter_table_stmt`
- `PT_alter_table_standalone_stmt`

### 2. HA_CREATE_INFO / Alter_info / Table_ref 如何填充

这三个对象分别承担不同职责：

- `HA_CREATE_INFO`
  - 保存目标表的 table options，例如 engine、charset、row_format、tablespace 等
- `Alter_info`
  - 保存列、索引、约束、rename、algorithm/lock 等变更请求
- `Table_ref`
  - 表示这条语句正在操作的目标表，以及它的锁/MDL信息

在 `PT_alter_table_stmt::make_cmd()` 中：

1. `thd->lex->create_info = &m_create_info`
2. 构造 `Table_ddl_parse_context`
3. `init_alter_table_stmt()` 中调用 `add_table_to_list()` 创建 `Table_ref`
4. 把 `requested_algorithm`、`requested_lock`、`with_validation` 等写入 `Alter_info`
5. 各个 action 节点的 `do_contextualize()` 再把列/索引/option 分别写进 `Alter_info` 和 `HA_CREATE_INFO`

### 3. 执行入口

最终 parse tree 会生成 `Sql_cmd_alter_table`，执行时进入：

1. `Sql_cmd_alter_table::execute()`
2. `mysql_alter_table()`

`execute()` 里会从 `LEX` 取出 `create_info`、`alter_info`、`first_table`，复制后传给 `mysql_alter_table()`。

## 四、mysql_alter_table() 的主流程

`mysql_alter_table()` 可以概括成：

1. 参数与权限校验
2. `open_tables()` 打开旧表
3. 获取 DD schema / old_table_def
4. `mysql_prepare_alter_table()` 生成“完整新表定义”
5. 判断是否理论上还能走 inplace
6. 如果不是纯 COPY，构造 `Alter_inplace_info`
7. 调 `fill_alter_inplace_info()`
8. 调引擎 `check_if_supported_inplace_alter()`
9. 走 `mysql_inplace_alter_table()` 或 COPY 路径

### 1. mysql_prepare_alter_table()

这一步非常重要：

- parser 阶段的 `HA_CREATE_INFO` 只保存“用户显式指定的新选项”
- 到这里才通过 `init_create_options_from_share()` 把旧表未覆盖的属性补齐
- 同时整理出新表完整字段/索引集合

### 2. fill_alter_inplace_info()

这一步把 SQL 层的 diff 编译成 SE 可理解的 bitmap 和辅助结构，例如：

- `ALTER_STORED_COLUMN_TYPE`
- `ALTER_COLUMN_EQUAL_PACK_LENGTH`
- `ADD_STORED_BASE_COLUMN`
- `DROP_STORED_COLUMN`
- `ALTER_COLUMN_NAME`
- `ALTER_STORED_COLUMN_ORDER`

也正是这一步把 `VARCHAR(255) -> VARCHAR(256)` 这种变化翻译成“不兼容的 stored column type”。

## 五、mysql_inplace_alter_table() 的执行流程

`mysql_inplace_alter_table()` 是 SQL 层对 inplace 三阶段协议的驱动器：

1. 根据引擎返回值和用户指定的 `LOCK` 要求升级/降级 MDL
2. `ha_prepare_inplace_alter_table()`
3. `ha_inplace_alter_table()`
4. 升级到 `MDL_EXCLUSIVE`
5. `ha_commit_inplace_alter_table(..., true)`
6. 如果失败则调用 `ha_commit_inplace_alter_table(..., false)` 回滚

SQL 层本身并不决定“是否 rebuild”，真正的分叉在 InnoDB handler 内部。

## 六、InnoDB prepare / inplace / commit 三阶段

### 1. 是否 rebuild 的判定

InnoDB 通过 `innobase_need_rebuild()` 判断是否要“重建聚簇表”。

- `INNOBASE_ALTER_REBUILD`：需要重建
- `INNOBASE_ALTER_NOREBUILD`：不需要重建

典型需要 rebuild 的变更：

- 加/删主键
- 存储列顺序变化
- drop stored column
- add stored base column
- nullable/not nullable 某些变更
- `RECREATE_TABLE`

典型 no-rebuild 变更：

- add/drop secondary index
- rename index
- rename column
- `ALTER_COLUMN_EQUAL_PACK_LENGTH`
- add/drop virtual column

### 2. prepare 阶段

入口：

- `ha_innobase::prepare_inplace_alter_table()`
- `prepare_inplace_alter_table_impl()`

prepare 阶段主要负责：

- 做一轮 InnoDB 侧合法性校验
- 收集 drop/add index、drop/add FK、virtual column、autoinc 等内部对象
- 构造 `ha_innobase_inplace_ctx`
- 调 `prepare_inplace_alter_table_dict()`

这里是第一次真正分叉：

- 不重建：`ctx->new_table == ctx->old_table`
- 重建：`ctx->new_table != ctx->old_table`

重建时会准备一个新的 `dict_table_t`，后面主阶段会把数据 build 到这张新表里。

### 3. inplace 阶段

入口：

- `ha_innobase::inplace_alter_table()`
- `inplace_alter_table_impl()`

这一步是物理工作主阶段。

如果：

- 是 `INSTANT`
- 或没有 `INNOBASE_ALTER_DATA`
- 或只是无需 rebuild 的纯 create option 变更

那这里基本会直接返回。

否则会构造 `ddl::Context` 并执行 `ddl.build()`。

两类路径的差别：

- 重建表
  - 扫描旧表聚簇索引
  - 构建新的 clustered table 和其索引
  - 若 online rebuild，则记录 online rebuild log，commit 时补最后增量
- 非重建表
  - 不搬整表数据
  - 主要创建新的 secondary indexes
  - 必要时重建 virtual column template

### 4. commit 阶段

入口：

- `ha_innobase::commit_inplace_alter_table()`
- `commit_inplace_alter_table_impl()`

统一动作包括：

- 若 `commit=false`，走 `rollback_inplace_alter_table()`
- 启动/复用 trx
- 对旧表加 InnoDB `LOCK_X`
- 停止 background stats 对相关表的访问

然后再次分叉。

#### 4.1 重建表 commit

主要逻辑在：

- `commit_try_rebuild()`
- `commit_cache_rebuild()`

过程要点：

1. 检查 rebuilt table 上的 index 是否都完成且未损坏
2. 更新 FK 定义
3. 如果是 online rebuild，调用 `row_log_table_apply()` 应用最后增量
4. old/new 表在字典缓存和文件层面交换身份
5. `commit_cache_rebuild()` 把 old table 改成临时名，把 new table 改回正式名

所以 rebuild-inplace 的本质是：

- 主阶段“造新表”
- commit 阶段“补增量并交换新旧表”

#### 4.2 非重建表 commit

主要逻辑在：

- `commit_try_norebuild()`
- `commit_cache_norebuild()`

过程要点：

1. 检查新加 index 是否完成且未损坏
2. 更新 FK 定义
3. 把新加 index 标记为 committed
4. 把要删的旧 index 从 cache 移除
5. 调整 `ord_part`
6. 处理列 rename / enlarge 对缓存列定义的影响
7. 处理 index rename

所以 no-rebuild 的本质是：

- 不创建新的 clustered table
- 直接把新旧索引/列元信息切换到原表对象上

### 5. instant 情况

如果是 `INSTANT`：

- prepare/main 基本是 no-op
- 主要在 commit 时通过 `Instant_ddl_impl::commit_instant_ddl()` 更新 instant metadata

## 七、本次会话的核心结论

### 1. VARCHAR 的 255 barrier 既和 MySQL 层有关，也和 InnoDB record 编码有关

不是单纯 SQL 层保守，而是底层编码契约在 `255/256` 分界处发生了变化：

- MySQL true varchar 前缀从 1 字节切换到 2 字节
- InnoDB record header 的 var-len 编码也切换类别

### 2. SQL 层先把这个变化抽象成“不兼容列类型变化”

之后：

- `fill_alter_inplace_info()` 会把它翻译成 `ALTER_STORED_COLUMN_TYPE`
- InnoDB `check_if_supported_inplace_alter()` 会拒绝这种操作进入 inplace

### 3. MySQL 的 INPLACE 是“server 视角的非 COPY”

它内部仍可能：

- 完全不重建：no-rebuild inplace
- 在 InnoDB 内部重建聚簇表：rebuild-inplace

所以判断是否“真正 rebuild”，一定要看 InnoDB 的 `innobase_need_rebuild()` 和 `ctx->need_rebuild()`。

## 附：本次会话的辅助事项

- 确认当前工程目录：`D:\Project\mysql-server`
- 尝试启动了 `Microsoft Word`

