#!/usr/bin/env python3
"""纸坊污水站加药与达标排放系统 — HTTP 服务层(仅标准库)。

运行: python3 server.py   (环境变量 PORT, 默认 3039; WTP_DB 指定数据库文件)
"""
import json
import os
import re
import sqlite3
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import domain
from domain import DomainError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("WTP_DB", os.path.join(BASE_DIR, "data", "wastewater.db"))
PORT = int(os.environ.get("PORT", "3039"))

# 角色 → 允许的动作;ADMIN 拥有全部动作(但"投加人与复检人不能同人"等业务规则仍生效)
ROLE_ACTIONS = {
    "ADMIN": {"handover", "takeover", "create_order", "dose", "recheck",
              "open", "close", "stop", "restock"},
    "LEADER": {"handover", "takeover", "stop"},
    "OPERATOR": {"create_order", "dose", "open", "close", "stop"},
    "INSPECTOR": {"recheck"},
}


def _body_json(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise DomainError(400, "请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise DomainError(400, "请求体需为 JSON 对象")
    return data


# ------------------------------------------------------------ 路由处理

def h_login(conn, user, m, body):
    token, info = domain.login(conn, body.get("username"), body.get("password"))
    return 200, {"token": token, "user": info}


def h_state(conn, user, m, body):
    return 200, domain.get_state(conn)


def h_public_info(conn, user, m, body):
    users = [dict(r) for r in conn.execute(
        "SELECT username, name, role FROM users ORDER BY id")]
    return 200, {"users": users, "default_password": domain.DEFAULT_PASSWORD_HINT}


def h_handover(conn, user, m, body):
    return 200, domain.handover(conn, user, body.get("note"))


def h_takeover(conn, user, m, body):
    return 201, domain.takeover(conn, user, body.get("team"), body.get("shift_name"), body.get("note"))


def h_create_order(conn, user, m, body):
    return 201, domain.create_order(conn, user, body)


def h_get_order(conn, user, m, body):
    return 200, domain.order_detail(conn, int(m.group(1)))


def h_dose(conn, user, m, body):
    return 200, domain.execute_dosing(conn, user, int(m.group(1)))


def h_recheck(conn, user, m, body):
    return 200, domain.recheck(conn, user, int(m.group(1)), body)


def h_open_valve(conn, user, m, body):
    return 201, domain.open_valve(conn, user, body)


def h_close_valve(conn, user, m, body):
    return 200, domain.close_valve(conn, user, m.group(1))


def h_stop(conn, user, m, body):
    return 200, domain.emergency_stop(conn, user, body)


def h_restock(conn, user, m, body):
    return 200, domain.restock(conn, user, m.group(1), body.get("qty"))


# (method, path_regex, action, handler, public, idempotent)
ROUTES = [
    ("POST", r"/api/login",                        "login",        h_login,       True,  False),
    ("GET",  r"/api/public-info",                  "public_info",  h_public_info, True,  False),
    ("GET",  r"/api/state",                        "state",        h_state,       False, False),
    ("POST", r"/api/shifts/handover",              "handover",     h_handover,    False, False),
    ("POST", r"/api/shifts/takeover",              "takeover",     h_takeover,    False, False),
    ("POST", r"/api/dosing-orders",                "create_order", h_create_order, False, True),
    ("GET",  r"/api/dosing-orders/(\d+)",          "get_order",    h_get_order,   False, False),
    ("POST", r"/api/dosing-orders/(\d+)/dose",     "dose",         h_dose,        False, True),
    ("POST", r"/api/dosing-orders/(\d+)/recheck",  "recheck",      h_recheck,     False, True),
    ("POST", r"/api/discharge/open",               "open",         h_open_valve,  False, True),
    ("POST", r"/api/discharge/([^/]+)/close",      "close",        h_close_valve, False, False),
    ("POST", r"/api/discharge/stop",               "stop",         h_stop,        False, False),
    ("POST", r"/api/chemicals/([A-Za-z0-9_-]+)/restock", "restock", h_restock,    False, False),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZhiFangWTP/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志,测试输出更干净
        pass

    # -- 响应 --
    def _send_json(self, status, obj, extra_headers=None):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, text):
        payload = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # -- 入口 --
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            path = self.path.split("?", 1)[0]
            if method == "GET" and path == "/":
                with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
                    return self._send_html(f.read())
            if path.startswith("/api/"):
                return self._handle_api(method, path)
            self._send_json(404, {"error": "not_found"})
        except DomainError as e:
            self._send_json(e.status, {"error": e.message, "errors": e.errors})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"error": f"服务器内部错误: {e}"})

    def _handle_api(self, method, path):
        for m_method, pattern, action, handler, public, idempotent in ROUTES:
            if method != m_method:
                continue
            m = re.fullmatch(pattern, path)
            if not m:
                continue
            body = _body_json(self) if method == "POST" else {}
            conn = db.connect(DB_PATH)
            try:
                user = None
                if not public:
                    token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                    user = domain.user_by_token(conn, token)
                    if not user:
                        raise DomainError(401, "未登录或会话已失效")
                    if action not in ("state", "get_order") and action not in ROLE_ACTIONS.get(user["role"], ()):
                        raise DomainError(403, f"越权:角色[{user['role']}]无权执行[{action}]")
                if method == "GET":
                    status, obj = handler(conn, user, m, body)
                    self._send_json(status, obj)
                    return
                # 写操作:BEGIN IMMEDIATE,业务写入与幂等记录同一事务
                conn.execute("BEGIN IMMEDIATE")
                try:
                    idem_key = (self.headers.get("Idempotency-Key")
                                or body.get("idempotency_key") or "").strip()
                    if idempotent and idem_key:
                        hit = conn.execute(
                            "SELECT * FROM idempotency_keys WHERE key=?", (idem_key,)).fetchone()
                        if hit:
                            if hit["user_id"] != user["id"] or hit["request_hash"] != domain.request_hash(body):
                                raise DomainError(409, "幂等键冲突:相同键对应了不同的请求")
                            self._send_json(hit["response_status"], json.loads(hit["response_json"]),
                                            {"X-Idempotent-Replay": "true"})
                            conn.execute("COMMIT")
                            return
                    status, obj = handler(conn, user, m, body)
                    if idempotent and idem_key and 200 <= status < 300:
                        conn.execute(
                            "INSERT INTO idempotency_keys(key, user_id, endpoint, request_hash,"
                            " response_status, response_json, created_at) VALUES (?,?,?,?,?,?,?)",
                            (idem_key, user["id"], f"{method} {path}", domain.request_hash(body),
                             status, json.dumps(obj, ensure_ascii=False), domain.now_iso()),
                        )
                    conn.execute("COMMIT")
                    self._send_json(status, obj)
                    return
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        self._send_json(404, {"error": "not_found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    db.init_db(DB_PATH)
    server = Server(("127.0.0.1", PORT), Handler)
    actual_port = server.server_address[1]
    print(f"LISTENING {actual_port}", flush=True)
    print(f"纸坊污水站加药与达标排放系统 http://127.0.0.1:{actual_port}  数据库: {DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
