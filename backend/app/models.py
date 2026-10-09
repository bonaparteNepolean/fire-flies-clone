from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from .database import Base


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    meeting_date = Column(DateTime, nullable=False)
    duration_seconds = Column(Integer, default=0)
    participants = Column(Text, default="")
    summary = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    # Uploaded recording filename
    video_filename = Column(String(255), nullable=True)

    @property
    def video_url(self):
        if not self.video_filename:
            return None

        return f"/uploads/{self.video_filename}"


class TranscriptLine(Base):
    __tablename__ = "transcript_lines"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(
        Integer,
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    speaker = Column(String(255), nullable=False)
    text = Column(Text, nullable=False)
    start_time = Column(Integer, default=0)


class ActionItem(Base):
    __tablename__ = "action_items"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(
        Integer,
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    text = Column(Text, nullable=False)
    completed = Column(Integer, default=0)