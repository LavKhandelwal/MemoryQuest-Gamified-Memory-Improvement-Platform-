"""
Memory Quest: a gamified memory improvement platform.

Runs the same way in VS Code (python memory_quest.py) and in Google Colab
(%run memory_quest.py). Only needs: gradio (5.x), pandas, matplotlib.

Modules (numbering follows the project analysis):
  1. User registration and profile      -> accounts in SQLite, PBKDF2 password hashes
  2. Baseline assessment                -> 5 fixed unscramble rounds, repeatable as a re-test
  3. Memory game engine                 -> Unscramble, Word Chain vs computer, Daily Quests
  4. Adaptive difficulty                -> keeps accuracy near 85% (Wilson et al., 2019)
  5. Spaced repetition scheduler        -> simplified half-life model (Settles & Meeder, 2016)
  6. Gamification                       -> XP, levels, daily streaks, badges
  7. Progress dashboard                 -> charts, baseline vs re-test, CSV export

This is a practice tool, not a medical device. It does not diagnose or treat anything.
"""

import difflib
import hashlib
import inspect
import html
import math
import os
import random
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, timedelta

import gradio as gr
import pandas as pd
from matplotlib.figure import Figure

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

IN_COLAB = "google.colab" in sys.modules
DB_PATH = os.environ.get(
    "MQ_DB", "/content/memory_quest.db" if IN_COLAB else "memory_quest.db"
)

TARGET_ACCURACY = 0.85          # Wilson et al. 2019
BAND = 0.10                     # level changes when the rolling mean leaves 0.75..0.95
ROLLING_ROUNDS = 5              # rounds used for the rolling mean
MIN_ROUNDS_BEFORE_CHANGE = 3
MAX_SCRAMBLE_LEVEL = 12         # level 12 = 15 characters
BASELINE_LENGTHS = [4, 5, 6, 7, 8]
SEQ_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I to avoid look-alikes

MAX_CHAIN = 20
CHAIN_FORGET = {1: 0.10, 2: 0.05, 3: 0.02}          # computer's chance of forgetting each word
CHAIN_HINTS = {1: 2, 2: 1, 3: 0}
CHAIN_NAMES = {1: "Easy", 2: "Medium", 3: "Hard"}

TASK_XP = {"Easy": 10, "Medium": 20, "Hard": 40}
ALL_TASKS_BONUS = 25

REVIEW_DUE_BELOW = 0.70         # an item is due when predicted recall drops under this
MAX_HALF_LIFE_DAYS = 60.0

AGE_GROUPS = ["Under 18", "18-25", "26-40", "41-60", "60+"]
GOALS = ["Study and exams", "Everyday memory", "Work focus", "Just for fun"]

EASY_WORDS = """apple bread chair cloud river stone tiger horse lemon spoon table glass brush clock
mouse plant sheep train water beach candy dream flute grape house knife lamp moon nurse ocean paint
queen robot salad tower voice wheel yacht zebra bird boat cake coin desk duck farm fish frog gold
hill kite lion nest pear rain ring sand ship sock star tree wolf""".split()
MEDIUM_WORDS = """basket bridge candle carpet castle circle dinner forest garden guitar hammer island
jacket kitchen ladder magnet market mirror orange pencil pillow planet pocket rabbit rocket saddle
school silver sponge statue summer ticket tunnel turtle window winter wizard blanket compass
lantern harbor meadow pepper violin""".split()
HARD_WORDS = """elephant umbrella mountain notebook telescope sandwich airplane bookshelf chocolate
dinosaur fireplace hamburger helicopter lighthouse microscope orchestra pineapple playground
raincoat skateboard strawberry sunflower thunderstorm waterfall watermelon xylophone butterfly
calendar crocodile dictionary scarecrow submarine wheelbarrow marshmallow lamppost parachute
toothbrush spaceship""".split()
WORD_POOLS = {1: EASY_WORDS, 2: MEDIUM_WORDS, 3: HARD_WORDS}

BADGES = {
    "first_round": ("First Steps", "Finish your first game round."),
    "baseline_done": ("Baseline Set", "Complete the baseline assessment."),
    "sharp_mind": ("Sharp Mind", "Recall a sequence of 8 or more characters perfectly."),
    "streak_3": ("Three in a Row", "Practise 3 days in a row."),
    "streak_7": ("Week Warrior", "Practise 7 days in a row."),
    "chain_breaker": ("Chain Breaker", "Beat the computer on Hard."),
    "long_memory": ("Long Memory", "Recall a word chain of 10 or more words."),
    "clean_sweep": ("Clean Sweep", "Finish every daily quest in one day."),
    "reviewer": ("Rememberer", "Complete 5 spaced-review attempts."),
    "level_5": ("Level 5", "Reach player level 5."),
}


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

@contextmanager
def conn():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
                age_group TEXT, goal TEXT, created TEXT,
                xp INTEGER DEFAULT 0, streak INTEGER DEFAULT 0,
                best_streak INTEGER DEFAULT 0, last_active TEXT);
            CREATE TABLE IF NOT EXISTS rounds(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                game TEXT, phase TEXT, level INTEGER, size INTEGER,
                correct INTEGER, score REAL, rt REAL, ts REAL, day TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS levels(
                user_id INTEGER, game TEXT, level INTEGER, recent TEXT,
                PRIMARY KEY(user_id, game));
            CREATE TABLE IF NOT EXISTS badges(
                user_id INTEGER, code TEXT, ts REAL, PRIMARY KEY(user_id, code));
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                title TEXT NOT NULL, difficulty TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS task_log(
                user_id INTEGER, task_id INTEGER, day TEXT,
                done INTEGER DEFAULT 0, awarded INTEGER DEFAULT 0,
                PRIMARY KEY(user_id, task_id, day));
            CREATE TABLE IF NOT EXISTS bonuses(
                user_id INTEGER, day TEXT, kind TEXT, PRIMARY KEY(user_id, day, kind));
            CREATE TABLE IF NOT EXISTS review_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                kind TEXT, text TEXT, half_life REAL DEFAULT 1.0,
                last_seen REAL, n_seen INTEGER DEFAULT 0, n_ok INTEGER DEFAULT 0,
                UNIQUE(user_id, kind, text));
            """
        )


def today():
    return date.today().isoformat()


# --------------------------------------------------------------------------
# Module 1: accounts
# --------------------------------------------------------------------------

def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()


def register_user(username, password, age_group, goal):
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        return False, "Username must be 3 to 20 letters, digits or underscores."
    if len(password or "") < 6:
        return False, "Password must be at least 6 characters."
    salt = secrets.token_hex(16)
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO users(username, pw_hash, salt, age_group, goal, created) VALUES(?,?,?,?,?,?)",
                (username, _hash(password, salt), salt, age_group, goal, today()),
            )
    except sqlite3.IntegrityError:
        return False, "That username is taken."
    return True, "Account created. You can log in now."


def check_login(username, password):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE username=?", ((username or "").strip(),)
        ).fetchone()
    if row and secrets.compare_digest(row["pw_hash"], _hash(password or "", row["salt"])):
        return {"id": row["id"], "username": row["username"]}
    return None


def get_user(uid):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


# --------------------------------------------------------------------------
# Module 6: gamification (XP, levels, streaks, badges)
# --------------------------------------------------------------------------

def xp_for_level(level):
    return 50 * (level - 1) ** 2


def level_from_xp(xp):
    return 1 + int(math.sqrt(max(xp, 0) / 50))


def touch_activity(uid):
    """Update the daily streak. Called whenever the user earns XP."""
    t = today()
    with conn() as c:
        u = c.execute("SELECT streak, best_streak, last_active FROM users WHERE id=?", (uid,)).fetchone()
        if u["last_active"] == t:
            return u["streak"]
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        streak = u["streak"] + 1 if u["last_active"] == yesterday else 1
        c.execute(
            "UPDATE users SET streak=?, best_streak=?, last_active=? WHERE id=?",
            (streak, max(u["best_streak"], streak), t, uid),
        )
    return streak


def add_xp(uid, amount):
    with conn() as c:
        c.execute("UPDATE users SET xp = xp + ? WHERE id=?", (int(amount), uid))


def check_badges(uid):
    """Award any badges whose conditions are now met. Returns the new badge codes."""
    with conn() as c:
        have = {r["code"] for r in c.execute("SELECT code FROM badges WHERE user_id=?", (uid,))}
        u = c.execute("SELECT xp, best_streak FROM users WHERE id=?", (uid,)).fetchone()

        def one(sql, *args):
            return c.execute(sql, (uid, *args)).fetchone()[0]

        conditions = {
            "first_round": one("SELECT COUNT(*) FROM rounds WHERE user_id=?") > 0,
            "baseline_done": one("SELECT COUNT(*) FROM rounds WHERE user_id=? AND phase='baseline'") >= len(BASELINE_LENGTHS),
            "sharp_mind": one("SELECT COUNT(*) FROM rounds WHERE user_id=? AND game='scramble' AND correct=1 AND size>=8") > 0,
            "streak_3": u["best_streak"] >= 3,
            "streak_7": u["best_streak"] >= 7,
            "chain_breaker": one("SELECT COUNT(*) FROM rounds WHERE user_id=? AND game='word_chain' AND correct=1 AND level>=3") > 0,
            "long_memory": one("SELECT COUNT(*) FROM rounds WHERE user_id=? AND game='word_chain' AND score>=10") > 0,
            "clean_sweep": one("SELECT COUNT(*) FROM bonuses WHERE user_id=? AND kind='all_tasks'") > 0,
            "reviewer": one("SELECT COALESCE(SUM(n_seen),0) FROM review_items WHERE user_id=?") >= 5,
            "level_5": level_from_xp(u["xp"]) >= 5,
        }
        new = [code for code, ok in conditions.items() if ok and code not in have]
        for code in new:
            c.execute("INSERT INTO badges(user_id, code, ts) VALUES(?,?,?)", (uid, code, time.time()))
    return new


def reward(uid, xp):
    """Give XP, update streak, check badges. Returns markdown describing what happened."""
    old_level = level_from_xp(get_user(uid)["xp"])
    touch_activity(uid)
    add_xp(uid, xp)
    new_level = level_from_xp(get_user(uid)["xp"])
    lines = [f"+{xp} XP"]
    if new_level > old_level:
        lines.append(f"Level up. You are now level {new_level}.")
    for code in check_badges(uid):
        lines.append(f"Badge unlocked: **{BADGES[code][0]}** ({BADGES[code][1]})")
    return "  \n".join(lines)


def stats_html(uid):
    u = get_user(uid)
    xp = u["xp"]
    lvl = level_from_xp(xp)
    lo, hi = xp_for_level(lvl), xp_for_level(lvl + 1)
    pct = int(100 * (xp - lo) / (hi - lo))
    with conn() as c:
        nb = c.execute("SELECT COUNT(*) FROM badges WHERE user_id=?", (uid,)).fetchone()[0]
    s = u["streak"]
    return (
        f'<div class="mq-stats"><div><b>{html.escape(u["username"])}</b> &nbsp;|&nbsp; Level {lvl}'
        f' &nbsp;|&nbsp; {xp} XP &nbsp;|&nbsp; Streak {s} day{"s" if s != 1 else ""}'
        f' &nbsp;|&nbsp; {nb} badge{"s" if nb != 1 else ""}</div>'
        f'<div class="mq-bar"><div style="width:{pct}%"></div></div>'
        f"<small>{hi - xp} XP to level {lvl + 1}</small></div>"
    )


# --------------------------------------------------------------------------
# Logging and module 4: adaptive difficulty
# --------------------------------------------------------------------------

def log_round(uid, game, phase, level, size, correct, score, rt=None, detail=""):
    with conn() as c:
        c.execute(
            "INSERT INTO rounds(user_id, game, phase, level, size, correct, score, rt, ts, day, detail)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uid, game, phase, level, size, int(correct), float(score), rt, time.time(), today(), detail),
        )


def get_level_state(uid, game):
    import json
    with conn() as c:
        r = c.execute("SELECT level, recent FROM levels WHERE user_id=? AND game=?", (uid, game)).fetchone()
    if r:
        return r["level"], json.loads(r["recent"])
    return 1, []


def set_level_state(uid, game, level, recent):
    import json
    with conn() as c:
        c.execute(
            "INSERT INTO levels(user_id, game, level, recent) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, game) DO UPDATE SET level=excluded.level, recent=excluded.recent",
            (uid, game, level, json.dumps(recent)),
        )


def adapt_scramble(uid, accuracy):
    """Keep rolling accuracy near 85%: raise the level above 95%, lower it below 75%."""
    level, recent = get_level_state(uid, "scramble")
    recent = (recent + [accuracy])[-ROLLING_ROUNDS:]
    mean = sum(recent) / len(recent)
    change = 0
    if len(recent) >= MIN_ROUNDS_BEFORE_CHANGE:
        if mean >= TARGET_ACCURACY + BAND:
            change = 1
        elif mean <= TARGET_ACCURACY - BAND:
            change = -1
    new_level = min(MAX_SCRAMBLE_LEVEL, max(1, level + change))
    if new_level != level:
        recent = []
    set_level_state(uid, "scramble", new_level, recent)
    return new_level, new_level - level, mean


def adapt_chain(uid, won):
    """Word chain: two wins in a row go up one level, two losses in a row go down one."""
    level, recent = get_level_state(uid, "word_chain")
    recent = (recent + [1 if won else 0])[-2:]
    new_level = level
    if recent == [1, 1]:
        new_level, recent = min(3, level + 1), []
    elif recent == [0, 0]:
        new_level, recent = max(1, level - 1), []
    set_level_state(uid, "word_chain", new_level, recent)
    return new_level


# --------------------------------------------------------------------------
# Module 5: spaced repetition (simplified half-life model)
# --------------------------------------------------------------------------

def recall_probability(half_life_days, last_seen, now=None):
    now = now or time.time()
    days = max(0.0, (now - last_seen) / 86400.0)
    return 2 ** (-days / half_life_days)


def add_review_item(uid, kind, text):
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO review_items(user_id, kind, text, half_life, last_seen) VALUES(?,?,?,?,?)",
            (uid, kind, text, 1.0, time.time()),
        )


def list_review_items(uid):
    with conn() as c:
        rows = c.execute("SELECT * FROM review_items WHERE user_id=?", (uid,)).fetchall()
    items = []
    for r in rows:
        p = recall_probability(r["half_life"], r["last_seen"])
        items.append({**dict(r), "p": p, "due": p < REVIEW_DUE_BELOW})
    return sorted(items, key=lambda i: i["p"])


def update_review_item(item_id, ok):
    with conn() as c:
        r = c.execute("SELECT half_life FROM review_items WHERE id=?", (item_id,)).fetchone()
        h = min(MAX_HALF_LIFE_DAYS, r["half_life"] * 2) if ok else max(0.25, r["half_life"] * 0.5)
        c.execute(
            "UPDATE review_items SET half_life=?, last_seen=?, n_seen=n_seen+1, n_ok=n_ok+? WHERE id=?",
            (h, time.time(), int(ok), item_id),
        )
    return h


# --------------------------------------------------------------------------
# Game helpers
# --------------------------------------------------------------------------

def make_sequence(n):
    return "".join(random.sample(SEQ_ALPHABET, n))


def scramble(seq):
    chars = list(seq)
    for _ in range(25):
        random.shuffle(chars)
        if "".join(chars) != seq:
            break
    return "".join(chars)


def show_time(n):
    return round(min(10.0, 1.5 + 0.55 * n), 1)


def clean_seq(text):
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def position_accuracy(answer, seq):
    return sum(a == b for a, b in zip(answer, seq)) / len(seq)


def tokens_of(text):
    return re.findall(r"[a-z']+", (text or "").lower())


def computer_turn(chain, level):
    """The computer tries to recite the chain, then adds a word. Returns (ok, new_word, forgotten_word)."""
    f = CHAIN_FORGET[level]
    for w in chain:
        if random.random() < f:
            return False, None, w
    pool = [w for w in WORD_POOLS[level] if w not in chain]
    new = random.choice(pool)
    chain.append(new)
    return True, new, None


def big_html(text, caption, scrambled=False):
    spaced = " ".join(html.escape(text))
    cls = "mq-seq mq-scr" if scrambled else "mq-seq"
    return f'<div class="{cls}">{spaced}</div><div class="mq-cap">{html.escape(caption)}</div>'


PLACEHOLDER = '<div class="mq-seq mq-idle">. . . .</div><div class="mq-cap">Press start when you are ready.</div>'

# --------------------------------------------------------------------------
# Look and feel: soft pastel theme + text-size control
# --------------------------------------------------------------------------

# Palette: misty sage, lavender, warm cream and a touch of peach. Easy on the eyes.
PAL = {
    "cream": "#fbf8f3", "paper": "#fffdfa", "mist": "#eef6f3", "sage": "#7fb8ac",
    "teal": "#3f8077", "teal_dark": "#2f6a62", "lavender": "#a79be0", "lav_deep": "#6a5fb3",
    "lav_soft": "#ece8f8", "peach": "#f2b9a1", "ink": "#33474a", "muted": "#6b7f82", "line": "#e4ded3",
}

_SAGE = gr.themes.Color(c50="#f0f8f6", c100="#dff0ec", c200="#c3e1da", c300="#a1cdc3", c400="#7fb8ac",
                        c500="#5fa093", c600="#3f8077", c700="#336a62", c800="#2b5650", c900="#244541", c950="#162b28")
_LAV = gr.themes.Color(c50="#f6f4fd", c100="#ece8f8", c200="#ddd7f3", c300="#c6bdea", c400="#a79be0",
                       c500="#8b7fd0", c600="#6a5fb3", c700="#574d96", c800="#483f7a", c900="#3b3463", c950="#221e3d")
_WARM = gr.themes.Color(c50="#fbf8f3", c100="#f5f0e8", c200="#e9e2d6", c300="#d8d0c1", c400="#b3aa9a",
                        c500="#8d877b", c600="#6b7f82", c700="#52666a", c800="#3f5255", c900="#33474a", c950="#243335")

_LIGHT = dict(
    body_background_fill=PAL["cream"], body_text_color=PAL["ink"], body_text_color_subdued=PAL["muted"],
    background_fill_primary=PAL["paper"], background_fill_secondary="#f6f3ec",
    border_color_primary=PAL["line"], border_color_accent=PAL["sage"],
    color_accent="#7fb8ac", color_accent_soft=PAL["lav_soft"],
    block_background_fill="rgba(255,253,250,.82)", block_border_color=PAL["line"], block_border_width="1px",
    block_shadow="0 10px 30px -18px rgba(63,128,119,.35)", block_radius="18px",
    block_label_background_fill=PAL["mist"], block_label_text_color=PAL["teal_dark"],
    block_title_text_color=PAL["teal_dark"], block_title_text_weight="600",
    panel_background_fill="transparent", panel_border_color=PAL["line"],
    input_background_fill="#ffffff", input_border_color="#d9d2c4", input_border_color_focus=PAL["lavender"],
    input_shadow="inset 0 1px 2px rgba(60,70,80,.05)", input_shadow_focus="0 0 0 4px rgba(167,155,224,.25)",
    input_radius="12px", input_placeholder_color="#a2aeae",
    button_primary_background_fill="linear-gradient(135deg,#7fb8ac 0%,#5fa093 100%)",
    button_primary_background_fill_hover="linear-gradient(135deg,#8cc4b8 0%,#4f9084 100%)",
    button_primary_text_color="#ffffff", button_primary_border_color="transparent",
    button_primary_shadow="0 6px 16px -6px rgba(63,128,119,.55)",
    button_secondary_background_fill=PAL["lav_soft"], button_secondary_background_fill_hover="#e1dbf6",
    button_secondary_text_color=PAL["lav_deep"], button_secondary_border_color="#d6cef2",
    button_large_radius="14px", button_medium_radius="14px", button_small_radius="12px",
    slider_color=PAL["lavender"],
    checkbox_background_color="#ffffff", checkbox_background_color_selected=PAL["sage"],
    checkbox_border_color="#cfc8ba", checkbox_border_color_selected=PAL["sage"],
    checkbox_label_background_fill="#ffffff", checkbox_label_background_fill_hover=PAL["mist"],
    checkbox_label_background_fill_selected=PAL["mist"], checkbox_label_text_color_selected=PAL["teal_dark"],
    table_even_background_fill="#fffdfa", table_odd_background_fill="#f6f9f7",
    table_border_color=PAL["line"], table_row_focus=PAL["lav_soft"],
    link_text_color=PAL["lav_deep"], link_text_color_hover=PAL["teal"],
    code_background_fill=PAL["mist"],
)
# The app is designed as a light theme only, so dark-mode variables use the same values.
_ALLOWED = set(inspect.signature(gr.themes.Base.set).parameters)  # tolerate small differences between 5.x releases
_THEME_VARS = {k: v for k, v in {**_LIGHT, **{k + "_dark": v for k, v in _LIGHT.items()}}.items() if k in _ALLOWED}

THEME = gr.themes.Soft(
    primary_hue=_SAGE, secondary_hue=_LAV, neutral_hue=_WARM,
    radius_size=gr.themes.sizes.radius_lg,
    font=[gr.themes.GoogleFont("Nunito"), "ui-rounded", "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "Consolas", "monospace"],
    # Fixed px sizes so the text-size slider can scale all of them with one multiplier.
    text_size=gr.themes.Size(xxs="10px", xs="11px", sm="13px", md="15px", lg="17px", xl="22px", xxl="28px"),
).set(**_THEME_VARS)

# Text-size slider (80%-160%). The JS sets --mq-fs on <html>; the CSS below multiplies every
# text size by it. The choice is remembered in the browser (localStorage) when allowed.
FS_MIN, FS_MAX, FS_STEP, FS_DEFAULT = 80, 160, 5, 100
FS_APPLY_JS = """(v) => {
  const n = Math.min(%d, Math.max(%d, Number(v) || %d));
  document.documentElement.style.setProperty('--mq-fs', String(n / 100));
  try { localStorage.setItem('mq_font_pct', String(n)); } catch (e) {}
}""" % (FS_MAX, FS_MIN, FS_DEFAULT)
FS_RESTORE_JS = """() => {
  let n = %d;
  try { const s = parseInt(localStorage.getItem('mq_font_pct'), 10); if (s) n = Math.min(%d, Math.max(%d, s)); } catch (e) {}
  document.documentElement.style.setProperty('--mq-fs', String(n / 100));
  document.body.classList.remove('dark');
  return n;
}""" % (FS_DEFAULT, FS_MAX, FS_MIN)

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&display=swap');
:root{--mq-fs:1;color-scheme:light}
.gradio-container{
  --text-xxs:calc(10px*var(--mq-fs));--text-xs:calc(11px*var(--mq-fs));--text-sm:calc(13px*var(--mq-fs));
  --text-md:calc(15px*var(--mq-fs));--text-lg:calc(17px*var(--mq-fs));--text-xl:calc(22px*var(--mq-fs));
  --text-xxl:calc(28px*var(--mq-fs));
  max-width:1080px !important;margin:0 auto;
}
body{
  background:
    radial-gradient(900px 520px at 6% -8%,#e4f2ee 0%,transparent 62%),
    radial-gradient(820px 520px at 104% 2%,#ece7f9 0%,transparent 58%),
    radial-gradient(760px 520px at 48% 112%,#fcebe2 0%,transparent 62%),
    #fbf8f3 !important;
  background-attachment:fixed !important;
}
.gradio-container{background:transparent !important}
.gradio-container .prose{font-size:var(--text-md);line-height:1.65;color:var(--body-text-color)}
.gradio-container .prose h1{font-family:'Fraunces',Georgia,serif;font-weight:600;font-size:calc(2.1rem*var(--mq-fs));
  letter-spacing:-.01em;margin:0 0 .15em;background:linear-gradient(100deg,#2f6a62 10%,#6a5fb3 90%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.gradio-container .prose h3{font-family:'Fraunces',Georgia,serif;font-weight:600;color:var(--block-title-text-color)}
.gradio-container .prose a{text-decoration-color:#c6bdea;text-underline-offset:3px}

/* header strip with the text-size control */
#mq-head{align-items:center;gap:1rem;margin-bottom:.4rem}
#mq-size{min-width:300px;border-radius:18px;padding:.55rem .9rem;
  background:linear-gradient(135deg,rgba(236,232,248,.85),rgba(238,246,243,.85));border:1px solid #ddd7f3}
#mq-size input[type=range]{accent-color:#8b7fd0}
#mq-size label span{font-weight:600}

/* tabs as soft pills */
.gradio-container [role=tablist]{border:0 !important;gap:.4rem;padding:.35rem;border-radius:18px;
  background:rgba(255,253,250,.7);box-shadow:inset 0 0 0 1px var(--line,#e4ded3);margin-bottom:.8rem}
.gradio-container button[role=tab]{border:0 !important;border-radius:13px !important;padding:.5rem 1.05rem;
  font-weight:600;font-size:var(--text-md);color:#6b7f82;background:transparent;transition:all .25s ease}
.gradio-container button[role=tab]:hover{background:#eef6f3;color:#2f6a62}
.gradio-container button[role=tab].selected,.gradio-container button[role=tab][aria-selected=true]{
  background:linear-gradient(135deg,#dff0ec,#ece8f8);color:#2f6a62;box-shadow:0 4px 12px -6px rgba(106,95,179,.45)}
.gradio-container button[role=tab]::after{display:none !important}
.gradio-container [role=tabpanel]{animation:mq-rise .45s ease both}
@keyframes mq-rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

/* buttons */
.gradio-container button.primary,.gradio-container button.secondary{font-size:var(--text-lg);font-weight:600;letter-spacing:.01em;
  transition:transform .18s ease,box-shadow .18s ease,filter .18s ease}
.gradio-container button.primary:hover:not(:disabled),.gradio-container button.secondary:hover:not(:disabled){transform:translateY(-2px)}
.gradio-container button:disabled{opacity:.5}

/* make sure form controls follow the text-size multiplier */
.gradio-container textarea,.gradio-container input:not([type=range]):not([type=checkbox]):not([type=radio]),
.gradio-container label span,.gradio-container .wrap .item,.gradio-container li,.gradio-container table{font-size:var(--text-md)}
.gradio-container .info{font-size:var(--text-sm)}

/* tabs, tables and dataframes: explicit sizes (Gradio hardcodes some of these) */
.gradio-container button[role=tab]{font-size:var(--text-md) !important}
.gradio-container table,.gradio-container table *,.gradio-container .cell-wrap,.gradio-container .cell-wrap *,
.gradio-container [data-testid=dataframe] *{font-size:var(--text-md) !important}
.gradio-container button[role=tab]{padding:calc(.5rem*var(--mq-fs)) calc(1.05rem*var(--mq-fs))}
/* the size control itself stays a constant size so it is always easy to reach */
#mq-size,#mq-size *{--text-md:15px;--text-sm:12px;--text-lg:15px;font-size:14px !important}
#mq-size .info{font-size:12px !important}

/* stats card */
.mq-stats{padding:.85rem 1.15rem;border-radius:18px;
  background:linear-gradient(135deg,#ffffff 0%,#eef6f3 55%,#f1eefb 100%);border:1px solid #e0e7e2;
  box-shadow:0 10px 26px -18px rgba(63,128,119,.5);color:var(--body-text-color)}
.mq-stats b{color:#2f6a62}
.mq-bar{height:10px;border-radius:999px;background:#e6e1f2;margin:.55rem 0 .3rem;overflow:hidden}
.mq-bar>div{height:100%;border-radius:999px;background:linear-gradient(90deg,#7fc4b6,#a79be0);transition:width .6s ease}

/* the sequence shown in the games */
.mq-seq{font-family:'JetBrains Mono',ui-monospace,Consolas,monospace;font-size:calc(2.4rem*var(--mq-fs));font-weight:600;
  text-align:center;padding:1.5rem .75rem;border-radius:22px;word-break:break-all;color:#2f6a62;
  background:linear-gradient(135deg,#f3faf8 0%,#f5f2fc 60%,#fdf1ea 100%);
  border:2px dashed #cfc8ee;box-shadow:inset 0 2px 10px rgba(106,95,179,.06),0 12px 28px -20px rgba(106,95,179,.6)}
.mq-scr{letter-spacing:.1em;color:#6a5fb3}
.mq-idle{opacity:.5}
.mq-cap{text-align:center;color:#6b7f82;font-size:var(--text-md);margin-top:.55rem}

@media (max-width:640px){#mq-size{min-width:0;width:100%}}
@media (prefers-reduced-motion:reduce){*{animation:none !important;transition:none !important}}
"""

# Matplotlib palette matching the theme
CH = {"bg": "#fffdfa", "grid": "#ece7dc", "text": "#4b5f62", "teal": "#4f9a8f", "lav": "#8b7fd0", "peach": "#e9a58a"}


def style_ax(fig, *axes):
    fig.patch.set_facecolor(CH["bg"])
    for ax in axes:
        ax.set_facecolor(CH["bg"])
        for sp in ax.spines.values():
            sp.set_color(CH["grid"])
        ax.tick_params(colors=CH["text"])
        ax.xaxis.label.set_color(CH["text"])
        ax.yaxis.label.set_color(CH["text"])
        ax.title.set_color(CH["text"])
        ax.grid(axis="y", color=CH["grid"], lw=0.8)
        ax.set_axisbelow(True)


NOT_LOGGED = "Please log in first."


# --------------------------------------------------------------------------
# Handlers: auth
# --------------------------------------------------------------------------

def on_register(username, password, age_group, goal):
    return register_user(username, password, age_group, goal)[1]


def on_login(username, password, st):
    user = check_login(username, password)
    if not user:
        return st, gr.update(), gr.update(), "Wrong username or password.", gr.update()
    st = {"user": user}
    return st, gr.update(visible=False), gr.update(visible=True), "", stats_html(user["id"])


def on_logout(st):
    return {}, gr.update(visible=True), gr.update(visible=False), PLACEHOLDER


# --------------------------------------------------------------------------
# Handlers: Unscramble game (also used for baseline and re-test)
# --------------------------------------------------------------------------

def sc_start(mode, st):
    u = st.get("user")
    if not u:
        return PLACEHOLDER, gr.Timer(active=False), gr.update(), gr.update(), NOT_LOGGED, "", st
    uid = u["id"]
    sc = st.setdefault("sc", {})
    if mode == "Practice":
        level, _ = get_level_state(uid, "scramble")
        n, phase = 3 + level, "train"
        info = f"Practice, level {level} ({n} characters)"
    else:
        phase = "baseline" if mode == "Baseline test" else "retest"
        series = sc.get("series")
        if not series or series["phase"] != phase or series["idx"] >= len(BASELINE_LENGTHS):
            series = {"phase": phase, "idx": 0, "results": []}
        sc["series"] = series
        n = BASELINE_LENGTHS[series["idx"]]
        info = f"{mode}, round {series['idx'] + 1} of {len(BASELINE_LENGTHS)} ({n} characters)"
    seq, show = make_sequence(n), show_time(n)
    sc.update(seq=seq, n=n, phase=phase, active=True, awaiting=False)
    return (
        big_html(seq, f"Memorise the order. It disappears in {show} seconds."),
        gr.Timer(value=show, active=True),
        gr.update(value="", interactive=False),
        gr.update(interactive=False),
        "",
        info,
        st,
    )


def sc_tick(st):
    sc = st.get("sc", {})
    if not sc.get("active"):
        return gr.update(), gr.Timer(active=False), gr.update(), gr.update(), st
    sc.update(active=False, awaiting=True, shown=time.time(), scrambled=scramble(sc["seq"]))
    return (
        big_html(sc["scrambled"], "Scrambled. Type the characters in their ORIGINAL order.", scrambled=True),
        gr.Timer(active=False),
        gr.update(interactive=True),
        gr.update(interactive=True),
        st,
    )


def sc_submit(answer, st):
    u, sc = st.get("user"), st.get("sc", {})
    if not u or not sc.get("awaiting"):
        return gr.update(), "Start a round first.", gr.update(), gr.update(), gr.update(), st
    uid, seq = u["id"], sc["seq"]
    ans = clean_seq(answer)
    exact = ans == seq
    acc = position_accuracy(ans, seq)
    rt = time.time() - sc["shown"]
    sc["awaiting"] = False
    n, phase = sc["n"], sc["phase"]
    log_round(uid, "scramble", phase, n - 3, n, exact, acc, rt, detail=f"{seq}|{ans}")

    xp = round(10 * acc) + (5 if exact else 0)
    out = [("Correct." if exact else f"Not quite. You scored {acc:.0%} of positions."), f"Original: `{seq}`"]
    if not exact and phase == "train":
        add_review_item(uid, "seq", seq)
        out.append("This sequence was added to your Review queue.")

    if phase == "train":
        level, change, mean = adapt_scramble(uid, acc)
        out.append(f"Rolling accuracy: {mean:.0%} (target {TARGET_ACCURACY:.0%}).")
        if change > 0:
            out.append(f"Difficulty up: level {level}.")
        elif change < 0:
            out.append(f"Difficulty down: level {level}.")
    else:
        series = sc["series"]
        series["results"].append((n, exact, acc))
        series["idx"] += 1
        if series["idx"] >= len(BASELINE_LENGTHS):
            res = series["results"]
            avg = sum(r[2] for r in res) / len(res)
            perfect = [r[0] for r in res if r[1]]
            best = max(perfect, default=3)
            if phase == "baseline":
                start_level = min(MAX_SCRAMBLE_LEVEL, max(1, best - 3))
                set_level_state(uid, "scramble", start_level, [])
                longest = f"{best} characters" if perfect else "none"
                out.append(f"Baseline finished. Average accuracy {avg:.0%}, longest perfect recall: {longest}.")
                out.append(f"Your practice starts at level {start_level}.")
                xp += 20
            else:
                out.append(f"Re-test finished. Average accuracy {avg:.0%}.")
                with conn() as c:
                    base = c.execute(
                        "SELECT AVG(score) FROM (SELECT score FROM rounds WHERE user_id=? AND phase='baseline' "
                        "AND game='scramble' ORDER BY id ASC LIMIT ?)", (uid, len(BASELINE_LENGTHS))
                    ).fetchone()[0]
                if base is not None:
                    out.append(f"Baseline was {base:.0%}, so the change is {100 * (avg - base):+.0f} percentage points. "
                               "A short gap between tests mostly measures practice on this task, not memory in general.")
                xp += 20
        else:
            out.append(f"Round {series['idx']} of {len(BASELINE_LENGTHS)} done. Press Start for the next one.")

    out.append(reward(uid, xp))
    return (
        big_html(seq, "The original sequence"),
        "  \n".join(out),
        gr.update(value="", interactive=False),
        gr.update(interactive=False),
        stats_html(uid),
        st,
    )


def sc_mode_change(st):
    st.get("sc", {}).pop("series", None)
    return st


# --------------------------------------------------------------------------
# Handlers: Word Chain vs computer
# --------------------------------------------------------------------------

def wc_note(g, note):
    n = len(g["chain"])
    return (
        f"{note}\n\n**Chain length:** {n} &nbsp;|&nbsp; **Hints left:** {g['hints']} "
        f"&nbsp;|&nbsp; **Difficulty:** {CHAIN_NAMES[g['level']]}"
    )


def wc_finish(uid, g, won, headline):
    g["over"] = True
    lvl, chain = g["level"], g["chain"]
    log_round(uid, "word_chain", "train", lvl, len(chain), won, g["recalled"], detail=",".join(chain))
    for w in g["missed"][:3]:
        add_review_item(uid, "word", w)
    xp = 6 * g["recalled"] + 5 * lvl + (30 if won else 0)
    lines = [headline, f"Full chain: {', '.join(chain)}", f"You recalled the chain correctly {g['recalled']} time(s)."]
    if g["missed"]:
        lines.append("Words you missed were added to your Review queue.")
    if g["auto"]:
        new_level = adapt_chain(uid, won)
        if new_level != g.get("start_level", lvl):
            lines.append(f"Next game difficulty: {CHAIN_NAMES[new_level]}.")
    lines.append(reward(uid, xp))
    return "\n\n".join(lines)


def _wc_computer_step(uid, g):
    """Run the computer's turn. Returns (finished, markdown)."""
    if len(g["chain"]) >= MAX_CHAIN:
        return True, wc_finish(uid, g, True, f"The chain reached {MAX_CHAIN} words. You win.")
    ok, new, forgot = computer_turn(g["chain"], g["level"])
    if not ok:
        return True, wc_finish(uid, g, True, f"The computer forgot **{forgot}**. You win.")
    note = (f"The computer recalled the chain correctly and added **{new}**.\n\n"
            f"Now type all {len(g['chain'])} words in order from memory, then add one new word of your own.")
    return False, wc_note(g, note)


_WC_OFF = (gr.update(value="", interactive=False), gr.update(interactive=False), gr.update(interactive=False))
_WC_ON = (gr.update(value="", interactive=True), gr.update(interactive=True), gr.update(interactive=True))


def wc_start(diff, first, st):
    u = st.get("user")
    if not u:
        return NOT_LOGGED, *_WC_OFF, gr.update(), st
    uid = u["id"]
    toks = tokens_of(first)
    if len(toks) != 1 or not 2 <= len(toks[0]) <= 15:
        return "Enter exactly one word (2 to 15 letters) to open the chain.", *_WC_OFF, gr.update(), st
    auto = diff.startswith("Auto")
    level = get_level_state(uid, "word_chain")[0] if auto else {"Easy": 1, "Medium": 2, "Hard": 3}[diff]
    g = {"chain": [toks[0]], "level": level, "start_level": level, "auto": auto,
         "hints": CHAIN_HINTS[level], "over": False, "recalled": 0, "missed": []}
    st["wc"] = g
    finished, md = _wc_computer_step(uid, g)
    return (md, *(_WC_OFF if finished else _WC_ON), stats_html(uid), st)


def wc_submit(text, st):
    u, g = st.get("user"), st.get("wc")
    if not u or not g or g["over"]:
        return "Start a new game first.", *_WC_OFF, gr.update(), st
    uid, chain = u["id"], g["chain"]
    n, toks = len(chain), tokens_of(text)
    if toks[:n] == chain and len(toks) == n:
        return wc_note(g, "You recalled the chain but did not add a new word. Add one at the end."), gr.update(), gr.update(), gr.update(), gr.update(), st
    if toks[:n] == chain and len(toks) > n + 1:
        return wc_note(g, "Add only ONE new word after the chain."), gr.update(), gr.update(), gr.update(), gr.update(), st
    if toks[:n] == chain:
        new = toks[n]
        if new in chain or not 2 <= len(new) <= 15:
            return wc_note(g, "Your new word must be 2 to 15 letters and not already in the chain."), gr.update(), gr.update(), gr.update(), gr.update(), st
        chain.append(new)
        g["recalled"] += 1
        finished, md = _wc_computer_step(uid, g)
        return (md, *(_WC_OFF if finished else _WC_ON), stats_html(uid), st)
    g["missed"] = [w for i, w in enumerate(chain) if i >= len(toks) or toks[i] != w]
    md = wc_finish(uid, g, False, "That was not the chain. The computer wins this one.")
    return md, *_WC_OFF, stats_html(uid), st


def wc_hint(st):
    g = st.get("wc")
    if not g or g["over"]:
        return "Start a new game first.", gr.update(), st
    if g["hints"] <= 0:
        return wc_note(g, "No hints left."), gr.update(), st
    g["hints"] -= 1
    return wc_note(g, f"Hint, the chain so far: **{', '.join(g['chain'])}**"), gr.update(interactive=g["hints"] > 0), st


# --------------------------------------------------------------------------
# Handlers: Daily Quests (gamified checklist with a recall challenge)
# --------------------------------------------------------------------------

def _tasks_today(uid):
    with conn() as c:
        return c.execute(
            "SELECT t.id, t.title, t.difficulty, COALESCE(l.done,0) AS done, COALESCE(l.awarded,0) AS awarded "
            "FROM tasks t LEFT JOIN task_log l ON l.task_id=t.id AND l.day=? AND l.user_id=t.user_id "
            "WHERE t.user_id=? AND t.active=1 ORDER BY t.id", (today(), uid)).fetchall()


def tk_refresh(st):
    u = st.get("user")
    if not u:
        return gr.update(choices=[], value=[]), gr.update(choices=[], value=None), ""
    rows = _tasks_today(u["id"])
    choices = [(f"{r['title']}  [{r['difficulty']}, {TASK_XP[r['difficulty']]} XP]", str(r["id"])) for r in rows]
    done = [str(r["id"]) for r in rows if r["done"]]
    n_done = len(done)
    msg = f"{n_done} of {len(rows)} quests done today." if rows else "No quests yet. Add your first one above."
    return (gr.update(choices=choices, value=done),
            gr.update(choices=[(r["title"], str(r["id"])) for r in rows], value=None), msg)


def tk_add(title, diff, st):
    u = st.get("user")
    if not u:
        return NOT_LOGGED, gr.update(), gr.update(), gr.update(), st
    title = (title or "").strip()
    if not 2 <= len(title) <= 80:
        return "Quest title must be 2 to 80 characters.", gr.update(), gr.update(), gr.update(), st
    with conn() as c:
        c.execute("INSERT INTO tasks(user_id, title, difficulty) VALUES(?,?,?)", (u["id"], title, diff))
    cg, dd, msg = tk_refresh(st)
    return msg, cg, dd, "", st


def tk_delete(task_id, st):
    u = st.get("user")
    if not u or not task_id:
        return "Pick a quest to remove.", gr.update(), gr.update(), st
    with conn() as c:
        c.execute("UPDATE tasks SET active=0 WHERE id=? AND user_id=?", (int(task_id), u["id"]))
    cg, dd, msg = tk_refresh(st)
    return msg, cg, dd, st


def tk_save(selected, st):
    u = st.get("user")
    if not u:
        return NOT_LOGGED, gr.update(), gr.update(), st
    uid, selected = u["id"], set(selected or [])
    rows = _tasks_today(uid)
    xp, t = 0, today()
    with conn() as c:
        for r in rows:
            done = str(r["id"]) in selected
            award = done and not r["awarded"]
            c.execute(
                "INSERT INTO task_log(user_id, task_id, day, done, awarded) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id, task_id, day) DO UPDATE SET done=excluded.done, "
                "awarded=MAX(awarded, excluded.awarded)",
                (uid, r["id"], t, int(done), int(award or r["awarded"])))
            if award:
                xp += TASK_XP[r["difficulty"]]
        all_done = rows and all(str(r["id"]) in selected for r in rows)
        bonus = False
        if all_done:
            cur = c.execute("INSERT OR IGNORE INTO bonuses(user_id, day, kind) VALUES(?,?,'all_tasks')", (uid, t))
            bonus = cur.rowcount > 0
    if bonus:
        xp += ALL_TASKS_BONUS
    text = reward(uid, xp) if xp else "No new XP. Completed quests only pay out once per day."
    if bonus:
        text += f"  \nAll quests done: bonus {ALL_TASKS_BONUS} XP included."
    cg, _, _ = tk_refresh(st)
    return text, cg, stats_html(uid), st


def tk_recall_start(st):
    u = st.get("user")
    if not u:
        return NOT_LOGGED, gr.update(), gr.update(), gr.update(), st
    uid = u["id"]
    with conn() as c:
        used = c.execute("SELECT 1 FROM bonuses WHERE user_id=? AND day=? AND kind='task_recall'", (uid, today())).fetchone()
    if used:
        return "You already did today's recall challenge. Come back tomorrow.", gr.update(), gr.update(), gr.update(), st
    if not _tasks_today(uid):
        return "Add some quests first.", gr.update(), gr.update(), gr.update(), st
    return ("The list is hidden. Type the quests you remember, one per line.",
            gr.update(visible=False), gr.update(visible=True, value=""), gr.update(visible=True), st)


def tk_recall_submit(text, st):
    u = st.get("user")
    if not u:
        return NOT_LOGGED, gr.update(), gr.update(), gr.update(), gr.update(), st
    uid = u["id"]
    rows = _tasks_today(uid)
    typed = [l.strip().lower() for l in (text or "").splitlines() if l.strip()]
    hit, miss = [], []
    for r in rows:
        title = r["title"].lower()
        best = max((difflib.SequenceMatcher(None, title, t).ratio() for t in typed), default=0)
        (hit if best >= 0.75 else miss).append(r["title"])
    xp = 5 * len(hit) + (10 if rows and not miss else 0)
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO bonuses(user_id, day, kind) VALUES(?,?,'task_recall')", (uid, today()))
    msg = f"You remembered {len(hit)} of {len(rows)}."
    if miss:
        msg += "  \nMissed: " + ", ".join(miss)
    msg += "  \n" + reward(uid, xp)
    cg, _, _ = tk_refresh(st)
    return (msg, gr.update(visible=True, choices=cg["choices"], value=cg["value"]),
            gr.update(visible=False), gr.update(visible=False), stats_html(uid), st)


# --------------------------------------------------------------------------
# Handlers: spaced-repetition review
# --------------------------------------------------------------------------

def rv_table(st):
    u = st.get("user")
    cols = ["Item", "Type", "Seen", "Correct", "Half-life (days)", "Recall chance", "Due"]
    if not u:
        return pd.DataFrame(columns=cols)
    rows = [[i["text"], "sequence" if i["kind"] == "seq" else "word", i["n_seen"], i["n_ok"],
             round(i["half_life"], 2), f"{i['p']:.0%}", "yes" if i["due"] else "no"]
            for i in list_review_items(u["id"])]
    return pd.DataFrame(rows, columns=cols)


def rv_start(st):
    u = st.get("user")
    if not u:
        return PLACEHOLDER, gr.Timer(active=False), gr.update(), gr.update(), NOT_LOGGED, st
    items = list_review_items(u["id"])
    if not items:
        return PLACEHOLDER, gr.Timer(active=False), gr.update(), gr.update(), \
            "Nothing to review yet. Items you miss in the games land here.", st
    item = items[0]
    show = round(min(6.0, 1.5 + 0.4 * len(item["text"])), 1)
    st["rv"] = {"id": item["id"], "text": item["text"], "kind": item["kind"], "active": True, "awaiting": False}
    note = "This item is due." if item["due"] else "This item is not due yet, practising early is fine."
    return (big_html(item["text"], f"{note} It disappears in {show} seconds."),
            gr.Timer(value=show, active=True), gr.update(value="", interactive=False),
            gr.update(interactive=False), "", st)


def rv_tick(st):
    rv = st.get("rv", {})
    if not rv.get("active"):
        return gr.update(), gr.Timer(active=False), gr.update(), gr.update(), st
    rv.update(active=False, awaiting=True)
    return (big_html("? " * len(rv["text"]), "Type what you saw."), gr.Timer(active=False),
            gr.update(interactive=True), gr.update(interactive=True), st)


def rv_submit(answer, st):
    u, rv = st.get("user"), st.get("rv", {})
    if not u or not rv.get("awaiting"):
        return gr.update(), "Start a review first.", gr.update(), gr.update(), gr.update(), gr.update(), st
    uid, target = u["id"], rv["text"]
    given = clean_seq(answer) if rv["kind"] == "seq" else "".join(tokens_of(answer))
    ok = given == (target if rv["kind"] == "seq" else target.lower())
    rv["awaiting"] = False
    h = update_review_item(rv["id"], ok)
    msg = ("Correct. " if ok else f"Not quite, it was `{target}`. ") + f"Next review in about {h:.1f} day(s) of half-life.  \n"
    msg += reward(uid, 5 if ok else 1)
    return (big_html(target, "The item"), msg, gr.update(value="", interactive=False),
            gr.update(interactive=False), stats_html(uid), rv_table(st), st)


# --------------------------------------------------------------------------
# Module 7: dashboard
# --------------------------------------------------------------------------

def _empty_fig(msg):
    fig = Figure(figsize=(7, 3.2))
    ax = fig.subplots()
    ax.text(0.5, 0.5, msg, ha="center", va="center", color=CH["text"])
    ax.set_axis_off()
    fig.patch.set_facecolor(CH["bg"])
    return fig


def accuracy_fig(uid):
    with conn() as c:
        df = pd.read_sql_query(
            "SELECT level, score FROM rounds WHERE user_id=? AND game='scramble' AND phase='train' ORDER BY id",
            c, params=(uid,))
    if df.empty:
        return _empty_fig("Play some Unscramble practice rounds to see your accuracy here.")
    x = list(range(1, len(df) + 1))
    fig = Figure(figsize=(7, 3.4))
    ax = fig.subplots()
    ax.plot(x, df["score"], "o-", alpha=0.5, color=CH["lav"], label="Round accuracy")
    ax.plot(x, df["score"].rolling(ROLLING_ROUNDS, min_periods=1).mean(), lw=2.4, color=CH["teal"], label=f"{ROLLING_ROUNDS}-round average")
    ax.axhline(TARGET_ACCURACY, ls="--", color=CH["peach"], label="85% target")
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("Practice round")
    ax.set_ylabel("Position accuracy")
    ax2 = ax.twinx()
    ax2.step(x, df["level"], where="mid", color=CH["peach"], alpha=0.8, label="Level")
    ax2.set_ylabel("Level")
    ax2.set_ylim(0, MAX_SCRAMBLE_LEVEL + 1)
    style_ax(fig, ax, ax2)
    ax2.grid(False)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="lower right", fontsize=8, frameon=True, facecolor=CH["bg"], edgecolor=CH["grid"])
    fig.tight_layout()
    return fig


def activity_fig(uid):
    with conn() as c:
        df = pd.read_sql_query("SELECT day, COUNT(*) AS n FROM rounds WHERE user_id=? GROUP BY day", c, params=(uid,))
    if df.empty:
        return _empty_fig("No games played yet.")
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    counts = dict(zip(df["day"], df["n"]))
    fig = Figure(figsize=(7, 2.8))
    ax = fig.subplots()
    ax.bar([d[5:] for d in days], [counts.get(d, 0) for d in days], color=CH["teal"], alpha=0.85)
    style_ax(fig, ax)
    ax.set_ylabel("Rounds played")
    ax.set_title("Last 14 days", fontsize=10)
    ax.tick_params(axis="x", labelrotation=60, labelsize=8)
    fig.tight_layout()
    return fig


def progress_table(uid):
    def avg(sql, *args):
        with conn() as c:
            rows = c.execute(sql, (uid, *args)).fetchall()
        if not rows:
            return "-", "-"
        scores = [r["score"] for r in rows]
        return f"{sum(scores) / len(scores):.0%}", f"{sum(r['correct'] for r in rows)} of {len(rows)}"

    base = avg("SELECT score, correct FROM rounds WHERE user_id=? AND phase='baseline' AND game='scramble' ORDER BY id ASC LIMIT 5")
    retest = avg("SELECT score, correct FROM rounds WHERE user_id=? AND phase='retest' AND game='scramble' ORDER BY id DESC LIMIT 5")
    recent = avg("SELECT score, correct FROM rounds WHERE user_id=? AND phase='train' AND game='scramble' ORDER BY id DESC LIMIT 10")
    with conn() as c:
        g = c.execute("SELECT COUNT(*) n, COALESCE(SUM(correct),0) w, COALESCE(MAX(size),0) b FROM rounds "
                      "WHERE user_id=? AND game='word_chain'", (uid,)).fetchone()
    return pd.DataFrame(
        [["Baseline (first 5 rounds)", base[0], base[1]],
         ["Latest re-test (5 rounds)", retest[0], retest[1]],
         ["Recent practice (last 10 rounds)", recent[0], recent[1]],
         ["Word Chain: wins / games", f"{g['w']} / {g['n']}", f"longest chain {g['b']}"]],
        columns=["Measure", "Accuracy / result", "Perfect rounds / detail"])


def dash_refresh(st):
    u = st.get("user")
    if not u:
        return "", _empty_fig("Log in to see your progress."), _empty_fig(""), pd.DataFrame(), rv_table(st), ""
    uid = u["id"]
    row = get_user(uid)
    with conn() as c:
        earned = {r["code"] for r in c.execute("SELECT code FROM badges WHERE user_id=?", (uid,))}
    badge_lines = [f"- **{BADGES[k][0]}**: {BADGES[k][1]}" if k in earned else f"- ~~{BADGES[k][0]}~~: {BADGES[k][1]}"
                   for k in BADGES]
    md = (f"**Goal:** {row['goal']} &nbsp;|&nbsp; **Age group:** {row['age_group']} &nbsp;|&nbsp; "
          f"**Best streak:** {row['best_streak']} day(s)\n\n### Badges\n" + "\n".join(badge_lines))
    return md, accuracy_fig(uid), activity_fig(uid), progress_table(uid), rv_table(st), stats_html(uid)


def export_csv(st):
    u = st.get("user")
    if not u:
        return None
    with conn() as c:
        df = pd.read_sql_query(
            "SELECT game, phase, level, size, correct, score, rt, day, detail FROM rounds WHERE user_id=? ORDER BY id",
            c, params=(u["id"],))
    path = os.path.join(tempfile.gettempdir(), f"memory_quest_{u['username']}.csv")
    df.to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def build_app():
    with gr.Blocks(title="Memory Quest", theme=THEME, css=CSS) as demo:
        st = gr.State({})
        with gr.Row(elem_id="mq-head"):
            gr.Markdown("# Memory Quest\nShort memory games with levels, XP and daily streaks.")
            font_slider = gr.Slider(FS_MIN, FS_MAX, value=FS_DEFAULT, step=FS_STEP, scale=0, min_width=300,
                                    label="Text size (%)", info="Drag to shrink or enlarge all text",
                                    elem_id="mq-size")

        # ---- login / registration ----
        with gr.Column(visible=True) as auth_col:
            with gr.Row():
                with gr.Column():
                    gr.Markdown("### Log in")
                    log_user = gr.Textbox(label="Username")
                    log_pass = gr.Textbox(label="Password", type="password")
                    log_btn = gr.Button("Log in", variant="primary")
                    log_msg = gr.Markdown()
                with gr.Column():
                    gr.Markdown("### Create an account")
                    reg_user = gr.Textbox(label="Username")
                    reg_pass = gr.Textbox(label="Password (6+ characters)", type="password")
                    reg_age = gr.Dropdown(AGE_GROUPS, value="18-25", label="Age group")
                    reg_goal = gr.Dropdown(GOALS, value=GOALS[0], label="Main goal")
                    reg_btn = gr.Button("Register")
                    reg_msg = gr.Markdown()
            gr.Markdown("*Practice tool only. It does not diagnose or treat memory problems.*")

        # ---- main app ----
        with gr.Column(visible=False) as app_col:
            with gr.Row():
                stats_box = gr.HTML()
                logout_btn = gr.Button("Log out", scale=0)

            with gr.Tabs():
                # Dashboard
                with gr.Tab("Dashboard") as tab_dash:
                    dash_md = gr.Markdown()
                    acc_plot = gr.Plot(label="Unscramble accuracy and level")
                    act_plot = gr.Plot(label="Activity")
                    prog_df = gr.Dataframe(label="Progress", interactive=False)
                    with gr.Row():
                        dash_btn = gr.Button("Refresh")
                        csv_btn = gr.Button("Export my data (CSV)")
                    csv_file = gr.File(label="Your data", interactive=False)

                # Unscramble
                with gr.Tab("Unscramble"):
                    gr.Markdown(
                        "A sequence of letters and digits appears, then vanishes. You then see the same "
                        "characters shuffled. Type them back in the **original order**. Practice adapts "
                        "to keep you near 85% accuracy. Do the Baseline test once, and the Re-test after a few days.")
                    sc_mode = gr.Radio(["Practice", "Baseline test", "Re-test"], value="Practice", label="Mode")
                    sc_info = gr.Markdown()
                    sc_display = gr.HTML(PLACEHOLDER)
                    sc_timer = gr.Timer(1.0, active=False)
                    sc_answer = gr.Textbox(label="Original order", interactive=False, max_lines=1)
                    with gr.Row():
                        sc_start_btn = gr.Button("Start round", variant="primary")
                        sc_submit_btn = gr.Button("Submit", interactive=False)
                    sc_fb = gr.Markdown()

                # Word chain
                with gr.Tab("Word Chain"):
                    gr.Markdown(
                        "You say a word. The computer recalls it and adds one. Then YOU must recall the whole "
                        "chain in order and add a new word, and so on. The first one to slip loses. "
                        "The chain stays hidden, so remember it.")
                    with gr.Row():
                        wc_diff = gr.Radio(["Auto (adaptive)", "Easy", "Medium", "Hard"], value="Auto (adaptive)", label="Difficulty")
                        wc_first = gr.Textbox(label="Your opening word", max_lines=1)
                    wc_start_btn = gr.Button("Start game", variant="primary")
                    wc_board = gr.Markdown()
                    wc_input = gr.Textbox(label="Whole chain from memory, then one new word (spaces or commas)", interactive=False, max_lines=2)
                    with gr.Row():
                        wc_submit_btn = gr.Button("Submit turn", interactive=False)
                        wc_hint_btn = gr.Button("Use a hint", interactive=False)

                # Daily quests
                with gr.Tab("Daily Quests"):
                    gr.Markdown("Turn your daily tasks into quests. Finished quests pay XP once per day, "
                                "and finishing all of them pays a bonus. The recall challenge hides the list "
                                "and asks you to remember it.")
                    with gr.Row():
                        tk_title = gr.Textbox(label="New quest", max_lines=1, scale=3)
                        tk_diff = gr.Dropdown(list(TASK_XP), value="Easy", label="Difficulty", scale=1)
                        tk_add_btn = gr.Button("Add quest", scale=1)
                    tk_list = gr.CheckboxGroup(label="Today's quests", choices=[])
                    tk_save_btn = gr.Button("Save progress", variant="primary")
                    with gr.Row():
                        tk_del = gr.Dropdown(label="Remove a quest", choices=[])
                        tk_del_btn = gr.Button("Remove", scale=0)
                    tk_recall_btn = gr.Button("Recall challenge")
                    tk_recall_box = gr.Textbox(label="Quests you remember (one per line)", lines=5, visible=False)
                    tk_recall_submit_btn = gr.Button("Check my memory", visible=False)
                    tk_msg = gr.Markdown()

                # Review
                with gr.Tab("Review") as tab_rv:
                    gr.Markdown(
                        "Sequences and words you missed return here. Each correct recall doubles the item's "
                        "half-life, so it comes back later. A miss halves it. Items are due when the "
                        "predicted recall chance drops below 70%.")
                    rv_df = gr.Dataframe(interactive=False)
                    rv_display = gr.HTML(PLACEHOLDER)
                    rv_timer = gr.Timer(1.0, active=False)
                    rv_answer = gr.Textbox(label="What did you see?", interactive=False, max_lines=1)
                    with gr.Row():
                        rv_start_btn = gr.Button("Review the next item", variant="primary")
                        rv_submit_btn = gr.Button("Submit", interactive=False)
                    rv_msg = gr.Markdown()

        # ---------------- wiring ----------------
        # Text size runs fully in the browser (no server round trip) and is remembered between visits.
        font_slider.change(None, font_slider, None, js=FS_APPLY_JS)
        demo.load(None, None, font_slider, js=FS_RESTORE_JS)

        reg_btn.click(on_register, [reg_user, reg_pass, reg_age, reg_goal], reg_msg)
        dash_outs = [dash_md, acc_plot, act_plot, prog_df, rv_df, stats_box]
        tk_outs_refresh = [tk_list, tk_del, tk_msg]
        for trig in (log_btn.click, log_pass.submit):
            trig(on_login, [log_user, log_pass, st], [st, auth_col, app_col, log_msg, stats_box]) \
                .then(dash_refresh, st, dash_outs).then(tk_refresh, st, tk_outs_refresh)
        logout_btn.click(on_logout, st, [st, auth_col, app_col, sc_display])

        tab_dash.select(dash_refresh, st, dash_outs)
        dash_btn.click(dash_refresh, st, dash_outs)
        csv_btn.click(export_csv, st, csv_file)

        sc_mode.change(sc_mode_change, st, st)
        sc_start_btn.click(sc_start, [sc_mode, st], [sc_display, sc_timer, sc_answer, sc_submit_btn, sc_fb, sc_info, st])
        sc_timer.tick(sc_tick, st, [sc_display, sc_timer, sc_answer, sc_submit_btn, st])
        sc_out = [sc_display, sc_fb, sc_answer, sc_submit_btn, stats_box, st]
        sc_submit_btn.click(sc_submit, [sc_answer, st], sc_out)
        sc_answer.submit(sc_submit, [sc_answer, st], sc_out)

        wc_out = [wc_board, wc_input, wc_submit_btn, wc_hint_btn, stats_box, st]
        wc_start_btn.click(wc_start, [wc_diff, wc_first, st], wc_out) \
            .then(None, None, wc_first, js="() => ''")
        wc_submit_btn.click(wc_submit, [wc_input, st], wc_out) \
            .then(None, None, wc_input, js="() => ''")
        wc_input.submit(wc_submit, [wc_input, st], wc_out) \
            .then(None, None, wc_input, js="() => ''")
        wc_hint_btn.click(wc_hint, st, [wc_board, wc_hint_btn, st])

        tk_add_btn.click(tk_add, [tk_title, tk_diff, st], [tk_msg, tk_list, tk_del, tk_title, st])
        tk_del_btn.click(tk_delete, [tk_del, st], [tk_msg, tk_list, tk_del, st])
        tk_save_btn.click(tk_save, [tk_list, st], [tk_msg, tk_list, stats_box, st])
        tk_recall_btn.click(tk_recall_start, st, [tk_msg, tk_list, tk_recall_box, tk_recall_submit_btn, st])
        tk_recall_submit_btn.click(tk_recall_submit, [tk_recall_box, st],
                                   [tk_msg, tk_list, tk_recall_box, tk_recall_submit_btn, stats_box, st])

        tab_rv.select(lambda s: rv_table(s), st, rv_df)
        rv_start_btn.click(rv_start, st, [rv_display, rv_timer, rv_answer, rv_submit_btn, rv_msg, st])
        rv_timer.tick(rv_tick, st, [rv_display, rv_timer, rv_answer, rv_submit_btn, st])
        rv_out = [rv_display, rv_msg, rv_answer, rv_submit_btn, stats_box, rv_df, st]
        rv_submit_btn.click(rv_submit, [rv_answer, st], rv_out)
        rv_answer.submit(rv_submit, [rv_answer, st], rv_out)
    return demo


def main():
    init_db()
    demo = build_app()
    if IN_COLAB:
        demo.launch(debug=False)
    else:
        demo.launch(inbrowser=True)


if __name__ == "__main__":
    main()
