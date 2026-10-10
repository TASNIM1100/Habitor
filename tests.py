"""Habitor test suite — run with:  pytest tests.py

Covers the pure game rules (game.py) and the HTTP flows end to end:
auth, CSRF, scoring + idempotent check-ins, streaks/shields, voting,
taunt limits, clap-backs and leaderboards.
"""

import io
import os
import re
import tempfile
from datetime import date, datetime, timedelta

_TMP = tempfile.mkdtemp(prefix="habitor-test-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{_TMP}/test.db", UPLOAD_DIR=f"{_TMP}/uploads", DEBUG="1",
    SECRET_KEY="test-secret-key-" * 3, DISABLE_SCHEDULER="1", ANTHROPIC_API_KEY="",
    VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import game  # noqa: E402
import main  # noqa: E402

TODAY = date(2026, 10, 7)  # a Wednesday
D = lambda n: TODAY - timedelta(days=n)  # noqa: E731


# ===========================================================================
# Pure game rules
# ===========================================================================

def test_points_formula():
    assert game.checkin_points(0, False, "easy") == 10
    assert game.checkin_points(5, True, "normal") == 25            # (10+5+5) x 1.25
    assert game.checkin_points(30, True, "hard") == 53             # streak bonus capped at 20: 35 x 1.5 = 52.5
    assert game.checkin_points(3, False, "normal") == 16           # 13 x 1.25 = 16.25
    assert game.checkin_points(0, False, "easy", prove_it=True) == 20
    assert game.late_verified_bonus(5, "normal") == 25 - 19        # verified later: just the difference


def test_levels():
    assert [game.level_for_xp(x) for x in (0, 49, 50, 199, 200, 449, 450)] == [1, 1, 2, 2, 3, 3, 4]
    assert game.xp_for_level(3) == 200
    prog = game.level_progress(125)
    assert (prog.level, prog.floor, prog.ceiling, prog.pct) == (2, 50, 200, 50)
    assert game.segments(50, 10) == [True] * 5 + [False] * 5


def test_streak_daily_today_is_open():
    s = game.Schedule("daily")
    assert game.compute_streak(s, {D(1), D(2), D(3)}, set(), TODAY) == 3
    assert game.compute_streak(s, {TODAY, D(1), D(2), D(3)}, set(), TODAY) == 4
    assert game.compute_streak(s, {D(1), D(3)}, set(), TODAY) == 1


def test_streak_shield_protects_but_does_not_add():
    s = game.Schedule("daily")
    assert game.compute_streak(s, {D(1), D(3), D(4)}, {D(2)}, TODAY) == 3


def test_streak_specific_days_skip_rest_days():
    s = game.Schedule.from_fields("days", 1, "0,2,4")  # Mon/Wed/Fri
    mon, fri_prev, wed_prev = D(2), D(5), D(7)
    assert game.compute_streak(s, {mon, fri_prev, wed_prev}, set(), TODAY) == 3
    assert game.compute_streak(s, {mon, wed_prev}, set(), TODAY) == 1  # missed the Friday


def test_streak_weekly_quota():
    s = game.Schedule("weekly", times_per_week=2)
    last_week = {D(7), D(8)}           # 2 completions last week: quota met
    this_week = {D(1)}
    assert game.compute_streak(s, last_week | this_week, set(), TODAY) == 3
    assert game.compute_streak(s, {D(7)} | this_week, set(), TODAY) == 1  # last week short


def test_scheduled_count_and_consistency():
    assert game.scheduled_count(game.Schedule("daily"), D(6), TODAY) == 7
    assert game.scheduled_count(game.Schedule.from_fields("days", 1, "0,2,4"), D(6), TODAY) == 3
    assert game.scheduled_count(game.Schedule("weekly", times_per_week=3), game.week_start(TODAY), game.week_start(TODAY) + timedelta(days=6)) == 3
    assert game.consistency_pct(3, 4) == 75
    assert game.consistency_pct(5, 4) == 100
    assert game.consistency_pct(0, 0) == 0


def test_longest_run():
    days = {D(9), D(8), D(7), D(4), D(3)}
    assert game.longest_daily_run(days, D(9), TODAY) == 3


@pytest.mark.parametrize("legit,sus,expected", [
    (0, 0, "pending"), (1, 0, "pending"), (2, 0, "verified"), (0, 1, "flagged"),
    (1, 1, "pending"), (1, 2, "flagged"), (2, 1, "verified"), (5, 3, "flagged"),
])
def test_vote_outcome(legit, sus, expected):
    assert game.vote_outcome(legit, sus) == expected


def test_can_taunt_rules():
    base = dict(is_self=False, is_friend=True, target_chill=False, target_eligible=True, sent_today=0, sent_to_target_today=0)
    assert game.can_taunt(**base) == (True, "OK")
    assert not game.can_taunt(**{**base, "is_self": True})[0]
    assert not game.can_taunt(**{**base, "is_friend": False})[0]
    assert "Chill Mode" in game.can_taunt(**{**base, "target_chill": True})[1]
    assert not game.can_taunt(**{**base, "target_eligible": False})[0]
    assert "out of taunts" in game.can_taunt(**{**base, "sent_today": 3})[1].lower()
    assert "already" in game.can_taunt(**{**base, "sent_to_target_today": 1})[1].lower()
    assert game.is_clapback(23.9) and not game.is_clapback(24.1)


def test_haversine_and_tiles():
    assert abs(game.haversine_m(0, 0, 1, 0) - 111_195) < 100
    assert game.haversine_m(37.7690, -122.4838, 37.7700, -122.4838) < game.GPS_RADIUS_M
    tx, ty = game.tile_for(37.7690, -122.4838)
    (s, w), (n, e) = game.tile_bounds(tx, ty)
    assert s <= 37.7690 < n and w <= -122.4838 < e
    assert game.tile_new_owner(None, {7: 1}) == 7
    assert game.tile_new_owner(7, {7: 3, 8: 3}) == 7      # a tie doesn't steal
    assert game.tile_new_owner(7, {7: 3, 8: 4}) == 8      # strictly more check-ins steals


def test_pot_seasons_ranks():
    assert game.split_pot(200, 3) == [67, 67, 66]
    assert game.split_pot(50, 0) == []
    promote, relegate = game.season_moves([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert promote == {1, 2} and relegate == {9, 10}
    assert game.season_moves([1]) == (set(), set())
    assert game.shift_tier("Diamond", 1) == "Diamond" and game.shift_tier("Silver", -1) == "Bronze"
    assert game.rank_changes([3, 1, 2], [1, 2, 3]) == {3: 2, 1: -1, 2: -1}


def test_badges_and_cosmetics():
    assert len(game.BADGES) >= 15
    earned = game.earned_badges({"checkins": 1, "best_streak": 7, "early_checkins": 1})
    assert {"first_quest", "iron_week", "early_bird"} <= earned and "iron_month" not in earned
    assert game.hat_unlocked("crown", 5) and not game.hat_unlocked("halo", 14)
    assert game.avatar_pixels("alice") == game.avatar_pixels("ALICE")  # deterministic per username
    assert game.avatar_pixels("alice") != game.avatar_pixels("bob")
    assert game.avatar_svg("alice").startswith("<svg")


def test_pet_boss_dares_prove_it():
    assert game.pet_stage(0) == "egg" and game.pet_stage(200) == "legend"
    assert game.pet_health_update(50, 4, 4) == 60 and game.pet_health_update(50, 0, 4) == 35
    assert game.boss_hp(4) == 4 * game.BOSS_HP_PER_MEMBER
    assert game.boss_for_week(1, TODAY) == game.boss_for_week(1, TODAY)
    assert game.daily_dare(1, TODAY) in game.DAILY_DARES
    t = game.prove_it_time(42, TODAY)
    assert 9 <= t.hour < 21


def test_coach_rules():
    assert "level 1" in game.coach_tip(game.CoachStats())
    s = game.CoachStats(total_checkins=20, misses_by_weekday=[4, 0, 0, 1, 0, 0, 0])
    assert "Mondays" in game.coach_tip(s)


# ===========================================================================
# HTTP flows
# ===========================================================================

@pytest.fixture(autouse=True)
def fresh_db():
    SQLModel.metadata.drop_all(main.engine)
    SQLModel.metadata.create_all(main.engine)
    main.limiter.reset()
    yield


class Player:
    def __init__(self, name: str):
        self.name = name
        self.client = TestClient(main.app)
        self.refresh_token()

    def refresh_token(self) -> None:
        html = self.client.get("/offline").text
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def post(self, url: str, data=None, files=None, hx: bool = True):
        headers = {"X-CSRF-Token": self.token}
        if hx:
            headers["HX-Request"] = "true"
        return self.client.post(url, data=data or {}, files=files, headers=headers, follow_redirects=False)

    @property
    def user(self) -> main.User:
        with Session(main.engine) as db:
            return db.exec(select(main.User).where(main.User.username == self.name)).one()


def signup(name: str) -> Player:
    main.limiter.reset()  # many sign-ups from one test IP would trip the real limiter
    p = Player(name)
    r = p.post("/signup", {"username": name, "email": f"{name}@example.com", "password": "hunter2hunter2"}, hx=False)
    assert r.status_code == 303, r.text
    p.refresh_token()  # the token rotates on login
    return p


def befriend(a: Player, b: Player) -> None:
    assert a.post(f"/friends/request/{b.name}", hx=False).status_code == 303
    with Session(main.engine) as db:
        f = db.exec(select(main.Friendship).where(main.Friendship.addressee_id == b.user.id,
                                                  main.Friendship.status == "pending")).first()
    assert b.post(f"/friends/{f.id}/accept", hx=False).status_code == 303


def new_habit(p: Player, **overrides) -> int:
    data = {"name": "Practice", "category": "Music", "icon": "🎸", "frequency": "daily", "difficulty": "normal",
            "proof_methods": ["photo"], "min_minutes": "10"}
    data.update(overrides)
    assert p.post("/habits", data, hx=False).status_code == 303
    with Session(main.engine) as db:
        return db.exec(select(main.Habit.id).where(main.Habit.user_id == p.user.id).order_by(main.Habit.id.desc())).first()


def photo() -> dict:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "#39ff14").save(buf, "JPEG")
    return {"photo": ("proof.jpg", buf.getvalue(), "image/jpeg")}


def checkin_photo(p: Player, hid: int):
    return p.post(f"/habits/{hid}/checkin", {"method": "photo"}, files=photo())


def get_checkin(hid: int) -> main.CheckIn:
    with Session(main.engine) as db:
        return db.exec(select(main.CheckIn).where(main.CheckIn.habit_id == hid)).one()


def test_health():
    r = TestClient(main.app).get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_signup_login_logout():
    alice = signup("alice")
    assert "Today's quests" in alice.client.get("/").text
    assert alice.post("/logout", hx=False).status_code == 303
    assert "Create your account" in alice.client.get("/").text
    alice.refresh_token()
    bad = alice.post("/login", {"username": "alice", "password": "wrong-password"}, hx=False)
    assert bad.status_code == 400 and "Wrong username or password" in bad.text
    ok = alice.post("/login", {"username": "alice", "password": "hunter2hunter2"}, hx=False)
    assert ok.status_code == 303
    assert main.verify_password(alice.user.password_hash, "hunter2hunter2")
    assert alice.user.password_hash.startswith("$argon2")


def test_signup_validation_and_duplicates():
    p = Player("x")
    r = p.post("/signup", {"username": "a!", "email": "a@b.co", "password": "hunter2hunter2"}, hx=False)
    assert r.status_code == 400
    signup("bob")
    q = Player("bob2")
    r = q.post("/signup", {"username": "BOB", "email": "other@b.co", "password": "hunter2hunter2"}, hx=False)
    assert r.status_code == 400 and "already taken" in r.text.lower()


def test_csrf_is_enforced():
    alice = signup("alice")
    r = alice.client.post("/habits", data={"name": "x", "proof_methods": "photo"}, follow_redirects=False)
    assert r.status_code == 403
    r = alice.client.post("/habits", data={"name": "x", "proof_methods": "photo", "csrf_token": "forged"}, follow_redirects=False)
    assert r.status_code == 403


def test_ownership_checks():
    alice, bob = signup("alice"), signup("bob")
    hid = new_habit(alice)
    assert bob.client.get(f"/habits/{hid}/edit").status_code == 404
    assert bob.post(f"/habits/{hid}/checkin", {"method": "photo"}, files=photo()).status_code == 404


def test_photo_checkin_scores_once():
    alice = signup("alice")
    hid = new_habit(alice)
    r = checkin_photo(alice, hid)
    assert r.status_code == 200 and "Completed" in r.text
    ci = get_checkin(hid)
    assert ci.status == "pending" and ci.streak == 1
    assert ci.points == game.checkin_points(1, False, "normal") == 14
    assert alice.user.xp == 14
    again = checkin_photo(alice, hid)
    assert again.status_code == 409
    with Session(main.engine) as db:
        events = db.exec(select(main.PointEvent).where(main.PointEvent.user_id == alice.user.id)).all()
    assert len(events) == 1 and alice.user.xp == 14  # no double-count
    stored = os.listdir(os.path.join(os.environ["UPLOAD_DIR"], os.path.dirname(ci.photo_path)))
    assert any(f.endswith(".webp") for f in stored)


def test_upload_validation():
    alice = signup("alice")
    hid = new_habit(alice)
    r = alice.post(f"/habits/{hid}/checkin", {"method": "photo"}, files={"photo": ("x.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 415
    big = {"photo": ("big.jpg", b"0" * (main.MAX_UPLOAD_BYTES + 10), "image/jpeg")}
    assert alice.post(f"/habits/{hid}/checkin", {"method": "photo"}, files=big).status_code == 413


def test_gps_checkin_radius():
    alice = signup("alice")
    hid = new_habit(alice, proof_methods=["gps"], name="Gym", category="Gym")
    alice.post(f"/habits/{hid}/location", {"lat": "37.7690", "lon": "-122.4838", "label": "Gym"}, hx=False)
    far = alice.post(f"/habits/{hid}/checkin", {"method": "gps", "lat": "37.7800", "lon": "-122.4838"})
    assert far.status_code == 400 and "Too far" in far.text
    near = alice.post(f"/habits/{hid}/checkin", {"method": "gps", "lat": "37.7695", "lon": "-122.4838"})
    assert near.status_code == 200
    ci = get_checkin(hid)
    assert ci.status == "verified" and ci.distance_m < 150
    assert ci.points == game.checkin_points(1, True, "normal")  # verified bonus included
    with Session(main.engine) as db:
        assert db.exec(select(main.Tile).where(main.Tile.owner_id == alice.user.id)).first() is not None


def test_qr_checkin():
    alice = signup("alice")
    hid = new_habit(alice, proof_methods=["qr"])
    with Session(main.engine) as db:
        habit = db.get(main.Habit, hid)
        token = main.qr_signer.dumps([habit.id, habit.qr_secret])
    assert alice.post("/qr/checkin", {"token": "garbage"}).status_code == 400
    r = alice.post("/qr/checkin", {"token": token})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/"
    assert get_checkin(hid).status == "verified"


def test_voting_flag_and_rejection():
    alice, bob, cara, dan = (signup(n) for n in ("alice", "bob", "cara", "dan"))
    for other in (bob, cara, dan):
        befriend(alice, other)
    hid = new_habit(alice)
    checkin_photo(alice, hid)
    ci = get_checkin(hid)
    xp_before = alice.user.xp
    assert alice.post(f"/checkins/{ci.id}/vote", {"value": "sus"}).status_code == 403   # own proof
    assert bob.post(f"/checkins/{ci.id}/vote", {"value": "legit"}).status_code == 200
    assert bob.post(f"/checkins/{ci.id}/vote", {"value": "sus"}).status_code == 409    # one vote
    assert cara.post(f"/checkins/{ci.id}/vote", {"value": "sus"}).status_code == 200
    assert get_checkin(hid).status == "pending"                                       # 1 legit vs 1 sus
    assert dan.post(f"/checkins/{ci.id}/vote", {"value": "sus"}).status_code == 200
    flagged = get_checkin(hid)
    assert flagged.status == "flagged" and flagged.reprove_deadline is not None       # sus majority
    with Session(main.engine) as db:  # the 12h re-prove window passes
        row = db.get(main.CheckIn, ci.id)
        row.reprove_deadline = main.utcnow() - timedelta(minutes=1)
        db.add(row)
        db.commit()
    main.job_votes()
    assert get_checkin(hid).status == "rejected"
    assert alice.user.xp == max(0, xp_before - ci.points - game.REJECT_PENALTY)
    with Session(main.engine) as db:
        badge = db.exec(select(main.UserBadge).where(main.UserBadge.user_id == alice.user.id,
                                                     main.UserBadge.key == "caught_cappin")).one()
        assert badge.expires_at > main.utcnow() + timedelta(days=6)
        assert db.get(main.Habit, hid).current_streak == 0                           # streak day lost


def test_votes_verify_with_bonus_and_non_friends_blocked():
    alice, bob, cara, eve = (signup(n) for n in ("alice", "bob", "cara", "eve"))
    befriend(alice, bob)
    befriend(alice, cara)
    hid = new_habit(alice)
    checkin_photo(alice, hid)
    ci = get_checkin(hid)
    assert eve.post(f"/checkins/{ci.id}/vote", {"value": "sus"}).status_code == 404     # can't even see it
    bob.post(f"/checkins/{ci.id}/vote", {"value": "legit"})
    cara.post(f"/checkins/{ci.id}/vote", {"value": "legit"})
    done = get_checkin(hid)
    assert done.status == "verified" and done.verified_by == "votes"
    assert done.points == game.checkin_points(1, True, "normal")
    assert alice.user.xp == done.points


def test_photo_auto_verifies_after_12h():
    alice = signup("alice")
    hid = new_habit(alice)
    checkin_photo(alice, hid)
    with Session(main.engine) as db:
        ci = db.exec(select(main.CheckIn)).one()
        ci.created_at = main.utcnow() - timedelta(hours=13)
        db.add(ci)
        db.commit()
    main.job_votes()
    assert get_checkin(hid).status == "verified" and get_checkin(hid).verified_by == "timeout"


def _make_tauntable(p: Player) -> None:
    hid = new_habit(p, name=f"{p.name} quest")
    with Session(main.engine) as db:
        db.add(main.DayMark(habit_id=hid, user_id=p.user.id, day=main.local_today(p.user) - timedelta(days=1), kind="miss"))
        db.commit()


def test_taunt_limits():
    alice, bob, cara, dan, eve, zed = (signup(n) for n in ("alice", "bob", "cara", "dan", "eve", "zed"))
    for other in (bob, cara, dan, eve):
        befriend(alice, other)
    for p in (bob, cara, dan, eve, zed):
        _make_tauntable(p)
    assert alice.post("/taunt/bob", {"key": "chicken"}).status_code == 200
    r = alice.post("/taunt/bob", {"key": "snail"})
    assert r.status_code == 429 and "already taunted" in r.text.lower()                   # 1 per target per day
    assert alice.post("/taunt/cara", {"key": "couch"}).status_code == 200
    assert alice.post("/taunt/dan", {"key": "loading"}).status_code == 200
    r = alice.post("/taunt/eve", {"key": "crying"})
    assert r.status_code == 429 and "out of taunts" in r.text.lower()                     # 3 per day
    assert bob.post("/taunt/zed", {"key": "chicken"}).status_code == 403          # friends only
    assert bob.post("/taunt/alice", {"key": "free text!"}).status_code == 400     # presets only


def test_chill_mode_blocks_taunts():
    alice, bob = signup("alice"), signup("bob")
    befriend(alice, bob)
    _make_tauntable(bob)
    assert bob.post("/settings/chill", hx=False).status_code == 303
    r = alice.post("/taunt/bob", {"key": "chicken"})
    assert r.status_code == 403 and "Chill Mode" in r.text


def test_clapback():
    alice, bob = signup("alice"), signup("bob")
    befriend(alice, bob)
    _make_tauntable(alice)
    assert bob.post("/taunt/alice", {"key": "crying"}).status_code == 200
    with Session(main.engine) as db:
        hid = db.exec(select(main.Habit.id).where(main.Habit.user_id == alice.user.id)).first()
    bob_xp = bob.user.xp
    r = checkin_photo(alice, hid)
    assert r.status_code == 200 and "Comeback" in r.headers.get("HX-Trigger", "")
    ci = get_checkin(hid)
    assert ci.clapback
    assert alice.user.xp == ci.points + game.CLAPBACK_BONUS
    assert bob.user.xp == max(0, bob_xp - game.TAUNTER_PENALTY)
    with Session(main.engine) as db:
        assert db.exec(select(main.Taunt)).one().resolved


def test_missed_days_use_shields_first():
    alice = signup("alice")
    hid = new_habit(alice)
    with Session(main.engine) as db:
        user = db.exec(select(main.User).where(main.User.username == "alice")).one()
        habit = db.get(main.Habit, hid)
        habit.created_at = main.utcnow() - timedelta(days=10)
        user.shields = 1
        today = main.local_today(user)
        for n in (5, 4, 3):  # a 3-day streak, then two missed days
            ci = main.CheckIn(habit_id=hid, user_id=user.id, slot=(today - timedelta(days=n)).isoformat(),
                              local_date=today - timedelta(days=n), status="verified")
            db.add(ci)
        db.commit()
        main.process_user_day(db, user, today - timedelta(days=2))
        main.process_user_day(db, user, today - timedelta(days=2))  # re-running is a no-op
        assert user.shields == 0
        main.refresh_streak(db, habit, user, today - timedelta(days=1))
        assert habit.current_streak == 3                                  # shield kept it alive
        main.process_user_day(db, user, today - timedelta(days=1))
        db.commit()
        marks = db.exec(select(main.DayMark).where(main.DayMark.habit_id == hid).order_by(main.DayMark.day)).all()
        assert [m.kind for m in marks] == ["shield", "miss"]
        assert marks[1].streak_lost == 3
        assert db.get(main.Habit, hid).current_streak == 0


def test_leaderboard_points_and_consistency():
    alice, bob, cara = (signup(n) for n in ("alice", "bob", "cara"))
    befriend(alice, bob)
    befriend(alice, cara)
    with Session(main.engine) as db:
        users = {u.username: u for u in db.exec(select(main.User)).all()}
        today = main.local_today(users["alice"])
        for name, pts in (("alice", 30), ("bob", 50), ("cara", 10)):
            main.award(db, users[name], pts, "test", f"t:{name}", today)
        main.award(db, users["cara"], 100, "test", "t:cara-old", today - timedelta(days=30))
        db.commit()
        week = main.leaderboard(db, users["alice"], "friends", "week", "points")
        assert [r["user"].username for r in week["rows"]] == ["bob", "alice", "cara"]
        assert [r["trophy"] for r in week["rows"]] == ["🥇", "🥈", "🥉"]
        assert next(r for r in week["rows"] if r["me"])["user"].username == "alice"
        alltime = main.leaderboard(db, users["alice"], "friends", "all", "points")
        assert alltime["rows"][0]["user"].username == "cara"
        cons = main.leaderboard(db, users["alice"], "friends", "week", "consistency")
        assert cons["unit"] == "%" and all(0 <= r["score"] <= 100 for r in cons["rows"])
    page = alice.client.get("/boards?scope=friends&period=week&mode=points")
    assert page.status_code == 200 and "Wall of fame" in page.text


def test_feed_and_reactions():
    alice, bob = signup("alice"), signup("bob")
    befriend(alice, bob)
    hid = new_habit(alice)
    checkin_photo(alice, hid)
    ci = get_checkin(hid)
    feed = bob.client.get("/feed/items")
    assert feed.status_code == 200 and f"ci-{ci.id}" in feed.text and "Legit" in feed.text
    r = bob.post(f"/checkins/{ci.id}/react", {"emoji": "🔥"})
    assert r.status_code == 200 and 'class="reaction is-on"' in r.text
    assert bob.client.get(f"/media/{ci.photo_path}").status_code == 200
    stranger = signup("mallory")
    assert stranger.client.get(f"/media/{ci.photo_path}").status_code == 404


def test_security_headers():
    r = TestClient(main.app).get("/login")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()


def test_signup_rate_limit():
    for i in range(5):
        Player(f"p{i}").post("/signup", {"username": f"rl{i}", "email": f"rl{i}@x.io", "password": "hunter2hunter2"}, hx=False)
    r = Player("p6").post("/signup", {"username": "rl6", "email": "rl6@x.io", "password": "hunter2hunter2"})
    assert r.status_code == 429


def test_ai_verification_with_fake_claude(monkeypatch):
    """The Claude path end to end, with a stub client (no network)."""
    from types import SimpleNamespace

    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        text = '{"match": true, "confidence": 0.92, "reason": "Guitar in hand"}'
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])

    api = SimpleNamespace(create=fake_create)
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(main, "_claude_client", SimpleNamespace(messages=api, beta=SimpleNamespace(messages=api)))
    alice = signup("alice")
    hid = new_habit(alice)
    checkin_photo(alice, hid)  # TestClient runs the background task after the response
    ci = get_checkin(hid)
    assert ci.status == "verified" and ci.verified_by == "ai" and ci.ai_match
    assert ci.points == game.checkin_points(1, True, "normal")
    sent = calls[0]
    assert sent["model"] == main.CLAUDE_MODEL
    assert sent["messages"][0]["content"][0]["type"] == "image"
    assert sent["output_config"]["format"]["type"] == "json_schema"


def test_reprove_flagged_proof_with_photo_and_gps():
    alice, bob = signup("alice"), signup("bob")
    befriend(alice, bob)
    hid = new_habit(alice)
    checkin_photo(alice, hid)
    ci = get_checkin(hid)
    bob.post(f"/checkins/{ci.id}/vote", {"value": "sus"})  # 1 of 1 voters say sus → flagged
    assert get_checkin(hid).status == "flagged"
    assert "Needs re-verification" not in alice.client.get("/").text   # today's proof: re-prove lives on the quest card
    assert "Submit re-verification" in alice.client.get("/").text
    no_gps = alice.post(f"/checkins/{ci.id}/reprove", {"method": "photo_gps"}, files=photo())
    assert no_gps.status_code == 400
    ok = alice.post(f"/checkins/{ci.id}/reprove", {"method": "photo_gps", "lat": "51.5", "lon": "-0.12"}, files=photo())
    assert ok.status_code == 200
    done = get_checkin(hid)
    assert done.status == "verified" and done.verified_by == "reprove" and "gps" in done.methods



def test_nfc_checkpoint_checkin_and_landing():
    alice, bob = signup("alice"), signup("bob")
    hid = new_habit(alice, proof_methods=["nfc"], name="Gym", category="Gym")
    with Session(main.engine) as db:
        habit = db.get(main.Habit, hid)
        nfc_url = main.checkpoint_url(habit, "nfc")
        qr_url = main.checkpoint_url(habit, "qr")
    token = nfc_url.split("/nfc/")[-1]
    landing = alice.client.get(f"/nfc/{token}")
    assert landing.status_code == 200 and "NFC checkpoint" in landing.text
    assert bob.client.get(f"/nfc/{token}", follow_redirects=False).status_code == 303  # not their tag: sent home with an error
    assert alice.post("/nfc/checkin", {"token": qr_url}).status_code == 400   # a QR token isn't an NFC token
    r = alice.post("/nfc/checkin", {"token": nfc_url})                        # full URL as read from the tag
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/"
    ci = get_checkin(hid)
    assert ci.status == "verified" and ci.verified_by == "nfc" and ci.methods == "nfc"
    assert ci.points == game.checkin_points(1, True, "normal")                # instant-verify bonus


def test_qr_checkpoint_rejected_when_method_disabled():
    alice = signup("alice")
    hid = new_habit(alice, proof_methods=["photo"])
    with Session(main.engine) as db:
        url = main.checkpoint_url(db.get(main.Habit, hid), "qr")
    r = alice.post("/qr/checkin", {"token": url})
    assert r.status_code == 400 and "enabled for this quest" in r.text


def test_friend_code_connect():
    alice, bob = signup("alice"), signup("bob")
    code = alice.user.invite_code
    page = bob.client.get(f"/invite/{code}")                                  # scanned QR / tapped NFC tag
    assert page.status_code == 200 and "Connect" in page.text
    assert not main.are_friends(Session(main.engine), alice.user.id, bob.user.id)
    r = bob.post("/friends/connect", {"code": f"http://testserver/invite/{code}"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/u/alice"
    with Session(main.engine) as db:
        assert main.are_friends(db, alice.user.id, bob.user.id)
    assert alice.post("/friends/connect", {"code": code}).status_code == 400  # own code
    assert bob.post("/friends/connect", {"code": "nope"}).status_code == 404
    assert "Your friend code" in alice.client.get("/friends").text


def test_themes_are_saved_in_a_cookie_and_rendered():
    alice = signup("alice")
    assert 'data-theme="trail"' in alice.client.get("/").text            # default theme
    r = alice.post("/settings/theme", {"theme": "blaze"}, hx=False)
    assert r.status_code == 303 and "habitor_theme=blaze" in r.headers["set-cookie"]
    page = alice.client.get("/settings").text
    assert 'data-theme="blaze"' in page and 'content="#f7efe9"' in page  # browser bar colour follows the theme
    assert alice.post("/settings/theme", {"theme": "neon"}, hx=False).status_code == 303  # rejected → back with an error
    assert 'data-theme="blaze"' in alice.client.get("/").text
    alice.client.cookies.set("habitor_theme", "bogus")
    assert 'data-theme="trail"' in alice.client.get("/").text            # unknown cookie values fall back


def test_quick_sheet_and_scanner_pages():
    alice = signup("alice")
    hid = new_habit(alice, name="Run")
    sheet = alice.client.get("/quick")
    assert sheet.status_code == 200 and f'href="/#quest-{hid}"' in sheet.text and "Check in" in sheet.text
    checkin_photo(alice, hid)
    assert "Done ·" in alice.client.get("/quick").text
    scan = alice.client.get("/scan")
    assert scan.status_code == 200 and "data-qr-scan" in scan.text and "data-nfc-scan" in scan.text
    assert Player("anon").client.get("/scan", follow_redirects=False).status_code == 303  # login required


# ===========================================================================
# Security fixes
# ===========================================================================

def test_login_next_cannot_redirect_off_site():
    alice = signup("alice")
    alice.post("/logout", hx=False)
    for evil in ("/\\evil.com", "//evil.com", "https://evil.com", "javascript:alert(1)"):
        alice.refresh_token()
        r = alice.post("/login", {"username": "alice", "password": "hunter2hunter2", "next": evil}, hx=False)
        assert r.status_code == 303 and r.headers["location"] == "/", evil
        alice.post("/logout", hx=False)
    alice.refresh_token()
    r = alice.post("/login", {"username": "alice", "password": "hunter2hunter2", "next": "/habits"}, hx=False)
    assert r.headers["location"] == "/habits"


def test_push_endpoints_limited_to_real_push_services():
    assert main.allowed_push_endpoint("https://fcm.googleapis.com/fcm/send/abc")
    assert main.allowed_push_endpoint("https://web.push.apple.com/QF")
    for bad in ("https://localhost/x", "https://169.254.169.254/latest", "http://fcm.googleapis.com/x",
                "https://fcm.googleapis.com.evil.io/x", "https://fcm.googleapis.com:8443/x", "https://u:p@fcm.googleapis.com/x"):
        assert not main.allowed_push_endpoint(bad), bad


def test_secret_tokens_are_redacted_from_logs():
    assert main.redact_path("/qr/WzEsImFiYyJd.sig") == "/qr/[redacted]"
    assert main.redact_path("/nfc/abc/") == "/nfc/[redacted]/"
    assert main.redact_path("/invite/Zpta1d8c") == "/invite/[redacted]"
    assert main.redact_path("/party/join/Abc123") == "/party/join/[redacted]"
    assert main.redact_path("/habits/3/edit") == "/habits/3/edit"
    import logging
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/qr/secret-token", "1.1", 200), None)
    main._RedactAccessLog().filter(rec)
    assert "secret-token" not in rec.getMessage()


def test_dockerfile_copies_only_the_app_and_trusts_only_the_proxy():
    docker = open(os.path.join(os.path.dirname(__file__), "Dockerfile"), encoding="utf-8").read()
    assert "COPY . ." not in docker and "'*'" not in docker
    assert "FORWARDED_ALLOW_IPS=127.0.0.1" in docker


def test_demo_seed_only_on_local_dev_databases():
    assert main.seed_allowed(False, False, "127.0.0.1", "sqlite:///habitor.db")
    assert not main.seed_allowed(False, False, "0.0.0.0", "sqlite:///habitor.db")
    assert not main.seed_allowed(False, False, "127.0.0.1", "postgresql://db/habitor")
    assert main.seed_allowed(True, False, "0.0.0.0", "postgresql://db/habitor")
    assert main.seed_allowed(False, True, "0.0.0.0", "postgresql://db/habitor")


def test_timezone_change_cannot_double_check_in():
    alice = signup("alice")
    hid = new_habit(alice)
    form = {"bio": "", "avatar_color": game.DEFAULT_AVATAR_COLOR, "avatar_hat": "none", "tz": "UTC"}
    assert checkin_photo(alice, hid).status_code == 200
    r = alice.post("/settings", {**form, "tz": "Pacific/Kiritimati"}, hx=False)      # UTC+14: "tomorrow" locally
    assert r.status_code == 303 and alice.user.tz == "Pacific/Kiritimati"
    again = checkin_photo(alice, hid)
    assert again.status_code == 429 and "changed timezone" in again.text           # no second check-in today
    r = alice.post("/settings", {**form, "tz": "Europe/London"})                     # one change per week
    assert r.status_code == 429 and alice.user.tz == "Pacific/Kiritimati"
