from datetime import datetime

from pydantic import BaseModel, ConfigDict


# -----------------------------
# Meeting schemas
# -----------------------------

class MeetingBase(BaseModel):
    title: str
    meeting_date: datetime
    duration_seconds: int = 0
    participants: str = ""
    summary: str = ""


class MeetingCreate(MeetingBase):
    pass


class MeetingUpdate(BaseModel):
    title: str | None = None
    meeting_date: datetime | None = None
    duration_seconds: int | None = None
    participants: str | None = None
    summary: str | None = None


class MeetingResponse(MeetingBase):
    id: int
    created_at: datetime
    video_url: str | None = None

    model_config = ConfigDict(from_attributes=True)


# -----------------------------
# Transcript schemas
# -----------------------------

class TranscriptLineBase(BaseModel):
    speaker: str
    text: str
    start_time: int = 0


class TranscriptLineCreate(TranscriptLineBase):
    pass


class TranscriptLineResponse(TranscriptLineBase):
    id: int
    meeting_id: int

    model_config = ConfigDict(from_attributes=True)


# -----------------------------
# Action item schemas
# -----------------------------

class ActionItemBase(BaseModel):
    text: str


class ActionItemCreate(ActionItemBase):
    pass


class ActionItemUpdate(BaseModel):
    text: str | None = None
    completed: int | None = None


class ActionItemResponse(ActionItemBase):
    id: int
    meeting_id: int
    completed: int

    model_config = ConfigDict(from_attributes=True)