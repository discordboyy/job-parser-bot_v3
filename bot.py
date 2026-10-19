#!/usr/bin/env python3
"""Telegram-бот с вакансиями (NAV + FINN).

- Каждое воскресенье в 23:00 (Europe/Oslo) запускает arbeidsplassen_parser.py
  и сохраняет список в data/jobs.json (указатель /next сбрасывается на 0).
- Каждое утро в 07:00 отправляет в группу следующую вакансию из списка.
- Команда /next в группе отправляет следующую вакансию (общий указатель).
"""
import asyncio
import html
import json
import logging
import os
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
JOBS_FILE = DATA / "jobs.json"
STATE_FILE = DATA / "state.json"

# ---- настройки из переменных окружения (.env) ----
TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.getenv("CHAT_ID", "0"))                 # id группы
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Oslo"))
PARSE_PAGES = int(os.getenv("PARSE_PAGES", "5"))
DAILY_COUNT = int(os.getenv("DAILY_COUNT", "1"))         # сколько вакансий в утренней рассылке

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("jobbot")

state_lock = asyncio.Lock()
parse_lock = asyncio.Lock()


# ---------------------------------------------------------------- хранилище
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


async def pop_next():
    """Возвращает (вакансия | None, номер, всего) и сдвигает указатель."""
    async with state_lock:
        jobs = load_json(JOBS_FILE, [])
        st = load_json(STATE_FILE, {"index": 0})
        i = st.get("index", 0)
        if i >= len(jobs):
            return None, i, len(jobs)
        st["index"] = i + 1
        save_json(STATE_FILE, st)
        return jobs[i], i + 1, len(jobs)


# ---------------------------------------------------------------- форматирование
def e(x) -> str:
    return html.escape(str(x)) if x else ""


def format_job(j: dict, pos: int, total: int) -> str:
    src = "NAV" if j.get("source") == "nav" else "FINN"
    lines = []
    if j.get("is_new_today"):
        lines.append("🆕 <b>Новая сегодня</b>")
    lines.append(f"💼 <b>{e(j.get('title'))}</b>")
    if j.get("employer"):
        lines.append(f"🏢 {e(j['employer'])}")
    if j.get("location"):
        loc = j["location"]
        lines.append(f"📍 {e(loc if len(loc) <= 150 else loc[:147] + '...')}")
    if j.get("occupation"):
        lines.append(f"🧰 {e(j['occupation'])}")
    if j.get("description"):
        lines.append(f"📝 {e(j['description'])}")
    if j.get("deadline"):
        lines.append(f"⏳ {e(j['deadline'])}")
    elif j.get("published_text"):
        lines.append(f"🕒 {e(j['published_text'])}")
    lines.append(f'🔗 <a href="{html.escape(j["url"], quote=True)}">Открыть на {src}</a>')
    lines.append(f"<i>Вакансия {pos} из {total}</i>")
    return "\n".join(lines)


NO_MORE = "📭 Список закончился. Новый появится в воскресенье в 23:00."
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_job(bot, chat_id: int, job, pos, total):
    text = format_job(job, pos, total) if job else NO_MORE
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                           link_preview_options=NO_PREVIEW)


# ---------------------------------------------------------------- парсинг
async def run_parser() -> int:
    """Запускает парсер подпроцессом, при успехе заменяет список. Возвращает число вакансий."""
    async with parse_lock:
        out_base = DATA / "jobs_new"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(BASE / "arbeidsplassen_parser.py"),
            "--pages", str(PARSE_PAGES), "--out", str(out_base),
            cwd=str(BASE),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            log.error("парсер завис, убит по таймауту")
            return 0
        log.info("вывод парсера:\n%s", out.decode("utf-8", "replace")[-1500:])

        jobs = load_json(out_base.with_suffix(".json"), [])
        if proc.returncode != 0 or not jobs:
            return 0
        async with state_lock:
            save_json(JOBS_FILE, jobs)
            save_json(STATE_FILE, {"index": 0, "updated": datetime.now(TZ).isoformat(timespec="minutes")})
        return len(jobs)


# ---------------------------------------------------------------- расписание
async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(CHAT_ID, "☀️ Доброе утро! Вакансия дня:")
    for _ in range(DAILY_COUNT):
        job, pos, total = await pop_next()
        await send_job(context.bot, CHAT_ID, job, pos, total)
        if job is None:
            break


async def weekly_parse(context: ContextTypes.DEFAULT_TYPE):
    log.info("запуск парсинга")
    n = await run_parser()
    if CHAT_ID:
        msg = (f"🔄 Список обновлён: {n} вакансий. Команда /next покажет следующую."
               if n else "⚠️ Не удалось обновить список вакансий, оставлен прошлый.")
        await context.bot.send_message(CHAT_ID, msg)


# ---------------------------------------------------------------- команды
def allowed(update: Update) -> bool:
    if not CHAT_ID:
        return True
    uid = update.effective_user.id if update.effective_user else 0
    return update.effective_chat.id == CHAT_ID or uid in ADMIN_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я присылаю вакансии из Telemark (NAV + FINN).\n\n"
        "/next - следующая вакансия\n"
        "/status - сколько осталось в списке\n"
        "/chatid - id этого чата\n\n"
        "Каждое утро в 07:00 - вакансия дня, список обновляется по воскресеньям в 23:00."
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id: {update.effective_chat.id}")


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    job, pos, total = await pop_next()
    await send_job(context.bot, update.effective_chat.id, job, pos, total)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    jobs = load_json(JOBS_FILE, [])
    st = load_json(STATE_FILE, {})
    await update.message.reply_text(
        f"В списке: {len(jobs)}, показано: {st.get('index', 0)}, "
        f"осталось: {max(len(jobs) - st.get('index', 0), 0)}\n"
        f"Обновлён: {st.get('updated', 'ещё не обновлялся')}"
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in ADMIN_IDS:
        return
    await update.message.reply_text("Запускаю парсинг, это займёт пару минут...")
    n = await run_parser()
    await update.message.reply_text(f"Готово: {n} вакансий." if n else "Не удалось обновить список.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.exception("ошибка в обработчике", exc_info=context.error)


# ---------------------------------------------------------------- запуск
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_error_handler(on_error)

    jq = app.job_queue
    if CHAT_ID:
        jq.run_daily(daily_job, time=time(7, 0, tzinfo=TZ), name="daily")
    else:
        log.warning("CHAT_ID не задан - утренняя рассылка выключена (узнай id командой /chatid)")
    # days: 0 = воскресенье (нумерация python-telegram-bot)
    jq.run_daily(weekly_parse, time=time(23, 0, tzinfo=TZ), days=(0,), name="weekly")
    if not JOBS_FILE.exists():
        jq.run_once(weekly_parse, 10)  # первый запуск: сразу собрать список

    log.info("бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
