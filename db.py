"""纸坊污水站加药与达标排放系统 — 数据库层(SQLite, WAL 模式)。

所有多步写入都在 BEGIN IMMEDIATE 事务内完成:药耗、库存、水质、排放状态
要么一起提交,要么一起回滚。并发安全依赖:
  - 部分唯一索引(同一排口仅一条 OPEN 批次、全站仅一条活动班次、仅一条已批准配方)
  - 条件更新(库存扣减 UPDATE ... WHERE stock_qty >= ?)
"""
import hashlib
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('ADMIN','LEADER','OPERATOR','INSPECTOR'))
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);

-- 班次:同一时刻全站最多一条活动班次(ON_DUTY 或 HANDED_OVER)
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team TEXT NOT NULL,
  shift_name TEXT NOT NULL,
  leader_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK (status IN ('ON_DUTY','HANDED_OVER','CLOSED')),
  handover_note TEXT,
  handed_over_at TEXT,
  created_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shift_active
  ON shifts((1)) WHERE status IN ('ON_DUTY','HANDED_OVER');

CREATE TABLE IF NOT EXISTS chemicals (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  unit TEXT NOT NULL,
  stock_qty REAL NOT NULL CHECK (stock_qty >= 0)
);

-- 药剂配方:全站最多一条 APPROVED
CREATE TABLE IF NOT EXISTS formulas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('DRAFT','APPROVED','RETIRED')),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_formula_approved
  ON formulas((1)) WHERE status = 'APPROVED';

-- 配方行:按水质指标区间 [low, high) 给出投加率(g/m3)
CREATE TABLE IF NOT EXISTS formula_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  formula_id INTEGER NOT NULL REFERENCES formulas(id),
  chemical_code TEXT NOT NULL REFERENCES chemicals(code),
  param TEXT NOT NULL CHECK (param IN ('cod','ss')),
  low REAL NOT NULL,
  high REAL NOT NULL,
  dose_rate REAL NOT NULL CHECK (dose_rate >= 0)
);

CREATE TABLE IF NOT EXISTS dosing_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no TEXT NOT NULL UNIQUE,
  shift_id INTEGER NOT NULL REFERENCES shifts(id),
  status TEXT NOT NULL CHECK (status IN
    ('CREATED','DOSED','RECHECKED','DISCHARGING','DISCHARGED','FAILED','STOPPED')),
  volume_m3 REAL NOT NULL,
  formula_id INTEGER NOT NULL REFERENCES formulas(id),
  created_by INTEGER NOT NULL REFERENCES users(id),
  dosed_by INTEGER REFERENCES users(id),
  dosed_at TEXT,
  rechecked_by INTEGER REFERENCES users(id),
  rechecked_at TEXT,
  recheck_result TEXT,
  stop_reason TEXT,
  idem_key TEXT UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dosing_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES dosing_orders(id),
  chemical_code TEXT NOT NULL REFERENCES chemicals(code),
  dose_rate REAL NOT NULL,
  amount_kg REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS water_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES dosing_orders(id),
  phase TEXT NOT NULL CHECK (phase IN ('INFLUENT','EFFLUENT')),
  cod REAL, ss REAL, ph REAL, flow REAL,
  recorded_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES dosing_orders(id),
  chemical_code TEXT NOT NULL REFERENCES chemicals(code),
  qty REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_txn (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chemical_code TEXT NOT NULL REFERENCES chemicals(code),
  delta REAL NOT NULL,
  reason TEXT NOT NULL,
  ref TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rechecks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES dosing_orders(id),
  result TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
  inspector_id INTEGER NOT NULL REFERENCES users(id),
  idem_key TEXT UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outlets (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

-- 排放批次:同一排口同一时刻最多一条 OPEN(部分唯一索引保证并发开阀只成功一次)
CREATE TABLE IF NOT EXISTS discharge_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_no TEXT NOT NULL UNIQUE,
  outlet_code TEXT NOT NULL REFERENCES outlets(code),
  order_id INTEGER NOT NULL REFERENCES dosing_orders(id),
  status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED','STOPPED')),
  opened_by INTEGER NOT NULL REFERENCES users(id),
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  stop_reason TEXT,
  idem_key TEXT UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_open_batch_per_outlet
  ON discharge_batches(outlet_code) WHERE status = 'OPEN';

-- 幂等键:重复请求返回原结果
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  endpoint TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
"""

USERS = [
    ("admin",   "系统管理员", "ADMIN"),
    ("leader01", "王班长",   "LEADER"),
    ("op01",    "李投加",   "OPERATOR"),
    ("op02",    "赵副操",   "OPERATOR"),
    ("qc01",    "陈复检",   "INSPECTOR"),
]
DEFAULT_PASSWORD = "123456"

# 已批准配方 V2026.09:PAC 按进水 COD 区间、PAM 按进水 SS 区间投加
FORMULA_LINES = [
    # (chemical, param, low, high, dose_rate g/m3)
    ("PAC", "cod", 0,   150, 60),
    ("PAC", "cod", 150, 300, 90),
    ("PAC", "cod", 300, 1000000, 120),
    ("PAM", "ss",  0,   200, 1.0),
    ("PAM", "ss",  200, 400, 1.5),
    ("PAM", "ss",  400, 1000000, 2.0),
]


def hash_password(username, password):
    return hashlib.sha256(f"{username}:{password}".encode("utf-8")).hexdigest()


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path):
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
        _seed(conn)
    return conn


def _seed(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        for username, name, role in USERS:
            conn.execute(
                "INSERT INTO users(username, password_hash, name, role) VALUES (?,?,?,?)",
                (username, hash_password(username, DEFAULT_PASSWORD), name, role),
            )
        conn.execute("INSERT INTO chemicals VALUES ('PAC','聚合氯化铝','kg',500)")
        conn.execute("INSERT INTO chemicals VALUES ('PAM','聚丙烯酰胺','kg',50)")
        conn.execute("INSERT INTO outlets VALUES ('OUT-01','总排口')")
        conn.execute("INSERT INTO outlets VALUES ('OUT-02','旁路排口')")
        cur = conn.execute(
            "INSERT INTO formulas(name, version, status, created_at) VALUES (?,?,?,?)",
            ("纸坊污水加药配方", "V2026.09", "APPROVED", "2026-09-01T08:00:00+08:00"),
        )
        fid = cur.lastrowid
        for chem, param, low, high, rate in FORMULA_LINES:
            conn.execute(
                "INSERT INTO formula_lines(formula_id, chemical_code, param, low, high, dose_rate)"
                " VALUES (?,?,?,?,?,?)",
                (fid, chem, param, low, high, rate),
            )
        conn.execute(
            "INSERT INTO audit_log(actor, action, detail, created_at) VALUES (?,?,?,?)",
            ("system", "seed", "初始化用户/药剂/配方/排口", "2026-09-01T08:00:00+08:00"),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
