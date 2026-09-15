#!/usr/bin/env python3
"""纸坊污水站加药与达标排放系统 — 验证测试(标准库 unittest)。

覆盖:交接班、整单拒绝、投加量计算、库存不足回滚、越权、同人复检、
未复核开阀、复检不合格自动停排、排口唯一批次、幂等重放、并发开阀、重启恢复。

运行: python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(BASE, "server.py")


def start_server(db_path):
    env = dict(os.environ, WTP_DB=db_path, PORT="0")
    proc = subprocess.Popen(
        [sys.executable, SERVER], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    port = None
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.startswith("LISTENING"):
            port = int(line.split()[1])
            break
        if proc.poll() is not None:
            raise RuntimeError("服务器启动失败")
    if not port:
        proc.kill()
        raise RuntimeError("服务器启动超时")
    return proc, f"http://127.0.0.1:{port}"


class Client:
    def __init__(self, base):
        self.base = base
        self.tokens = {}

    def login(self, username, password="123456"):
        status, data, _ = self.req("POST", "/api/login",
                                   {"username": username, "password": password})
        assert status == 200, f"登录失败 {username}: {data}"
        self.tokens[username] = data["token"]
        return data["token"]

    def req(self, method, path, body=None, user=None, token=None, headers=None):
        h = {"Content-Type": "application/json"}
        tok = token or (self.tokens.get(user) if user else None)
        if tok:
            h["Authorization"] = "Bearer " + tok
        if headers:
            h.update(headers)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode()), dict(e.headers)

    def get(self, path, user=None, token=None):
        return self.req("GET", path, user=user, token=token)

    def post(self, path, body=None, user=None, headers=None, token=None):
        return self.req("POST", path, body or {}, user=user, headers=headers, token=token)


class WtpTestBase(unittest.TestCase):
    """每个测试类一个独立数据库与服务进程。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "test.db")
        cls.proc, cls.base = start_server(cls.db_path)
        cls.c = Client(cls.base)
        for u in ("admin", "leader01", "op01", "op02", "qc01"):
            cls.c.login(u)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.proc.stdout.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.ensure_on_duty()

    # -- 工具 --
    def state(self):
        status, data, _ = self.c.get("/api/state", user="admin")
        self.assertEqual(status, 200)
        return data

    def ensure_on_duty(self):
        status, data, _ = self.c.get("/api/state", user="admin")
        shift = data.get("shift")
        if shift and shift["status"] == "ON_DUTY":
            return
        status, data, _ = self.c.post("/api/shifts/takeover",
                                      {"team": "甲班", "shift_name": "早班"}, user="leader01")
        self.assertIn(status, (200, 201), f"接班失败: {data}")

    def make_order(self, cod=200, ss=300, ph=7.2, flow=120, volume=100, user="op01"):
        status, data, _ = self.c.post("/api/dosing-orders", {
            "volume_m3": volume,
            "influent": {"cod": cod, "ss": ss, "ph": ph, "flow": flow},
        }, user=user)
        self.assertEqual(status, 201, f"开单失败: {data}")
        return data

    def dose_order(self, order_id, user="op01"):
        return self.c.post(f"/api/dosing-orders/{order_id}/dose", {}, user=user)

    def recheck_order(self, order_id, cod=30, ss=8, ph=7.0, user="qc01"):
        return self.c.post(f"/api/dosing-orders/{order_id}/recheck",
                           {"effluent": {"cod": cod, "ss": ss, "ph": ph}}, user=user)

    def make_rechecked(self, user_dose="op01"):
        o = self.make_order()
        status, data, _ = self.dose_order(o["id"], user=user_dose)
        self.assertEqual(status, 200, f"投加失败: {data}")
        status, data, _ = self.recheck_order(o["id"])
        self.assertEqual(status, 200, f"复检失败: {data}")
        self.assertEqual(data["recheck_result"], "PASS")
        return o["id"]

    def stock(self, code):
        for c in self.state()["chemicals"]:
            if c["code"] == code:
                return c["stock_qty"]
        raise AssertionError("药剂不存在")


class TestShiftHandover(WtpTestBase):
    def test_handover_gate(self):
        # 首个班次:直接接班成功(系统初始化)
        # 在岗时未交班 → 接班被拒
        status, data, _ = self.c.post("/api/shifts/takeover",
                                      {"team": "乙班", "shift_name": "中班"}, user="leader01")
        self.assertEqual(status, 409)
        self.assertIn("尚未完成交接", data["error"])
        # 交班
        status, data, _ = self.c.post("/api/shifts/handover", {"note": "设备正常"}, user="leader01")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "HANDED_OVER")
        # 已交班未接班 → 业务操作被拒
        status, data, _ = self.c.post("/api/dosing-orders", {
            "volume_m3": 10, "influent": {"cod": 100, "ss": 100, "ph": 7, "flow": 100}}, user="op01")
        self.assertEqual(status, 409)
        self.assertIn("无在岗班次", data["error"])
        # 接班后恢复
        status, data, _ = self.c.post("/api/shifts/takeover",
                                      {"team": "乙班", "shift_name": "中班"}, user="leader01")
        self.assertEqual(status, 201)
        self.assertEqual(data["status"], "ON_DUTY")
        shifts = [s for s in self.state()["shifts"]]
        self.assertEqual(len([s for s in shifts if s["status"] in ("ON_DUTY", "HANDED_OVER")]), 1)

    def test_handover_role_denied(self):
        # 操作员/复检员无权交班接班
        for u in ("op01", "qc01"):
            status, data, _ = self.c.post("/api/shifts/handover", {}, user=u)
            self.assertEqual(status, 403, f"{u} 应被拒")
            status, data, _ = self.c.post("/api/shifts/takeover",
                                          {"team": "X", "shift_name": "晚班"}, user=u)
            self.assertEqual(status, 403, f"{u} 应被拒")


class TestWaterQualityValidation(WtpTestBase):
    def orders_count(self):
        return len(self.state()["orders"])

    def test_out_of_range_rejected_whole_order(self):
        before = self.orders_count()
        bad_cases = [
            {"cod": 1200, "ss": 100, "ph": 7, "flow": 100},   # COD 超量程
            {"cod": 100, "ss": 999, "ph": 7, "flow": 100},    # SS 超量程
            {"cod": 100, "ss": 100, "ph": 15, "flow": 100},   # pH 超量程
            {"cod": 100, "ss": 100, "ph": 7, "flow": 6000},   # 流量超量程
            {"cod": -1, "ss": 100, "ph": 7, "flow": 100},     # 负值
        ]
        for influent in bad_cases:
            status, data, _ = self.c.post("/api/dosing-orders",
                                          {"volume_m3": 100, "influent": influent}, user="op01")
            self.assertEqual(status, 422, f"{influent} 应整单拒绝")
            self.assertIn("整单拒绝", data["error"])
            self.assertTrue(data["errors"])
        self.assertEqual(self.orders_count(), before, "整单拒绝后不得产生任何单据")

    def test_format_error_rejected(self):
        before = self.orders_count()
        bad_cases = [
            {"cod": "abc", "ss": 100, "ph": 7, "flow": 100},   # 非数字
            {"cod": "200", "ss": 100, "ph": 7, "flow": 100},   # 数字字符串也算格式错误
            {"cod": 100, "ss": None, "ph": 7, "flow": 100},    # null
            {"cod": 100, "ph": 7, "flow": 100},                # 缺 SS
            {"cod": 100, "ss": 100, "ph": 7, "flow": True},    # 布尔
        ]
        for influent in bad_cases:
            status, data, _ = self.c.post("/api/dosing-orders",
                                          {"volume_m3": 100, "influent": influent}, user="op01")
            self.assertEqual(status, 422, f"{influent} 应整单拒绝")
        self.assertEqual(self.orders_count(), before)

    def test_effluent_validation_rejected(self):
        oid = self.make_order()["id"]
        self.dose_order(oid)
        status, data, _ = self.c.post(f"/api/dosing-orders/{oid}/recheck",
                                      {"effluent": {"cod": 999, "ss": 8, "ph": 7}}, user="qc01")
        self.assertEqual(status, 422)
        self.assertIn("整单拒绝", data["error"])
        status, data, _ = self.c.post(f"/api/dosing-orders/{oid}/recheck",
                                      {"effluent": {"cod": "x", "ss": 8, "ph": 7}}, user="qc01")
        self.assertEqual(status, 422)
        # 订单状态未被污染
        status, detail, _ = self.c.get(f"/api/dosing-orders/{oid}", user="admin")
        self.assertEqual(detail["status"], "DOSED")


class TestDosingCalculation(WtpTestBase):
    def test_lines_by_water_quality_interval(self):
        # COD=200 → [150,300) → PAC 90 g/m3;SS=300 → [200,400) → PAM 1.5 g/m3;水量100m3
        o = self.make_order(cod=200, ss=300, volume=100)
        lines = {l["chemical_code"]: l for l in o["lines"]}
        self.assertAlmostEqual(lines["PAC"]["dose_rate"], 90.0)
        self.assertAlmostEqual(lines["PAC"]["amount_kg"], 9.0)
        self.assertAlmostEqual(lines["PAM"]["dose_rate"], 1.5)
        self.assertAlmostEqual(lines["PAM"]["amount_kg"], 0.15)
        # 区间切换:COD=100 → [0,150) → 60;COD=400 → 120
        o2 = self.make_order(cod=100, ss=100, volume=50)
        lines2 = {l["chemical_code"]: l for l in o2["lines"]}
        self.assertAlmostEqual(lines2["PAC"]["amount_kg"], 3.0)   # 60*50/1000
        self.assertAlmostEqual(lines2["PAM"]["amount_kg"], 0.05)  # 1.0*50/1000
        o3 = self.make_order(cod=400, ss=500, volume=10)
        lines3 = {l["chemical_code"]: l for l in o3["lines"]}
        self.assertAlmostEqual(lines3["PAC"]["dose_rate"], 120.0)
        self.assertAlmostEqual(lines3["PAM"]["dose_rate"], 2.0)

    def test_uses_approved_formula(self):
        o = self.make_order()
        f = self.state()["formula"]
        self.assertEqual(o["formula_id"], f["id"])
        self.assertEqual(f["status"], "APPROVED")


class TestDosingAndStock(WtpTestBase):
    def test_dose_deducts_stock_and_records_consumption(self):
        pac0, pam0 = self.stock("PAC"), self.stock("PAM")
        o = self.make_order(cod=200, ss=300, volume=100)  # PAC 9.0 / PAM 0.15
        status, data, _ = self.dose_order(o["id"])
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "DOSED")
        self.assertAlmostEqual(self.stock("PAC"), round(pac0 - 9.0, 3))
        self.assertAlmostEqual(self.stock("PAM"), round(pam0 - 0.15, 3))
        cons = [c for c in self.state()["consumptions"] if c["order_no"] == o["order_no"]]
        self.assertEqual(len(cons), 2, "药耗记录应与配方行一致")
        # 重复投加被拒
        status, data, _ = self.dose_order(o["id"])
        self.assertEqual(status, 409)

    def test_insufficient_stock_rolls_back_everything(self):
        # 让 PAC 充足、PAM 不足:先给 PAC 大补库,再用大水量单把 PAM 压到 0.15kg 以下
        self.c.post("/api/chemicals/PAC/restock", {"qty": 2000}, user="admin")
        pam_now = self.stock("PAM")
        v = round((pam_now - 0.1) * 1000 / 1.5, 3)  # PAM@ss=300 → 1.5 g/m3
        self.assertGreater(v, 0)
        drain = self.make_order(cod=100, ss=300, volume=v)
        status, data, _ = self.dose_order(drain["id"])
        self.assertEqual(status, 200, f"压低库存投加失败: {data}")
        pam_left = self.stock("PAM")
        self.assertLess(pam_left, 0.15)
        pac_before = self.stock("PAC")
        cons_before = len(self.state()["consumptions"])
        # 新单需要 PAM 0.15 > pam_left(PAC 足够)→ 第二行扣减失败,整单回滚
        o = self.make_order(cod=200, ss=300, volume=100)
        status, data, _ = self.dose_order(o["id"])
        self.assertEqual(status, 409)
        self.assertIn("库存不足", data["error"])
        self.assertIn("不得出库", data["error"])
        # 回滚验证:PAC 库存不变、无新药耗、订单仍是 CREATED
        self.assertAlmostEqual(self.stock("PAC"), pac_before)
        self.assertEqual(len(self.state()["consumptions"]), cons_before)
        status, detail, _ = self.c.get(f"/api/dosing-orders/{o['id']}", user="admin")
        self.assertEqual(detail["status"], "CREATED")


class TestPermissions(WtpTestBase):
    def test_unauthenticated(self):
        status, data, _ = self.c.get("/api/state")
        self.assertEqual(status, 401)
        status, data, _ = self.c.post("/api/dosing-orders", {"volume_m3": 1,
                                      "influent": {"cod": 1, "ss": 1, "ph": 1, "flow": 1}})
        self.assertEqual(status, 401)

    def test_role_denied(self):
        # 复检员开单 → 403
        status, data, _ = self.c.post("/api/dosing-orders", {
            "volume_m3": 10, "influent": {"cod": 100, "ss": 100, "ph": 7, "flow": 100}}, user="qc01")
        self.assertEqual(status, 403)
        self.assertIn("越权", data["error"])
        # 操作员复检 → 403
        oid = self.make_order()["id"]
        self.dose_order(oid)
        status, data, _ = self.c.post(f"/api/dosing-orders/{oid}/recheck",
                                      {"effluent": {"cod": 30, "ss": 8, "ph": 7}}, user="op02")
        self.assertEqual(status, 403)
        # 复检员开阀 → 403
        status, data, _ = self.c.post("/api/discharge/open",
                                      {"order_id": oid, "outlet_code": "OUT-01"}, user="qc01")
        self.assertEqual(status, 403)
        # 操作员入库 → 403
        status, data, _ = self.c.post("/api/chemicals/PAC/restock", {"qty": 10}, user="op01")
        self.assertEqual(status, 403)

    def test_same_person_dose_recheck(self):
        # admin 拥有全部角色权限,但投加人与复检人不能为同一人
        o = self.make_order(user="admin")
        status, data, _ = self.c.post(f"/api/dosing-orders/{o['id']}/dose", {}, user="admin")
        self.assertEqual(status, 200)
        status, data, _ = self.c.post(f"/api/dosing-orders/{o['id']}/recheck",
                                      {"effluent": {"cod": 30, "ss": 8, "ph": 7}}, user="admin")
        self.assertEqual(status, 409)
        self.assertIn("不能为同一人", data["error"])
        # 换人复检 → 通过
        status, data, _ = self.recheck_order(o["id"])
        self.assertEqual(status, 200)


class TestDischargeRules(WtpTestBase):
    def test_no_valve_without_recheck(self):
        o = self.make_order()
        status, data, _ = self.c.post("/api/discharge/open",
                                      {"order_id": o["id"], "outlet_code": "OUT-01"}, user="op01")
        self.assertEqual(status, 409)
        self.assertIn("未复核", data["error"])
        self.dose_order(o["id"])
        status, data, _ = self.c.post("/api/discharge/open",
                                      {"order_id": o["id"], "outlet_code": "OUT-01"}, user="op01")
        self.assertEqual(status, 409)
        self.assertIn("未复核", data["error"])

    def test_recheck_fail_blocks_and_auto_stops(self):
        # 场景1:未开阀时复检不合格 → 订单 FAILED,禁止开阀
        o1 = self.make_order()
        self.dose_order(o1["id"])
        status, data, _ = self.recheck_order(o1["id"], cod=80, ss=8, ph=7)
        self.assertEqual(status, 200)
        self.assertEqual(data["recheck_result"], "FAIL")
        self.assertEqual(data["status"], "FAILED")
        status, data, _ = self.c.post("/api/discharge/open",
                                      {"order_id": o1["id"], "outlet_code": "OUT-01"}, user="op01")
        self.assertEqual(status, 409)
        # 场景2:排放中复检不合格 → 自动停排
        oid = self.make_rechecked()
        status, batch, _ = self.c.post("/api/discharge/open",
                                       {"order_id": oid, "outlet_code": "OUT-01"}, user="op01")
        self.assertEqual(status, 201)
        status, data, _ = self.recheck_order(oid, cod=88, ss=9, ph=7)
        self.assertEqual(status, 200)
        self.assertEqual(data["recheck_result"], "FAIL")
        self.assertEqual(data["status"], "STOPPED")
        self.assertEqual(data["auto_stopped_batch"], batch["batch_no"])
        # 批次已停、排口释放
        b = [b for b in self.state()["batches"] if b["batch_no"] == batch["batch_no"]][0]
        self.assertEqual(b["status"], "STOPPED")
        self.assertIn("自动停排", b["stop_reason"])
        outlet = [o for o in self.state()["outlets"] if o["code"] == "OUT-01"][0]
        self.assertIsNone(outlet["open_batch"])

    def test_one_batch_per_outlet(self):
        o1 = self.make_rechecked()
        o2 = self.make_rechecked()
        status, b1, _ = self.c.post("/api/discharge/open",
                                    {"order_id": o1, "outlet_code": "OUT-01"}, user="op01")
        self.assertEqual(status, 201)
        # 同排口再开 → 409
        status, data, _ = self.c.post("/api/discharge/open",
                                      {"order_id": o2, "outlet_code": "OUT-01"}, user="op01")
        self.assertEqual(status, 409)
        self.assertIn("只能有一个批次", data["error"])
        # 换排口 → 成功
        status, b2, _ = self.c.post("/api/discharge/open",
                                    {"order_id": o2, "outlet_code": "OUT-02"}, user="op01")
        self.assertEqual(status, 201)
        # 关阀后排口释放
        status, _, _ = self.c.post(f"/api/discharge/{b1['batch_no']}/close", {}, user="op01")
        self.assertEqual(status, 200)
        outlet = [o for o in self.state()["outlets"] if o["code"] == "OUT-01"][0]
        self.assertIsNone(outlet["open_batch"])
        # 清理 OUT-02
        self.c.post(f"/api/discharge/{b2['batch_no']}/close", {}, user="op01")

    def test_manual_emergency_stop(self):
        oid = self.make_rechecked()
        status, batch, _ = self.c.post("/api/discharge/open",
                                       {"order_id": oid, "outlet_code": "OUT-01"}, user="op01")
        self.assertEqual(status, 201)
        status, data, _ = self.c.post("/api/discharge/stop",
                                      {"batch_no": batch["batch_no"], "reason": "在线仪表超标"},
                                      user="leader01")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "STOPPED")
        status, detail, _ = self.c.get(f"/api/dosing-orders/{oid}", user="admin")
        self.assertEqual(detail["status"], "STOPPED")


class TestIdempotency(WtpTestBase):
    def test_repeated_request_returns_original(self):
        oid = self.make_rechecked()
        key = "idem-" + str(time.time_ns())
        body = {"order_id": oid, "outlet_code": "OUT-01", "idempotency_key": key}
        status1, r1, h1 = self.c.post("/api/discharge/open", body, user="op01")
        self.assertEqual(status1, 201)
        self.assertNotEqual(h1.get("X-Idempotent-Replay"), "true")
        # 重复请求(同键同体)→ 返回原结果
        status2, r2, h2 = self.c.post("/api/discharge/open", body, user="op01")
        self.assertEqual(status2, 201)
        self.assertEqual(h2.get("X-Idempotent-Replay"), "true")
        self.assertEqual(r1["batch_no"], r2["batch_no"])
        # 数据库中该订单只有一条批次
        batches = [b for b in self.state()["batches"] if b["order_id"] == oid]
        self.assertEqual(len(batches), 1)
        # 同键不同体 → 409 冲突
        status3, r3, _ = self.c.post("/api/discharge/open",
                                     {"order_id": oid, "outlet_code": "OUT-02",
                                      "idempotency_key": key}, user="op01")
        self.assertEqual(status3, 409)
        self.assertIn("幂等键冲突", r3["error"])
        # 清理
        self.c.post(f"/api/discharge/{r1['batch_no']}/close", {}, user="op01")

    def test_key_cannot_cross_operation(self):
        # 键 K 先用于开阀
        oid = self.make_rechecked()
        key = "cross-op-" + str(time.time_ns())
        body = {"order_id": oid, "outlet_code": "OUT-01", "idempotency_key": key}
        s1, r1, _ = self.c.post("/api/discharge/open", body, user="op01")
        self.assertEqual(s1, 201)
        orders_before = len(self.state()["orders"])
        # 同一幂等键+同一请求体换到开单操作 → 必须拒绝,不得重放旧结果,也不得执行新操作
        s2, r2, h2 = self.c.post("/api/dosing-orders", body, user="op01")
        self.assertEqual(s2, 409)
        self.assertIn("幂等键", r2["error"])
        self.assertNotIn("batch_no", r2)                        # 不得返回开阀的旧结果
        self.assertNotEqual(h2.get("X-Idempotent-Replay"), "true")
        self.assertEqual(len(self.state()["orders"]), orders_before)  # 开单未被执行
        # 原操作上的重放仍然有效(键绑定未被破坏)
        s3, r3, h3 = self.c.post("/api/discharge/open", body, user="op01")
        self.assertEqual(s3, 201)
        self.assertEqual(h3.get("X-Idempotent-Replay"), "true")
        self.assertEqual(r3["batch_no"], r1["batch_no"])
        self.c.post(f"/api/discharge/{r1['batch_no']}/close", {}, user="op01")

    def test_key_cannot_cross_resource(self):
        # 同键用于不同单据的投加 → 第二个请求被拒且未执行
        o1 = self.make_order()
        o2 = self.make_order()
        key = "cross-res-" + str(time.time_ns())
        s1, _, _ = self.c.post(f"/api/dosing-orders/{o1['id']}/dose",
                               {"idempotency_key": key}, user="op01")
        self.assertEqual(s1, 200)
        s2, r2, _ = self.c.post(f"/api/dosing-orders/{o2['id']}/dose",
                                {"idempotency_key": key}, user="op01")
        self.assertEqual(s2, 409)
        self.assertIn("幂等键", r2["error"])
        _, detail, _ = self.c.get(f"/api/dosing-orders/{o2['id']}", user="admin")
        self.assertEqual(detail["status"], "CREATED", "第二张单不得被投加")

    def test_order_create_idempotent(self):
        key = "idem-order-" + str(time.time_ns())
        body = {"volume_m3": 66, "idempotency_key": key,
                "influent": {"cod": 123, "ss": 234, "ph": 7, "flow": 100}}
        s1, r1, _ = self.c.post("/api/dosing-orders", body, user="op01")
        s2, r2, h2 = self.c.post("/api/dosing-orders", body, user="op01")
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(r1["order_no"], r2["order_no"])
        self.assertEqual(h2.get("X-Idempotent-Replay"), "true")


class TestConcurrency(WtpTestBase):
    def test_concurrent_open_only_one_succeeds(self):
        n = 8
        order_ids = [self.make_rechecked() for _ in range(n)]

        def open_one(i):
            c = Client(self.base)
            c.tokens["op01"] = self.c.tokens["op01"]
            status, data, _ = c.post("/api/discharge/open",
                                     {"order_id": order_ids[i], "outlet_code": "OUT-01",
                                      "idempotency_key": f"conc-{i}-{time.time_ns()}"},
                                     user="op01")
            return status, data

        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(open_one, range(n)))
        successes = [r for r in results if r[0] == 201]
        conflicts = [r for r in results if r[0] == 409]
        self.assertEqual(len(successes), 1, f"并发开阀必须且只能成功一次: {results}")
        self.assertEqual(len(conflicts), n - 1)
        open_batches = [b for b in self.state()["batches"]
                        if b["outlet_code"] == "OUT-01" and b["status"] == "OPEN"]
        self.assertEqual(len(open_batches), 1)
        # 清理
        self.c.post(f"/api/discharge/{open_batches[0]['batch_no']}/close", {}, user="op01")


class TestRestartRecovery(unittest.TestCase):
    """重启恢复:杀进程后状态(班次/库存/开阀/幂等键)完整恢复。"""

    def test_restart_recovers_state(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp.name, "restart.db")
        proc, base = start_server(db_path)
        c = Client(base)
        for u in ("leader01", "op01", "qc01"):
            c.login(u)
        try:
            c.post("/api/shifts/takeover", {"team": "甲班", "shift_name": "早班"}, user="leader01")
            status, o, _ = c.post("/api/dosing-orders", {
                "volume_m3": 100,
                "influent": {"cod": 200, "ss": 300, "ph": 7, "flow": 100}}, user="op01")
            self.assertEqual(status, 201)
            c.post(f"/api/dosing-orders/{o['id']}/dose", {}, user="op01")
            c.post(f"/api/dosing-orders/{o['id']}/recheck",
                   {"effluent": {"cod": 30, "ss": 8, "ph": 7}}, user="qc01")
            key = "restart-idem-1"
            status, batch, _ = c.post("/api/discharge/open",
                                      {"order_id": o["id"], "outlet_code": "OUT-01",
                                       "idempotency_key": key}, user="op01")
            self.assertEqual(status, 201)
            pac_before = [x for x in c.get("/api/state", user="op01")[1]["chemicals"]
                          if x["code"] == "PAC"][0]["stock_qty"]
            op_token = c.tokens["op01"]
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
        # 重启(同一数据库文件)
        proc2, base2 = start_server(db_path)
        c2 = Client(base2)
        try:
            # 会话未失效(持久化)
            status, state, _ = c2.get("/api/state", token=op_token)
            self.assertEqual(status, 200)
            # 班次恢复
            self.assertEqual(state["shift"]["status"], "ON_DUTY")
            self.assertEqual(state["shift"]["team"], "甲班")
            # 库存恢复
            pac_after = [x for x in state["chemicals"] if x["code"] == "PAC"][0]["stock_qty"]
            self.assertAlmostEqual(pac_after, pac_before)
            # 开阀状态恢复:排口仍被占用
            outlet = [o for o in state["outlets"] if o["code"] == "OUT-01"][0]
            self.assertIsNotNone(outlet["open_batch"])
            self.assertEqual(outlet["open_batch"]["batch_no"], batch["batch_no"])
            # 幂等键恢复:重放返回原结果
            status, replay, h = c2.post("/api/discharge/open",
                                        {"order_id": o["id"], "outlet_code": "OUT-01",
                                         "idempotency_key": key}, token=op_token)
            self.assertEqual(status, 201)
            self.assertEqual(h.get("X-Idempotent-Replay"), "true")
            self.assertEqual(replay["batch_no"], batch["batch_no"])
            # 同排口新开仍被拒
            status, data, _ = c2.post("/api/discharge/open",
                                      {"order_id": o["id"], "outlet_code": "OUT-01"},
                                      token=op_token)
            # 注意:同订单同排口无幂等键 → 排口占用 409
            self.assertEqual(status, 409)
            # 关阀仍可用
            status, closed, _ = c2.post(f"/api/discharge/{batch['batch_no']}/close",
                                        {}, token=op_token)
            self.assertEqual(status, 200)
            self.assertEqual(closed["status"], "CLOSED")
        finally:
            proc2.kill()
            proc2.wait()
            proc2.stdout.close()
            tmp.cleanup()


class TestPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.proc, cls.base = start_server(os.path.join(cls.tmp.name, "page.db"))

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.proc.stdout.close()
        cls.tmp.cleanup()

    def test_index_page_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            html = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("纸坊污水站加药与达标排放系统", html)
        self.assertIn("交接班", html)
        self.assertIn("复检", html)

    def test_page_stop_reason_inline_no_prompt(self):
        # 回归:紧急停排不得使用 prompt()/confirm()/alert()(内置浏览器不支持),
        # 停排原因必须在页面内填写
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            html = resp.read().decode()
        for bad in ("prompt(", "confirm(", "alert("):
            self.assertNotIn(bad, html, f"页面不得使用 {bad}")
        self.assertIn('id="stopModal"', html)
        self.assertIn('id="stopReason"', html)
        self.assertIn('id="btnStopConfirm"', html)

    def test_public_info(self):
        with urllib.request.urlopen(self.base + "/api/public-info", timeout=5) as resp:
            data = json.loads(resp.read().decode())
        self.assertTrue(any(u["username"] == "admin" for u in data["users"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
