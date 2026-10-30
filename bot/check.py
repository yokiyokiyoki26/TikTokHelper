"""
TikTok Касса — бот уведомлений о рубежах просмотров.
Запускается GitHub Actions каждые 15 минут. Ничего тут менять не нужно:
настройки берутся из секретов репозитория (BOT_TOKEN, CHAT_ID, TIKTOK_ACCOUNTS).
"""
import base64
import hashlib
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
# ссылки на любые свои видео — по ним бот узнаёт внутренний ID профиля, если TikTok не отдаёт его напрямую
VIDEO_LINKS = [v.strip() for v in os.environ.get("TIKTOK_VIDEO", "").replace(";", ",").replace(" ", ",").split(",") if v.strip()]
SECUIDS = {}  # заполняется из памяти бота
PAGES = 3  # 3 страницы по 30 = последние ~90 видео каждого аккаунта
DATA_FILE = os.environ.get("DATA_FILE", "data.json")  # файл со статистикой для мини-приложения
DATA_KEY = os.environ.get("DATA_KEY", "").strip()      # ключ шифрования: тот же вводится в приложении
KDF_ITER = 200000


def _kdf(salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITER).derive(DATA_KEY.encode("utf-8"))


def encrypt(obj):
    """AES-GCM: без ключа содержимое файла прочитать невозможно."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_kdf(salt)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"enc": 1, "iter": KDF_ITER, "salt": b(salt), "iv": b(iv), "data": b(ct)}


def decrypt(box):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = lambda x: base64.b64decode(x)
    return json.loads(AESGCM(_kdf(d(box["salt"]))).decrypt(d(box["iv"]), d(box["data"]), None).decode("utf-8"))

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
        params = urllib.parse.urlencode({"unique_id": user, "count": 30, "cursor": cursor})
        j = {}
        for attempt in range(2):
            try:
                if attempt % 2 == 0:
                    j = http_json("https://www.tikwm.com/api/user/posts?" + params)
                else:
                    j = http_json("https://www.tikwm.com/api/user/posts", params.encode())
            except Exception as e:
                j = {"msg": str(e)}
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


def _num(x):
    try:
        return int(x or 0)
    except Exception:
        return 0


def _ytdlp(args, timeout):
    import subprocess
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "-J"] + args,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError((r.stderr or "yt-dlp без ответа").strip().splitlines()[-1][:300])
    return json.loads(r.stdout)


def find_secuid(user):
    """Узнаёт внутренний ID профиля (secUid) по ссылке на одно из видео."""
    if SECUIDS.get(user):
        return SECUIDS[user]
    for link in VIDEO_LINKS:
        try:
            v = _ytdlp(["--skip-download", link], 120)
        except Exception as e:
            print(f"  не удалось открыть видео {link}: {e}")
            continue
        owner = str(v.get("uploader") or "").lstrip("@").lower()
        cid = v.get("channel_id") or ""
        print(f"  видео {link} → автор @{owner}")
        if cid and (owner == user.lower() or len(ACCOUNTS) == 1):
            SECUIDS[user] = cid
            return cid
    return None


def fetch_posts_ytdlp(user):
    """Видео аккаунта прямо с сайта TikTok через yt-dlp."""
    limit = str(PAGES * 30)
    url = f"https://www.tiktok.com/@{user}"
    if SECUIDS.get(user):
        url = "tiktokuser:" + SECUIDS[user]
    try:
        j = _ytdlp(["--flat-playlist", "--playlist-end", limit, url], 240)
    except Exception as e:
        if url.startswith("tiktokuser:"):
            raise
        sec = find_secuid(user)
        if not sec:
            raise RuntimeError(f"{e}. Добавь секрет TIKTOK_VIDEO со ссылкой на любое своё видео")
        url = "tiktokuser:" + sec
        j = _ytdlp(["--flat-playlist", "--playlist-end", limit, url], 240)
    entries = [e for e in (j.get("entries") or []) if e]
    if entries and all(e.get("view_count") is None for e in entries):
        # в быстром режиме цифр нет — берём подробно последние 30 видео
        j = _ytdlp(["--skip-download", "--ignore-errors", "--playlist-end", "30", url], 480)
        entries = [e for e in (j.get("entries") or []) if e]
    out = []
    for e in entries:
        vid = str(e.get("id") or "")
        if not vid.isdigit():
            continue
        out.append({
            "video_id": vid,
            "title": e.get("description") or e.get("title") or "",
            "create_time": _num(e.get("timestamp")),
            "play_count": _num(e.get("view_count")),
            "digg_count": _num(e.get("like_count")),
            "comment_count": _num(e.get("comment_count")),
            "share_count": _num(e.get("repost_count")),
            "collect_count": _num(e.get("save_count") or e.get("collect_count")),
            "download_count": 0,
        })
    if not out:
        raise RuntimeError("TikTok не отдал список видео")
    return out


def get_posts(user):
    errs = []
    for name, fn in (("yt-dlp", fetch_posts_ytdlp), ("tikwm", fetch_posts)):
        try:
            posts = fn(user)
            print(f"  источник: {name}")
            return posts
        except Exception as e:
            print(f"  {name} не сработал: {e}")
            errs.append(f"{name}: {e}")
    raise RuntimeError("; ".join(errs))


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def main():
    if not TOKEN or not CHAT_ID or not ACCOUNTS:
        print("Не заданы секреты BOT_TOKEN, CHAT_ID или TIKTOK_ACCOUNTS — проверь настройки репозитория.")
        sys.exit(1)
    if len(DATA_KEY) < 12:
        print("Не задан секрет DATA_KEY (минимум 12 символов). Без него статистика была бы видна всем — останавливаюсь.")
        sys.exit(1)

    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
        if state.get("enc"):
            state = decrypt(state)
        elif state:
            print("Память бота была без шифрования — с этого запуска она шифруется")
    except Exception as e:
        print(f"Память бота не прочиталась ({e}) — начинаю заново")
        state = {}
    state.setdefault("videos", {})
    state.setdefault("accounts", {})
    SECUIDS.update(state.get("secuid") or {})

    if TEST:
        send("✅ <b>Бот подключён.</b>\nСлежу за: " + ", ".join("@" + html.escape(a) for a in ACCOUNTS) +
             "\nПришлю сообщение, когда видео пройдёт 3K, 5K, 10K … 100K, 125K, 150K и дальше каждые 25K.")
        print("Тестовое сообщение отправлено")

    sent = 0
    errors = {}
    for user in ACCOUNTS:
        print(f"@{user}")
        try:
            posts = get_posts(user)
        except Exception as e:
            print(f"  не удалось получить видео: {e}")
            errors[user] = str(e)
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
            rec["u"] = user
            rec["t"] = (p.get("title") or "")[:200]
            rec["c"] = int(p.get("create_time") or 0)
            rec["s"] = [views, int(p.get("digg_count") or 0), int(p.get("comment_count") or 0),
                        int(p.get("share_count") or 0), int(p.get("collect_count") or 0), int(p.get("download_count") or 0)]
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

    state["secuid"] = SECUIDS

    # статистика для мини-приложения (лежит на твоём сайте github.io)
    videos = [{"id": vid, "user": r["u"], "title": r.get("t", ""), "created": r.get("c", 0), "s": r["s"]}
              for vid, r in state["videos"].items() if r.get("s") and r.get("u") in ACCOUNTS]
    videos.sort(key=lambda x: x["created"], reverse=True)
    data = {"accounts": ACCOUNTS, "errors": errors, "videos": videos}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if digest != state.get("data_hash") or not os.path.exists(DATA_FILE):
        # перезаписываем файл только когда цифры изменились — меньше лишних обновлений сайта
        data["updated"] = int(time.time())
        box = encrypt(data)
        box["updated"] = data["updated"]
        json.dump(box, open(DATA_FILE, "w", encoding="utf-8"), separators=(",", ":"))
        state["data_hash"] = digest
    json.dump(encrypt(state), open(STATE_FILE, "w", encoding="utf-8"))
    print(f"Готово. Отправлено уведомлений: {sent}")


if __name__ == "__main__":
    main()
