#!/usr/bin/env python3
"""GEO Audit 대시보드 데이터를 노출하는 MCP 서버 (stdio, 무의존성).

MCP 클라이언트(Claude 등)가 아래 도구로 최신 감사 집계를 pull 한다:
  - list_countries              : 전략 10국 요약(평균/표본/파싱실패/날짜)
  - get_scorecard(country)      : 한 국가 전체 집계(카테고리 breakdown 포함)
  - get_item_passrates(country?, category?) : 항목별 pass율(필터 가능)
  - get_overall                 : 전체 가중평균 요약

데이터 소스: reports/dashboard_data.json (gen_dashboard_data.py 산출).
파일이 없으면 생성기를 1회 호출해 만든다. 경로는 __file__ 기준이라 cwd 독립적.
표준 라이브러리만 사용 — JSON-RPC 2.0 over newline-delimited stdio 직접 구현.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "reports", "dashboard_data.json")
PROTOCOL_VERSION = "2024-11-05"


# ── 데이터 로딩 ──────────────────────────────────────────────────────────────

def _load_data() -> dict:
    """대시보드 집계 로드. 없으면 생성기 1회 호출."""
    if not os.path.exists(DATA):
        try:
            sys.path.insert(0, HERE)
            import gen_dashboard_data
            gen_dashboard_data.main()
        except Exception as e:
            raise RuntimeError(f"dashboard_data.json 없음 & 생성 실패: {e}")
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)


# ── 도구 구현 ────────────────────────────────────────────────────────────────

def tool_list_countries(_args: dict) -> dict:
    d = _load_data()
    rows = []
    for c, v in d["countries"].items():
        rows.append({
            "country": c,
            "total_avg": v["total_avg"],
            "sample_size": v["sample_size"],
            "parse_fail_rate": v["parse_fail_rate"],
            "date": v["date"],
        })
    rows.sort(key=lambda r: r["total_avg"], reverse=True)
    return {"countries": rows, "overall": d["overall"]}


def tool_get_scorecard(args: dict) -> dict:
    c = (args.get("country") or "").lower()
    d = _load_data()
    if c not in d["countries"]:
        return {"error": f"국가 '{c}' 없음. 사용 가능: {list(d['countries'])}"}
    return {"country": c, **d["countries"][c]}


def tool_get_item_passrates(args: dict) -> dict:
    country = (args.get("country") or "").lower()
    category = (args.get("category") or "").lower()
    d = _load_data()
    targets = [country] if country else list(d["countries"])
    out = []
    for c in targets:
        cv = d["countries"].get(c)
        if not cv:
            continue
        for iid, it in cv["items"].items():
            if category and it.get("category") != category:
                continue
            out.append({
                "country": c, "item_id": iid, "label": it.get("label"),
                "category": it.get("category"), "pass_rate": it.get("pass_rate"),
                "applicable_n": it.get("applicable_n"),
            })
    out.sort(key=lambda r: (r["pass_rate"] is None, r["pass_rate"] or 0))
    return {"items": out, "count": len(out)}


def tool_get_overall(_args: dict) -> dict:
    return _load_data()["overall"]


def tool_get_raw(_args: dict) -> dict:
    """dashboard_data.json 전체를 가공 없이 그대로 반환."""
    return _load_data()


TOOLS = {
    "list_countries": {
        "fn": tool_list_countries,
        "description": "전략 10국 GEO/AI Readability 감사 요약(총점 평균·표본수·파싱실패율·감사일). 총점 내림차순.",
        "schema": {"type": "object", "properties": {}},
    },
    "get_scorecard": {
        "fn": tool_get_scorecard,
        "description": "한 국가의 전체 감사 집계. 4개 카테고리(performance/accessibility/seo/ai_readiness) 획득률·항목별 pass율 포함.",
        "schema": {
            "type": "object",
            "properties": {"country": {"type": "string", "description": "국가 코드 (us, uk, de, es, ca, au, br, mx, in, vn)"}},
            "required": ["country"],
        },
    },
    "get_item_passrates": {
        "fn": tool_get_item_passrates,
        "description": "항목(49개 기준)별 pass율. country/category로 필터. pass율 오름차순(약점부터).",
        "schema": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "description": "국가 코드(생략 시 전국)"},
                "category": {"type": "string", "description": "performance|accessibility|seo|ai_readiness (생략 시 전체)"},
            },
        },
    },
    "get_overall": {
        "fn": tool_get_overall,
        "description": "전체 요약: 국가수·총표본·표본가중 총점 평균·누락 국가.",
        "schema": {"type": "object", "properties": {}},
    },
    "get_raw": {
        "fn": tool_get_raw,
        "description": "dashboard_data.json 전체(10국 x 카테고리 x 항목 + overall)를 가공 없이 그대로 반환. 완전한 raw JSON.",
        "schema": {"type": "object", "properties": {}},
    },
}


# ── JSON-RPC / MCP stdio 루프 ────────────────────────────────────────────────

def _handle(msg: dict):
    """요청 처리 → 응답 dict 반환(알림이면 None)."""
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return _ok(mid, {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "geo-audit-dashboard", "version": "1.0.0"},
        })

    if method in ("notifications/initialized", "initialized"):
        return None  # 알림 — 응답 없음

    if method == "ping":
        return _ok(mid, {})

    if method == "tools/list":
        tools = [{
            "name": name, "description": t["description"], "inputSchema": t["schema"],
        } for name, t in TOOLS.items()]
        return _ok(mid, {"tools": tools})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        t = TOOLS.get(name)
        if not t:
            return _err(mid, -32602, f"unknown tool: {name}")
        try:
            result = t["fn"](args)
            return _ok(mid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]})
        except Exception as e:
            return _ok(mid, {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}], "isError": True})

    if mid is not None:
        return _err(mid, -32601, f"method not found: {method}")
    return None


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
