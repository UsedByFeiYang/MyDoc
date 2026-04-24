# 排查 "Failed to parse message string for error 'ER_VECTOR_DISABLED'"

## 错误背景

在对社区版 MySQL 8.0.x 应用向量索引 patch（`0001-Support-vector-index.patch`）后编译时，CMake 构建步骤中 `comp_err` 工具可能报错：

```
Failed to parse message string for error 'ER_VECTOR_DISABLED'
```

该错误由 `utilities/comp_err.cc` 在处理 `share/messages_to_clients.txt` 时抛出，属于**构建期错误**，会导致 `include/mysqld_error.h` 等头文件无法生成，进而使整个编译失败。

## 根本原因

`comp_err` 解析 `messages_to_clients.txt` 时，读到以空格或 Tab 开头的行即认为是上一个错误的消息行，并调用 `parse_message_string()` 解析。该函数在两种情况下返回 NULL（触发上述错误）：

1. **空白行**：行内只有空格或 Tab，剥除后内容为空（对应源码中 `No error message was found on line`）。
2. **格式错误**：语言标识符（`eng`）之后没有紧跟 `"`。

Patch 通过 `git am --ignore-whitespace --3way` 应用时，若目标机器的 `messages_to_clients.txt` 与 patch 上下文不完全匹配，3-way merge 可能在 `ER_VECTOR_DISABLED` 与其 `eng "..."` 行之间插入一个**仅含空白字符的空行**，即可触发此错误。

## 诊断步骤

在**目标 Linux 机器**上执行以下命令排查：

### 第一步：确认错误条目位置

```bash
grep -n 'ER_VECTOR_DISABLED\|ER_DATA_INCOMPATIBLE_WITH_VECTOR\|ER_TO_VECTOR\|ER_VEC_DISTANCE\|ER_VECTOR_BINARY\|ER_VECTOR_INDEX' \
  share/messages_to_clients.txt
```

预期输出（行号因目标版本而异，但各条目之间只差 3 行）：

```
10047:ER_VECTOR_DISABLED
10050:ER_DATA_INCOMPATIBLE_WITH_VECTOR
10053:ER_TO_VECTOR_CONVERSION
10056:ER_VEC_DISTANCE_TYPE
10059:ER_VECTOR_BINARY_FORMAT_INVALID
10062:ER_VECTOR_INDEX_USAGE
10065:ER_VECTOR_INDEX_FAILED
```

若两个相邻条目行号之差超过 3，说明之间有多余行插入。

### 第二步：显示隐藏字符，确认格式

将 `ER_VECTOR_DISABLED` 的行号（例如 10047）代入：

```bash
LINE=10047
sed -n "$((LINE-1)),$((LINE+4))p" share/messages_to_clients.txt | cat -A
```

**正确格式**（每行以 `$` 结尾，表示 Unix LF）：

```
$
ER_VECTOR_DISABLED$
  eng "Creating vector columns or indexes is disabled."$
$
ER_DATA_INCOMPATIBLE_WITH_VECTOR$
```

**问题格式示例**（空白行含空格，触发报错）：

```
$
ER_VECTOR_DISABLED$
   $                      <- 仅含空格，comp_err 误认为消息行但解析失败
  eng "Creating vector columns or indexes is disabled."$
```

### 第三步：扫描 RDS 块内所有异常空白行

```bash
awk '
  /^start-error-number 7500/ { found=1 }
  found && /^[ \t]+$/ { print NR ": [BLANK WITH WHITESPACE]" }
  found && /End of RDS error message/ { exit }
' share/messages_to_clients.txt
```

无输出则无问题；有输出则记录行号，进入修复步骤。

### 第四步：用 comp_err 直接验证文件

```bash
mkdir -p /tmp/comp_err_test
./build/runtime_output_directory/comp_err \
  -C share/charsets \
  -D /tmp/comp_err_test/ \
  -c share/messages_to_clients.txt
echo "exit code: $?"
```

无输出且退出码为 0 表示文件格式正确。

## 修复方法

### 方法一：Python 脚本删除 RDS 块内的异常空白行（推荐）

```bash
cp share/messages_to_clients.txt share/messages_to_clients.txt.bak

python3 fix_rds_block.py share/messages_to_clients.txt
```

`fix_rds_block.py` 内容：

```python
import sys
path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()
in_rds = False
out = []
for i, line in enumerate(lines, 1):
    if 'start-error-number 7500' in line:
        in_rds = True
    if in_rds and 'End of RDS error message' in line:
        in_rds = False
    if in_rds and line != '\n' and line.strip() == '':
        print(f'Removed line {i}: {repr(line)}')
    else:
        out.append(line)
with open(path, 'w') as f:
    f.writelines(out)
```

修复后再次执行第四步验证。

### 方法二：手动替换整个 RDS 错误块

若方法一无效，找到 `start-error-number 7500` 所在行，将从该行到
`# End of RDS error message.` 的内容替换为以下标准内容
（注意：错误名行与 `eng` 行之间**不能有任何空白行**）：

```
start-error-number 7500

ER_NATIVE_PROC_PARAMETER_MISMATCH
   eng "Native procedure %s the %dth parameter mismatch"

ER_DUCKDB_CLIENT
  eng "[DuckDB] %s."

ER_DUCKDB_QUERY_ERROR
  eng "[DuckDB] Execute sql failed. %s"

ER_DUCKDB_TABLE_STRUCT_INVALID
  eng "[DuckDB] DuckDB table structure is invalid. Reason: %s."

ER_DUCKDB_TABLE_AUTO_INCREMENT_REMOVED
  eng "[DuckDB] AUTO_INCREMENT of field '%s' is removed."

ER_DUCKDB_TABLE_INDEX_REMOVED
  eng "[DuckDB] Index '%s' is removed."

ER_DUCKDB_TABLE_INDEX_UPGRADED
  eng "[DuckDB] Index '%s' is upgraded to primary key."

ER_DUCKDB_ALTER_OPERATION_NOT_SUPPORTED
  eng "[DuckDB] %s is not supported for this operation."

ER_DUCKDB_SETTING_SESSION_VARIABLE
  eng "[DuckDB] Exception when setting duckdb session variables. %s"

ER_DUCKDB_ALTER_FLAG_REMOVED
  eng "[DuckDB] '%s' operation will not take effect."

ER_DUCKDB_TABLE_ON_UPDATE_NOW_REMOVED
  eng "[DuckDB] ON_UPDATE_NOW of field '%s' is removed."

ER_DUCKDB_COMMIT_ERROR
  eng "[DuckDB] DuckDB commit transaction error. %s"

ER_DUCKDB_ROLLBACK_ERROR
  eng "[DuckDB] DuckDB rollback transaction error. %s"

ER_DUCKDB_SEND_RESULT_ERROR
  eng "[DuckDB] DuckDB send result error. %s"

ER_DUCKDB_APPENDER_ERROR
  eng "[DuckDB] DuckDB appender error. %s"

ER_DUCKDB_PREPARE_ERROR
  eng "[DuckDB] DuckDB prepare transaction error. %s"

ER_ACCESS_DENIED_DURING_DUCKDB_CONVERT
  eng "Access denied for user '%-.48s'@'%-.64s'. DuckDB covnerting."

ER_DUCKDB_DATA_IMPORT_MODE
  eng "[DuckDB] Data import mode: %s."

ER_VECTOR_DISABLED
  eng "Creating vector columns or indexes is disabled."

ER_DATA_INCOMPATIBLE_WITH_VECTOR
  eng "Value of type '%.16s, size: %zu' cannot be converted to 'vector(%zu)' type."

ER_TO_VECTOR_CONVERSION
  eng "Data cannot be converted to a valid vector: '%.*s'"

ER_VEC_DISTANCE_TYPE
  eng "Cannot determine distance type for VEC_DISTANCE, index is not found"

ER_VECTOR_BINARY_FORMAT_INVALID
  eng "Invalid binary vector format. Must use IEEE standard float representation in little-endian format. Use VEC_FromText() to generate it."

ER_VECTOR_INDEX_USAGE
  eng "Incorrect usage of vector index: %s"

ER_VECTOR_INDEX_FAILED
  eng "%s vector index `%s` in `%s`.`%s` (aux_tab: %s) failed: %s"

#
#  End of RDS error message.
#
```

## 向量相关错误号对照表

| 错误名 | 错误号 |
|---|---|
| ER_NATIVE_PROC_PARAMETER_MISMATCH | 7500 |
| ER_DUCKDB_CLIENT | 7501 |
| ER_DUCKDB_QUERY_ERROR | 7502 |
| ER_DUCKDB_TABLE_STRUCT_INVALID | 7503 |
| ER_DUCKDB_TABLE_AUTO_INCREMENT_REMOVED | 7504 |
| ER_DUCKDB_TABLE_INDEX_REMOVED | 7505 |
| ER_DUCKDB_TABLE_INDEX_UPGRADED | 7506 |
| ER_DUCKDB_ALTER_OPERATION_NOT_SUPPORTED | 7507 |
| ER_DUCKDB_SETTING_SESSION_VARIABLE | 7508 |
| ER_DUCKDB_ALTER_FLAG_REMOVED | 7509 |
| ER_DUCKDB_TABLE_ON_UPDATE_NOW_REMOVED | 7510 |
| ER_DUCKDB_COMMIT_ERROR | 7511 |
| ER_DUCKDB_ROLLBACK_ERROR | 7512 |
| ER_DUCKDB_SEND_RESULT_ERROR | 7513 |
| ER_DUCKDB_APPENDER_ERROR | 7514 |
| ER_DUCKDB_PREPARE_ERROR | 7515 |
| ER_ACCESS_DENIED_DURING_DUCKDB_CONVERT | 7516 |
| ER_DUCKDB_DATA_IMPORT_MODE | 7517 |
| **ER_VECTOR_DISABLED** | **7518** |
| **ER_DATA_INCOMPATIBLE_WITH_VECTOR** | **7519** |
| **ER_TO_VECTOR_CONVERSION** | **7520** |
| **ER_VEC_DISTANCE_TYPE** | **7521** |
| **ER_VECTOR_BINARY_FORMAT_INVALID** | **7522** |
| **ER_VECTOR_INDEX_USAGE** | **7523** |
| **ER_VECTOR_INDEX_FAILED** | **7524** |

这些错误号由 `start-error-number 7500` 加顺序位置决定。若目标机器已有错误占用 7500 以下的号段导致该指令失效（报 `start-error-number may only increase the index`），需将 7500 改为更大的未占用起始值，并同步修改源码中所有 `my_error(ER_VECTOR_*, ...)` 引用处生成的实际错误号。
