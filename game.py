"""Habitor game rules.

Everything in this module is a *pure* function or constant: no database, no
network, no clock reads. main.py feeds in data and persists the results, and
tests.py exercises these rules directly.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

BASE_POINTS = 10
STREAK_BONUS_CAP = 20
VERIFIED_BONUS = 5
REJECT_PENALTY = 15
CLAPBACK_BONUS = 20
TAUNTER_PENALTY = 5
PROVE_IT_MULTIPLIER = 2
PROVE_IT_WINDOW_SECONDS = 120

DIFFICULTY: dict[str, float] = {"easy": 1.0, "normal": 1.25, "hard": 1.5}
DIFFICULTY_LABEL: dict[str, str] = {"easy": "Easy · 1×", "normal": "Normal · 1.25×", "hard": "Hard · 1.5×"}


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def checkin_points(streak_days: int, verified: bool, difficulty: str, prove_it: bool = False) -> int:
    """points = (10 + min(streak, 20) + 5 if verified) x difficulty  (x2 for Prove It Now)."""
    raw = BASE_POINTS + min(max(streak_days, 0), STREAK_BONUS_CAP) + (VERIFIED_BONUS if verified else 0)
    value = raw * DIFFICULTY.get(difficulty, 1.0)
    if prove_it:
        value *= PROVE_IT_MULTIPLIER
    return _round_half_up(value)


def late_verified_bonus(streak_days: int, difficulty: str, prove_it: bool = False) -> int:
    """Extra points owed when a pending check-in is later verified (votes or AI)."""
    return checkin_points(streak_days, True, difficulty, prove_it) - checkin_points(streak_days, False, difficulty, prove_it)


def coins_for_points(points: int) -> int:
    """Coins drip alongside XP: roughly one coin per three points, at least one."""
    return max(1, points // 3) if points > 0 else 0


# ---------------------------------------------------------------------------
# XP and levels
# ---------------------------------------------------------------------------

def level_for_xp(xp: int) -> int:
    """level = floor(sqrt(XP / 50)) + 1, computed with integer maths (no float drift)."""
    return math.isqrt(max(xp, 0) // 50) + 1


def xp_for_level(level: int) -> int:
    """Minimum XP needed to reach ``level``."""
    return 50 * (max(level, 1) - 1) ** 2


@dataclass(frozen=True)
class LevelProgress:
    level: int
    xp: int
    floor: int
    ceiling: int

    @property
    def into(self) -> int:
        return self.xp - self.floor

    @property
    def span(self) -> int:
        return self.ceiling - self.floor

    @property
    def pct(self) -> int:
        return int(100 * self.into / self.span) if self.span else 100


def level_progress(xp: int) -> LevelProgress:
    lvl = level_for_xp(xp)
    return LevelProgress(level=lvl, xp=max(xp, 0), floor=xp_for_level(lvl), ceiling=xp_for_level(lvl + 1))


def segments(pct: float, count: int = 10) -> list[bool]:
    """Turn a percentage into lit/unlit blocks for the segmented bars."""
    lit = max(0, min(count, int(round(pct / 100 * count))))
    return [i < lit for i in range(count)]


# ---------------------------------------------------------------------------
# Schedules and streaks
# ---------------------------------------------------------------------------

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_PLURAL = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]
FREQUENCIES = {"daily": "Every day", "weekly": "X times per week", "days": "Specific days"}


@dataclass(frozen=True)
class Schedule:
    frequency: str = "daily"  # daily | weekly | days
    times_per_week: int = 1
    days: frozenset[int] = field(default_factory=frozenset)  # 0 = Monday

    @classmethod
    def from_fields(cls, frequency: str, times_per_week: int, days_csv: str) -> "Schedule":
        days = frozenset(int(d) for d in days_csv.split(",") if d.strip().isdigit() and 0 <= int(d) <= 6)
        return cls(frequency=frequency, times_per_week=max(1, min(7, times_per_week)), days=days)

    def describe(self) -> str:
        if self.frequency == "weekly":
            return f"{self.times_per_week}× per week"
        if self.frequency == "days":
            return ", ".join(WEEKDAY_SHORT[d] for d in sorted(self.days)) or "No days"
        return "Daily"


def week_start(d: date) -> date:
    """Monday of the week containing ``d``."""
    return d - timedelta(days=d.weekday())


def is_scheduled_day(s: Schedule, d: date) -> bool:
    """Whether a habit is due on ``d``. Weekly-quota habits may be done any day."""
    if s.frequency == "days":
        return d.weekday() in s.days
    return True


def daterange(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def scheduled_count(s: Schedule, start: date, end: date) -> int:
    """How many completions a schedule expects between two dates (inclusive)."""
    if end < start:
        return 0
    if s.frequency == "daily":
        return (end - start).days + 1
    if s.frequency == "days":
        return sum(1 for d in daterange(start, end) if d.weekday() in s.days)
    total = 0
    ws = week_start(start)
    while ws <= end:
        overlap_start, overlap_end = max(ws, start), min(ws + timedelta(days=6), end)
        overlap = (overlap_end - overlap_start).days + 1
        total += s.times_per_week if overlap == 7 else min(s.times_per_week, overlap)
        ws += timedelta(days=7)
    return total


def compute_streak(s: Schedule, done: set[date], protected: set[date], today: date, start: date | None = None) -> int:
    """Current streak in completions.

    * ``done`` - days with a non-rejected check-in.
    * ``protected`` - days covered by a Streak Shield or Chill Mode: they neither
      break nor extend the streak.
    * Today is still "open": not having checked in yet today never breaks a streak.
    """
    limit = start or (today - timedelta(days=3650))
    if s.frequency == "weekly":
        return _weekly_streak(s, done, protected, today, limit)
    count = 0
    d = today if today in done else today - timedelta(days=1)
    while d >= limit:
        if d in done:
            count += 1
        elif is_scheduled_day(s, d) and d not in protected:
            break
        d -= timedelta(days=1)
    return count


def _weekly_streak(s: Schedule, done: set[date], protected: set[date], today: date, limit: date) -> int:
    ws = week_start(today)
    current = sum(1 for d in done if ws <= d <= today)
    count = current
    ws -= timedelta(days=7)
    while ws + timedelta(days=6) >= limit:
        week_days = [ws + timedelta(days=i) for i in range(7)]
        n = sum(1 for d in week_days if d in done)
        if n >= s.times_per_week or any(d in protected for d in week_days):
            count += n
        else:
            break
        ws -= timedelta(days=7)
    return count


def longest_daily_run(days: set[date], start: date, end: date) -> int:
    """Longest run of consecutive days inside [start, end] (used by Streak Duels)."""
    best = run = 0
    for d in daterange(start, end):
        run = run + 1 if d in days else 0
        best = max(best, run)
    return best


def consistency_pct(completed: int, scheduled: int) -> int:
    if scheduled <= 0:
        return 100 if completed > 0 else 0
    return min(100, _round_half_up(100 * completed / scheduled))


SHIELD_EVERY = 7
MAX_SHIELDS = 3


def shield_earned(streak: int) -> bool:
    return streak > 0 and streak % SHIELD_EVERY == 0


# ---------------------------------------------------------------------------
# Voting (anti-cheat)
# ---------------------------------------------------------------------------

SUS_FLAG_COUNT = 3
LEGIT_VERIFY_COUNT = 2
VOTE_WINDOW_HOURS = 12
REPROVE_WINDOW_HOURS = 12
CAUGHT_CAPPIN_DAYS = 7


def vote_outcome(legit: int, sus: int) -> str:
    """'flagged' on >=3 Sus or a Sus majority (>50%), 'verified' on >=2 Legit, else 'pending'."""
    total = legit + sus
    if sus >= SUS_FLAG_COUNT or (total > 0 and sus * 2 > total):
        return "flagged"
    if legit >= LEGIT_VERIFY_COUNT:
        return "verified"
    return "pending"


# ---------------------------------------------------------------------------
# Taunts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Taunt:
    key: str
    sticker: str
    text: str
    sound: str


TAUNTS: dict[str, Taunt] = {t.key: t for t in [
    Taunt("snail", "🐌", "The gym misses you…", "slide"),
    Taunt("couch", "🛋️", "Couch Champion detected", "thud"),
    Taunt("crying", "🔥", "Your streak called. It's crying.", "wail"),
    Taunt("chicken", "🐔", "Bawk bawk!", "bawk"),
    Taunt("loading", "💤", "Still loading…?", "blip"),
    Taunt("ghost", "👻", "Seen your streak? Me neither.", "slide"),
    Taunt("clock", "⏰", "Tick tock, champ.", "blip"),
]}
MAX_TAUNTS_PER_DAY = 3
MAX_TAUNTS_PER_TARGET_PER_DAY = 1
CLAPBACK_WINDOW_HOURS = 24


def can_taunt(*, is_self: bool, is_friend: bool, target_chill: bool, target_eligible: bool,
              sent_today: int, sent_to_target_today: int) -> tuple[bool, str]:
    """Return (allowed, reason). Reason is game copy shown to the player."""
    if is_self:
        return False, "You can't taunt yourself."
    if not is_friend:
        return False, "Only friends can be taunted."
    if target_chill:
        return False, "This player is in Chill Mode."
    if not target_eligible:
        return False, "Nothing to taunt — they're on track."
    if sent_today >= MAX_TAUNTS_PER_DAY:
        return False, f"You're out of taunts for today ({MAX_TAUNTS_PER_DAY} per day)."
    if sent_to_target_today >= MAX_TAUNTS_PER_TARGET_PER_DAY:
        return False, "You already taunted this player today."
    return True, "OK"


def is_clapback(taunt_hours_ago: float) -> bool:
    return 0 <= taunt_hours_ago <= CLAPBACK_WINDOW_HOURS


# ---------------------------------------------------------------------------
# Badges and cosmetics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Badge:
    key: str
    icon: str
    name: str
    desc: str
    stat: str | None = None  # stats key checked by earned_badges
    threshold: int = 1
    temporary: bool = False


BADGES: list[Badge] = [
    Badge("first_quest", "🎮", "First Quest", "Complete your first check-in", "checkins", 1),
    Badge("early_bird", "🐦", "Early Bird", "Check in before 6am", "early_checkins", 1),
    Badge("night_owl", "🦉", "Night Owl", "Check in after 11pm", "late_checkins", 1),
    Badge("iron_week", "🛡️", "Iron Week", "Hit 7/7 — a 7-day streak", "best_streak", 7),
    Badge("iron_month", "🏰", "Iron Month", "Hold a 30-day streak", "best_streak", 30),
    Badge("centurion", "💯", "Centurion", "100 check-ins", "checkins", 100),
    Badge("shutterbug", "📸", "Shutterbug", "25 live-photo proofs", "photo_checkins", 25),
    Badge("trailblazer", "🧭", "Trailblazer", "10 GPS check-ins", "gps_checkins", 10),
    Badge("scanner", "🔳", "Scanner", "10 QR or NFC check-ins", "qr_checkins", 10),
    Badge("deep_focus", "⏳", "Deep Focus", "10 focus-timer sessions", "timer_checkins", 10),
    Badge("zen", "🧘", "Zen Master", "Log 10 reflections", "reflections", 10),
    Badge("social_butterfly", "🦋", "Social Butterfly", "Make 5 friends", "friends", 5),
    Badge("judge", "⚖️", "The Judge", "Cast 25 Legit/Sus votes", "votes_cast", 25),
    Badge("trash_talker", "🗣️", "Trash Talker", "Send 10 taunts", "taunts_sent", 10),
    Badge("conqueror", "🚩", "Conqueror", "Own 10 map tiles", "tiles_owned", 10),
    Badge("veteran", "⭐", "Veteran", "Reach level 10", "level", 10),
    Badge("daredevil", "😈", "Daredevil", "Complete 5 daily dares", "dares_done", 5),
    Badge("paparazzi", "⚡", "Paparazzi", "Beat a Prove It Now alert", "prove_it_checkins", 1),
    Badge("comeback_kid", "🔁", "Comeback Kid", "Clap back within 24h of a taunt"),
    Badge("boss_slayer", "🗡️", "Boss Slayer", "Defeat a weekly boss with your party"),
    Badge("duelist", "⚔️", "Duelist", "Win a Streak Duel"),
    Badge("high_roller", "🎰", "High Roller", "Win coins from a stake pot"),
    Badge("caught_cappin", "🧢", "Caught Cappin'", "Proof rejected by the party (7 days)", temporary=True),
]
BADGE_MAP: dict[str, Badge] = {b.key: b for b in BADGES}


def earned_badges(stats: Mapping[str, int]) -> set[str]:
    """Stat-based badges a player qualifies for (event badges are awarded directly)."""
    return {b.key for b in BADGES if b.stat and stats.get(b.stat, 0) >= b.threshold}


@dataclass(frozen=True)
class Hat:
    """A level-locked avatar accessory (rendered as a frame). Keys are stable; names are display copy."""
    key: str
    name: str
    level: int


HATS: list[Hat] = [
    Hat("none", "Classic", 1), Hat("cap", "Bronze frame", 2), Hat("headband", "Silver frame", 3),
    Hat("beanie", "Gold frame", 4), Hat("crown", "Crown", 5), Hat("wizard", "Laurel", 7),
    Hat("viking", "Double frame", 10), Hat("halo", "Platinum", 15),
]
HAT_MAP = {h.key: h for h in HATS}
# Professional palette: navy, teal, plum, slate, forest, burgundy, bronze, graphite.
# Matte sport palette: brick, mustard, olive, teal, slate blue, plum, saddle brown, charcoal.
AVATAR_COLORS = ["#b5452f", "#c98f1c", "#6b7a3a", "#2f7a72", "#3f5f8a", "#7a4a6b", "#7a5136", "#3a3835"]
# Earlier palettes (neon, matte, professional) mapped onto this one, used to migrate saved avatar colours.
LEGACY_COLORS = {
    **dict(zip(["#39ff14", "#ffd700", "#ff2e88", "#00e5ff", "#ff8c00", "#b388ff", "#ff3b3b", "#f5f5f5"], AVATAR_COLORS)),
    **dict(zip(["#7d8a62", "#b0954c", "#b07a6f", "#6f8f96", "#a8743f", "#8a7a9b", "#a8625a", "#8c8579"], AVATAR_COLORS)),
    **dict(zip(["#1f3556", "#1e6e6a", "#6b4a7a", "#4a5a70", "#2f6b4a", "#8a3a3f", "#8a6a3a", "#3d434d"],
               ["#3f5f8a", "#2f7a72", "#7a4a6b", "#3f5f8a", "#6b7a3a", "#b5452f", "#7a5136", "#3a3835"])),
}
DEFAULT_AVATAR_COLOR = AVATAR_COLORS[0]


def hat_unlocked(hat: str, level: int) -> bool:
    h = HAT_MAP.get(hat)
    return bool(h and level >= h.level)


# ---------------------------------------------------------------------------
# Generated artwork (avatars, pets, app icon) — deterministic SVG
# ---------------------------------------------------------------------------

Pixel = tuple[int, int, str]
_INK = "#2e2119"
_GOLD = "#d39b1f"


def _digest(*parts: object) -> bytes:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()


def _tint(color: str, amount: float) -> str:
    """Blend a hex colour towards white (amount=1 → white)."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(c + (255 - c) * amount):02x}" for c in (r, g, b))


def avatar_pixels(username: str, color: str = DEFAULT_AVATAR_COLOR, hat: str = "none") -> list[Pixel]:
    """A mirrored 5x5 identicon derived from the username hash (cells in the player's colour)."""
    h = _digest("avatar", username.lower())
    cells: set[Pixel] = set()
    for y in range(5):
        for x in range(3):
            if h[y * 3 + x] % 2:
                cells.add((x, y, color))
                cells.add((4 - x, y, color))
    if len(cells) < 6:  # never render an almost-empty avatar
        cells |= {(2, 1, color), (2, 2, color), (1, 3, color), (3, 3, color)}
    return sorted(cells)


_FRAMES = {  # hat key → (ring colour, ring width, extra mark)
    "cap": ("#a0714f", 2.4, ""), "headband": ("#9aa3ae", 2.4, ""), "beanie": (_GOLD, 2.6, ""),
    "crown": (_GOLD, 2.6, "crown"), "wizard": ("#5f7f4f", 2.4, "laurel"),
    "viking": (_INK, 1.6, "double"), "halo": ("#8f9aa8", 2.8, "star"),
}


def avatar_svg(username: str, color: str = DEFAULT_AVATAR_COLOR, hat: str = "none") -> str:
    color = matte_color(color)
    cells = "".join(f'<rect x="{9 + x * 4.4:.1f}" y="{9 + y * 4.4:.1f}" width="4.5" height="4.5" fill="{c}"/>'
                    for x, y, c in avatar_pixels(username, color, hat))
    parts = [f'<rect width="40" height="40" fill="{_tint(color, .86)}"/>', cells]
    frame = _FRAMES.get(hat)
    if frame:
        ring, width, mark = frame
        parts.append(f'<circle cx="20" cy="20" r="{19 - width / 2:.1f}" fill="none" stroke="{ring}" stroke-width="{width}"/>')
        if mark == "double":
            parts.append(f'<circle cx="20" cy="20" r="15.4" fill="none" stroke="{ring}" stroke-width="0.9"/>')
        elif mark == "crown":
            parts.append(f'<path d="M14 6.2 16.2 2.6 20 5.2 23.8 2.6 26 6.2Z" fill="{ring}"/>')
        elif mark == "laurel":
            parts.append(f'<path d="M7 27c2 4 6 7 11 8M33 27c-2 4-6 7-11 8" fill="none" stroke="{ring}" '
                         f'stroke-width="1.6" stroke-linecap="round"/>')
        elif mark == "star":
            parts.append(f'<path d="M20 1.2 21.3 4.2 24.5 4.4 22 6.4 22.8 9.5 20 7.8 17.2 9.5 18 6.4 15.5 4.4 18.7 4.2Z" fill="{_GOLD}"/>')
    title = _xml_escape(f"{username}'s avatar")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" role="img"><title>{title}</title>'
            f'{"".join(parts)}</svg>')


def pixels_to_svg(pixels: Sequence[Pixel], size: int, background: str | None = None, title: str = "") -> str:
    rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1" fill="{c}"/>' for x, y, c in pixels)
    bg = f'<rect width="{size}" height="{size}" fill="{background}"/>' if background else ""
    t = f"<title>{_xml_escape(title)}</title>" if title else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'shape-rendering="crispEdges" role="img">{t}{bg}{rects}</svg>')


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def matte_color(color: str) -> str:
    """Map a colour from an earlier palette onto the current one; other colours pass through."""
    return LEGACY_COLORS.get(color.lower(), color)


PET_STAGES = [("egg", 0), ("baby", 15), ("teen", 60), ("legend", 200)]
PET_STAGE_LABEL = {"egg": "Egg", "baby": "Hatchling", "teen": "Juvenile", "legend": "Legend"}


def pet_stage(points: int) -> str:
    stage = "egg"
    for name, threshold in PET_STAGES:
        if points >= threshold:
            stage = name
    return stage


def pet_next_stage_at(points: int) -> int | None:
    for _, threshold in PET_STAGES:
        if points < threshold:
            return threshold
    return None


def pet_mood(health: int) -> str:
    if health >= 70:
        return "happy"
    if health >= 40:
        return "okay"
    if health >= 15:
        return "sad"
    return "sick"


def pet_health_update(health: int, active_members: int, total_members: int) -> int:
    """Full party participation heals +10, nobody showing up hurts -15."""
    if total_members <= 0:
        return health
    ratio = active_members / total_members
    return max(0, min(100, health + _round_half_up(25 * ratio) - 15))


_PET_BODY = {"happy": "#5f7f3a", "okay": "#2f7a72", "sad": "#8a7f74", "sick": "#9a8a4a"}


def pet_pixels(stage: str, mood: str) -> list[Pixel]:
    """16x16 party pet. Egg wobbles via CSS; later stages grow ears, wings, crown."""
    px: dict[tuple[int, int], str] = {}

    def put(x: int, y: int, c: str) -> None:
        if 0 <= x < 16 and 0 <= y < 16:
            px[(x, y)] = c

    def blob(cx: float, cy: float, rx: float, ry: float, c: str) -> None:
        for y in range(16):
            for x in range(16):
                if ((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2 <= 1:
                    put(x, y, c)

    body = _PET_BODY.get(mood, _PET_BODY["happy"])
    if stage == "egg":
        blob(8, 9, 4.5, 6, "#f6ecd8" if mood in ("happy", "okay") else "#ddd3c3")
        for x, y in ((6, 6), (10, 9), (7, 12)):
            put(x, y, body)
        if mood in ("sad", "sick"):
            put(8, 5, _INK); put(9, 6, _INK); put(8, 7, _INK)
        return [(x, y, c) for (x, y), c in sorted(px.items())]
    if stage == "legend":
        for i in range(4):  # wings
            for j in range(4 - i):
                put(1 + i, 6 + j + i, _GOLD)
                put(14 - i, 6 + j + i, _GOLD)
    size = {"baby": (4.5, 4), "teen": (5.5, 5), "legend": (6, 5.5)}[stage]
    blob(8, 10, size[0], size[1], body)
    if stage in ("teen", "legend"):  # ears / horns
        put(4, 4, body); put(4, 5, body); put(11, 4, body); put(11, 5, body)
    if stage == "legend":
        for x in (5, 7, 8, 10):
            put(x, 2, _GOLD)
        for x in range(5, 11):
            put(x, 3, _GOLD)
    ey, my, face = 9, 12, "#ffffff"
    if mood == "sick":  # X-shaped eyes
        put(5, ey, face); put(6, ey + 1, face)
        put(10, ey, face); put(9, ey + 1, face)
    else:
        put(6, ey, face); put(9, ey, face)
    if mood == "happy":
        put(6, my, face); put(7, my + 1, face); put(8, my + 1, face); put(9, my, face)
    elif mood == "okay":
        put(7, my, face); put(8, my, face)
    else:
        put(6, my + 1, face); put(7, my, face); put(8, my, face); put(9, my + 1, face)
    return [(x, y, c) for (x, y), c in sorted(px.items())]


def pet_svg(stage: str, mood: str) -> str:
    return pixels_to_svg(pet_pixels(stage, mood), 16, None, f"Party pet: {stage}, feeling {mood}")


# App icon: a bold "H" monogram on espresso with a yellow race stripe, in a 0..1 coordinate space.
ICON_BACKGROUND = "#3b2a20"
ICON_SHAPES: list[tuple[float, float, float, float, str]] = [
    (0.26, 0.22, 0.40, 0.74, "#fff4e0"),   # left stem
    (0.60, 0.22, 0.74, 0.74, "#fff4e0"),   # right stem
    (0.40, 0.42, 0.60, 0.54, "#fff4e0"),   # crossbar
    (0.26, 0.80, 0.74, 0.86, "#e3a923"),   # race stripe
]


def app_icon_svg() -> str:
    rects = "".join(f'<rect x="{x0 * 64:.1f}" y="{y0 * 64:.1f}" width="{(x1 - x0) * 64:.1f}" '
                    f'height="{(y1 - y0) * 64:.1f}" rx="1.5" fill="{c}"/>' for x0, y0, x1, y1, c in ICON_SHAPES)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img"><title>Habitor</title>'
            f'<rect width="64" height="64" rx="14" fill="{ICON_BACKGROUND}"/>{rects}</svg>')


# ---------------------------------------------------------------------------
# Weekly boss battle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Boss:
    key: str
    name: str
    sprite: str
    taunt: str
    laugh: str


BOSSES: list[Boss] = [
    Boss("lazy_dragon", "Lazy Dragon", "🐉", "Zzz… you'll never beat me from the couch.", "HAHAHA! The Lazy Dragon naps on your broken streaks!"),
    Boss("procrastinator", "Procrasti-Nator", "🤖", "I'll defeat you… tomorrow.", "Procrasti-Nator wins! He'll celebrate… eventually."),
    Boss("couch_golem", "Couch Golem", "🛋️", "Sit. Stay. Forever.", "The Couch Golem absorbs another party. *creak*"),
    Boss("snooze_hydra", "Snooze Hydra", "🐍", "Hit snooze. Two more heads grow.", "The Snooze Hydra yawns victoriously. Heads: 9."),
    Boss("doomscroll_wraith", "Doomscroll Wraith", "👻", "Just one more scroll…", "The Doomscroll Wraith scrolls past your party's corpse."),
    Boss("sugar_slime", "Sugar Slime", "🍩", "Mmm, skip leg day.", "Sugar Slime jiggles in triumph. Sticky defeat!"),
]
BOSS_MAP = {b.key: b for b in BOSSES}
BOSS_HP_PER_MEMBER = 250
BOSS_LOOT_COINS = 100


def boss_for_week(party_id: int, week: date) -> Boss:
    return BOSSES[_digest("boss", party_id, week.isoformat())[0] % len(BOSSES)]


def boss_hp(party_size: int) -> int:
    return BOSS_HP_PER_MEMBER * max(1, party_size)


def boss_damage(points: int) -> int:
    return max(1, points)


# ---------------------------------------------------------------------------
# Dares, Prove It Now
# ---------------------------------------------------------------------------

DAILY_DARES = [
    "20 push-ups before noon", "Drink 2 litres of water", "10-minute walk, phone stays home",
    "Finish your shower with 30s of cold", "Read 10 pages of anything", "No social media until 6pm",
    "Plank for 60 seconds", "Tidy one drawer or shelf", "Stretch for 10 minutes",
    "Text someone you appreciate", "Take the stairs all day", "Meditate for 5 minutes",
    "Cook a meal from scratch", "50 squats spread through the day", "Learn 5 words in a new language",
]
DARE_COINS = 25

DUEL_DARES = {
    "burpees": "Do 30 burpees and post the proof",
    "inside_out": "Wear a shirt inside out for a whole day",
    "chicken": "Post a chicken-dance photo 🐔",
    "cold": "Full 60-second cold shower",
    "fan": "React 🔥 to the winner's next 5 check-ins",
    "double": "Do the winner's habit tomorrow as well",
    "dessert": "No dessert for 3 days",
}


def daily_dare(party_id: int, d: date) -> str:
    return DAILY_DARES[_digest("dare", party_id, d.isoformat())[0] % len(DAILY_DARES)]


def prove_it_time(user_id: int, d: date) -> time:
    """A per-user, per-day random time between 09:00 and 21:00 (deterministic, so re-runs agree)."""
    raw = int.from_bytes(_digest("prove", user_id, d.isoformat())[:4], "big")
    minutes = 9 * 60 + raw % (12 * 60)
    return time(minutes // 60, minutes % 60)


# ---------------------------------------------------------------------------
# GPS and Map Conquest
# ---------------------------------------------------------------------------

GPS_RADIUS_M = 150
EARTH_RADIUS_M = 6_371_000
TILE_METERS = 500
TILE_DEG_LAT = TILE_METERS / 111_320


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def valid_coords(lat: float | None, lon: float | None) -> bool:
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180


def _lon_step(ty: int) -> float:
    center_lat = (ty + 0.5) * TILE_DEG_LAT
    return TILE_DEG_LAT / max(math.cos(math.radians(center_lat)), 0.01)


def tile_for(lat: float, lon: float) -> tuple[int, int]:
    """(tx, ty) of the ~500 m grid tile containing a point."""
    ty = math.floor(lat / TILE_DEG_LAT)
    tx = math.floor(lon / _lon_step(ty))
    return tx, ty


def tile_bounds(tx: int, ty: int) -> tuple[tuple[float, float], tuple[float, float]]:
    step = _lon_step(ty)
    return (ty * TILE_DEG_LAT, tx * step), ((ty + 1) * TILE_DEG_LAT, (tx + 1) * step)


def tile_new_owner(current_owner: int | None, counts: Mapping[int, int]) -> int | None:
    """A challenger steals a tile only with strictly more check-ins there than the owner."""
    if not counts:
        return current_owner
    best_user, best = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    if current_owner is None:
        return best_user if best > 0 else None
    if best_user != current_owner and best > counts.get(current_owner, 0):
        return best_user
    return current_owner


# ---------------------------------------------------------------------------
# Leaderboards, leagues, stake pots
# ---------------------------------------------------------------------------

def rank_order(scores: Mapping[int, float], tiebreak: Mapping[int, float] | None = None) -> list[int]:
    tiebreak = tiebreak or {}
    return sorted(scores, key=lambda uid: (-scores[uid], -tiebreak.get(uid, 0), uid))


def rank_changes(current: Sequence[int], previous: Sequence[int]) -> dict[int, int]:
    """Positive = moved up. Players new to the board get 0."""
    prev_pos = {uid: i for i, uid in enumerate(previous)}
    return {uid: (prev_pos[uid] - i if uid in prev_pos else 0) for i, uid in enumerate(current)}


TROPHIES = {1: "🥇", 2: "🥈", 3: "🥉"}
TIERS = ["Bronze", "Silver", "Gold", "Diamond"]
TIER_ICON = {"Bronze": "🥉", "Silver": "🥈", "Gold": "🥇", "Diamond": "💎"}
LEAGUE_MOVE_FRACTION = 0.2


def season_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def previous_season(d: date) -> tuple[str, date, date]:
    first = d.replace(day=1)
    last_prev = first - timedelta(days=1)
    return season_key(last_prev), last_prev.replace(day=1), last_prev


def season_moves(ranked: Sequence[int]) -> tuple[set[int], set[int]]:
    """Top 20% promote, bottom 20% relegate (at least one each when 2+ compete)."""
    n = len(ranked)
    if n < 2:
        return set(), set()
    k = min(max(1, _round_half_up(n * LEAGUE_MOVE_FRACTION)), n // 2)
    return set(ranked[:k]), set(ranked[n - k:])


def shift_tier(tier: str, move: int) -> str:
    i = TIERS.index(tier) if tier in TIERS else 0
    return TIERS[max(0, min(len(TIERS) - 1, i + move))]


def split_pot(pot: int, finishers: int) -> list[int]:
    """Integer split of a virtual-coin pot; leftover coins go to the first finishers."""
    if finishers <= 0:
        return []
    share, rem = divmod(pot, finishers)
    return [share + (1 if i < rem else 0) for i in range(finishers)]


# ---------------------------------------------------------------------------
# Coach (rule-based fallback)
# ---------------------------------------------------------------------------

@dataclass
class CoachStats:
    total_checkins: int = 0
    misses_by_weekday: list[int] = field(default_factory=lambda: [0] * 7)
    checkins_by_hour: list[int] = field(default_factory=lambda: [0] * 24)
    habit_rates: list[tuple[str, int, bool]] = field(default_factory=list)  # (name, pct, has_tiny)
    best_streak_now: int = 0
    avg_mood: float | None = None


def coach_tip(s: CoachStats) -> str:
    if s.total_checkins == 0:
        return "Every legend starts at level 1. Pick ONE quest and do its 2-minute version today — showing up beats going big."
    misses = s.misses_by_weekday
    total_miss = sum(misses)
    if total_miss >= 3:
        worst = max(range(7), key=lambda i: misses[i])
        if misses[worst] >= 2 and misses[worst] >= 1.5 * (total_miss / 7):
            day = WEEKDAY_PLURAL[worst]
            return f"You miss {day} most. Try the 2-minute version on {day} — a tiny win still keeps the streak alive."
    weak = [r for r in s.habit_rates if r[1] < 50]
    if weak:
        name, pct, has_tiny = min(weak, key=lambda r: r[1])
        if has_tiny:
            return f"'{name}' is at {pct}%. This week, its 2-minute version counts as a full win. Shrink the quest, keep the streak."
        return f"'{name}' is at {pct}%. Give it a 2-minute version in quest settings so bad days still score."
    if s.best_streak_now >= 7:
        return f"{s.best_streak_now}-day streak! Stack a new habit right after this one — momentum is your best power-up."
    if s.avg_mood is not None and s.avg_mood < 2.5:
        return "Energy's been low lately. Swap one hard quest for an easy one this week — rest is part of the grind."
    if sum(s.checkins_by_hour):
        hour = max(range(24), key=lambda h: s.checkins_by_hour[h])
        return f"You're most consistent around {hour:02d}:00. Schedule your toughest quest in that window."
    return "Solid run! Invite a friend to a Streak Duel — rivals make habits stick."


# ---------------------------------------------------------------------------
# Habit catalogue
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, str] = {
    "Gym": "🏋️", "Study": "📖", "Music": "🎸", "Reading": "📚",
    "Sleep": "😴", "Meditation": "🧘", "Custom": "⭐",
}
ICON_CHOICES = ["🏋️", "🏃", "🚴", "🏊", "📖", "✏️", "🎸", "🎹", "🥁", "📚", "😴", "🧘", "💧", "🥗", "🧹", "💻", "🎨", "🌱", "⭐", "🔥"]
# Proof methods → display label. QR and NFC are physical "checkpoints" placed where the habit happens.
PROOF_METHODS: dict[str, str] = {"photo": "Live photo", "gps": "GPS location", "qr": "QR checkpoint",
                                 "nfc": "NFC checkpoint", "timer": "Focus timer"}
PROOF_ICONS: dict[str, str] = {"photo": "camera", "gps": "pin", "qr": "qr", "nfc": "nfc", "timer": "timer"}
INSTANT_VERIFY_METHODS = ("gps", "qr", "nfc")  # verified immediately and earn the +5 bonus
MOODS = ["😫", "😕", "😐", "🙂", "🤩"]
ENERGY = ["🪫", "🔋", "⚡"]
REACTIONS = ["🔥", "💪", "👏", "😂", "🤯"]

HABIT_TEMPLATES: dict[str, dict] = {
    "c25k": {
        "title": "Couch to 5K", "icon": "🏃", "blurb": "3 runs a week, from walk/jog to 5K.",
        "habits": [dict(name="C25K run", category="Gym", icon="🏃", frequency="days", days=[0, 2, 4],
                        difficulty="hard", proof=["gps", "photo"], tiny="Shoes on, walk 2 minutes outside",
                        reminder="07:00")],
    },
    "guitar30": {
        "title": "Learn Guitar 30 Days", "icon": "🎸", "blurb": "15 focused minutes a day.",
        "habits": [dict(name="Guitar practice", category="Music", icon="🎸", frequency="daily",
                        difficulty="normal", proof=["timer", "photo"], min_minutes=15,
                        tiny="Play one chord change 10 times", reminder="19:00")],
    },
    "read12": {
        "title": "Read 12 Books/Year", "icon": "📚", "blurb": "20 pages a day ≈ a book a month.",
        "habits": [dict(name="Read 20 pages", category="Reading", icon="📚", frequency="daily",
                        difficulty="easy", proof=["photo", "timer"], min_minutes=20,
                        tiny="Read a single page", reminder="21:30")],
    },
    "gym3": {
        "title": "Gym 3x/Week", "icon": "🏋️", "blurb": "Any 3 days. GPS + QR at your gym.",
        "habits": [dict(name="Gym session", category="Gym", icon="🏋️", frequency="weekly", times_per_week=3,
                        difficulty="hard", proof=["qr", "nfc", "gps", "photo"], tiny="10 bodyweight squats at home")],
    },
    "sleep11": {
        "title": "Sleep by 11", "icon": "😴", "blurb": "Lights out by 23:00, every night.",
        "habits": [dict(name="Lights out by 11", category="Sleep", icon="😴", frequency="daily",
                        difficulty="normal", proof=["photo"], tiny="Phone on the charger outside the bedroom",
                        reminder="22:30")],
    },
    "calm": {
        "title": "Daily Calm", "icon": "🧘", "blurb": "10 minutes of meditation.",
        "habits": [dict(name="Meditate", category="Meditation", icon="🧘", frequency="daily",
                        difficulty="easy", proof=["timer"], min_minutes=10, tiny="Three slow breaths")],
    },
}
