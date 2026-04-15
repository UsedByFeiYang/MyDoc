# bug108396 复现说明

这个目录包含 3 个文件：

- `bug108396.test`
- `bug108396.result`
- `repro_bug108396_release.py`

其中：

- `bug108396.test/.result` 是在 `mysql-8.0.22` 上验证过的 MTR 用例
- `repro_bug108396_release.py` 是给 release/类生产环境用的概率复现脚本

## 脚本用途

脚本通过并发执行：

- `OPTIMIZE TABLE`
- `INSERT ... ON DUPLICATE KEY UPDATE`

去放大 `Index PRIMARY is corrupted` 的复现概率。

脚本只有在 `OPTIMIZE TABLE` 的输出里命中：

`Index PRIMARY is corrupted`

时，才会判定为成功复现并立即退出。

## 最推荐的试跑命令

下面这个命令适合直接打现有业务表，并通过 `IP + 端口` 连接：

```bash
python3 repro_bug108396_release.py \
  --host 你的IP \
  --port 你的端口 \
  --user 你的用户 \
  --password '你的密码' \
  --database 你的库名 \
  --table event_subscriptionoffset \
  --use-existing-table \
  --writer-threads 16 \
  --hot-id-count 256 \
  --json-pad-size 8192 \
  --max-optimize-loops 5000 \
  --max-runtime-seconds 1800 \
  --log-file /tmp/repro_bug108396_release.log
```

## 参数建议

- `--use-existing-table`
  直接对现有表施压，不重建表。
- `--writer-threads`
  并发 `IODKU` 线程数。优先从 `8/16/32` 逐步加大。
- `--hot-id-count`
  热点主键数量。值越小，越容易形成高冲突更新。
- `--json-pad-size`
  JSON 负载填充大小。适当加大可以拉长 `OPTIMIZE TABLE` 的窗口。
- `--max-runtime-seconds`
  整体最长运行时间，避免脚本无限跑。
- `--log-file`
  记录每轮 `OPTIMIZE TABLE` 的输出，方便和审计日志对时间线。

## 结果判断

- 返回码 `0`
  表示脚本在 `OPTIMIZE TABLE` 输出中捕获到了目标错误。
- 返回码 `1`
  表示本轮没有命中目标错误，或者达到了时间/轮数上限。

## 日志文件里看什么

重点关注类似下面的输出：

```text
test.xxx  optimize  error   Index PRIMARY is corrupted
Error     1712      Index PRIMARY is corrupted
```

如果日志里只有：

```text
optimize  status    OK
```

说明这一轮没有命中。

## 使用建议

- 优先在隔离环境或低风险时段执行。
- 建议先从较短的 `--max-runtime-seconds` 开始试跑。
- 如果没有复现，优先增大：
  `--writer-threads`
  `--hot-id-count`
  `--json-pad-size`
  `--max-optimize-loops`

