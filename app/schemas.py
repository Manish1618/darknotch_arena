import re
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, StringConstraints, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{2,40}$")


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


USER_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


Code = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{6}$")]  # the 6 digits from the email


class EmailIn(Strict):
    email: EmailStr


class RegisterIn(Strict):
    email: EmailStr
    username: str
    password: str = Field(min_length=10, max_length=128)

    @field_validator("username")
    @classmethod
    def _user(cls, v):
        if not USER_RE.match(v):
            raise ValueError("use 3-32 letters, digits or underscores")
        return v


class LoginIn(Strict):
    email: EmailStr
    password: str = Field(max_length=128)
    totp: str | None = Field(default=None, max_length=16)  # app code or a recovery code


class TotpIn(Strict):
    code: str = Field(max_length=16)


class TotpOffIn(Strict):
    password: str = Field(max_length=128)
    code: str = Field(max_length=16)


class PasswordIn(Strict):
    current: str = Field(max_length=128)
    new: str = Field(min_length=10, max_length=128)


class ResetIn(Strict):
    email: EmailStr
    code: Code
    password: str = Field(min_length=10, max_length=128)


class TicketIn(Strict):
    ticket: str = Field(min_length=20, max_length=100)
    totp: str = Field(max_length=16)  # app code or recovery code


class SiteIn(Strict):
    registration_open: bool
    block_disposable: bool
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, v):
        from .site import DOMAIN_RE
        out = []
        for d in v:
            d = d.strip().lower().lstrip("@")
            if not d:
                continue
            if not DOMAIN_RE.match(d):
                raise ValueError(f"{d!r} isn't a domain name like college.edu")
            out.append(d)
        return sorted(set(out))


class StepUpIn(Strict):
    code: str = Field(default="", max_length=16)  # the admin's own 2FA code


class EventStatusIn(Strict):
    status: str = Field(pattern=r"^(draft|published|archived)$")


class BanIn(Strict):
    reason: str = Field(min_length=3, max_length=200)


class VisibilityIn(Strict):
    scoreboard: str = Field(pattern=r"^(public|players|organisers)$")
    graph: str = Field(pattern=r"^(public|players|organisers)$")
    feed: str = Field(pattern=r"^(public|players|organisers)$")
    challenge_stats: str = Field(pattern=r"^(public|players|organisers)$")
    solvers: str = Field(pattern=r"^(public|players|organisers)$")
    teams: str = Field(pattern=r"^(public|players|organisers)$")
    matrix: str = Field(pattern=r"^(public|players|organisers)$")


class OrgIn(Strict):
    name: str = Field(min_length=2, max_length=80)
    slug: str

    @field_validator("slug")
    @classmethod
    def _slug(cls, v):
        if not SLUG_RE.match(v):
            raise ValueError("slug: 3-40 chars, lowercase letters, digits, dashes")
        return v


class MemberIn(Strict):
    email: EmailStr
    role: str = Field(pattern=r"^(admin)$")  # only one owner; owner is the creator


class EventIn(Strict):
    slug: str
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(default="", max_length=10000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    max_team_size: int = Field(default=4, ge=1, le=10)
    team_mode: str = Field(default="team", pattern=r"^(team|solo)$")

    @field_validator("slug")
    @classmethod
    def _slug(cls, v):
        if not SLUG_RE.match(v):
            raise ValueError("slug: 3-40 chars, lowercase letters, digits, dashes")
        return v


class EventPatch(Strict):
    title: str | None = Field(default=None, min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=10000)
    status: str | None = Field(default=None, pattern=r"^(draft|published|archived)$")
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    registration_open: bool | None = None
    max_team_size: int | None = Field(default=None, ge=1, le=10)
    team_mode: str | None = Field(default=None, pattern=r"^(team|solo)$")
    paused: bool | None = None
    freeze_at: datetime | None = None
    first_blood_bonus: int | None = Field(default=None, ge=0, le=1000)
    lockout_threshold: int | None = Field(default=None, ge=0, le=100)
    lockout_minutes: int | None = Field(default=None, ge=1, le=1440)
    wrong_penalty: int | None = Field(default=None, ge=0, le=10000)


class TeamIn(Strict):
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError("use 2-40 letters, digits, spaces, dots, dashes or underscores")
        return v


class JoinIn(Strict):
    code: str = Field(min_length=8, max_length=32)


# --- challenges ------------------------------------------------------------

def _check_slug(v):
    if not SLUG_RE.match(v):
        raise ValueError("slug: 3-40 chars, lowercase letters, digits, dashes")
    return v


class FlagIn(Strict):
    flag: str = Field(min_length=1, max_length=256)
    case_insensitive: bool = False


class HintIn(Strict):
    content: str = Field(min_length=1, max_length=5000)
    cost: int = Field(default=0, ge=0, le=100000)


class ChallengeIn(Strict):
    slug: str
    name: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=20000)
    value: int = Field(ge=1, le=100000)
    scoring: str = Field(default="static", pattern=r"^(static|dynamic)$")
    minimum: int | None = Field(default=None, ge=0, le=100000)
    decay: int | None = Field(default=None, ge=1, le=100000)
    state: str = Field(default="hidden", pattern=r"^(hidden|visible)$")
    max_attempts: int = Field(default=0, ge=0, le=1000)
    requires: list[str] = Field(default_factory=list, max_length=20)
    flags: list[FlagIn] = Field(min_length=1, max_length=20)
    hints: list[HintIn] = Field(default_factory=list, max_length=10)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v):
        return _check_slug(v)

    @model_validator(mode="after")
    def _scoring(self):
        if self.scoring == "dynamic":
            if self.minimum is None or self.decay is None:
                raise ValueError("dynamic scoring needs minimum and decay")
            if self.minimum > self.value:
                raise ValueError("minimum can't be above the starting value")
        if self.slug in self.requires:
            raise ValueError("a challenge can't require itself")
        return self


class ChallengePatch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    scoring: str | None = Field(default=None, pattern=r"^(static|dynamic)$")
    category: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=20000)
    value: int | None = Field(default=None, ge=1, le=100000)
    minimum: int | None = Field(default=None, ge=0, le=100000)
    decay: int | None = Field(default=None, ge=1, le=100000)
    state: str | None = Field(default=None, pattern=r"^(hidden|visible)$")
    max_attempts: int | None = Field(default=None, ge=0, le=1000)
    requires: list[str] | None = Field(default=None, max_length=20)


class AttemptIn(Strict):
    flag: str = Field(min_length=1, max_length=256)


class HintPatch(Strict):
    content: str | None = Field(default=None, min_length=1, max_length=5000)
    cost: int | None = Field(default=None, ge=0, le=100000)


class AwardIn(Strict):
    team_id: int
    points: int = Field(ge=-100000, le=100000)
    reason: str = Field(min_length=3, max_length=200)

    @field_validator("points")
    @classmethod
    def _nonzero(cls, v):
        if v == 0:
            raise ValueError("use a positive number to add points or a negative one to deduct")
        return v


class AnnouncementIn(Strict):
    text: str = Field(min_length=1, max_length=500)
