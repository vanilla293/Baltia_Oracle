"""Сквозной прогон без ключей и интернета: фейковые Telegram Bot API и DeepSeek + настоящий `python -m oracle`.

Запуск из корня проекта:  python scripts/smoke.py
Поднимает scripts/smoke_fake_servers.py (порты 18081/18082), запускает бота с TELEGRAM_API_URL и
LLM_BASE_URL на них, шлёт сценарий апдейтов (команды, текст, голосовое, кнопки, чужой пользователь)
и проверяет, что бот ответил. Данные — во временной папке, .env проекта не читается.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = sys.executable
TG = "http://127.0.0.1:18081"
DATA = os.path.join(tempfile.gettempdir(), "oracle-smoke-data")


def post(path, body):
    req = urllib.request.Request(TG + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def state():
    return json.loads(urllib.request.urlopen(TG + "/ctl/state", timeout=5).read())


def wait_sent(n_before, timeout=15.0, min_new=1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = state()
        if len(s["sent"]) >= n_before + min_new:
            time.sleep(0.8)          # добрать хвост (вложения, кнопки)
            return state()["sent"][n_before:]
        time.sleep(0.2)
    return state()["sent"][n_before:]


def main() -> int:
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA)
    env_clean = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env_clean.update({"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    fake = subprocess.Popen([PY, os.path.join(HERE, "smoke_fake_servers.py")], env=env_clean,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    fake.stdout.readline()
    app_env = dict(env_clean, BOT_TOKEN="123456789:AAFakeTokenForSmokeTest_abcdefghijk", OWNER_ID="42",
                   DEEPSEEK_API_KEY="sk-test", LLM_BASE_URL="http://127.0.0.1:18082", LLM_MODEL="deepseek-flash",
                   LLM_API_KEY="sk-test",
                   TELEGRAM_API_URL=TG, DATA_DIR=DATA, TIMEZONE="Europe/Moscow", STT_PROVIDER="off",
                   MORNING_BRIEF_TIME="off", NEWS_DIGEST_TIME="off", REFLECTION_TIME="off", BIRTHDAY_TIME="off",
                   WEB_SEARCH="0", NEWS_FEEDS="http://127.0.0.1:18081/none", LOG_LEVEL="INFO", TTS_DEFAULT="off")
    # ENV_FILE указывает в никуда — .env проекта (с настоящими ключами) не подмешивается
    app_env["ENV_FILE"] = os.path.join(DATA, "no.env")
    app = subprocess.Popen([PY, "-m", "oracle"], cwd=REPO, env=app_env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    fails = []
    try:
        time.sleep(3.0)
        steps = [
            ("owner /start", {"kind": "text", "text": "/start"}, lambda out: any("Оракул" in m["text"] or "голос" in m["text"].lower() for m in out)),
            # чужому — одна вежливая отбивка в час, дальше тишина; агент и модель не трогаются
            ("stranger", {"kind": "text", "text": "привет", "from": 99},
             lambda out: all(m.get("chat_id") in (99, None) for m in out) and len(out) <= 1),
            ("chat", {"kind": "text", "text": "привет, ты кто?"}, lambda out: any("Я тут" in m["text"] for m in out)),
            ("reminder by text", {"kind": "text", "text": "напомни 30 декабря в 7 утра позвонить маме"},
             lambda out: any("Сделал" in m["text"] for m in out)),
            ("/reminders", {"kind": "text", "text": "/reminders"}, lambda out: any("позвонить маме" in m["text"] for m in out)),
            ("idea", {"kind": "text", "text": "у меня идея: кофейня на колёсах у вокзала"},
             lambda out: any("5/10" in m["text"] for m in out)),
            ("/ideas", {"kind": "text", "text": "/ideas"}, lambda out: any("Кофейня" in m["text"] for m in out)),
            ("html escaping", {"kind": "text", "text": "что думаешь о криптовалюте?"},
             lambda out: any("<b>плохая</b>" in m["text"] and "&lt;потому что&gt;" in m["text"] for m in out)),
            ("voice without stt", {"kind": "voice"}, lambda out: any("GROQ" in m["text"] or "голос" in m["text"].lower() for m in out)),
            ("/status", {"kind": "text", "text": "/status"}, lambda out: any("deepseek-flash" in m["text"] for m in out)),
            ("/help", {"kind": "text", "text": "/help"}, lambda out: len(out) >= 1),
            ("/memory", {"kind": "text", "text": "/memory"}, lambda out: len(out) >= 1),
            ("/today", {"kind": "text", "text": "/today"}, lambda out: len(out) >= 1),
            ("/mode", {"kind": "text", "text": "/mode"}, lambda out: any("глуб" in m["text"].lower() for m in out)),
            ("/mode back", {"kind": "text", "text": "/mode"}, lambda out: any("быстр" in m["text"].lower() for m in out)),
            ("/ics", {"kind": "text", "text": "/ics"}, lambda out: len(out) >= 1),
            ("/backup", {"kind": "text", "text": "/backup"}, lambda out: any(m["method"] == "sendDocument" for m in out)),
            ("bad callback", {"kind": "callback", "data": "rem:done:abc"}, lambda out: any(m["method"] == "answerCallbackQuery" for m in out)),
            ("/idea no arg", {"kind": "text", "text": "/idea"}, lambda out: len(out) >= 1),
            ("/forget junk", {"kind": "text", "text": "/forget abc"}, lambda out: len(out) >= 1),
        ]
        for name, upd, check in steps:
            n = len(state()["sent"])
            post("/ctl/update", upd)
            out = wait_sent(n, timeout=4.0 if check is None else 15.0)
            print(f"\n=== {name}: {len(out)} sent")
            for m in out:
                t = (m.get("text") or "")
                print(f"  [{m['method']}{' ' + str(m.get('parse_mode')) if m.get('parse_mode') else ''}] "
                      f"{t[:400]!r}" + (f"  buttons={json.dumps(m['reply_markup'], ensure_ascii=False)[:200]}" if m.get("reply_markup") else "")
                      + (f" file={m.get('file')}" if m.get("file") else ""))
            if check is None:
                if any(m.get("chat_id") == 99 for m in out):
                    fails.append(name)
            elif not check(out):
                fails.append(name)
        s = state()
        last = s["llm_last"] or {}
        print("\nLLM calls:", s["llm_calls"], "| last payload keys:", sorted(last.keys()),
              "| thinking:", last.get("thinking"), "| tools:", len(last.get("tools") or []))
        unhandled = [m for m in s["sent"] if m.get("unhandled")]
        if unhandled:
            print("UNHANDLED methods:", unhandled)
    finally:
        app.terminate()
        try:
            out, _ = app.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            app.kill()
            out, _ = app.communicate()
        fake.terminate()
        print("\n--- app log (tail) ---")
        print("\n".join(out.splitlines()[-40:]))
        print("exit code:", app.returncode)
    print("\nFAILS:", fails or "none")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
