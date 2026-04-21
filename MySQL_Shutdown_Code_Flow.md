# MySQL Shutdown 代码流程详解

本文档详细分析MySQL执行shutdown命令的完整代码流程，基于AliSQL源码。

## 目录

- [1. SQL命令入口](#1-sql命令入口)
- [2. Shutdown核心函数](#2-shutdown核心函数)
- [3. 触发关闭信号](#3-触发关闭信号)
- [4. 信号处理线程](#4-信号处理线程)
- [5. 主函数清理流程](#5-主函数清理流程)
- [6. 清理函数](#6-清理函数)
- [7. 最终退出](#7-最终退出)
- [8. 完整调用链路](#8-完整调用链路)
- [9. 其他触发shutdown的方式](#9-其他触发shutdown的方式)

---

## 1. SQL命令入口

**文件**: `sql/sql_admin.cc:1832-1838`

```cpp
bool Sql_cmd_shutdown::execute(THD *thd) {
  DBUG_TRACE;
  bool res = true;
  res = !shutdown(thd, SHUTDOWN_DEFAULT);
  return res;
}
```

这是SHUTDOWN SQL语句的执行入口，调用`shutdown()`函数。

---

## 2. Shutdown核心函数

**文件**: `sql/sql_parse.cc:2554-2583`

```cpp
bool shutdown(THD *thd, enum mysql_enum_shutdown_level level) {
  // 1. 权限检查 - 需要SHUTDOWN_ACL权限
  if (check_global_access(thd, SHUTDOWN_ACL))
    goto error;

  // 2. 设置shutdown级别 (默认为SHUTDOWN_WAIT_ALL_BUFFERS)
  if (level == SHUTDOWN_DEFAULT)
    level = SHUTDOWN_WAIT_ALL_BUFFERS;

  // 3. 返回OK给客户端
  my_ok(thd);

  // 4. 记录shutdown日志
  LogErr(SYSTEM_LEVEL, ER_SERVER_SHUTDOWN_INFO, ...);

  // 5. 调用kill_mysql()触发关闭
  kill_mysql();
  return true;
}
```

主要职责：
- 检查用户是否有SHUTDOWN权限
- 记录shutdown日志
- 调用`kill_mysql()`触发实际关闭

---

## 3. 触发关闭信号

**文件**: `sql/mysqld.cc:2439-2465`

```cpp
void kill_mysql(void) {
  // 如果服务器还未完全启动，设置标志位直接退出
  if (!mysqld_server_started) {
    mysqld_process_must_end_at_startup = true;
    return;
  }

#if defined(_WIN32)
  // Windows: 使用事件对象通知
  SetEvent(hEventShutdown);
#else
  // Linux/Unix: 向信号线程发送SIGTERM信号
  pthread_kill(signal_thread_id.thread, SIGTERM);
#endif
}
```

主要职责：
- 向信号处理线程(`signal_hand`)发送SIGTERM信号(Unix)或设置事件(Windows)

---

## 4. 信号处理线程

**文件**: `sql/mysqld.cc:3644-3786`

```cpp
extern "C" void *signal_hand(void *arg) {
  // 等待SIGTERM, SIGQUIT, SIGHUP, SIGUSR1, SIGUSR2信号
  sigset_t set;
  sigaddset(&set, SIGTERM);
  sigaddset(&set, SIGQUIT);
  ...

  for (;;) {
    int sig = sigwaitinfo(&set, &sig_info);
    
    switch (sig) {
      case SIGTERM:
      case SIGQUIT:
        // 1. 设置连接循环中止标志
        set_connection_events_loop_aborted(true);
        
        // 2. 停止socket监听器
        mysql_mutex_lock(&LOCK_socket_listener_active);
        while (socket_listener_active) {
          pthread_kill(main_thread_id, SIGALRM);
          mysql_cond_wait(&COND_socket_listener_active, ...);
        }
        
        // 3. 关闭所有连接
        close_connections();
        
        // 4. 线程退出
        my_thread_exit(nullptr);
        break;
    }
  }
}
```

主要职责：
- 接收SIGTERM信号
- 设置中止标志，停止接受新连接
- 关闭所有现有连接
- 触发主线程进行清理

---

## 5. 主函数清理流程

### 5.1 调用位置

`clean_up()`和`mysqld_exit()`是在**主函数**`win_main()`中调用的。

**文件**: `sql/mysqld.cc:7308` (win_main函数定义)

```cpp
int win_main(int argc, char **argv) {
  // ... 服务器初始化代码 ...
  
  // 启动信号处理线程
  start_signal_handler();  // 行8231
  
  // ... 更多初始化 ...
  
  // 进入连接事件循环（主循环）
  mysqld_socket_acceptor->check_and_spawn_admin_connection_handler_thread();
  mysqld_socket_acceptor->connection_event_loop();  // 行8349 - 阻塞在这里
  
  // === 当connection_event_loop返回后（shutdown被触发）===
  
  server_operational_state = SERVER_SHUTTING_DOWN;  // 行8351
  
  // 保存GTID到表
  if (opt_bin_log)
    if (gtid_state->save_gtids_of_last_binlog_into_table())
      LogErr(WARNING_LEVEL, ER_CANT_SAVE_GTIDS);
  
  // 通知信号处理线程socket监听已停止
  mysql_mutex_lock(&LOCK_socket_listener_active);
  socket_listener_active = false;
  mysql_cond_broadcast(&COND_socket_listener_active);
  mysql_mutex_unlock(&LOCK_socket_listener_active);
  
  // 等待信号处理线程退出
  int ret = 0;
  if (signal_thread_id.thread != 0)
    ret = my_thread_join(&signal_thread_id, nullptr);  // 行8394-8395
  
  // === 最终清理 ===
  clean_up(true);                          // 行8401
  mysqld_exit(signal_hand_thr_exit_code);  // 行8402
}
```

### 5.2 主线程阻塞机制

服务器启动后，主线程进入连接事件循环，等待新连接。这个循环会检查`connection_events_loop_aborted`标志。

当收到SIGTERM信号后，`signal_hand()`设置`connection_events_loop_aborted=true`，导致`connection_event_loop()`返回。

### 5.3 函数调用关系

```
mysqld_main() [mysqld.cc:8521]
    │
    ├── Linux: 直接调用 win_main()
    │
    └── Windows: mysql_service() [mysqld.cc:8416]
                    │
                    └── win_main(argc, argv) [mysqld.cc:7308]
                            │
                            ├── 初始化服务器
                            ├── start_signal_handler() [行8231]
                            ├── connection_event_loop() [行8349] ← 阻塞
                            │       ↓ (shutdown触发后返回)
                            ├── clean_up(true) [行8401]
                            └── mysqld_exit() [行8402]
```

---

## 6. 清理函数

**文件**: `sql/mysqld.cc:2618-2764`

```cpp
static void clean_up(bool print_message) {
  // 设置服务器状态为SHUTTING_DOWN
  set_server_shutting_down();

  // 按顺序清理各组件:
  ha_pre_dd_shutdown();          // 存储引擎预关闭
  dd::shutdown();                // 数据字典关闭
  Events::deinit();              // 事件调度器关闭
  memcached_shutdown();          // memcached关闭
  ha_binlog_end(current_thd);    // binlog结束
  mysql_bin_log.cleanup();       // binlog清理
  plugin_shutdown();             // 插件关闭
  gtid_server_cleanup();         // GTID清理
  ha_end();                      // 存储引擎结束
  delegates_destroy();           // 观察者销毁
  table_def_free();              // 表定义释放
  mdl_destroy();                 // MDL销毁
  free_connection_acceptors();   // 连接接受器释放
  Connection_handler_manager::destroy_instance();
  Global_THD_manager::destroy_instance();
  component_infrastructure_deinit(); // 组件基础设施清理
  sys_var_end();                 // 系统变量结束
  ...
}
```

主要职责：
- 按正确顺序关闭所有MySQL组件
- 释放资源、清理内存
- 确保数据一致性

---

## 7. 最终退出

**文件**: `sql/mysqld.cc:2521-2545`

```cpp
static void mysqld_exit(int exit_code) {
  mysql_audit_finalize();        // 审计结束
  Srv_session::module_deinit();  // 会话模块结束
  delete_optimizer_cost_module(); // 优化器模块删除
  clean_up_mutexes();            // 清理互斥锁
  my_end(...);                   // mysys结束
  shutdown_performance_schema(); // PFS关闭
  LO_cleanup();                  // 锁顺序清理
  exit(exit_code);               // 进程退出
}
```

---

## 8. 完整调用链路

```
用户执行 SHUTDOWN 命令
        ↓
Sql_cmd_shutdown::execute() [sql_admin.cc:1835]
        ↓
shutdown() [sql_parse.cc:2554]
    - 权限检查 (SHUTDOWN_ACL)
    - 记录日志
        ↓
kill_mysql() [mysqld.cc:2439]
    - 发送 SIGTERM 信号给 signal_hand 线程
        ↓
signal_hand() [mysqld.cc:3644] (信号处理线程)
    - 接收 SIGTERM
    - 设置 connection_events_loop_aborted = true
    - 停止 socket 监听
    - 关闭所有连接 (close_connections)
        ↓
connection_event_loop() 返回 [mysqld.cc:8349]
    - 因为 connection_events_loop_aborted=true
    - 主循环退出，返回到 win_main()
        ↓
win_main() 继续执行 [mysqld.cc:8351-8402]
    - 设置 SERVER_SHUTTING_DOWN 状态
    - 保存 GTID
    - 等待 signal_hand 线程退出 (my_thread_join)
    - 调用 clean_up(true) 清理所有组件
    - 调用 mysqld_exit() 最终退出进程
```

---

## 9. 其他触发shutdown的方式

除了执行SQL命令`SHUTDOWN`，还有以下方式可以触发MySQL关闭：

### 9.1 mysqladmin shutdown

通过客户端工具发送shutdown命令，最终调用相同的`shutdown()`函数。

### 9.2 Ctrl-C (Windows)

**文件**: `sql/mysqld.cc:3416`

Windows下控制台事件处理器`console_event_handler()`会调用`kill_mysql()`。

### 9.3 系统信号

直接发送信号给mysqld进程：
```bash
kill -TERM <pid>   # SIGTERM
kill -QUIT <pid>   # SIGQUIT
```

这些信号会被`signal_hand()`线程捕获处理。

### 9.4 组件接口

**文件**: `sql/server_component/host_application_signal_imp.cc:54-58`

```cpp
case HOST_APPLICATION_SIGNAL_SHUTDOWN:
  LogErr(SYSTEM_LEVEL, ER_SERVER_SHUTDOWN_INFO,
         "<via component signal>", "");
  kill_mysql();
  break;
```

通过MySQL组件基础设施发送shutdown信号。

### 9.5 Clone操作

**文件**: `sql/sql_admin.cc:2084`

Clone完成后可能触发shutdown/restart：
```cpp
if (clone_shutdown) {
  LogErr(ERROR_LEVEL, ER_CLONE_SHUTDOWN_TRACE);
  kill_mysql();
}
```

---

## 关键代码位置汇总

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `Sql_cmd_shutdown::execute()` | sql/sql_admin.cc | 1832-1838 | SQL命令入口 |
| `shutdown()` | sql/sql_parse.cc | 2554-2583 | Shutdown核心函数 |
| `kill_mysql()` | sql/mysqld.cc | 2439-2465 | 触发关闭信号 |
| `signal_hand()` | sql/mysqld.cc | 3644-3786 | 信号处理线程 |
| `win_main()` | sql/mysqld.cc | 7308 | 主函数 |
| `start_signal_handler()` | sql/mysqld.cc | 8231 | 启动信号线程 |
| `connection_event_loop()` | sql/mysqld.cc | 8349 | 连接事件循环 |
| `clean_up()` | sql/mysqld.cc | 2618-2764 | 清理函数 |
| `mysqld_exit()` | sql/mysqld.cc | 2521-2545 | 最终退出 |
| `mysqld_main()` | sql/mysqld.cc | 8521 | 程序入口 |

---

## 参考文档

- AliSQL源码: https://gitee.com/mirrors/AliSQL.git
- MySQL官方文档: https://dev.mysql.com/doc/

---

*文档创建时间: 2026-04-21*