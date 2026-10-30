"""
TikTok Касса — бот уведомлений о рубежах просмотров.
Запускается GitHub Actions каждые 15 минут. Ничего тут менять не нужно:
настройки берутся из секретов репозитория (BOT_TOKEN, CHAT_ID, TIKTOK_ACCOUNTS).
"""
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request

STATE_FILE = sys.argv[1] if len(sys.argv) > 1 else "state.json"
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
ACCOUNTS = [a.strip().lstrip("@") for a in os.environ.get("TIKTOK_ACCOUNTS", "").replace(";", ",").replace(" ", ",").split(",") if a.strip()]
TEST = os.environ.get("TEST", "").lower() == "true"
PAGES = 2  # 2 страницы по 35 = последние ~70 видео каждого аккаунта

BASE = [3000, 5000, 10000, 15000, 20000, 25000, 30000, 40000, 50000, 60000, 70000, 80000, 90000, 100000]


def top_milestone(views):
    """Самый большой рубеж, который видео уже прошло (0, если ни одного)."""
    if views >= 125000:
        return 125000 + (views - 125000) // 25000 * 25000
    best = 0
    for m in BASE:
        if m <= views:
            best = m
    return best


def http_json(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 (TikTokKassa bot)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    res = http_json(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data)
    if not res.get("ok"):
        raise RuntimeError(res)


def fetch_posts(user):
    videos, cursor = [], 0
    for page in range(PAGES):
        url = "https://www.tikwm.com/api/user/posts?" + urllib.parse.urlencode({"unique_id": user, "count": 35, "cursor": cursor})
        for attempt in range(3):
            j = http_json(url)
            if j.get("code") == 0:
                break
            print(f"  tikwm ответил: {j.get('msg')} — жду и пробую ещё раз")
            time.sleep(3)
        if j.get("code") != 0 or not j.get("data"):
            if page == 0:
                raise RuntimeError(j.get("msg") or "нет данных")
            break
        d = j["data"]
        videos += d.get("videos") or []
        time.sleep(1.5)
        if not d.get("hasMore"):
            break
        cursor = d.get("cursor", 0)
    return videos


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def main():
    if not TOKEN or not CHAT_ID or not ACCOUNTS:
        print("Не заданы секреты BOT_TOKEN, CHAT_ID или TIKTOK_ACCOUNTS — проверь настройки репозитория.")
        sys.exit(1)

    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}
    state.setdefault("videos", {})
    state.setdefault("accounts", {})

    if TEST:
        send("✅ <b>Бот подключён.</b>\nСлежу за: " + ", ".join("@" + html.escape(a) for a in ACCOUNTS) +
             "\nПришлю сообщение, когда видео пройдёт 3K, 5K, 10K … 100K, 125K, 150K и дальше каждые 25K.")
        print("Тестовое сообщение отправлено")

    sent = 0
    for user in ACCOUNTS:
        print(f"@{user}")
        try:
            posts = fetch_posts(user)
        except Exception as e:
            print(f"  не удалось получить видео: {e}")
            continue
        first_time = not state["accounts"].get(user)
        print(f"  видео получено: {len(posts)}" + (" (первый запуск — запоминаю без уведомлений)" if first_time else ""))

        for p in posts:
            vid = str(p.get("video_id") or p.get("id") or "")
            if not vid:
                continue
            views = int(p.get("play_count") or 0)
            top = top_milestone(views)
            rec = state["videos"].get(vid)
            if rec is None:
                # видео без записи: на первом запуске просто запоминаем,
                # а новое видео стартует с нуля, чтобы о первом рубеже пришло сообщение
                rec = {"m": top if first_time else 0}
                state["videos"][vid] = rec
            rec["v"] = views
            if top > rec["m"]:
                likes = int(p.get("digg_count") or 0)
                com = int(p.get("comment_count") or 0)
                sh = int(p.get("share_count") or 0)
                fav = int(p.get("collect_count") or 0)
                er = (likes + com + sh + fav) / views * 100 if views else 0
                title = html.escape((p.get("title") or "Без описания")[:120])
                text = (f"🚀 <b>{fmt(top)} просмотров!</b>\n"
                        f"{title}\n\n"
                        f"👁 {fmt(views)}  ·  ER {er:.1f}%\n"
                        f"❤️ {fmt(likes)}  💬 {fmt(com)}  🔁 {fmt(sh)}  ⭐ {fmt(fav)}\n"
                        f"https://www.tiktok.com/@{user}/video/{vid}")
                try:
                    send(text)
                    rec["m"] = top
                    sent += 1
                    time.sleep(0.5)
                except Exception as e:
                    print(f"  не отправилось ({vid}): {e}")
        state["accounts"][user] = int(time.time())

    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Готово. Отправлено уведомлений: {sent}")


if __name__ == "__main__":
    main()
