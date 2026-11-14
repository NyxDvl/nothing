import asyncio
import logging
import os
import re

import discord
import yt_dlp
from discord.ext import tasks

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:  # spotipy اختياري
    spotipy = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("musicbot")

# ───────────────────────── الإعدادات ─────────────────────────
TOKEN = os.environ["DISCORD_TOKEN"]
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID", "1207035591829757993"))
PREFIX = os.getenv("PREFIX", "!")  # خله فاضي "" اذا تبي الأوامر بدون بادئة
TEXT_CHANNEL_ID = int(os.getenv("TEXT_CHANNEL_ID", "0"))  # اختياري: تحديد روم كتابي للأوامر
PREFERRED_SOURCE = os.getenv("SOURCE", "youtube").lower()  # youtube | soundcloud

PLAY_CMDS = {"play", "شغل", "p"}
STOP_CMDS = {"stop", "وقف"}
SKIP_CMDS = {"skip", "سكب", "سكيب"}

COOKIES_PATH = "/tmp/yt_cookies.txt"
if os.getenv("YT_COOKIES_B64"):
    import base64
    with open(COOKIES_PATH, "wb") as f:
        f.write(base64.b64decode(os.environ["YT_COOKIES_B64"]))
elif os.getenv("YT_COOKIES"):
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(os.environ["YT_COOKIES"])
elif os.path.exists("cookies.txt"):
    import shutil
    shutil.copy("cookies.txt", COOKIES_PATH)

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": None,
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
}
if os.path.exists(COOKIES_PATH):
    YDL_OPTS["cookiefile"] = COOKIES_PATH

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

URL_RE = re.compile(r"^https?://", re.I)
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)")

sp = None
if spotipy and os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET"):
    sp = spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=os.environ["SPOTIFY_CLIENT_ID"],
            client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        )
    )

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
client = discord.Client(intents=intents)


# ───────────────────────── جلب الأغاني ─────────────────────────
def _extract(query: str) -> dict:
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("no results")
        info = entries[0]
    return info


class Track:
    def __init__(self, query: str, requester: discord.Member, title: str | None = None):
        self.query = query
        self.requester = requester
        self.title = title or query
        self.stream_url = None
        self.page_url = None
        self.thumbnail = None
        self.duration = None

    async def resolve(self):
        loop = asyncio.get_running_loop()
        if URL_RE.match(self.query):
            attempts = [self.query]
        else:
            yt, sc = f"ytsearch1:{self.query}", f"scsearch1:{self.query}"
            attempts = [sc, yt] if PREFERRED_SOURCE == "soundcloud" else [yt, sc]

        last_err = None
        for q in attempts:
            try:
                info = await loop.run_in_executor(None, _extract, q)
                self.stream_url = info["url"]
                self.title = info.get("title") or self.title
                self.page_url = info.get("webpage_url") or info.get("original_url")
                self.thumbnail = info.get("thumbnail")
                self.duration = info.get("duration")
                return
            except Exception as e:  # نجرب المصدر الثاني
                last_err = e
                log.warning("فشل المصدر %s: %s", q.split(":")[0], e)
        raise RuntimeError(last_err)


async def spotify_to_queries(url: str) -> list[str]:
    m = SPOTIFY_RE.search(url)
    if not m or not sp:
        return []
    kind, sid = m.groups()
    loop = asyncio.get_running_loop()

    def fetch():
        out = []
        if kind == "track":
            t = sp.track(sid)
            out.append(f"{t['artists'][0]['name']} - {t['name']}")
        elif kind == "album":
            for t in sp.album_tracks(sid, limit=50)["items"]:
                out.append(f"{t['artists'][0]['name']} - {t['name']}")
        else:
            for it in sp.playlist_items(sid, limit=100)["items"]:
                t = it.get("track")
                if t:
                    out.append(f"{t['artists'][0]['name']} - {t['name']}")
        return out

    return await loop.run_in_executor(None, fetch)


# ───────────────────────── المشغّل ─────────────────────────
def fmt_duration(sec):
    if not sec:
        return "؟"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Player:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Track | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self._done = asyncio.Event()
        self.task = asyncio.create_task(self._loop())

    async def _say(self, **kw):
        if self.text_channel:
            try:
                await self.text_channel.send(**kw)
            except discord.HTTPException:
                pass

    async def _loop(self):
        while True:
            track = await self.queue.get()

            # ننتظر لين يرجع البوت للروم لو انقطع
            vc = self.guild.voice_client
            for _ in range(30):
                if vc and vc.is_connected():
                    break
                await asyncio.sleep(1)
                vc = self.guild.voice_client
            else:
                continue

            try:
                await track.resolve()
            except Exception as e:
                log.error("تعذر جلب %s: %s", track.query, e)
                await self._say(content=f"❌ ما قدرت أجيب: **{track.title}**")
                continue

            self.current = track
            self._done.clear()
            source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTS)
            try:
                vc.play(source, after=lambda err: client.loop.call_soon_threadsafe(self._done.set))
            except Exception as e:
                log.error("خطأ تشغيل: %s", e)
                self.current = None
                continue

            embed = discord.Embed(
                title="🎶 يشتغل الحين",
                description=f"[{track.title}]({track.page_url})" if track.page_url else track.title,
                color=0x5865F2,
            )
            embed.add_field(name="المدة", value=fmt_duration(track.duration))
            embed.add_field(name="طلبها", value=track.requester.mention)
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            await self._say(embed=embed)

            await self._done.wait()
            self.current = None

    def clear(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break


players: dict[int, Player] = {}


def get_player(guild: discord.Guild) -> Player:
    if guild.id not in players:
        players[guild.id] = Player(guild)
    return players[guild.id]


# ───────────────────────── البقاء 24/7 في الروم ─────────────────────────
@tasks.loop(seconds=10)
async def keep_alive():
    channel = client.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        return
    guild = channel.guild
    vc = guild.voice_client
    try:
        if vc is None or not vc.is_connected():
            if vc:
                await vc.disconnect(force=True)
            await channel.connect(reconnect=True, timeout=30, self_deaf=True)
            log.info("دخلت الروم %s", channel.name)
        elif vc.channel.id != VOICE_CHANNEL_ID:
            await vc.move_to(channel)
    except Exception as e:
        log.warning("فشل الاتصال بالروم: %s", e)


@client.event
async def on_ready():
    log.info("جاهز: %s", client.user)
    if not keep_alive.is_running():
        keep_alive.start()


# ───────────────────────── الأوامر ─────────────────────────
def in_room(member: discord.Member) -> bool:
    return bool(member.voice and member.voice.channel and member.voice.channel.id == VOICE_CHANNEL_ID)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if TEXT_CHANNEL_ID and message.channel.id != TEXT_CHANNEL_ID:
        return

    content = message.content.strip()
    if PREFIX:
        if not content.startswith(PREFIX):
            return
        content = content[len(PREFIX):].lstrip()
    parts = content.split(None, 1)
    if not parts:
        return
    cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")

    if cmd not in PLAY_CMDS | STOP_CMDS | SKIP_CMDS:
        return

    if not in_room(message.author):
        await message.reply("🎧 لازم تكون داخل الروم الصوتي عشان تتحكم بالبوت.", mention_author=False)
        return

    player = get_player(message.guild)
    player.text_channel = message.channel
    vc = message.guild.voice_client

    # ── play ──
    if cmd in PLAY_CMDS:
        if not arg:
            await message.reply(f"اكتب اسم الأغنية أو الرابط: `{PREFIX}شغل اسم الأغنية`", mention_author=False)
            return
        if not vc or not vc.is_connected():
            await message.reply("⏳ البوت يتصل بالروم، جرب بعد ثواني.", mention_author=False)
            return

        if SPOTIFY_RE.search(arg):
            if not sp:
                await message.reply("❌ روابط سبوتيفاي غير مفعلة، استخدم اسم الأغنية أو رابط يوتيوب/ساوند كلاود.", mention_author=False)
                return
            try:
                queries = await spotify_to_queries(arg)
            except Exception as e:
                log.error("سبوتيفاي: %s", e)
                await message.reply("❌ ما قدرت أقرأ رابط سبوتيفاي.", mention_author=False)
                return
            for q in queries:
                await player.queue.put(Track(q, message.author))
            await message.reply(f"✅ انضافت **{len(queries)}** أغنية من سبوتيفاي للقائمة.", mention_author=False)
            return

        track = Track(arg, message.author)
        await player.queue.put(track)
        if player.current or player.queue.qsize() > 1:
            await message.reply(f"✅ انضافت للقائمة: **{arg}**", mention_author=False)
        else:
            await message.add_reaction("🔎")

    # ── stop ──
    elif cmd in STOP_CMDS:
        player.clear()
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await message.reply("⏹️ وقفت الأغنية ومسحت القائمة.", mention_author=False)

    # ── skip ──
    elif cmd in SKIP_CMDS:
        if vc and vc.is_playing():
            vc.stop()
            await message.reply("⏭️ تم التخطي.", mention_author=False)
        else:
            await message.reply("ما فيه شي يشتغل الحين.", mention_author=False)


client.run(TOKEN, log_handler=None)
