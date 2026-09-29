"""Фейковые Telegram Bot API и DeepSeek для прогона настоящего приложения (python -m oracle)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from aiohttp import web

TG_PORT = int(os.environ.get("TG_PORT", "18081"))
LLM_PORT = int(os.environ.get("LLM_PORT", "18082"))
OWNER = 42
STRANGER = 99
BOT = {"id": 777000111, "is_bot": True, "first_name": "Oracle", "username": "oracle_test_bot"}

updates: list[dict] = []
sent: list[dict] = []           # всё, что бот отправил: {"method", ...params}
llm_calls: list[dict] = []
_uid = 0
_mid = 1000
new_update = asyncio.Event()


def _msg(chat_id: int, text: str = "", **extra) -> dict:
    global _mid
    _mid += 1
    return {"message_id": _mid, "date": int(time.time()), "chat": {"id": int(chat_id), "type": "private"},
            "from": BOT, "text": text, **extra}


async def _params(request: web.Request) -> dict:
    ct = request.content_type or ""
    if "json" in ct:
        return await request.json()
    out = {}
    if ct.startswith("multipart/"):
        reader = await request.multipart()
        async for part in reader:
            if part.filename:
                data = await part.read()
                out[part.name] = {"filename": part.filename, "size": len(data)}
            else:
                out[part.name] = (await part.read()).decode()
        return out
    data = await request.post()
    for k, v in data.items():
        out[k] = v if isinstance(v, str) else {"filename": getattr(v, "filename", ""), "size": 0}
    return out


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


async def tg(request: web.Request) -> web.Response:
    method = request.match_info["method"]
    p = await _params(request)
    ok = lambda r: web.json_response({"ok": True, "result": r})
    if method == "getMe":
        return ok(BOT)
    if method in ("deleteWebhook", "setMyCommands", "sendChatAction", "answerCallbackQuery", "deleteMessage"):
        if method in ("answerCallbackQuery",):
            sent.append({"method": method, **{k: p.get(k) for k in ("callback_query_id", "text", "show_alert")}})
        return ok(True)
    if method == "getUpdates":
        offset = int(_j(p.get("offset", 0)) or 0)
        timeout = float(_j(p.get("timeout", 0)) or 0)
        deadline = time.monotonic() + min(timeout, 1.0)
        while True:
            ready = [u for u in updates if u["update_id"] >= offset]
            if ready or time.monotonic() >= deadline:
                return ok(ready)
            await asyncio.sleep(0.05)
    if method in ("sendMessage", "sendDocument", "sendVoice", "sendAudio"):
        chat = int(_j(p.get("chat_id")))
        rec = {"method": method, "chat_id": chat, "text": p.get("text") or p.get("caption") or "",
               "parse_mode": p.get("parse_mode"), "reply_markup": _j(p.get("reply_markup")),
               "file": p.get("document") or p.get("voice") or p.get("audio")}
        sent.append(rec)
        return ok(_msg(chat, rec["text"]))
    if method in ("editMessageReplyMarkup", "editMessageText"):
        sent.append({"method": method, "text": p.get("text", ""), "reply_markup": _j(p.get("reply_markup"))})
        return ok(_msg(OWNER, p.get("text", "")))
    if method == "getFile":
        return ok({"file_id": p.get("file_id"), "file_unique_id": "u1", "file_size": 10, "file_path": "voice/f.ogg"})
    sent.append({"method": method, "unhandled": True, "params": {k: str(v)[:200] for k, v in p.items()}})
    return ok(True)


async def tg_file(request: web.Request) -> web.Response:
    return web.Response(body=b"OggS fake audio")


# ── DeepSeek ──
def _completion(content: str = "", tool_calls: list | None = None) -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": f"call_{i}_{int(time.time()*1000)}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                             for i, (n, a) in enumerate(tool_calls)]
    return {"id": "x", "model": "deepseek-flash", "choices": [{"index": 0, "message": msg,
            "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


async def llm(request: web.Request) -> web.Response:
    body = await request.json()
    llm_calls.append(body)
    msgs = body["messages"]
    last = msgs[-1]
    if last["role"] == "tool":
        res = last["content"]
        return web.json_response(_completion(f"Сделал. Итог инструмента: {res[:120]}"))
    text = str(last.get("content") or "")
    low = text.lower()
    if body.get("response_format", {}).get("type") == "json_object":
        return web.json_response(_completion('{"journal": "день как день", "facts": [], "opinions": [], "followups": []}'))
    if "запиши его др" in low and "переслано от" in low and body.get("tools"):
        # пересылка с комментарием дошла одним ходом: комментарий и пересланное — вместе
        return web.json_response(_completion("", [("add_birthday", {"name": "Лёха", "date": "14 марта"})]))
    if "напомни" in low and body.get("tools"):
        return web.json_response(_completion("", [("create_reminder", {"text": "позвонить маме", "when": "2026-12-30 07:00"})]))
    if "идея" in low and body.get("tools"):
        return web.json_response(_completion("Кофейня на колёсах: 5/10, аренда съест маржу.",
                                             [("save_idea", {"title": "Кофейня на колёсах", "content": text,
                                                             "evaluation": "аренда съест маржу", "score": 5})]))
    if "что думаешь" in low:
        return web.json_response(_completion("Думаю, что это **плохая** идея, и вот почему: <потому что>."))
    return web.json_response(_completion("Я тут. Что нужно?"))


async def control(request: web.Request) -> web.Response:
    """POST /ctl/update {kind, from, text|data} — положить апдейт ({kind: "batch", items: […]} — несколько сразу,
    одним ответом getUpdates); GET /ctl/state — что бот отправил."""
    if request.method == "GET":
        return web.json_response({"sent": sent, "llm_calls": len(llm_calls),
                                  "llm_last": llm_calls[-1] if llm_calls else None})
    d = await request.json()
    if d["kind"] == "batch":            # несколько апдейтов сразу — одним ответом getUpdates
        ids = []
        for item in d["items"]:
            ids.append(_add_update(item))
        return web.json_response({"ok": True, "update_ids": ids})
    return web.json_response({"ok": True, "update_id": _add_update(d)})


def _add_update(d: dict) -> int:
    global _uid
    _uid += 1
    user = {"id": d.get("from", OWNER), "is_bot": False, "first_name": "Тест"}
    chat = {"id": d.get("from", OWNER), "type": "private"}
    if d["kind"] == "text":
        m = {"message_id": 10 + _uid, "date": int(time.time()), "chat": chat, "from": user, "text": d["text"]}
        if d["text"].startswith("/"):
            cmd = d["text"].split()[0]
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        if d.get("forward_from"):       # пересланное от другого человека
            m["forward_origin"] = {"type": "user", "date": int(time.time()) - 3600,
                                   "sender_user": {"id": 555, "is_bot": False, "first_name": d["forward_from"]}}
        updates.append({"update_id": _uid, "message": m})
    elif d["kind"] == "voice":
        m = {"message_id": 10 + _uid, "date": int(time.time()), "chat": chat, "from": user,
             "voice": {"file_id": "v1", "file_unique_id": "uv1", "duration": 2, "mime_type": "audio/ogg", "file_size": 15}}
        updates.append({"update_id": _uid, "message": m})
    elif d["kind"] == "callback":
        updates.append({"update_id": _uid, "callback_query": {
            "id": f"cb{_uid}", "from": user, "chat_instance": "ci", "data": d["data"],
            "message": {"message_id": 5, "date": int(time.time()), "chat": chat, "from": BOT, "text": "x"}}})
    return _uid


def main() -> None:
    tg_app = web.Application()
    tg_app.router.add_post("/bot{token}/{method}", tg)
    tg_app.router.add_get("/file/bot{token}/{path:.*}", tg_file)
    tg_app.router.add_route("*", "/ctl/update", control)
    tg_app.router.add_get("/ctl/state", control)
    llm_app = web.Application()
    llm_app.router.add_post("/chat/completions", llm)

    async def run():
        r1 = web.AppRunner(tg_app); await r1.setup(); await web.TCPSite(r1, "127.0.0.1", TG_PORT).start()
        r2 = web.AppRunner(llm_app); await r2.setup(); await web.TCPSite(r2, "127.0.0.1", LLM_PORT).start()
        print("fake servers up", flush=True)
        await asyncio.Event().wait()
    asyncio.run(run())


if __name__ == "__main__":
    main()
