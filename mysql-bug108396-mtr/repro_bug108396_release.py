#!/usr/bin/env python3
"""
Best-effort reproducer for bug108396-like corruption during:
  OPTIMIZE TABLE + concurrent INSERT ... ON DUPLICATE KEY UPDATE

This targets a table shaped like:
  PRIMARY KEY(id)
  indexed virtual generated column over JSON -> mesh

It is designed for release builds where DEBUG_SYNC is unavailable.
The script widens the race window by:
  1. creating or reusing a table with the target structure
  2. preloading many rows with larger JSON payloads
  3. running OPTIMIZE TABLE in a loop
  4. hammering the table concurrently with IODKU updates

Use this on an isolated environment first.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
from typing import List, Optional, Sequence, TextIO


CORRUPTION_TEXT = "Index PRIMARY is corrupted"


def emit(message: str, log_fp: Optional[TextIO] = None) -> None:
    print(message, flush=True)
    if log_fp is not None:
        log_fp.write(message + "\n")
        log_fp.flush()


def sql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class MysqlTarget:
    def __init__(self, mysql_bin, host, port, user, password, socket, database):
        self.mysql_bin = mysql_bin
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.socket = socket
        self.database = database

    def command(self) -> List[str]:
        cmd = [
            self.mysql_bin,
            "--default-character-set=utf8mb4",
            "--batch",
            "--raw",
            "--skip-column-names",
            "--show-warnings",
            "--database",
            self.database,
            "--user",
            self.user,
        ]
        if self.socket:
            cmd.extend(["--protocol=SOCKET", "--socket", self.socket])
        else:
            cmd.extend(["--protocol=TCP", "--host", self.host or "127.0.0.1", "--port", str(self.port)])
        return cmd

    def run_sql(self, sql: str, check: bool = True):
        env = os.environ.copy()
        if self.password is not None:
            env["MYSQL_PWD"] = self.password
        result = subprocess.run(
            self.command(),
            input=sql,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                "mysql command failed\n"
                f"returncode={result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result


def build_table_ddl(table: str) -> str:
    tbl = sql_ident(table)
    return f"""
DROP TABLE IF EXISTS {tbl};
CREATE TABLE {tbl} (
  `$createTime` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `$json` JSON NOT NULL,
  `$updateTime` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `id` VARCHAR(200) CHARACTER SET utf8mb4 NOT NULL,
  `mesh` VARCHAR(40) CHARACTER SET utf8mb4
    GENERATED ALWAYS AS (
      JSON_UNQUOTE(JSON_EXTRACT(`$json`, _utf8mb4'$.mesh'))
    ) VIRTUAL COMMENT 'xx',
  PRIMARY KEY (`id`),
  KEY `idx_by_mesh` (`mesh`)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
""".strip()


def make_json_payload(mesh: str, seq: int, pad_size: int) -> str:
    payload = {
        "mesh": mesh,
        "seq": seq,
        "pad": "x" * pad_size,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def make_row_id(id_prefix: str, i: int) -> str:
    return f"{id_prefix}{i:08d}"


def preload_rows(
    target: MysqlTarget,
    table: str,
    seed_rows: int,
    mesh_count: int,
    pad_size: int,
    batch_size: int,
    id_prefix: str,
    log_fp: Optional[TextIO],
) -> None:
    if seed_rows <= 0:
        return
    emit(f"[setup] preloading {seed_rows} rows into {table} ...", log_fp)
    tbl = sql_ident(table)
    start = time.time()
    inserted = 0
    while inserted < seed_rows:
        upper = min(inserted + batch_size, seed_rows)
        values = []
        for i in range(inserted, upper):
            row_id = make_row_id(id_prefix, i)
            mesh = f"mesh-{i % mesh_count:04d}"
            payload = make_json_payload(mesh=mesh, seq=i, pad_size=pad_size)
            values.append(f"({sql_quote(payload)}, {sql_quote(row_id)})")
        sql = (
            f"INSERT INTO {tbl} (`$json`, `id`) VALUES\n"
            + ",\n".join(values)
            + "\nON DUPLICATE KEY UPDATE `$json` = VALUES(`$json`);"
        )
        target.run_sql(sql)
        inserted = upper
        if inserted % max(batch_size * 10, 1) == 0 or inserted == seed_rows:
            emit(f"[setup] inserted {inserted}/{seed_rows}", log_fp)
    emit(f"[setup] preload finished in {time.time() - start:.1f}s", log_fp)


def contains_corruption_text(output: str) -> bool:
    return CORRUPTION_TEXT.lower() in output.lower()


def writer_loop(
    worker_id: int,
    target: MysqlTarget,
    table: str,
    seed_rows: int,
    mesh_count: int,
    hot_id_count: int,
    pad_size: int,
    id_prefix: str,
    stop_event: threading.Event,
    log_fp: Optional[TextIO],
) -> None:
    tbl = sql_ident(table)
    rnd = random.Random(worker_id * 100003 + int(time.time()))
    hot_span = max(1, min(hot_id_count, max(seed_rows, hot_id_count)))
    local_count = 0
    while not stop_event.is_set():
        key_num = rnd.randrange(hot_span)
        row_id = make_row_id(id_prefix, key_num)
        mesh = f"mesh-{rnd.randrange(mesh_count):04d}"
        payload = make_json_payload(mesh=mesh, seq=local_count, pad_size=pad_size)
        sql = f"""
INSERT INTO {tbl} (`id`, `$json`)
VALUES ({sql_quote(row_id)}, {sql_quote(payload)})
ON DUPLICATE KEY UPDATE
  `$json` = VALUES(`$json`);
"""
        try:
            result = target.run_sql(sql, check=False)
        except Exception as exc:  # pragma: no cover - defensive logging
            emit(f"[writer-{worker_id}] unexpected exception: {exc}", log_fp)
            stop_event.set()
            return

        text = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0:
            emit(f"[writer-{worker_id}] mysql exited with {result.returncode}", log_fp)
            emit(text.rstrip(), log_fp)
            stop_event.set()
            return
        if contains_corruption_text(text):
            emit(f"[writer-{worker_id}] observed corruption text outside OPTIMIZE TABLE", log_fp)
            emit(text.rstrip(), log_fp)
            stop_event.set()
            return
        local_count += 1
        if local_count % 1000 == 0:
            emit(f"[writer-{worker_id}] statements={local_count}", log_fp)


def optimizer_loop(
    target: MysqlTarget,
    table: str,
    sleep_seconds: float,
    max_optimize_loops: int,
    stop_event: threading.Event,
    hit_event: threading.Event,
    log_fp: Optional[TextIO],
) -> None:
    tbl = sql_ident(table)
    optimize_count = 0
    while not stop_event.is_set() and optimize_count < max_optimize_loops:
        optimize_count += 1
        sql = f"OPTIMIZE TABLE {tbl}; SHOW WARNINGS;"
        result = target.run_sql(sql, check=False)
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        emit(f"[optimize] round={optimize_count} returncode={result.returncode}", log_fp)
        if log_fp is not None:
            emit(f"[optimize] round={optimize_count} output begin", log_fp)
            emit(text.rstrip(), log_fp)
            emit(f"[optimize] round={optimize_count} output end", log_fp)
        if contains_corruption_text(text):
            emit("[optimize] hit target error", log_fp)
            if log_fp is None:
                emit(text.rstrip(), None)
            hit_event.set()
            stop_event.set()
            return
        if result.returncode != 0:
            emit("[optimize] mysql exited non-zero", log_fp)
            if log_fp is None:
                emit(text.rstrip(), None)
            stop_event.set()
            return
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    stop_event.set()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Release-mode reproducer for OPTIMIZE TABLE + IODKU corruption.")
    parser.add_argument("--mysql-bin", default="mysql", help="Path to mysql client binary.")
    parser.add_argument("--host", default="127.0.0.1", help="MySQL host when using TCP.")
    parser.add_argument("--port", type=int, default=3306, help="MySQL TCP port.")
    parser.add_argument("--socket", default=None, help="MySQL socket path. If set, socket protocol is used.")
    parser.add_argument("--user", required=True, help="MySQL user.")
    parser.add_argument("--password", default=None, help="MySQL password. Prefer MYSQL_PWD in shell if possible.")
    parser.add_argument("--database", default="test", help="Database name.")
    parser.add_argument(
        "--table",
        default="event_subscriptionoffset_repro",
        help="Target table name. Default is a safe repro table, not your real business table.",
    )
    parser.add_argument(
        "--use-existing-table",
        action="store_true",
        help="Do not drop/create the table. Run directly against an existing table.",
    )
    parser.add_argument("--seed-rows", type=int, default=20000, help="Rows to preload before concurrency starts.")
    parser.add_argument("--seed-batch-size", type=int, default=500, help="Batch size for preload inserts.")
    parser.add_argument("--writer-threads", type=int, default=8, help="Concurrent IODKU worker threads.")
    parser.add_argument("--hot-id-count", type=int, default=128, help="Number of hot PK values targeted by IODKU.")
    parser.add_argument("--mesh-count", type=int, default=64, help="Distinct mesh values used in JSON payloads.")
    parser.add_argument("--json-pad-size", type=int, default=4096, help="Extra JSON payload size to slow OPTIMIZE.")
    parser.add_argument(
        "--id-prefix",
        default="repro-id-",
        help="Prefix for synthetic primary-key values used by this reproducer.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional log file path. When set, OPTIMIZE TABLE output and progress are appended there.",
    )
    parser.add_argument("--max-optimize-loops", type=int, default=1000, help="Maximum OPTIMIZE TABLE attempts.")
    parser.add_argument("--optimize-sleep", type=float, default=0.0, help="Sleep between OPTIMIZE TABLE rounds.")
    parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=0.0,
        help="Optional wall-clock timeout for the whole run. 0 means no limit.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    log_fp: Optional[TextIO] = None
    if args.log_file:
        log_fp = open(args.log_file, "a", encoding="utf-8")

    target = MysqlTarget(
        mysql_bin=args.mysql_bin,
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        socket=args.socket,
        database=args.database,
    )

    try:
        if not args.use_existing_table:
            emit("[setup] creating fresh repro table", log_fp)
            target.run_sql(build_table_ddl(args.table))
            preload_rows(
                target=target,
                table=args.table,
                seed_rows=args.seed_rows,
                mesh_count=args.mesh_count,
                pad_size=args.json_pad_size,
                batch_size=args.seed_batch_size,
                id_prefix=args.id_prefix,
                log_fp=log_fp,
            )
        else:
            emit("[setup] using existing table as-is", log_fp)
            preload_rows(
                target=target,
                table=args.table,
                seed_rows=args.hot_id_count,
                mesh_count=args.mesh_count,
                pad_size=args.json_pad_size,
                batch_size=min(args.seed_batch_size, max(args.hot_id_count, 1)),
                id_prefix=args.id_prefix,
                log_fp=log_fp,
            )

        stop_event = threading.Event()
        hit_event = threading.Event()
        threads: List[threading.Thread] = []

        optimizer = threading.Thread(
            target=optimizer_loop,
            name="optimizer",
            kwargs=dict(
                target=target,
                table=args.table,
                sleep_seconds=args.optimize_sleep,
                max_optimize_loops=args.max_optimize_loops,
                stop_event=stop_event,
                hit_event=hit_event,
                log_fp=log_fp,
            ),
            daemon=True,
        )
        optimizer.start()
        threads.append(optimizer)

        for worker_id in range(args.writer_threads):
            t = threading.Thread(
                target=writer_loop,
                name=f"writer-{worker_id}",
                kwargs=dict(
                    worker_id=worker_id,
                    target=target,
                    table=args.table,
                    seed_rows=args.seed_rows,
                    mesh_count=args.mesh_count,
                    hot_id_count=args.hot_id_count,
                    pad_size=args.json_pad_size,
                    id_prefix=args.id_prefix,
                    stop_event=stop_event,
                    log_fp=log_fp,
                ),
                daemon=True,
            )
            t.start()
            threads.append(t)

        started_at = time.time()
        try:
            while any(t.is_alive() for t in threads):
                if stop_event.wait(timeout=1.0):
                    break
                if args.max_runtime_seconds > 0 and time.time() - started_at >= args.max_runtime_seconds:
                    emit(f"[main] reached max runtime {args.max_runtime_seconds:.1f}s, stopping ...", log_fp)
                    stop_event.set()
                    break
        except KeyboardInterrupt:
            emit("\n[main] interrupted, stopping threads ...", log_fp)
            stop_event.set()

        for t in threads:
            t.join(timeout=5.0)

        elapsed = time.time() - started_at
        if hit_event.is_set():
            emit(f"[main] reproduced target corruption text after {elapsed:.1f}s", log_fp)
            return 0

        emit(f"[main] finished after {elapsed:.1f}s without observing '{CORRUPTION_TEXT}'", log_fp)
        emit("[main] try increasing seed rows, JSON pad size, hot writer threads, or optimize loop count", log_fp)
        return 1
    finally:
        if log_fp is not None:
            log_fp.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
