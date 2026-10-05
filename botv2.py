import os
os.environ["PATH"] = "/opt/render/.deno/bin:" + os.environ.get("PATH", "")

import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import os, re, time, json, uuid, socket, ipaddress, aiosqlite
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from cachetools import TTLCache
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified, UserNotParticipant

load_dotenv()

API_ID = int(os.getenv("API_ID", "36796111"))
API_HASH = os.getenv("API_HASH", "0d449e01366dbfa6919e11e6c2edb474")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8985489658:AAErCg3gxOYgI5f82b_oAPK4LyoJweYk3mI")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "7606139186").split(",") if x.strip()]
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "")

RATE_LIMIT_PER_MIN = 5
CACHE_TTL = 3600
PAGE_SIZE = 10
VIDEOS_PER_PAGE = 10

DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIES_DIR = Path("./cookies")
COOKIES_DIR.mkdir(exist_ok=True)
DB_PATH = Path("./data/bot.db")
DB_PATH.parent.mkdir(exist_ok=True)

print("API_ID=", API_ID)
print("BOT_TOKEN=", "SET" if BOT_TOKEN else "MISSING")
print("ADMIN_IDS=", ADMIN_IDS)
print("FORCE_CHANNEL=", FORCE_CHANNEL or "disabled")
print("DB_PATH=", DB_PATH)

URL_CACHE = TTLCache(maxsize=1000, ttl=CACHE_TTL)
RATE_CACHE = TTLCache(maxsize=10000, ttl=60)
ACTIVE_PROCESSES = {}
USERS_SEEN = {}
DOWNLOAD_LOG = []
START_TIME = int(time.time())

def is_admin(user_id):
    return user_id in ADMIN_IDS

def human_size(size):
    if not size: return "?"
    for u in ["B", "KB", "MB", "GB"]:
        if size < 1024.0: return f"{size:.2f} {u}"
        size /= 1024.0
    return f"{size:.2f} TB"

def human_time(sec):
    if not sec: return "?"
    h, r = divmod(int(sec), 3600); m, s = divmod(r, 60)
    if h: return f"{h}س {m}د"
    if m: return f"{m}د {s}ث"
    return f"{s}ث"

def progress_bar(cur, tot=100.0, length=12):
    p = (cur / tot) * 100 if tot > 0 else 0
    filled = int(p / 100 * length)
    return f"[{'#' * filled}{'.' * (length - filled)}] {p:.1f}%"

def esc(t):
    if not t: return ""
    for c in r"_*[]()~`>#+-=|{}.!":
        t = t.replace(c, f"\\{c}")
    return t

def short(t, n=50):
    if not t: return "بدون عنوان"
    return t[:n] + "..." if len(t) > n else t

def is_valid_url(url):
    return bool(re.match(r"https?://[^\s]+", url))

def is_safe_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"): return False, "بروتوكول غير مدعوم"
        if not p.hostname: return False, "رابط غير صالح"
        try:
            ip = socket.gethostbyname(p.hostname)
            addr = ipaddress.ip_address(ip)
            blocked = [
                ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
                ipaddress.ip_network("127.0.0.0/8"),
                ipaddress.ip_network("169.254.0.0/16"),
            ]
            for n in blocked:
                if addr in n: return False, "عنوان محظور"
        except socket.gaierror:
            return False, "تعذر الوصول"
        return True, ""
    except Exception as e:
        return False, str(e)

def check_rate(user_id):
    c = RATE_CACHE.get(user_id, 0)
    if c >= RATE_LIMIT_PER_MIN:
        return False
    RATE_CACHE[user_id] = c + 1
    return True

def track_user(user):
    uid = user.id
    now = int(time.time())
    if uid in USERS_SEEN:
        USERS_SEEN[uid]["last_seen"] = now
        USERS_SEEN[uid]["name"] = user.first_name or USERS_SEEN[uid]["name"]
    else:
        USERS_SEEN[uid] = {
            "name": user.first_name or "",
            "username": user.username or "",
            "joined": now,
            "last_seen": now,
            "count": 0,
            "banned": False,
        }
    asyncio.create_task(db_save_user(uid, user.first_name or "", user.username or ""))

def is_banned(user_id):
    return USERS_SEEN.get(user_id, {}).get("banned", False)

async def check_force_join(client, user_id):
    if not FORCE_CHANNEL: return True
    if is_admin(user_id): return True
    try:
        await client.get_chat_member(FORCE_CHANNEL, user_id)
        return True
    except Exception as e:
        print(f"force_join FAIL user={user_id}: {e}")
        return False

def force_join_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 اشترك في القناة", url=f"https://t.me/{FORCE_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ تحقّق", callback_data="check_join")],
    ])

def get_cookies_args(url):
    u = url.lower()
    cookie_map = {
        "youtube.com": "youtube.txt", "youtu.be": "youtube.txt",
        "instagram.com": "instagram.txt",
        "twitter.com": "twitter.txt", "x.com": "twitter.txt",
        "facebook.com": "facebook.txt", "tiktok.com": "tiktok.txt",
    }
    for domain, fname in cookie_map.items():
        if domain in u:
            fp = COOKIES_DIR / fname
            if fp.exists():
                return ["--cookies", str(fp)]
    generic = COOKIES_DIR / "cookies.txt"
    if generic.exists():
        return ["--cookies", str(generic)]
    return []

_db_conn = None

async def db_init():
    global _db_conn
    _db_conn = await aiosqlite.connect(DB_PATH)
    _db_conn.row_factory = aiosqlite.Row
    await _db_conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            joined INTEGER,
            last_seen INTEGER,
            count INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            title TEXT,
            quality TEXT,
            size INTEGER DEFAULT 0,
            time INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_dl_user ON downloads(user_id);
    """)
    await _db_conn.commit()

async def db_load_users():
    cur = await _db_conn.execute("SELECT * FROM users")
    rows = await cur.fetchall()
    for r in rows:
        USERS_SEEN[r["user_id"]] = {
            "name": r["name"] or "",
            "username": r["username"] or "",
            "joined": r["joined"],
            "last_seen": r["last_seen"],
            "count": r["count"],
            "banned": bool(r["banned"]),
        }
    cur2 = await _db_conn.execute("SELECT * FROM downloads ORDER BY id DESC LIMIT 100")
    rows2 = await cur2.fetchall()
    for r in rows2:
        DOWNLOAD_LOG.append({
            "user_id": r["user_id"], "name": r["name"] or "",
            "title": r["title"] or "", "quality": r["quality"] or "",
            "size": r["size"] or 0, "time": r["time"] or 0,
        })
    return len(rows)

async def db_save_user(user_id, name, username):
    if not _db_conn: return
    now = int(time.time())
    try:
        await _db_conn.execute("""
            INSERT INTO users (user_id, name, username, joined, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                username=excluded.username,
                last_seen=excluded.last_seen
        """, (user_id, name, username, now, now))
        await _db_conn.commit()
    except Exception as e:
        print(f"db_save_user err: {e}")

async def db_inc_count(user_id):
    if not _db_conn: return
    await _db_conn.execute("UPDATE users SET count = count + 1 WHERE user_id = ?", (user_id,))
    await _db_conn.commit()

async def db_log_download(user_id, name, title, quality, size):
    if not _db_conn: return
    await _db_conn.execute("""
        INSERT INTO downloads (user_id, name, title, quality, size, time)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, name, title, quality, size, int(time.time())))
    await _db_conn.commit()

async def db_ban_user(user_id, ban):
    if not _db_conn: return
    await _db_conn.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if ban else 0, user_id))
    await _db_conn.commit()

async def db_total_downloads():
    if not _db_conn: return 0
    cur = await _db_conn.execute("SELECT COUNT(*) as c FROM downloads")
    r = await cur.fetchone()
    return r["c"]

def extract_qualities(info):
    qualities = {}
    formats = info.get("formats", [])
    for f in formats:
        if f.get("vcodec") == "none" or not f.get("vcodec"):
            continue
        h = f.get("height")
        if not h:
            w = f.get("width")
            if w and w >= 640:
                if w >= 7680: h = 4320
                elif w >= 3840: h = 2160
                elif w >= 2560: h = 1440
                elif w >= 1920: h = 1080
                elif w >= 1280: h = 720
                elif w >= 854: h = 480
                elif w >= 640: h = 360
            if not h:
                continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if h not in qualities:
            qualities[h] = {"height": h, "size": size if size > 0 else None}
        else:
            if size > 0:
                if qualities[h]["size"] is None or size < qualities[h]["size"]:
                    qualities[h]["size"] = size

    sorted_heights = sorted(qualities.keys(), reverse=True)
    result = []
    for h in sorted_heights:
        q = qualities[h]
        if h >= 4320: label = "8K"
        elif h >= 2160: label = "4K"
        elif h >= 1440: label = "2K"
        elif h >= 1080: label = "1080p"
        else: label = f"{h}p"
        result.append({"height": h, "size": q["size"], "label": label})
    return result

def calc_mp3_size(duration):
    if not duration: return None
    return int(duration * 40 * 1024)

COMMON_HEADERS = [
    "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "--no-check-certificates",
    "--socket-timeout", "15",
    "--geo-bypass",
    "--no-warnings",
    "--remote-components", "ejs:github",
]

async def get_media_info(url):
    cookies = get_cookies_args(url)
    cmd = ["yt-dlp", "--dump-json", "--no-playlist", *COMMON_HEADERS, *cookies, url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode == 0 and out:
            return json.loads(out.decode().strip().split("\n")[0])
    except Exception as e:
        print(f"get_media_info err: {e}")
    return {}

async def get_playlist_flat(url):
    cookies = get_cookies_args(url)
    cmd = ["yt-dlp", "--dump-single-json", "--flat-playlist", *COMMON_HEADERS, *cookies, url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180.0)
        if proc.returncode == 0 and out:
            return json.loads(out.decode())
    except Exception as e:
        print(f"get_playlist_flat err: {e}")
    return {}

async def get_video_full_info(url):
    cookies = get_cookies_args(url)
    cmd = ["yt-dlp", "--dump-json", "--no-playlist", *COMMON_HEADERS, *cookies, url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode == 0 and out:
            return json.loads(out.decode().strip().split("\n")[0])
    except Exception as e:
        print(f"get_video_full_info err: {e}")
    return {}

async def download_media(url, quality, job_id, progress_cb=None, use_playlist=False, pl_limit=0):
    ts = int(time.time())
    prefix = DOWNLOAD_DIR / f"job_{job_id}_{ts}"

    if quality == "audio":
        fmt_args = ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif quality == "best":
        fmt_args = ["-f", "bv*+ba/b", "--merge-output-format", "mp4", "-S", "res,size,br"]
    else:
        h = quality
        fmt_args = [
            "-f", f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
            "--merge-output-format", "mp4",
            "-S", f"res:{h},+size,+br"
        ]

    cookies = get_cookies_args(url)

    if use_playlist:
        if pl_limit > 0:
            pl_args = ["--yes-playlist", "-I", f"1:{pl_limit}"]
        else:
            pl_args = ["--yes-playlist"]
    else:
        pl_args = ["--no-playlist"]

    cmd = ["yt-dlp", *COMMON_HEADERS, *cookies, *pl_args, "--newline",
           *fmt_args, "-o", f"{prefix}.%(ext)s", url]

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        ACTIVE_PROCESSES[job_id] = process
        last = 0
        async for line in process.stdout:
            s = line.decode("utf-8", errors="ignore").strip()
            if "[download]" in s and "%" in s:
                m = re.search(r"(\d+\.?\d*)%", s)
                if m and progress_cb and time.time() - last > 2.5:
                    await progress_cb(float(m.group(1)))
                    last = time.time()
        await process.wait()
        if process.returncode != 0:
            return False, None, "فشل التنزيل", 0
        files = sorted(DOWNLOAD_DIR.glob(f"job_{job_id}_{ts}*"))
        finals = [f for f in files if not f.name.endswith((".part", ".ytdl"))]
        if not finals:
            return False, None, "الملف غير مكتمل", 0
        if use_playlist:
            total = sum(f.stat().st_size for f in finals)
            return True, finals, "", total
        fp = finals[0]
        return True, fp, "", fp.stat().st_size
    except Exception as e:
        return False, None, str(e)[:200], 0
    finally:
        ACTIVE_PROCESSES.pop(job_id, None)

def cancel_job(job_id):
    p = ACTIVE_PROCESSES.get(job_id)
    if p:
        p.terminate()
        return True
    return False

app = Client("universal_bot_v3", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def build_quality_kb(qualities, link_id, mp3_size, page=0, is_playlist=False):
    all_items = []
    for q in qualities:
        label = f"🎬 {q['label']}"
        size_str = human_size(q['size']) if q['size'] else "?"
        all_items.append((label, size_str, f"prv|{q['height']}|{link_id}"))

    mp3_label = "🎵 MP3"
    mp3_size_str = human_size(mp3_size) if mp3_size else "?"
    all_items.append((mp3_label, mp3_size_str, f"prv|audio|{link_id}"))

    total_items = len(all_items)
    total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = all_items[start:end]

    rows = []
    row = []
    for label, size, callback in page_items:
        btn_text = f"{label} • {size}"
        row.append(InlineKeyboardButton(btn_text, callback_data=callback))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"page|{link_id}|{page-1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"page|{link_id}|{page+1}"))
    if nav: rows.append(nav)

    if is_playlist:
        rows.append([InlineKeyboardButton("📋 عرض القائمة كاملة", callback_data=f"pl_info|{link_id}")])

    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def build_playlist_qualities_kb(link_id, quality_summary, mp3_total=None):
    rows = []
    row = []
    for q in quality_summary:
        height = q["height"]
        label = q["label"]
        total_size = q["total_size"]
        btn_text = f"🎬 {label} • {human_size(total_size)}"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"pldl|{link_id}|{height}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)

    if mp3_total:
        rows.append([InlineKeyboardButton(f"🎵 MP3 • {human_size(mp3_total)}", callback_data=f"pldl|{link_id}|audio")])

    rows.append([InlineKeyboardButton("📋 عرض الفيديوهات", callback_data=f"pl_videos|{link_id}|0")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def build_videos_kb(link_id, page, total_pages):
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"pl_videos|{link_id}|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ التالي", callback_data=f"pl_videos|{link_id}|{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 رجوع للجودات", callback_data=f"pl_info|{link_id}")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def confirm_kb(quality, link_id, is_playlist=False):
    if is_playlist:
        back_cb = f"pl_info|{link_id}"
    else:
        back_cb = f"back|{link_id}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد", callback_data=f"dl|{quality}|{link_id}"),
         InlineKeyboardButton("🔙 رجوع", callback_data=back_cb)],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
    ])

def cancel_kb(job_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 إلغاء التحميل", callback_data=f"cdl|{job_id}")]
    ])

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 المستخدمين", callback_data="adm|users"),
         InlineKeyboardButton("📊 السجل", callback_data="adm|log")],
        [InlineKeyboardButton("📈 الإحصائيات", callback_data="adm|stats"),
         InlineKeyboardButton("⚙️ الحالة", callback_data="adm|status")],
        [InlineKeyboardButton("🗑️ تنظيف الكاش", callback_data="adm|clear")],
    ])

async def analyze_playlist_full(url, client, chat_id, msg_id):
    flat = await get_playlist_flat(url)
    entries = flat.get("entries") or []
    if not entries:
        return None

    total = len(entries)
    videos = []

    for i, entry in enumerate(entries, 1):
        vid_url = entry.get("url") or entry.get("webpage_url")
        if not vid_url:
            vid_id = entry.get("id")
            if vid_id:
                vid_url = f"https://www.youtube.com/watch?v={vid_id}"
        if not vid_url:
            continue

        if i % 3 == 1 or i == total:
            try:
                pct = (i / total) * 100
                await client.edit_message_text(
                    chat_id, msg_id,
                    f"🔍 **جاري فحص القائمة...**\n\n"
                    f"{progress_bar(pct)}\n"
                    f"📊 الفحص: {i}/{total} فيديو\n"
                    f"⏱️ يرجى الانتظار..."
                )
            except Exception:
                pass

        full_info = await get_video_full_info(vid_url)
        if not full_info:
            continue

        title = full_info.get("title", "بدون عنوان")
        duration = full_info.get("duration", 0)

        qualities = extract_qualities(full_info)
        sizes = {}
        for q in qualities:
            sizes[q["height"]] = q["size"]

        mp3_size = calc_mp3_size(duration)

        videos.append({
            "title": title,
            "duration": duration,
            "url": vid_url,
            "sizes": sizes,
            "mp3_size": mp3_size,
        })

    quality_totals = {}
    for v in videos:
        for h, sz in v["sizes"].items():
            if sz:
                quality_totals[h] = quality_totals.get(h, 0) + sz

    sorted_heights = sorted(quality_totals.keys(), reverse=True)
    quality_summary = []
    for h in sorted_heights:
        if h >= 4320: label = "8K"
        elif h >= 2160: label = "4K"
        elif h >= 1440: label = "2K"
        elif h >= 1080: label = "1080p"
        else: label = f"{h}p"
        quality_summary.append({
            "height": h,
            "label": label,
            "total_size": quality_totals[h],
        })

    mp3_total = sum(v["mp3_size"] for v in videos if v["mp3_size"])
    total_duration = sum(v["duration"] for v in videos if v["duration"])

    return {
        "videos": videos,
        "quality_summary": quality_summary,
        "mp3_total": mp3_total,
        "total_duration": total_duration,
        "pl_title": flat.get("title", "قائمة"),
    }

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client, msg: Message):
    print(f"START user={msg.from_user.id}")
    u = msg.from_user
    track_user(u)

    if is_banned(u.id):
        await msg.reply_text("🚫 أنت محظور من استخدام البوت.")
        return

    if not await check_force_join(client, u.id):
        await msg.reply_text(
            f"⚠️ يجب الاشتراك في القناة أولاً\n\n📢 {FORCE_CHANNEL}",
            reply_markup=force_join_kb()
        )
        return

    rows = []
    if is_admin(u.id):
        rows.append([InlineKeyboardButton("👑 لوحة التحكم", callback_data="adm|main")])
    rows.append([InlineKeyboardButton("📖 المساعدة", callback_data="help"),
                 InlineKeyboardButton("📊 إحصائياتي", callback_data="mystats")])

    await msg.reply_text(
        f"👋 أهلاً {esc(u.first_name)}!\n\n🎬 أرسل لي أي رابط وسأحمّله لك.",
        reply_markup=InlineKeyboardMarkup(rows)
    )

@app.on_message(filters.command("help") & filters.private)
async def cmd_help(client, msg: Message):
    track_user(msg.from_user)
    await msg.reply_text("📖 **المساعدة**\n\nأرسل رابط → اختر الجودة → استلم الملف.")

@app.on_message(filters.command("mystats") & filters.private)
async def cmd_mystats(client, msg: Message):
    u = msg.from_user
    track_user(u)
    d = USERS_SEEN.get(u.id, {})
    await msg.reply_text(f"📊 **إحصائياتك**\n\n👤 {esc(u.first_name)}\n📥 التحميلات: **{d.get('count', 0)}**")

@app.on_message(filters.command("admin") & filters.private)
async def cmd_admin(client, msg: Message):
    u = msg.from_user
    track_user(u)
    if not is_admin(u.id):
        await msg.reply_text("❓ أمر غير معروف")
        return
    await msg.reply_text("👑 لوحة التحكم", reply_markup=admin_kb())

@app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "mystats", "admin", "cancel"]))
async def handle_url(client, msg: Message):
    u = msg.from_user
    track_user(u)

    if is_banned(u.id):
        await msg.reply_text("🚫 أنت محظور.")
        return

    if not await check_force_join(client, u.id):
        await msg.reply_text(f"⚠️ يجب الاشتراك في القناة أولاً\n\n📢 {FORCE_CHANNEL}", reply_markup=force_join_kb())
        return

    if not check_rate(u.id):
        await msg.reply_text("⚠️ تجاوزت الحد. حاول بعد دقيقة.")
        return

    url = msg.text.strip()
    if not is_valid_url(url):
        await msg.reply_text("❌ الرابط غير صحيح.")
        return

    safe, reason = is_safe_url(url)
    if not safe:
        await msg.reply_text(f"❌ رابط غير مسموح: {reason}")
        return

    status = await msg.reply_text("🔍 جاري فحص الرابط...")

    # فحص القائمة أولاً
    url_lower = url.lower()
    is_playlist_url = (
        "list=" in url_lower
        or "/playlist" in url_lower
        or "playlist?" in url_lower
    )

    if is_playlist_url:
        flat = await get_playlist_flat(url)
        entries = flat.get("entries") or []
        pl_title = flat.get("title", "قائمة")

        if not entries:
            await status.edit_text("❌ فشل قراءة القائمة.\n\nتأكد من الرابط.")
            return

        link_id = uuid.uuid4().hex[:10]
        URL_CACHE[link_id] = {
            "url": url,
            "user_id": u.id,
            "is_playlist": True,
            "pl_title": pl_title,
            "pl_count": len(entries),
            "analyzed": False,
            "chat_id": msg.chat.id,
            "msg_id": status.id,
        }

        cap = (
            f"📚 **{esc(short(pl_title, 80))}**\n\n"
            f"📊 عدد الفيديوهات: **{len(entries)}**\n\n"
            f"اضغط الزر لتحليل القائمة وعرض الجودات:\n"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 تحليل القائمة", callback_data=f"pl_analyze|{link_id}")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
        ])

        await status.edit_text(cap, reply_markup=kb)
        return

    # فيديو عادي
    info = await get_media_info(url)

    if not info:
        await status.edit_text("❌ تعذر قراءة الرابط.")
        return

    link_id = uuid.uuid4().hex[:10]

    qualities = extract_qualities(info)
    if not qualities:
        qualities = [{"height": "best", "size": None, "label": "Best"}]

    mp3_size = calc_mp3_size(info.get("duration", 0))

    URL_CACHE[link_id] = {
        "url": url,
        "user_id": u.id,
        "is_playlist": False,
        "qualities": qualities,
        "mp3_size": mp3_size,
        "title": info.get("title", "بدون عنوان"),
    }

    title = short(info.get("title", "بدون عنوان"), 80)
    duration = human_time(info.get("duration", 0))
    uploader = info.get("uploader") or info.get("extractor_key") or "غير معروف"
    qualities_list = ", ".join([q["label"] for q in qualities])

    cap = (
        f"📌 **{esc(title)}**\n\n"
        f"🌐 المصدر: **{esc(uploader)}**\n"
        f"⏱️ المدة: **{duration}**\n"
        f"📊 الجودات: {qualities_list}\n\n"
        f"👇 اختر الجودة:"
    )

    kb = build_quality_kb(qualities, link_id, mp3_size, page=0, is_playlist=False)
    await status.edit_text(cap, reply_markup=kb)

@app.on_callback_query(filters.regex(r"^pl_analyze\|"))
async def cb_pl_analyze(client, cb: CallbackQuery):
    print(f"🔍 CB_PL_ANALYZE CALLED")
    link_id = cb.data.split("|", 1)[1]
    cached = URL_CACHE.get(link_id)
    if not cached or cached["user_id"] != cb.from_user.id:
        await cb.answer("انتهت صلاحية الرابط", show_alert=True); return

    await cb.answer("🔍 جاري التحليل...")

    chat_id = cb.message.chat.id
    msg_id = cb.message.id
    url = cached["url"]

    result = await analyze_playlist_full(url, client, chat_id, msg_id)

    print(f"🔍 RESULT CHECK: {result is not None}")
    if result:
        print(f"🔍 RESULT VIDEOS: {len(result.get('videos', []))}")
        print(f"🔍 RESULT TITLE: {result.get('pl_title')}")
    if not result or not result["videos"]:
        try:
            await cb.message.edit_text("❌ فشل تحليل القائمة.")
        except Exception: pass
        return

    cached["analyzed"] = True
    cached["videos"] = result["videos"]
    cached["quality_summary"] = result["quality_summary"]
    cached["mp3_total"] = result["mp3_total"]
    cached["total_duration"] = result["total_duration"]
    URL_CACHE[link_id] = cached

    total_videos = len(result["videos"])
    total_dur = human_time(result["total_duration"])

    cap = (
        f"📚 **{esc(short(result['pl_title'], 80))}**\n\n"
        f"📊 عدد الفيديوهات: **{total_videos}**\n"
        f"⏱️ المدة الإجمالية: **{total_dur}**\n\n"
        f"💾 **الحجم الكلي لكل جودة:**\n"
    )

    for q in result["quality_summary"][:10]:
        cap += f"• 🎬 {q['label']}: **{human_size(q['total_size'])}**\n"

    if result["mp3_total"]:
        cap += f"• 🎵 MP3: **{human_size(result['mp3_total'])}**\n"

    cap += "\n👇 اختر جودة لتحميل القائمة:"

    kb = build_playlist_qualities_kb(link_id, result["quality_summary"], result["mp3_total"])

    try:
        await cb.message.edit_text(cap, reply_markup=kb)
    except MessageNotModified:
        pass

@app.on_callback_query(filters.regex(r"^pl_info\|"))
async def cb_pl_info(client, cb: CallbackQuery):
    link_id = cb.data.split("|", 1)[1]
    cached = URL_CACHE.get(link_id)
    if not cached or cached["user_id"] != cb.from_user.id:
        await cb.answer("انتهت صلاحية الرابط", show_alert=True); return
    if not cached.get("analyzed"):
        await cb.answer("يجب التحليل أولاً", show_alert=True); return

    await cb.answer()

    result_q = cached["quality_summary"]
    result_mp3 = cached.get("mp3_total")
    total_videos = len(cached["videos"])
    total_dur = human_time(cached.get("total_duration", 0))

    cap = (
        f"📚 **{esc(short(cached.get('pl_title', 'قائمة'), 80))}**\n\n"
        f"📊 عدد الفيديوهات: **{total_videos}**\n"
        f"⏱️ المدة الإجمالية: **{total_dur}**\n\n"
        f"💾 **الحجم الكلي لكل جودة:**\n"
    )

    for q in result_q[:10]:
        cap += f"• 🎬 {q['label']}: **{human_size(q['total_size'])}**\n"

    if result_mp3:
        cap += f"• 🎵 MP3: **{human_size(result_mp3)}**\n"

    cap += "\n👇 اختر جودة لتحميل القائمة:"

    kb = build_playlist_qualities_kb(link_id, result_q, result_mp3)
    try:
        await cb.message.edit_text(cap, reply_markup=kb)
    except MessageNotModified:
        pass

@app.on_callback_query(filters.regex(r"^pl_videos\|"))
async def cb_pl_videos(client, cb: CallbackQuery):
    parts = cb.data.split("|")
    link_id = parts[1]
    page = int(parts[2])
    cached = URL_CACHE.get(link_id)
    if not cached or cached["user_id"] != cb.from_user.id:
        await cb.answer("انتهت صلاحية الرابط", show_alert=True); return
    if not cached.get("analyzed"):
        await cb.answer("يجب التحليل أولاً", show_alert=True); return

    await cb.answer()

    videos = cached["videos"]
    total = len(videos)
    total_pages = max(1, (total + VIDEOS_PER_PAGE - 1) // VIDEOS_PER_PAGE)

    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1

    start = page * VIDEOS_PER_PAGE
    end = min(start + VIDEOS_PER_PAGE, total)

    cap = f"📚 **الفيديوهات ({start+1}-{end} من {total})**\n\n"

    for i in range(start, end):
        v = videos[i]
        title = short(v["title"], 45)
        dur = human_time(v["duration"])

        smallest_h = None
        smallest_size = None
        for h in sorted(v["sizes"].keys()):
            if v["sizes"][h]:
                smallest_h = h
                smallest_size = v["sizes"][h]
                break

        size_line = ""
        if smallest_h and smallest_size:
            size_line = f" • {smallest_h}p: {human_size(smallest_size)}"
        elif v.get("mp3_size"):
            size_line = f" • MP3: {human_size(v['mp3_size'])}"

        cap += f"**{i+1}.** {esc(title)}\n   ⏱️ {dur}{size_line}\n\n"

    kb = build_videos_kb(link_id, page, total_pages)
    try:
        await cb.message.edit_text(cap, reply_markup=kb)
    except MessageNotModified:
        pass

@app.on_callback_query(filters.regex(r"^noop$"))
async def cb_noop(client, cb: CallbackQuery):
    await cb.answer()

@app.on_callback_query(filters.regex(r"^cancel$"))
async def cb_cancel(client, cb: CallbackQuery):
    await cb.answer("تم الإلغاء")
    try: await cb.message.edit_text("❌ تم إلغاء العملية.")
    except MessageNotModified: pass

@app.on_callback_query(filters.regex(r"^check_join$"))
async def cb_check_join(client, cb: CallbackQuery):
    ok = await check_force_join(client, cb.from_user.id)
    if ok:
        await cb.answer("✅ تم التحقق!", show_alert=True)
        try: await cb.message.edit_text("✅ تم التحقق من الاشتراك.\n\nأرسل رابطاً للبدء 🎬")
        except MessageNotModified: pass
    else:
        await cb.answer("❌ لم تشترك بعد!", show_alert=True)

@app.on_callback_query(filters.regex(r"^help$"))
async def cb_help(client, cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text("📖 **المساعدة**\n\nأرسل رابط → اختر الجودة → استلم الملف.",
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="home")]]))

@app.on_callback_query(filters.regex(r"^home$"))
async def cb_home(client, cb: CallbackQuery):
    u = cb.from_user
    await cb.answer()
    track_user(u)
    rows = []
    if is_admin(u.id):
        rows.append([InlineKeyboardButton("👑 لوحة التحكم", callback_data="adm|main")])
    rows.append([InlineKeyboardButton("📖 المساعدة", callback_data="help")])
    await cb.message.edit_text(f"👋 أهلاً {esc(u.first_name)}!", reply_markup=InlineKeyboardMarkup(rows))

@app.on_callback_query(filters.regex(r"^mystats$"))
async def cb_mystats(client, cb: CallbackQuery):
    u = cb.from_user
    await cb.answer()
    d = USERS_SEEN.get(u.id, {})
    await cb.message.edit_text(f"📊 التحميلات: {d.get('count', 0)}",
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="home")]]))

@app.on_callback_query(filters.regex(r"^page\|"))
async def cb_page(client, cb: CallbackQuery):
    parts = cb.data.split("|")
    link_id = parts[1]
    page = int(parts[2])
    cached = URL_CACHE.get(link_id)
    if not cached:
        await cb.answer("انتهت الصلاحية", show_alert=True); return
    if cached["user_id"] != cb.from_user.id:
        await cb.answer("هذا الرابط ليس لك", show_alert=True); return
    await cb.answer()
    qualities = cached["qualities"]
    mp3_size = cached.get("mp3_size")
    kb = build_quality_kb(qualities, link_id, mp3_size, page=page, is_playlist=False)
    try:
        await cb.message.edit_reply_markup(kb)
    except MessageNotModified: pass

@app.on_callback_query(filters.regex(r"^prv\|"))
async def cb_preview(client, cb: CallbackQuery):
    parts = cb.data.split("|")
    quality = parts[1]
    link_id = parts[2]
    cached = URL_CACHE.get(link_id)
    if not cached:
        await cb.answer("انتهت الصلاحية", show_alert=True); return
    if cached["user_id"] != cb.from_user.id:
        await cb.answer("ليس لك", show_alert=True); return
    await cb.answer()

    size = None
    label = quality
    if quality == "audio":
        label = "MP3"
        size = cached.get("mp3_size")
    elif quality == "best":
        label = "Best"
    else:
        for q in cached.get("qualities", []):
            if str(q["height"]) == str(quality):
                label = q["label"]
                size = q["size"]
                break

    size_str = human_size(size) if size else "?"
    text = f"⚠️ **تأكيد**\n\n🎯 الجودة: **{label}**\n💾 الحجم: **{size_str}**\n\nهل تريد البدء؟"
    try:
        await cb.message.edit_text(text, reply_markup=confirm_kb(quality, link_id, is_playlist=False))
    except MessageNotModified: pass

@app.on_callback_query(filters.regex(r"^back\|"))
async def cb_back(client, cb: CallbackQuery):
    link_id = cb.data.split("|", 1)[1]
    cached = URL_CACHE.get(link_id)
    if not cached:
        await cb.answer("انتهت الصلاحية", show_alert=True); return
    await cb.answer()
    kb = build_quality_kb(cached["qualities"], link_id, cached.get("mp3_size"), page=0, is_playlist=False)
    try:
        await cb.message.edit_reply_markup(kb)
    except MessageNotModified: pass

@app.on_callback_query(filters.regex(r"^pldl\|"))
async def cb_playlist_dl(client, cb: CallbackQuery):
    parts = cb.data.split("|")
    link_id = parts[1]
    quality = parts[2]
    cached = URL_CACHE.get(link_id)
    if not cached or cached["user_id"] != cb.from_user.id:
        await cb.answer("ليس لك", show_alert=True); return

    await cb.answer("...")

    quality_label = quality
    expected_size = None
    if quality == "audio":
        quality_label = "MP3"
        expected_size = cached.get("mp3_total")
    else:
        for q in cached.get("quality_summary", []):
            if str(q["height"]) == str(quality):
                quality_label = q["label"]
                expected_size = q["total_size"]
                break

    total_videos = len(cached.get("videos", []))
    size_str = human_size(expected_size) if expected_size else "?"

    text = (
        f"⚠️ **تأكيد تحميل القائمة**\n\n"
        f"🎯 الجودة: **{quality_label}**\n"
        f"📊 عدد الفيديوهات: **{total_videos}**\n"
        f"💾 الحجم التقريبي: **{size_str}**\n\n"
        f"هل تريد المتابعة؟"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم", callback_data=f"pldl_start|{link_id}|{quality}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"pl_info|{link_id}")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
    ])

    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except MessageNotModified: pass

@app.on_callback_query(filters.regex(r"^pldl_start\|"))
async def cb_playlist_dl_start(client, cb: CallbackQuery):
    parts = cb.data.split("|")
    link_id = parts[1]
    quality = parts[2]
    cached = URL_CACHE.get(link_id)
    if not cached or cached["user_id"] != cb.from_user.id:
        await cb.answer("ليس لك", show_alert=True); return

    await cb.answer("🚀 جاري البدء...")

    url = cached["url"]
    u = cb.from_user
    chat_id = cb.message.chat.id
    msg_id = cb.message.id
    job_id = int(time.time() * 1000) % 10**9

    await cb.message.edit_text("⏳ جاري تجهيز القائمة...", reply_markup=cancel_kb(job_id))
    asyncio.create_task(run_playlist_download(client, chat_id, msg_id, u, url, quality, job_id))

@app.on_callback_query(filters.regex(r"^dl\|"))
async def cb_download(client, cb: CallbackQuery):
    parts = cb.data.split("|")
    quality = parts[1]
    link_id = parts[2]
    cached = URL_CACHE.get(link_id)
    if not cached:
        await cb.answer("انتهت الصلاحية", show_alert=True); return
    if cached["user_id"] != cb.from_user.id:
        await cb.answer("ليس لك", show_alert=True); return
    await cb.answer("...")
    url = cached["url"]
    u = cb.from_user
    chat_id = cb.message.chat.id
    msg_id = cb.message.id
    job_id = int(time.time() * 1000) % 10**9
    await cb.message.edit_text("⏳ جاري التحضير...", reply_markup=cancel_kb(job_id))
    asyncio.create_task(run_download(client, chat_id, msg_id, u, url, quality, job_id))

async def run_download(client, chat_id, msg_id, user, url, quality, job_id):
    info = await get_media_info(url)
    title = info.get("title", "بدون عنوان")

    async def prog(p):
        try:
            await client.edit_message_text(chat_id, msg_id,
                f"⬇️ **جاري التنزيل...**\n\n{progress_bar(p)}\n📊 {p:.1f}%",
                reply_markup=cancel_kb(job_id))
        except Exception: pass

    try:
        await client.edit_message_text(chat_id, msg_id,
            f"⬇️ **بدء التنزيل...**\n\n📌 {esc(short(title, 60))}",
            reply_markup=cancel_kb(job_id))
    except Exception: pass

    success, fp, err, size = await download_media(url, quality, job_id, prog)

    if not success:
        try: await client.edit_message_text(chat_id, msg_id, f"❌ **فشل التنزيل**\n\n`{esc(err)}`")
        except Exception: pass
        return

    last = [0]
    async def up_prog(cur, tot):
        now = time.time()
        if now - last[0] > 3:
            try:
                await client.edit_message_text(chat_id, msg_id,
                    f"⬆️ **جاري الرفع...**\n\n{progress_bar(cur, tot)}\n📦 {human_size(cur)} / {human_size(tot)}")
                last[0] = now
            except Exception: pass

    try: await client.edit_message_text(chat_id, msg_id, "⬆️ **بدء الرفع...**")
    except Exception: pass

    try:
        cap = f"📌 {esc(short(title, 80))}\n📦 {human_size(size)}"
        if quality == "audio" or str(fp).endswith(".mp3"):
            await client.send_audio(chat_id, audio=str(fp), caption=cap, progress=up_prog)
        else:
            await client.send_video(chat_id, video=str(fp), caption=cap, supports_streaming=True, progress=up_prog)

        if user.id in USERS_SEEN:
            USERS_SEEN[user.id]["count"] += 1
        await db_inc_count(user.id)
        await db_log_download(user.id, user.first_name or "", title, str(quality), size)
        DOWNLOAD_LOG.append({"user_id": user.id, "name": user.first_name or "", "title": title, "quality": str(quality), "size": size, "time": int(time.time())})
        try: await client.delete_messages(chat_id, msg_id)
        except Exception: pass
    except Exception as e:
        print(f"upload err: {e}")
        try: await client.edit_message_text(chat_id, msg_id, f"❌ **فشل الرفع:**\n`{esc(str(e)[:200])}`")
        except Exception: pass
    finally:
        try: fp.unlink()
        except Exception: pass

async def run_playlist_download(client, chat_id, msg_id, user, url, quality, job_id):
    flat = await get_playlist_flat(url)
    pl_title = flat.get("title", "قائمة")

    async def prog(p):
        try:
            await client.edit_message_text(chat_id, msg_id,
                f"⬇️ **تحميل القائمة...**\n\n📚 {esc(short(pl_title, 50))}\n\n{progress_bar(p)}",
                reply_markup=cancel_kb(job_id))
        except Exception: pass

    try:
        await client.edit_message_text(chat_id, msg_id,
            f"⬇️ **بدء تحميل القائمة...**\n\n📚 {esc(short(pl_title, 60))}",
            reply_markup=cancel_kb(job_id))
    except Exception: pass

    success, files, err, size = await download_media(url, quality, job_id, prog, use_playlist=True, pl_limit=0)

    if not success:
        try: await client.edit_message_text(chat_id, msg_id, f"❌ **فشل:**\n`{esc(err)}`")
        except Exception: pass
        return

    total = len(files)
    sent = 0
    for i, fp in enumerate(files, 1):
        try:
            try:
                await client.edit_message_text(chat_id, msg_id,
                    f"⬆️ **رفع {i}/{total}...**\n\n📄 {esc(fp.name[:50])}")
            except Exception: pass

            cap = f"📌 {esc(fp.stem[:80])}\n📚 {esc(short(pl_title, 50))}\n📦 {i}/{total}"
            if str(fp).endswith(".mp3"):
                await client.send_audio(chat_id, audio=str(fp), caption=cap)
            else:
                await client.send_video(chat_id, video=str(fp), caption=cap, supports_streaming=True)
            sent += 1
        except Exception as e:
            print(f"pl upload err: {e}")
        finally:
            try: fp.unlink()
            except Exception: pass

    if user.id in USERS_SEEN:
        USERS_SEEN[user.id]["count"] += 1
    await db_inc_count(user.id)
    await db_log_download(user.id, user.first_name or "", pl_title, "playlist", size)
    DOWNLOAD_LOG.append({"user_id": user.id, "name": user.first_name or "", "title": pl_title, "quality": "playlist", "size": size, "time": int(time.time())})
    try: await client.edit_message_text(chat_id, msg_id, f"✅ **اكتملت القائمة!**\n\n📤 أُرسل: {sent}/{total}")
    except Exception: pass

@app.on_callback_query(filters.regex(r"^cdl\|"))
async def cb_cancel_dl(client, cb: CallbackQuery):
    job_id = int(cb.data.split("|")[1])
    if cancel_job(job_id):
        await cb.answer("🛑 جاري الإلغاء...", show_alert=True)
    else:
        await cb.answer("❌ التحميل غير نشط", show_alert=True)

def admin_only(_, __, cb: CallbackQuery):
    return is_admin(cb.from_user.id)

admin_filter = filters.create(admin_only)

@app.on_callback_query(filters.regex(r"^adm\|main$") & admin_filter)
async def cb_adm_main(client, cb: CallbackQuery):
    await cb.answer()
    try: await cb.message.edit_text("👑 لوحة التحكم", reply_markup=admin_kb())
    except MessageNotModified: pass

@app.on_callback_query(filters.regex(r"^adm\|") & ~admin_filter)
async def cb_adm_unauth(client, cb: CallbackQuery):
    await cb.answer("مخصص للمشرف", show_alert=True)

@app.on_callback_query(filters.regex(r"^adm\|stats$") & admin_filter)
async def cb_adm_stats(client, cb: CallbackQuery):
    await cb.answer()
    uptime = human_time(int(time.time()) - START_TIME)
    total_db = await db_total_downloads()
    txt = (
        f"📈 **الإحصائيات**\n\n"
        f"👥 المستخدمين: {len(USERS_SEEN)}\n"
        f"📥 التحميلات (ذاكرة): {len(DOWNLOAD_LOG)}\n"
        f"📥 التحميلات (db): {total_db}\n"
        f"🔥 قيد التنفيذ: {len(ACTIVE_PROCESSES)}\n"
        f"⏱️ وقت التشغيل: {uptime}"
    )
    await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="adm|main")]]))

@app.on_callback_query(filters.regex(r"^adm\|status$") & admin_filter)
async def cb_adm_status(client, cb: CallbackQuery):
    await cb.answer()
    txt = (
        f"⚙️ **الحالة**\n\n"
        f"🔥 قيد التنفيذ: {len(ACTIVE_PROCESSES)}\n"
        f"🍪 الكوكيز: {'موجودة' if any(COOKIES_DIR.iterdir()) else 'لا يوجد'}\n"
        f"📢 قناة الاشتراك: {FORCE_CHANNEL or 'معطلة'}"
    )
    await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="adm|main")]]))

@app.on_callback_query(filters.regex(r"^adm\|users$") & admin_filter)
async def cb_adm_users(client, cb: CallbackQuery):
    await cb.answer()
    users = list(USERS_SEEN.items())
    txt = f"👥 **المستخدمين** ({len(users)}):\n\n"
    for uid, u in users[:20]:
        txt += f"• {esc(u['name'][:25])} (`{uid}`)\n"
    await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="adm|main")]]))

@app.on_callback_query(filters.regex(r"^adm\|log$") & admin_filter)
async def cb_adm_log(client, cb: CallbackQuery):
    await cb.answer()
    logs = list(reversed(DOWNLOAD_LOG))[:15]
    txt = f"📊 **السجل** ({len(DOWNLOAD_LOG)}):\n\n"
    for d in logs:
        txt += f"• {esc(d['name'][:15])}: {esc(short(d['title'], 25))} [{d['quality']}, {human_size(d['size'])}]\n"
    await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="adm|main")]]))

@app.on_callback_query(filters.regex(r"^adm\|clear$") & admin_filter)
async def cb_adm_clear(client, cb: CallbackQuery):
    a = len(URL_CACHE); b = len(RATE_CACHE)
    URL_CACHE.clear(); RATE_CACHE.clear()
    await cb.answer("تم التنظيف", show_alert=True)
    await cb.message.edit_text(
        f"✅ **تم تنظيف الكاش**\n\n• روابط: {a}\n• rate: {b}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="adm|main")]])
    )

async def _startup():
    await db_init()
    count = await db_load_users()
    print(f"DB connected | loaded {count} users")
# ═══════════════════════════════════════════
#           HEALTH SERVER (للـ Render)
# ═══════════════════════════════════════════
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Bot is running")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        print(f"✅ Health server on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"Health server error: {e}")

# شغّل health server في thread منفصل
_health_thread = threading.Thread(target=run_health_server, daemon=True)
_health_thread.start()

# ═══════════════════════════════════════════
#           تشغيل البوت
# ═══════════════════════════════════════════

if __name__ == "__main__":
    print("Starting bot...")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_startup())
    app.run()
# Mon Oct  5 10:18:07 +03 2026
