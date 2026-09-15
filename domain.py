"""纸坊污水站加药与达标排放系统 — 领域逻辑。

每个公开函数假定调用方已开启事务(BEGIN IMMEDIATE),函数内的全部写入
(药耗/库存/水质/排放状态)随调用方一起提交或回滚。
"""
import hashlib
import json
import math
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))

# 仪表量程:进水/出水指标超出量程或格式错误 → 整单拒绝(422)
INFLUENT_SPEC = {"cod": (0.0, 1000.0), "ss": (0.0, 800.0), "ph": (0.0, 14.0), "flow": (0.0, 5000.0)}
EFFLUENT_SPEC = {"cod": (0.0, 150.0), "ss": (0.0, 100.0), "ph": (0.0, 14.0)}
FIELD_LABELS = {"cod": "COD", "ss": "SS", "ph": "pH", "flow": "流量"}
VOLUME_RANGE = (0.0, 100000.0)  # m3

# 达标排放限值
STD_COD, STD_SS, STD_PH = 50.0, 10.0, (6.0, 9.0)

DEFAULT_PASSWORD_HINT = "123456"

ORDER_STATUS_LABELS = {
    "CREATED": "已开单", "DOSED": "已投加", "RECHECKED": "复检合格",
    "DISCHARGING": "排放中", "DISCHARGED": "排放完成", "FAILED": "复检不合格", "STOPPED": "已停排",
}


class DomainError(Exception):
    def __init__(self, status, message, errors=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or []


def now_iso():
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def hash_password(username, password):
    return hashlib.sha256(f"{username}:{password}".encode("utf-8")).hexdigest()


def request_hash(body):
    clean = {k: v for k, v in (body or {}).items() if k != "idempotency_key"}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------------------------------------------------------------- 校验

def _validate_number(value, lo, hi, name, errors):
    if value is None:
        errors.append(f"{name} 缺失")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{name} 格式错误(需为数字)")
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        errors.append(f"{name} 格式错误(非法数值)")
        return None
    if value < lo or value > hi:
        errors.append(f"{name}={value} 超出量程[{lo:g},{hi:g}]")
        return None
    return float(value)


def validate_quality(data, spec, label):
    """进水/出水指标校验:任一指标缺失、格式错误或超出量程 → 整单拒绝。"""
    if not isinstance(data, dict):
        raise DomainError(422, f"{label}水质数据格式错误,整单拒绝", [f"{label}水质需为对象"])
    errors, out = [], {}
    for field, (lo, hi) in spec.items():
        name = f"{label}{FIELD_LABELS.get(field, field)}"
        v = _validate_number(data.get(field), lo, hi, name, errors)
        if v is not None:
            out[field] = v
    if errors:
        raise DomainError(422, f"{label}水质指标校验失败,整单拒绝", errors)
    return out


def validate_volume(value):
    errors = []
    v = _validate_number(value, VOLUME_RANGE[0], VOLUME_RANGE[1], "处理水量(m3)", errors)
    if errors:
        raise DomainError(422, "处理水量校验失败,整单拒绝", errors)
    if v <= 0:
        raise DomainError(422, "处理水量校验失败,整单拒绝", ["处理水量必须大于0"])
    return v


def meets_standard(eff):
    return eff["cod"] <= STD_COD and eff["ss"] <= STD_SS and STD_PH[0] <= eff["ph"] <= STD_PH[1]


# ---------------------------------------------------------------- 通用

def audit(conn, actor, action, detail=""):
    conn.execute(
        "INSERT INTO audit_log(actor, action, detail, created_at) VALUES (?,?,?,?)",
        (actor, action, detail, now_iso()),
    )


def next_no(conn, name, prefix):
    row = conn.execute(
        "INSERT INTO counters(name, value) VALUES (?, 1)"
        " ON CONFLICT(name) DO UPDATE SET value = value + 1 RETURNING value",
        (name,),
    ).fetchone()
    return f"{prefix}-{datetime.now(CN_TZ).strftime('%Y%m%d')}-{row['value']:04d}"


def active_shift(conn):
    return conn.execute(
        "SELECT s.*, u.name AS leader_name FROM shifts s JOIN users u ON u.id = s.leader_id"
        " WHERE s.status IN ('ON_DUTY','HANDED_OVER') ORDER BY s.id DESC LIMIT 1"
    ).fetchone()


def require_on_duty(conn):
    s = active_shift(conn)
    if not s or s["status"] != "ON_DUTY":
        raise DomainError(409, "无在岗班次:班组完成交接并接班后才能进行业务操作")
    return s


def get_order(conn, order_id):
    row = conn.execute("SELECT * FROM dosing_orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        raise DomainError(404, f"加药单不存在: {order_id}")
    return row


def order_detail(conn, order_id):
    o = get_order(conn, order_id)
    lines = [dict(r) for r in conn.execute(
        "SELECT chemical_code, dose_rate, amount_kg FROM dosing_lines WHERE order_id = ?", (order_id,))]
    samples = [dict(r) for r in conn.execute(
        "SELECT phase, cod, ss, ph, flow, created_at FROM water_samples WHERE order_id = ? ORDER BY id", (order_id,))]
    rechecks = [dict(r) for r in conn.execute(
        "SELECT r.id, r.result, r.created_at, u.name AS inspector"
        " FROM rechecks r JOIN users u ON u.id = r.inspector_id WHERE r.order_id = ? ORDER BY r.id", (order_id,))]
    batches = [dict(r) for r in conn.execute(
        "SELECT batch_no, outlet_code, status, opened_at, closed_at, stop_reason"
        " FROM discharge_batches WHERE order_id = ? ORDER BY id", (order_id,))]
    d = dict(o)
    d["status_label"] = ORDER_STATUS_LABELS.get(o["status"], o["status"])
    d["lines"] = lines
    d["samples"] = samples
    d["rechecks"] = rechecks
    d["batches"] = batches
    return d


# ---------------------------------------------------------------- 认证

def login(conn, username, password):
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username or "",)).fetchone()
    if not row or row["password_hash"] != hash_password(username or "", password or ""):
        raise DomainError(401, "用户名或密码错误")
    token = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at) VALUES (?,?,?)",
        (token, row["id"], now_iso()),
    )
    audit(conn, row["username"], "login", "登录系统")
    return token, {"id": row["id"], "username": row["username"], "name": row["name"], "role": row["role"]}


def user_by_token(conn, token):
    if not token:
        return None
    return conn.execute(
        "SELECT u.id, u.username, u.name, u.role FROM sessions s"
        " JOIN users u ON u.id = s.user_id WHERE s.token = ?", (token,),
    ).fetchone()


# ---------------------------------------------------------------- 交接班

def handover(conn, user, note):
    s = active_shift(conn)
    if not s or s["status"] != "ON_DUTY":
        raise DomainError(409, "当前无在岗班次可交班")
    conn.execute(
        "UPDATE shifts SET status='HANDED_OVER', handover_note=?, handed_over_at=?"
        " WHERE id=? AND status='ON_DUTY'",
        (note or "", now_iso(), s["id"]),
    )
    audit(conn, user["username"], "handover", f"{s['team']}/{s['shift_name']} 完成交接: {note or '-'}")
    return dict(conn.execute("SELECT * FROM shifts WHERE id=?", (s["id"],)).fetchone())


def takeover(conn, user, team, shift_name, note):
    if not team or not str(team).strip():
        raise DomainError(422, "接班失败", ["班组名称缺失"])
    if not shift_name or not str(shift_name).strip():
        raise DomainError(422, "接班失败", ["班次名称缺失"])
    s = active_shift(conn)
    if s and s["status"] == "ON_DUTY":
        raise DomainError(409, "当前班组尚未完成交接,不能接班")
    if s:  # 上一班已完成交接(HANDED_OVER) → 闭环
        conn.execute("UPDATE shifts SET status='CLOSED', closed_at=? WHERE id=?", (now_iso(), s["id"]))
    cur = conn.execute(
        "INSERT INTO shifts(team, shift_name, leader_id, status, created_at) VALUES (?,?,?,'ON_DUTY',?)",
        (str(team).strip(), str(shift_name).strip(), user["id"], now_iso()),
    )
    audit(conn, user["username"], "takeover", f"{team}/{shift_name} 接班上岗")
    return dict(conn.execute("SELECT * FROM shifts WHERE id=?", (cur.lastrowid,)).fetchone())


# ---------------------------------------------------------------- 加药

def compute_lines(conn, formula_id, influent, volume):
    """按当前批准配方的水质区间计算各药剂投加量(kg)。"""
    rows = conn.execute(
        "SELECT * FROM formula_lines WHERE formula_id=? ORDER BY id", (formula_id,)).fetchall()
    lines = []
    for r in rows:
        v = influent[r["param"]]
        if r["low"] <= v < r["high"]:
            lines.append({
                "chemical_code": r["chemical_code"],
                "dose_rate": r["dose_rate"],
                "amount_kg": round(r["dose_rate"] * volume / 1000.0, 3),
            })
    covered = {l["chemical_code"] for l in lines}
    missing = {r["chemical_code"] for r in rows} - covered
    if missing:
        raise DomainError(409, f"当前批准配方的水质区间未覆盖本次水质,无法计算投加量: {','.join(sorted(missing))}")
    return lines


def create_order(conn, user, body):
    shift = require_on_duty(conn)
    volume = validate_volume(body.get("volume_m3"))
    influent = validate_quality(body.get("influent"), INFLUENT_SPEC, "进水")
    formula = conn.execute("SELECT * FROM formulas WHERE status='APPROVED'").fetchone()
    if not formula:
        raise DomainError(409, "无已批准的药剂配方,无法开单")
    lines = compute_lines(conn, formula["id"], influent, volume)
    order_no = next_no(conn, "order", "DO")
    cur = conn.execute(
        "INSERT INTO dosing_orders(order_no, shift_id, status, volume_m3, formula_id,"
        " created_by, idem_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (order_no, shift["id"], "CREATED", volume, formula["id"], user["id"],
         body.get("idempotency_key"), now_iso()),
    )
    oid = cur.lastrowid
    for l in lines:
        conn.execute(
            "INSERT INTO dosing_lines(order_id, chemical_code, dose_rate, amount_kg) VALUES (?,?,?,?)",
            (oid, l["chemical_code"], l["dose_rate"], l["amount_kg"]),
        )
    conn.execute(
        "INSERT INTO water_samples(order_id, phase, cod, ss, ph, flow, recorded_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (oid, "INFLUENT", influent["cod"], influent["ss"], influent["ph"], influent["flow"],
         user["id"], now_iso()),
    )
    audit(conn, user["username"], "create_order",
          f"{order_no} 水量{volume}m3 配方{formula['version']} "
          + " ".join(f"{l['chemical_code']}={l['amount_kg']}kg" for l in lines))
    return order_detail(conn, oid)


def execute_dosing(conn, user, order_id):
    """执行投加=出库:药耗、库存、水质、状态同一事务;库存不足整单回滚。"""
    require_on_duty(conn)
    o = get_order(conn, order_id)
    if o["status"] != "CREATED":
        raise DomainError(409, f"当前状态[{ORDER_STATUS_LABELS.get(o['status'], o['status'])}]不可执行投加")
    lines = conn.execute("SELECT * FROM dosing_lines WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    for l in lines:
        cur = conn.execute(
            "UPDATE chemicals SET stock_qty = ROUND(stock_qty - ?, 3)"
            " WHERE code=? AND stock_qty >= ?",
            (l["amount_kg"], l["chemical_code"], l["amount_kg"]),
        )
        if cur.rowcount == 0:
            stock = conn.execute("SELECT stock_qty FROM chemicals WHERE code=?",
                                 (l["chemical_code"],)).fetchone()["stock_qty"]
            raise DomainError(409, f"库存不足,不得出库: {l['chemical_code']} 需要 {l['amount_kg']}kg,"
                                   f" 现存 {stock:g}kg")
        conn.execute(
            "INSERT INTO consumptions(order_id, chemical_code, qty, created_at) VALUES (?,?,?,?)",
            (order_id, l["chemical_code"], l["amount_kg"], now_iso()),
        )
        conn.execute(
            "INSERT INTO inventory_txn(chemical_code, delta, reason, ref, created_at) VALUES (?,?,?,?,?)",
            (l["chemical_code"], -l["amount_kg"], "投加出库", o["order_no"], now_iso()),
        )
    conn.execute(
        "UPDATE dosing_orders SET status='DOSED', dosed_by=?, dosed_at=? WHERE id=?",
        (user["id"], now_iso(), order_id),
    )
    audit(conn, user["username"], "dose",
          f"{o['order_no']} 投加出库 " + " ".join(f"{l['chemical_code']}-{l['amount_kg']}kg" for l in lines))
    return order_detail(conn, order_id)


# ---------------------------------------------------------------- 复检

def recheck(conn, user, order_id, body):
    """复检:投加人与复检人不能为同一人;不合格自动停排(同一事务)。"""
    require_on_duty(conn)
    o = get_order(conn, order_id)
    if o["status"] not in ("DOSED", "DISCHARGING"):
        raise DomainError(409, f"当前状态[{ORDER_STATUS_LABELS.get(o['status'], o['status'])}]不可复检")
    if o["dosed_by"] == user["id"]:
        raise DomainError(409, "投加人与复检人不能为同一人")
    eff = validate_quality(body.get("effluent"), EFFLUENT_SPEC, "出水")
    result = "PASS" if meets_standard(eff) else "FAIL"
    cur = conn.execute(
        "INSERT INTO rechecks(order_id, result, inspector_id, idem_key, created_at) VALUES (?,?,?,?,?)",
        (order_id, result, user["id"], body.get("idempotency_key"), now_iso()),
    )
    conn.execute(
        "INSERT INTO water_samples(order_id, phase, cod, ss, ph, flow, recorded_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (order_id, "EFFLUENT", eff["cod"], eff["ss"], eff["ph"], None, user["id"], now_iso()),
    )
    stopped_batch = None
    if result == "PASS":
        if o["status"] == "DOSED":
            conn.execute(
                "UPDATE dosing_orders SET status='RECHECKED', recheck_result='PASS',"
                " rechecked_by=?, rechecked_at=? WHERE id=?",
                (user["id"], now_iso(), order_id),
            )
    else:
        batch = conn.execute(
            "SELECT * FROM discharge_batches WHERE order_id=? AND status='OPEN'", (order_id,),
        ).fetchone()
        if batch:
            conn.execute(
                "UPDATE discharge_batches SET status='STOPPED', closed_at=?, stop_reason=?"
                " WHERE id=? AND status='OPEN'",
                (now_iso(), "复检不合格自动停排", batch["id"]),
            )
            stopped_batch = batch["batch_no"]
        conn.execute(
            "UPDATE dosing_orders SET status=?, recheck_result='FAIL', rechecked_by=?,"
            " rechecked_at=?, stop_reason=? WHERE id=?",
            ("STOPPED" if stopped_batch else "FAILED", user["id"], now_iso(),
             "复检不合格自动停排" if stopped_batch else "复检不合格", order_id),
        )
    audit(conn, user["username"], "recheck",
          f"{o['order_no']} 复检{'合格' if result == 'PASS' else '不合格'}"
          + (f",自动停排批次{stopped_batch}" if stopped_batch else ""))
    detail = order_detail(conn, order_id)
    detail["recheck_id"] = cur.lastrowid
    detail["recheck_result"] = result
    detail["auto_stopped_batch"] = stopped_batch
    return detail


# ---------------------------------------------------------------- 排放

def open_valve(conn, user, body):
    """开阀排放:未复核合格不得开阀;同一排口同一时刻只能有一个批次。"""
    require_on_duty(conn)
    order_id = body.get("order_id")
    outlet_code = body.get("outlet_code")
    if not order_id or not outlet_code:
        raise DomainError(422, "开阀失败", ["order_id 与 outlet_code 必填"])
    o = get_order(conn, order_id)
    if o["status"] in ("CREATED", "DOSED"):
        raise DomainError(409, "未复核合格,不得开阀")
    if o["status"] != "RECHECKED":
        raise DomainError(409, f"当前状态[{ORDER_STATUS_LABELS.get(o['status'], o['status'])}]不可开阀")
    outlet = conn.execute("SELECT * FROM outlets WHERE code=?", (outlet_code,)).fetchone()
    if not outlet:
        raise DomainError(404, f"排口不存在: {outlet_code}")
    existing = conn.execute(
        "SELECT * FROM discharge_batches WHERE outlet_code=? AND status='OPEN'", (outlet_code,),
    ).fetchone()
    if existing:
        raise DomainError(409, f"排口{outlet_code}已有批次{existing['batch_no']}在排,"
                               f"同一时刻只能有一个批次")
    batch_no = next_no(conn, "batch", "DC")
    try:
        conn.execute(
            "INSERT INTO discharge_batches(batch_no, outlet_code, order_id, status,"
            " opened_by, opened_at, idem_key) VALUES (?,?,?,'OPEN',?,?,?)",
            (batch_no, outlet_code, order_id, user["id"], now_iso(), body.get("idempotency_key")),
        )
    except sqlite3.IntegrityError:
        raise DomainError(409, f"排口{outlet_code}已有批次在排(并发冲突),本次开阀未生效")
    conn.execute(
        "UPDATE dosing_orders SET status='DISCHARGING' WHERE id=? AND status='RECHECKED'",
        (order_id,),
    )
    audit(conn, user["username"], "open_valve", f"{batch_no} 排口{outlet_code} 开阀,关联{o['order_no']}")
    return dict(conn.execute("SELECT * FROM discharge_batches WHERE batch_no=?", (batch_no,)).fetchone())


def close_valve(conn, user, batch_no):
    require_on_duty(conn)
    b = conn.execute("SELECT * FROM discharge_batches WHERE batch_no=?", (batch_no,)).fetchone()
    if not b:
        raise DomainError(404, f"排放批次不存在: {batch_no}")
    if b["status"] != "OPEN":
        raise DomainError(409, f"批次{batch_no}当前状态[{b['status']}]不可关阀")
    conn.execute(
        "UPDATE discharge_batches SET status='CLOSED', closed_at=? WHERE id=? AND status='OPEN'",
        (now_iso(), b["id"]),
    )
    conn.execute(
        "UPDATE dosing_orders SET status='DISCHARGED' WHERE id=? AND status='DISCHARGING'",
        (b["order_id"],),
    )
    audit(conn, user["username"], "close_valve", f"{batch_no} 关阀,排放完成")
    return dict(conn.execute("SELECT * FROM discharge_batches WHERE id=?", (b["id"],)).fetchone())


def emergency_stop(conn, user, body):
    """异常停排:人工紧急停排(复检不合格自动停排见 recheck)。"""
    require_on_duty(conn)
    batch_no = body.get("batch_no")
    reason = (body.get("reason") or "人工紧急停排").strip()
    b = conn.execute("SELECT * FROM discharge_batches WHERE batch_no=?", (batch_no,)).fetchone()
    if not b:
        raise DomainError(404, f"排放批次不存在: {batch_no}")
    if b["status"] != "OPEN":
        raise DomainError(409, f"批次{batch_no}当前状态[{b['status']}]不可停排")
    conn.execute(
        "UPDATE discharge_batches SET status='STOPPED', closed_at=?, stop_reason=?"
        " WHERE id=? AND status='OPEN'",
        (now_iso(), reason, b["id"]),
    )
    conn.execute(
        "UPDATE dosing_orders SET status='STOPPED', stop_reason=? WHERE id=? AND status='DISCHARGING'",
        (reason, b["order_id"]),
    )
    audit(conn, user["username"], "emergency_stop", f"{batch_no} 异常停排: {reason}")
    return dict(conn.execute("SELECT * FROM discharge_batches WHERE id=?", (b["id"],)).fetchone())


# ---------------------------------------------------------------- 库存

def restock(conn, user, code, qty):
    errors = []
    q = _validate_number(qty, 0.0, 1000000.0, "入库数量", errors)
    if errors or not q or q <= 0:
        raise DomainError(422, "入库失败", errors or ["入库数量必须大于0"])
    cur = conn.execute(
        "UPDATE chemicals SET stock_qty = ROUND(stock_qty + ?, 3) WHERE code=?", (q, code))
    if cur.rowcount == 0:
        raise DomainError(404, f"药剂不存在: {code}")
    conn.execute(
        "INSERT INTO inventory_txn(chemical_code, delta, reason, ref, created_at) VALUES (?,?,?,?,?)",
        (code, q, "入库", None, now_iso()),
    )
    audit(conn, user["username"], "restock", f"{code} 入库 {q}kg")
    return dict(conn.execute("SELECT * FROM chemicals WHERE code=?", (code,)).fetchone())


# ---------------------------------------------------------------- 查询

def get_state(conn):
    shift = active_shift(conn)
    chemicals = [dict(r) for r in conn.execute("SELECT * FROM chemicals ORDER BY code")]
    formula = conn.execute("SELECT * FROM formulas WHERE status='APPROVED'").fetchone()
    formula_lines = []
    if formula:
        formula_lines = [dict(r) for r in conn.execute(
            "SELECT chemical_code, param, low, high, dose_rate FROM formula_lines"
            " WHERE formula_id=? ORDER BY id", (formula["id"],))]
    outlets = []
    for o in conn.execute("SELECT * FROM outlets ORDER BY code"):
        d = dict(o)
        b = conn.execute(
            "SELECT batch_no, order_id, opened_at FROM discharge_batches"
            " WHERE outlet_code=? AND status='OPEN'", (o["code"],)).fetchone()
        d["open_batch"] = dict(b) if b else None
        outlets.append(d)
    orders = []
    for r in conn.execute(
            "SELECT o.*, u.name AS creator FROM dosing_orders o JOIN users u ON u.id=o.created_by"
            " ORDER BY o.id DESC LIMIT 50"):
        d = dict(r)
        d["status_label"] = ORDER_STATUS_LABELS.get(r["status"], r["status"])
        d["lines"] = [dict(x) for x in conn.execute(
            "SELECT chemical_code, dose_rate, amount_kg FROM dosing_lines WHERE order_id=?", (r["id"],))]
        orders.append(d)
    batches = [dict(r) for r in conn.execute(
        "SELECT * FROM discharge_batches ORDER BY id DESC LIMIT 50")]
    rechecks = [dict(r) for r in conn.execute(
        "SELECT r.*, u.name AS inspector FROM rechecks r JOIN users u ON u.id=r.inspector_id"
        " ORDER BY r.id DESC LIMIT 50")]
    consumptions = [dict(r) for r in conn.execute(
        "SELECT c.*, o.order_no FROM consumptions c JOIN dosing_orders o ON o.id=c.order_id"
        " ORDER BY c.id DESC LIMIT 50")]
    inventory_txn = [dict(r) for r in conn.execute(
        "SELECT * FROM inventory_txn ORDER BY id DESC LIMIT 50")]
    shifts = [dict(r) for r in conn.execute(
        "SELECT s.*, u.name AS leader_name FROM shifts s JOIN users u ON u.id=s.leader_id"
        " ORDER BY s.id DESC LIMIT 20")]
    audit_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 40")]
    return {
        "now": now_iso(),
        "shift": dict(shift) if shift else None,
        "chemicals": chemicals,
        "formula": dict(formula) if formula else None,
        "formula_lines": formula_lines,
        "outlets": outlets,
        "orders": orders,
        "batches": batches,
        "rechecks": rechecks,
        "consumptions": consumptions,
        "inventory_txn": inventory_txn,
        "shifts": shifts,
        "audit": audit_rows,
        "standard": {"cod": STD_COD, "ss": STD_SS, "ph": list(STD_PH)},
        "influent_spec": {k: list(v) for k, v in INFLUENT_SPEC.items()},
        "effluent_spec": {k: list(v) for k, v in EFFLUENT_SPEC.items()},
    }
