"""What a hand in front of the TV means: the calm rules, with no camera and no model, so tests can drive them.

The tracker (tracker.py) gives this one look at the hand per video frame: what shape the hand makes (MediaPipe's
names, such as "Open_Palm"), where its middle is and how big it is, all as parts of the picture (0 to 1).
This answers with the gestures to send to Home Assistant.

The rules, so cooking and talking never move anything:
1. Nothing counts until someone raises an open hand and holds it still for a moment (0.6 s). Then the TV listens
   ("ready") for five seconds, and every gesture gives five more.
2. A small hand far from the TV (someone at the back of the kitchen) counts for nothing, and so does a hand low
   in the picture (resting on the counter). When two hands show, the higher one talks.
3. While it listens: a swipe moves the glow (left, right, up, down). A shape held still for a moment acts once:
   one finger is OK, a fist is Back, a V plays or pauses. A thumb up or down steps the volume, again every
   0.8 seconds while it is held.
4. After a swipe the hand comes back: that move counts for nothing until the hand is back where the swipe started,
   or has rested still for two seconds (then a swipe the other way is meant).
5. Dropping the arm is not a "down": a down counts only when the hand is still in the picture a moment later.
6. A fast swipe blurs, and the hand model loses the hand (the kitchen, 24 Nov 2026). The tracker then gives the
   middle of what moves near where the hand was ("Motion"): the swipe's path goes on through the blur.
7. It keeps listening while a hand is up. The model often cannot name a fist seen from the front ("None" at about
   0.5): a small, still, unsure hand counts as a fist too.
"""

from __future__ import annotations

import math

from collections import deque
from dataclasses import dataclass, field

# MediaPipe gesture names -> what they do once the TV listens.
SHAPES = {
    "Closed_Fist": "back",
    "Victory": "playpause",
    "Thumb_Up": "volume_up",
    "Thumb_Down": "volume_down",
}
REPEATS = {"volume_up", "volume_down"}
# Point where the glow should go (Radu, 24 Nov 2026: a held sign instead of a swipe). It steps once, and keeps stepping
# slowly while the finger points. The OK sign (thumb and index finger in a circle) is OK.
MOVES = {"left", "right", "up", "down"}
OK_SIGN = "OK_Sign"
# The model is less sure of a fist seen from the front (the kitchen test, 24 Nov: 0.55-0.58 for a clear fist).
SURE = {"Closed_Fist": 0.45}
# A moving blur the tracker found where the hand was: it only carries a swipe on.
MOTION = "Motion"


@dataclass
class Hand:
    shape: str  # MediaPipe's name: Open_Palm, Pointing_Up, Closed_Fist, Victory, Thumb_Up, Thumb_Down, None, ...
    x: float  # the hand's middle, 0 (left of the picture) to 1
    y: float  # 0 (top) to 1
    size: float  # the hand's height or width, the larger, as a part of the picture's height
    score: float = 1.0
    point: str | None = None  # where the index finger points in the picture (left/right/up/down), when it points
    gap: float = 1.0  # thumb tip to index tip, as a part of the hand's size: small while the hand pinches
    pts: tuple | None = field(default=None, repr=False)  # the hand's 21 points around its middle, in hand sizes
    curled: bool = False  # the index finger folds toward the palm (its tip nearer the wrist than its middle joint): a fist
    anchor: tuple | None = None  # the middle of the wrist and the four knuckles: it stays put when the fingers close


@dataclass
class Settings:
    wake_hold: float = 0.6  # seconds an open hand is held still to start listening
    listen: float = 5.0  # seconds it listens after the last gesture
    min_size: float = 0.10  # a smaller hand is too far from the TV
    min_score: float = 0.55  # the model must be this sure of a shape
    wake_score: float = 0.5  # ... and this sure of the open hand that wakes it
    keep: float = 2.0  # while a hand is up it keeps listening at least this long
    max_y: float = 0.8  # a hand lower than this (on the counter, at a side) is resting, not talking
    still: float = 0.06  # a hand that moves less than this is "still"
    swipe: float = 0.13  # a swipe moves at least this far ...
    swipe_time: float = 0.45  # ... within this many seconds
    swipe_rest: float = 0.7  # seconds after a swipe before the next one can count
    shape_hold: float = 0.3  # seconds a shape is held before it acts
    repeat: float = 1.5  # seconds between slow volume steps while the thumb is held ...
    repeat_after: float = 2.5  # ... once it has been held this long: one thumb is one step
    point_repeat_after: float = 1.2  # a finger held pointing steps again after this long ...
    point_repeat: float = 0.9  # ... and then this often (Radu, 24 Nov: 0.6 s "moves really fast")
    vertical: bool = True  # swipe up and down too (off: only left and right)
    airtap: bool = False  # Air tap: the open hand moves the glow over the screen, the OK sign taps (no swipes)
    pointer: bool = False  # Air tap's Pointer style: the screen shows the live hand, and the tap hits what is under it
    reach: float = 0.25  # in Air tap, moving the hand this far from where it woke reaches the screen's edge
    mirror: bool = True  # the camera looks at the person: their right is the picture's left


@dataclass
class GestureReader:
    settings: Settings = field(default_factory=Settings)
    listening_until: float = 0.0
    _palm_since: float | None = None
    _palm_at: tuple[float, float] | None = None
    _trail: deque = field(default_factory=deque)  # (time, x, y) of the hand while it listens
    _aims: deque = field(default_factory=deque)  # Air tap: (time, x, y) of the glow's recent places
    _rest_until: float = 0.0
    _shape: str | None = None
    _shape_since: float = 0.0
    _shape_at: tuple[float, float] = (0.0, 0.0)
    _shape_done_at: float | None = None
    _shape_last: float = 0.0
    _back: tuple[str, float] | None = None  # after a swipe: its direction and where on its line it started
    _settling: bool = False  # after a swipe, until the hand stops
    _need_drop: bool = False  # after a swipe, until the hand goes down (out of the picture, or low)
    _gone_since: float | None = None  # no talking hand (or only a low one) since then
    _wake_y: float = 0.5  # where the open hand woke it
    _wake_x: float = 0.5
    aim: tuple[float, float] | None = None  # Air tap: where on the screen the hand is (0-1 across and down), or None
    _pinch_open: float | None = None  # Air tap: since then the hand is open (thumb away from the finger): a pinch may tap
    _aim_speed: tuple = (0.0, 0.0)  # Air tap: how fast the glow moves (smoothed), for the One Euro filter
    _pinch_seen: deque = field(default_factory=deque)  # Air tap: (time, gap, size) of the last moment, for the pinch
    _tapped_at: float = -9.0  # Air tap: the last pinch tap (a pinch that curls on is no fist)
    _back_at: float = -9.0  # the last fist that went back: a hand opening from a fist is no pinch
    _origin: tuple[float, float] | None = None  # Air tap: where the hand rested first: the screen's middle
    tap_at: tuple[float, float] | None = None
    shape: tuple | None = None  # Pointer style: the live hand's points to draw (mirrored like the person), or None  # Air tap: where the hand was just before it closed into the OK sign
    _steady_at: tuple[float, float] | None = None
    _steady_since: float = 0.0
    _steady_last: float = -1.0  # the last moment the hand had rested a while
    _down_at: float | None = None  # a "down" waits a moment: a dropped arm leaves the picture
    _pending_from: tuple[float, float] = (0.0, 0.0)
    _pending_way: str = "down"
    _still_at: tuple[float, float] | None = None
    _still_since: float = 0.0
    _seen_since: float | None = None  # the hand has been in the picture since then
    _palm_size: float = 0.0  # the open hand that woke it
    _at: tuple[float, float] | None = None  # where the talking hand was last seen
    _low: float = 0.8  # while listening, a hand lower than this is resting
    _streak: tuple[str, float, float, float] | None = None  # a volume shape: (action, since, last seen, last step)

    @property
    def listening(self) -> bool:
        return self.listening_until > 0

    @property
    def low(self) -> float:
        """While listening, a hand lower than this in the picture is resting."""
        return self._low

    def look(self, now: float, hands: list[Hand]) -> list[str]:
        """One video frame. Returns the gestures to send (often none)."""
        s = self.settings
        out: list[str] = []
        # While it listens, "low" is measured from where the hand woke it: a camera close to the person (the Mac's own,
        # 24 Nov 2026) sees the talking hand near the bottom of the picture.
        low = self._low if self.listening else s.max_y
        near = [h for h in hands if h.size >= s.min_size and h.y <= low and h.shape != MOTION]
        # The model sometimes sees a second, smaller copy of the same hand: that copy must not act.
        near = [h for h in near if not any(o.size > h.size and _far((o.x, o.y), h) < 0.1 for o in near)]
        # The raised hand talks: the highest one in the picture. While it listens, the one nearest where the talking
        # hand was.
        if self.listening and self._at is not None:
            hand = min(near, key=lambda h: _far(self._at, h)) if near else None
        else:
            hand = min(near, key=lambda h: h.y) if near else None
        if hand is not None:
            self._at = (hand.x, hand.y)
        blur = next((h for h in hands if h.shape == MOTION), None) if hand is None else None

        if not self.listening:
            if hand and hand.shape == "Open_Palm" and hand.score >= s.wake_score:
                if self._palm_since is None or self._palm_at is None or _far(self._palm_at, hand) > s.still:
                    self._palm_since, self._palm_at = now, (hand.x, hand.y)
                elif now - self._palm_since >= s.wake_hold:
                    self._start(now)
                    self._palm_size = hand.size
                    self._low = min(1.0, max(s.max_y, hand.y + 0.3))
                    self._wake_y, self._wake_x = hand.y, hand.x
                    self.aim = None
                    out.append("ready")
            else:
                self._palm_since = self._palm_at = None
            return out

        if hand:
            self.listening_until = max(self.listening_until, now + s.keep)
        if now > self.listening_until:
            self._stop()
            return ["gone"]
        if blur is not None and self._seen_since is not None and self._trail and not self._need_drop:
            # The hand blurred in a swipe: follow the movement.
            return self._path(now, blur.x, blur.y, blurred=True)
        if hand and self._seen_since is None:
            self._seen_since = now
        if hand and self._need_drop and hand.y >= self._wake_y + 0.2:
            # Low near the body counts as down too: a camera close to the person still sees a dropped hand.
            if self._gone_since is None:
                self._gone_since = now
        elif hand:
            # One swipe per raise of the arm: after a swipe, the hand must go down before the next one.
            if self._need_drop and self._gone_since is not None and now - self._gone_since >= 0.25:
                self._need_drop = False
            if self._gone_since is not None and now - self._gone_since >= 1.0:
                self._origin = None  # a hand raised again: its new resting place is the middle
            self._gone_since = None
            if self._steady_at is None or _far(self._steady_at, hand) > 0.04:
                self._steady_at, self._steady_since = (hand.x, hand.y), now
            elif now - self._steady_since >= 0.4:
                self._steady_last = now
        elif self._gone_since is None:
            self._gone_since = now
        if not hand:
            # A fast swipe blurs, or leaves the picture at its side: the way it was going still counts.
            way = self._exit_swipe(now)
            self._seen_since = None
            self._trail.clear()
            self._shape = None
            self._back = None
            self._settling = False
            self._down_at = None
            if way:
                return self._swiped(now, way)
            return out
        if self._down_at is not None:
            if hand.y > 0.9 or hand.shape not in ("Open_Palm", "None"):  # falling out of the picture, or a thumb
                self._down_at = None
                self._trail.clear()
            elif now - self._down_at >= 0.25:
                self._down_at = None
                return self._swiped(now, self._pending_way)
            return out
        # After a swipe: one swipe is one step. No swipe counts until the hand stops (or leaves the picture), and the
        # way back counts only once the hand is back where the swipe started, or has rested 2 s.
        if self._back is not None:
            way, start = self._back
            pos = hand.x if way in ("left", "right") else hand.y
            if self._still_at is None or _far(self._still_at, hand) > 0.04:
                self._still_at, self._still_since = (hand.x, hand.y), now
            if self._settling and now - self._still_since >= 0.35:
                self._settling = False
                self._trail.clear()
            if abs(pos - start) <= 0.08 or now - self._still_since >= 2.0:
                self._back = None
                self._settling = False
                self._trail.clear()

        if s.airtap:
            self._aim_at(now, hand)
            tap = self._pinch(now, hand)
            if tap:
                return tap
        else:
            got = self._path(now, hand.x, hand.y)
            if got or self._down_at is not None:
                return got
        if now < self._rest_until:
            return out

        # A shape held still.
        action = SHAPES.get(hand.shape) if hand.score >= SURE.get(hand.shape, s.min_score) else None
        if s.airtap and action == "back" and (hand.score < 0.7 or now - self._tapped_at < 1.0):
            action = None  # in Air tap only a sure fist goes back: a pinch with the fingers curled looks like one
        if s.airtap and action != "back":
            # Air tap reads only two signs: the pinch taps (above), the fist goes back. Nothing else fires by accident
            # while the hand moves about (the Pointer test, 24 Nov 2026: a fist turning into a thumb was "Louder").
            action = None
        elif hand.shape == OK_SIGN:
            action = "ok"
        elif hand.point and not s.airtap and hand.y < self._wake_y + 0.2:
            # (A hand dropping to rest often looks like a finger pointing somewhere: low points count for nothing.)
            action = hand.point
            if action in ("left", "right") and s.mirror:
                action = "right" if action == "left" else "left"
        # A small closed hand the model cannot name ("None" at about 0.5): a fist. An open hand it cannot name is
        # bigger, and the model is surer of it (0.7 and more).
        if (action is None and not s.airtap and hand.shape == "None" and hand.score < 0.65 and self._palm_size
                and hand.size <= 0.65 * self._palm_size):
            action = "back"
        # The model often loses a shape for a frame or two ("None"): that keeps the shape it had.
        if action is None and self._shape is not None and now - self._shape_last <= 0.3:
            return out
        if action is not None:
            self._shape_last = now
        if action != self._shape or _far(self._shape_at, hand) > 1.5 * s.still:
            self._shape, self._shape_since, self._shape_at, self._shape_done_at = action, now, (hand.x, hand.y), None
            return out
        if action is None:
            return out
        steps = action in REPEATS or action in MOVES
        if steps and self._streak and self._streak[0] == action and now - self._streak[2] <= 1.0:
            # The same thumb, lost for a moment (or moved a little): it goes on from where it was, not a new step.
            self._shape_since, self._shape_done_at = self._streak[1], self._streak[3]
        if self._shape_done_at is None:
            # A point is held a little longer: a hand passing from one shape to another often points for a moment.
            if now - self._shape_since >= s.shape_hold + (0.15 if action in MOVES else 0.0):
                out.append(action)
                self._shape_done_at = now
                if action == "back":
                    self._back_at = now
                self.listening_until = max(self.listening_until, now + s.listen)
        elif (steps and now - self._shape_since >= s.shape_hold + (s.point_repeat_after if action in MOVES else s.repeat_after)
              and now - self._shape_done_at >= (s.point_repeat if action in MOVES else s.repeat)):
            out.append(action)
            self._shape_done_at = now
            self.listening_until = max(self.listening_until, now + s.listen)
        if steps and self._shape_done_at is not None:
            self._streak = (action, self._shape_since, now, self._shape_done_at)
        return out

    def _pinch(self, now: float, hand: Hand) -> list[str]:
        """Air tap: a quick pinch taps, as on Apple Vision Pro: thumb and index finger touch, after the hand was open.

        No holding, so it is quick and gentle. One pinch is one tap: the next needs the hand open again first (the
        Pointer test, 24 Nov 2026: a half-closed hand flickered in and out of the OK sign and tapped 15 times). A fist
        closing also brings the thumb to the finger: a fist, or a hand that shrinks like one, is no pinch."""
        # Measured against the person's own hand (the Pointer test, 12:41 PM: Radu's relaxed hand kept thumb and finger
        # at a gap of 0.22-0.29, under the old fixed 0.3, and a pinch seen from the front bottomed out at 0.12-0.15):
        # it taps when the gap falls clearly, to under 0.17 and under 0.6 of the most it was in the last 0.8 s.
        seen = self._pinch_seen
        seen.append((now, hand.gap, hand.size))
        while seen and now - seen[0][0] > 0.8:
            seen.popleft()
        # A fist closing also brings the thumb onto the index finger, before the model names the fist (the Pointer test,
        # 12:55 PM: four fists tapped). In a pinch the index finger stays out and the hand keeps its size.
        # And a hand opening from a fist that went back is no pinch either (the Pointer test, 1:03 PM: Back, then at
        # once a tap): the hand must be open a moment first.
        if (hand.curled or (hand.shape == "Closed_Fist" and hand.score >= 0.7) or now - self._back_at < 0.8
                or self._shape == "back" or hand.size < 0.78 * max(z for _, _, z in seen)):
            self._pinch_open = None
            return []
        if hand.gap >= 0.2:
            if self._pinch_open is None:
                self._pinch_open = now
            return []
        peak = max(g for _, g, _ in seen)
        if hand.gap < 0.17 and hand.gap <= 0.6 * peak and self._pinch_open is not None and now - self._pinch_open >= 0.15:
            self._pinch_open = None
            self._tapped_at = now
            # Tap where the hand was just before the fingers began to close.
            before = [a for a in self._aims if a[0] <= now - 0.25]
            self.tap_at = (before[-1][1], before[-1][2]) if before else self.aim
            self.listening_until = max(self.listening_until, now + self.settings.listen)
            return ["ok"]
        return []

    def _aim_at(self, now: float, hand: Hand) -> None:
        """Air tap: an open hand (not pinching, not pointing) is a shadow over the screen; the glow follows it."""
        s = self.settings
        if s.pointer and hand.pts and self.aim is not None:
            # The live hand follows every shape, the OK sign too: the person sees the tap coming.
            self.shape = tuple((-px if s.mirror else px, py) for px, py in hand.pts)
        peak = max((g for _, g, _ in self._pinch_seen), default=0.0)
        if (hand.shape in SHAPES or hand.shape == OK_SIGN or hand.gap < 0.17 or (peak >= 0.2 and hand.gap < 0.8 * peak)
                or now - self._tapped_at < 0.3):
            # A sign, or a pinch beginning to close (the gap falls from where it was), or just tapped: the glow stays
            # where the tap is meant. Radu's pinch comes with a little push toward the camera that shifts the hand.
            return
        hx, hy = hand.anchor or (hand.x, hand.y)
        if self._origin is None:
            # The middle of the screen is where the hand first rests (0.3 s): people often wake it low, then raise
            # the hand to aim (the Mac test, 24 Nov 2026: woken at 0.87, aimed at 0.56-0.73, and the glow sat on the
            # top row). Until then, no glow.
            if self._steady_at is None or now - self._steady_since < 0.3:
                return
            self._origin = (hx, hy)
        dx = hx - self._origin[0]
        if s.mirror:
            dx = -dx
        # A little inside the screen's edge, so a corner button is under the hand, not half past it.
        x = min(0.985, max(0.015, 0.5 + dx / (2 * s.reach)))
        y = min(0.985, max(0.015, 0.5 + (hy - self._origin[1]) / (2 * s.reach)))
        if self.aim is not None and self._aims:
            # The One Euro filter (Casiez, Roussel, Vogel 2012), the usual one for pointers: steady when the hand
            # holds still (a small button stays lit), quick when it moves (no lag that catches up in steps: the
            # Pointer test, 1:02 PM, "a bit jumpy").
            dt = max(now - self._aims[-1][0], 1e-3)

            def alpha(cutoff: float) -> float:
                tau = 1.0 / (2 * math.pi * cutoff)
                return 1.0 / (1.0 + tau / dt)

            vx, vy = (x - self.aim[0]) / dt, (y - self.aim[1]) / dt
            a = alpha(1.0)
            self._aim_speed = (self._aim_speed[0] + a * (vx - self._aim_speed[0]), self._aim_speed[1] + a * (vy - self._aim_speed[1]))
            speed = math.hypot(*self._aim_speed)
            a = alpha(1.0 + 6.0 * speed)
            x, y = self.aim[0] + a * (x - self.aim[0]), self.aim[1] + a * (y - self.aim[1])
        else:
            self._aim_speed = (0.0, 0.0)
        self.aim = (x, y)
        self._aims.append((now, x, y))
        if s.pointer and hand.pts:
            self.shape = tuple((-px if s.mirror else px, py) for px, py in hand.pts)
        while self._aims and now - self._aims[0][0] > 1.5:
            self._aims.popleft()

    def _path(self, now: float, x: float, y: float, blurred: bool = False) -> list[str]:
        """The hand's path over the last moment: a swipe, or nothing yet."""
        s = self.settings
        out: list[str] = []
        # A swipe: the hand's path over the last moment.
        self._trail.append((now, x, y))
        while self._trail and now - self._trail[0][0] > s.swipe_time:
            self._trail.popleft()
        # A hand that just came into the picture (raised again from the counter) is arriving, not swiping.
        if now >= self._rest_until and len(self._trail) >= 3 and self._trail[0][0] - (self._seen_since or now) >= 0.25:
            _, x0, y0 = self._trail[0]
            dx, dy = x - x0, y - y0
            way = None
            if abs(dx) >= s.swipe and abs(dx) > 1.8 * abs(dy):
                way = "left" if (dx > 0) == s.mirror else "right"
            elif abs(dy) >= s.swipe and abs(dy) > 1.8 * abs(dx):
                way = "down" if dy > 0 else "up"
            if way and self._back is not None and (self._settling or way == OPPOSITE[self._back[0]]):
                way = None
            if self._need_drop:
                way = None
            # An arm being raised or dropped moves up or down too: an up or down starts only from a hand that rested.
            if way in ("up", "down") and (not s.vertical or self._steady_last < self._trail[0][0] - 0.3):
                way = None
            # A blur only carries a side swipe, and only one that stays at the hand's height: an arm falling to the
            # counter blurs too, and drifts to one side as it falls.
            if blurred and (way not in ("left", "right") or y - y0 > 0.08 or y > self._low - 0.1):
                way = None
            if way in ("up", "down"):
                # Raising a thumb, or dropping the arm, also moves the hand up or down: it counts only when an open
                # hand is still there a moment later.
                self._down_at = now
                self._pending_from = (x0, y0)
                self._pending_way = way
                return out
            if way:
                self._back = (way, x0 if way in ("left", "right") else y0)
                return self._swiped(now, way)
        return out

    def _exit_swipe(self, now: float) -> str | None:
        """The hand was lost: a side swipe that had gone most of the way, fast, toward the side it left by."""
        s = self.settings
        if now < self._rest_until or len(self._trail) < 2 or self._seen_since is None or self._need_drop:
            return None
        t0, x0, y0 = self._trail[0]
        _, x1, y1 = self._trail[-1]
        dx, dy = x1 - x0, y1 - y0
        if t0 - self._seen_since < 0.25 or abs(dx) < 0.6 * s.swipe or abs(dx) < 1.8 * abs(dy):
            return None
        # Only out at the side it was going to: a hand dropped to the counter is not a swipe.
        if (dx > 0 and x1 < 0.75) or (dx < 0 and x1 > 0.25):
            return None
        way = "left" if (dx > 0) == s.mirror else "right"
        if self._back is not None and (self._settling or way == OPPOSITE[self._back[0]]):
            return None
        return way

    def _swiped(self, now: float, way: str) -> list[str]:
        if way in ("up", "down"):
            self._back = (way, self._pending_from[1])
        self._settling = True
        self._need_drop = True
        self._rest_until = now + self.settings.swipe_rest
        self._still_at = None
        self._trail.clear()
        self._shape = None
        self.listening_until = now + self.settings.listen
        return [way]

    def _start(self, now: float) -> None:
        self.listening_until = now + self.settings.listen
        self._palm_since = self._palm_at = None
        self._trail.clear()
        self._shape = None
        # The open hand that woke it may drift: give it a moment before a swipe counts.
        self._rest_until = now + 0.4

    def _stop(self) -> None:
        self.listening_until = 0.0
        self._trail.clear()
        self._shape = None
        self._palm_since = self._palm_at = None
        self._back = None
        self._settling = False
        self._need_drop = False
        self._gone_since = None
        self._steady_at = None
        self._steady_last = -1.0
        self._down_at = None
        self._palm_size = 0.0
        self._streak = None
        self._at = None
        self.aim = None
        self.shape = None
        self._origin = None


OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}


def _far(at: tuple[float, float], hand: Hand) -> float:
    return max(abs(hand.x - at[0]), abs(hand.y - at[1]))


# ---- From the hand model's 21 points to a Hand (the Mac tracker and Home Assistant share this) ----
# Points: 0 wrist, 4 thumb tip, 5-8 index (knuckle to tip), 9-12 middle, 13-16 ring, 17-20 little finger. Each point
# is (x, y) from 0 to 1 across and down the picture; w and h are the picture's size in pixels.


def curled_of(p: list[tuple[float, float]]) -> tuple[bool, bool]:
    """(index folded into the palm, all four fingers folded), from points in pixels. A finger is folded when its tip is
    nearer the wrist than its middle joint. For the index alone the test is deeper, its tip nearer the wrist than its
    knuckle: in a pinch the index bends to meet the thumb, but its tip stays out past the knuckle (the Pointer test,
    24 Nov 2026, 1:00 PM: pinches stopped tapping when the bent index counted as folded)."""

    def d(a: int, b: int) -> float:
        return math.dist(p[a], p[b])

    folded = [d(0, tip) < d(0, tip - 2) for tip in (8, 12, 16, 20)]
    return d(0, 8) < d(0, 5), all(folded)


def sign_of(p: list[tuple[float, float]]) -> tuple[str | None, bool, float]:
    """Where the index finger points in the picture (or None), whether it is the OK sign, and the thumb-to-index gap
    in hand sizes, from points in pixels. MediaPipe names only "pointing up"; the hand's 21 points tell the rest."""

    def d(a: int, b: int) -> float:
        return math.dist(p[a], p[b])

    size = max(max(q[0] for q in p) - min(q[0] for q in p), max(q[1] for q in p) - min(q[1] for q in p)) or 1.0
    # A finger is out when its tip is farther from the wrist than its middle joint.
    out = {f: d(0, tip) > 1.1 * d(0, tip - 2) for f, tip in (("index", 8), ("middle", 12), ("ring", 16), ("little", 20))}
    gap = d(4, 8) / size
    ok = gap < 0.18 and out["middle"] and out["ring"] and out["little"]
    if ok:
        return None, True, gap
    if not out["index"] or out["middle"] or out["ring"] or out["little"]:
        return None, False, gap
    vx, vy = p[8][0] - p[5][0], p[8][1] - p[5][1]
    if math.hypot(vx, vy) < 0.3 * size:
        return None, False, gap
    if abs(vx) > 1.3 * abs(vy):
        return ("right" if vx > 0 else "left"), False, gap
    if abs(vy) > 1.3 * abs(vx):
        return ("down" if vy > 0 else "up"), False, gap
    return None, False, gap


def hand_of(points: list[tuple[float, float]], shape: str, score: float, w: int, h: int) -> Hand:
    """One hand from the model: its 21 points (0-1 in the picture), the model's name for the shape, and its score."""
    xs, ys = [q[0] for q in points], [q[1] for q in points]
    size = max((max(xs) - min(xs)) * w, (max(ys) - min(ys)) * h) / h
    px = [(x * w, y * h) for x, y in points]
    point, ok, gap = sign_of(px)
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    unit = max(size * h, 1.0)
    if ok:
        shape = OK_SIGN
    elif point and shape in ("Thumb_Up", "Thumb_Down", "Closed_Fist"):
        point = None  # the model's own name wins for these
    anchor = (sum(points[i][0] for i in (0, 5, 9, 13, 17)) / 5, sum(points[i][1] for i in (0, 5, 9, 13, 17)) / 5)
    index_in, fist = curled_of(px)
    if fist and not ok:
        # All four fingers folded is a fist, whatever the model's own name (it says "None" or a weak fist at first).
        shape, score = "Closed_Fist", max(score, 0.8)
    # The live hand is drawn around the palm (wrist and knuckles), so it stays put while the fingers close.
    ax, ay = anchor
    pts = tuple((round((x - ax) * w / unit, 3), round((y - ay) * h / unit, 3)) for x, y in points)
    return Hand(shape, cx, cy, size, score, point=point, gap=gap, pts=pts, anchor=anchor, curled=index_in)
