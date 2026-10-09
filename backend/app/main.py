from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from .database import Base, SessionLocal, engine
from .models import ActionItem, Meeting, TranscriptLine
from .schemas import (
    ActionItemCreate,
    ActionItemResponse,
    ActionItemUpdate,
    MeetingCreate,
    MeetingResponse,
    MeetingUpdate,
    TranscriptLineCreate,
    TranscriptLineResponse,
)


# --------------------------------------------------
# Setup
# --------------------------------------------------

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

Base.metadata.create_all(bind=engine)


# --------------------------------------------------
# Database migration for existing SQLite database
# --------------------------------------------------

inspector = inspect(engine)
existing_tables = inspector.get_table_names()

if "meetings" in existing_tables:
    columns = [column["name"] for column in inspector.get_columns("meetings")]

    if "video_filename" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE meetings ADD COLUMN video_filename VARCHAR(255)")
            )


# --------------------------------------------------
# FastAPI app
# --------------------------------------------------

app = FastAPI(title="Fireflies Clone API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


# --------------------------------------------------
# Helpers
# --------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
TRANSCRIPT_EXTENSIONS = {".txt", ".srt", ".vtt"}

TIMESTAMP_TEXT = r"(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?"
TIMESTAMP_RE = re.compile(TIMESTAMP_TEXT)

# Recognizes standard SRT/VTT timing cues.
TIMED_CUE_RE = re.compile(
    rf"(?m)^\s*{TIMESTAMP_TEXT}\s*-->"
)

# Recognizes timestamped text such as:
# 00:12 Speaker: Hello
# [00:12] Speaker: Hello
# (00:12) Hello
INLINE_TIMESTAMP_RE = re.compile(
    rf"^\s*[\[(]?\s*(?P<timestamp>{TIMESTAMP_TEXT})\s*[\])]?[\s:|–—-]+(?P<text>.*)$"
)


def get_meeting_or_404(db, meeting_id: int):
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()

    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    return meeting


def timestamp_to_seconds(value: str) -> int:
    """Convert HH:MM:SS or MM:SS timestamps to whole seconds."""
    normalized = value.strip().replace(",", ".")

    clock, dot, fraction = normalized.partition(".")
    parts = [int(part) for part in clock.split(":")]

    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        return 0

    milliseconds = 0

    if dot and fraction:
        milliseconds = int((fraction + "000")[:3])

    total_seconds = hours * 3600 + minutes * 60 + seconds

    return max(0, total_seconds + (milliseconds // 1000))


def clean_transcript_text(value: str) -> str:
    """Remove common subtitle markup while retaining visible text."""
    value = re.sub(r"</?c(?:\.[^>]*)?>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"</?i>|</?b>|</?u>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)

    return value.strip()


def speaker_and_text(lines: list[str]) -> tuple[str, str] | None:
    """Extract a speaker and spoken text from transcript lines."""
    cleaned_lines = [clean_transcript_text(line) for line in lines]
    cleaned_lines = [line for line in cleaned_lines if line]

    if not cleaned_lines:
        return None

    # WebVTT speaker markup: <v Alex Smith>Hello</v>
    for original in lines:
        voice_match = re.search(
            r"<v\s+([^>]+)>(.*?)</v>",
            original,
            flags=re.IGNORECASE,
        )

        if voice_match:
            speaker = voice_match.group(1).strip() or "Speaker 1"
            spoken_text = clean_transcript_text(voice_match.group(2))

            if spoken_text:
                return speaker, spoken_text

    combined = " ".join(cleaned_lines).strip()

    # Common diarized transcript format: "Alex Smith: Let's begin."
    label_match = re.match(r"^([^:]{1,60}):\s*(.+)$", combined)

    if label_match:
        candidate = label_match.group(1).strip()
        spoken_text = label_match.group(2).strip()

        if candidate and spoken_text and " --> " not in combined:
            return candidate, spoken_text

    # A transcript without speaker labels remains usable.
    return "Speaker 1", combined


def parse_transcript(
    content: str,
    extension: str,
) -> list[dict[str, object]]:
    """
    Parse TXT, SRT, or VTT into transcript rows.

    Timestamps are taken from the source. Plain text lines without timestamps
    receive start_time=0, but the upload route rejects an entirely untimed
    transcript to avoid presenting it as synchronized with the video.

    Speaker names are read from "Name: text" lines or WebVTT <v Name> tags.
    """
    content = (
        content.lstrip("\ufeff")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    records: list[dict[str, object]] = []

    if not content.strip():
        return records

    # SRT/VTT files are naturally grouped into cues separated by blank lines.
    if extension in {".srt", ".vtt"} or "-->" in content:
        blocks = re.split(r"\n\s*\n", content.strip())

        for block in blocks:
            lines = [
                line.strip()
                for line in block.split("\n")
                if line.strip()
            ]

            if not lines:
                continue

            if lines[0].upper().startswith("WEBVTT"):
                lines = lines[1:]

            if not lines:
                continue

            timing_index = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if "-->" in line
                ),
                None,
            )

            if timing_index is not None:
                timestamp_match = TIMESTAMP_RE.search(
                    lines[timing_index]
                )

                if not timestamp_match:
                    continue

                start_time = timestamp_to_seconds(
                    timestamp_match.group(0)
                )

                parsed = speaker_and_text(lines[timing_index + 1 :])

                if parsed:
                    speaker, spoken_text = parsed

                    records.append(
                        {
                            "speaker": speaker,
                            "text": spoken_text,
                            "start_time": start_time,
                        }
                    )

                continue

            # Also accept timestamped lines inside a TXT-like cue block.
            for line in lines:
                if line.isdigit() or line.upper().startswith("WEBVTT"):
                    continue

                inline_match = INLINE_TIMESTAMP_RE.match(line)

                if inline_match:
                    start_time = timestamp_to_seconds(
                        inline_match.group("timestamp")
                    )

                    parsed = speaker_and_text(
                        [inline_match.group("text")]
                    )

                    if parsed:
                        speaker, spoken_text = parsed

                        records.append(
                            {
                                "speaker": speaker,
                                "text": spoken_text,
                                "start_time": start_time,
                            }
                        )
                else:
                    parsed = speaker_and_text([line])

                    if parsed:
                        speaker, spoken_text = parsed

                        records.append(
                            {
                                "speaker": speaker,
                                "text": spoken_text,
                                "start_time": 0,
                            }
                        )

        return records

    # TXT: supports timestamped lines or a standalone timestamp followed
    # by a speaker/text line.
    pending_timestamp = 0

    for raw_line in content.split("\n"):
        line = raw_line.strip()

        if not line or line.upper().startswith("WEBVTT"):
            continue

        # Ignore numbered lines often found in exported subtitles.
        if line.isdigit():
            continue

        stripped_clock = line.strip("[]() ")

        if TIMESTAMP_RE.fullmatch(stripped_clock):
            pending_timestamp = timestamp_to_seconds(stripped_clock)
            continue

        inline_match = INLINE_TIMESTAMP_RE.match(line)

        if inline_match:
            start_time = timestamp_to_seconds(
                inline_match.group("timestamp")
            )

            parsed = speaker_and_text(
                [inline_match.group("text")]
            )

            pending_timestamp = 0
        else:
            start_time = pending_timestamp
            parsed = speaker_and_text([line])
            pending_timestamp = 0

        if parsed:
            speaker, spoken_text = parsed

            records.append(
                {
                    "speaker": speaker,
                    "text": spoken_text,
                    "start_time": start_time,
                }
            )

    return records


def extract_video_duration(video_path: Path) -> int:
    """Read video duration with ffprobe when installed; return 0 otherwise."""
    ffprobe_path = shutil.which("ffprobe")

    if not ffprobe_path:
        return 0

    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )

        return max(0, int(round(float(result.stdout.strip()))))

    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def meeting_title_from_filename(filename: str | None) -> str:
    stem = Path(filename or "").stem
    title = re.sub(r"[_-]+", " ", stem).strip()

    return title or "Untitled Meeting"


# --------------------------------------------------
# Basic routes
# --------------------------------------------------

@app.get("/")
def root():
    return {"message": "Fireflies Clone API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


# --------------------------------------------------
# Meetings
# --------------------------------------------------

@app.get("/meetings", response_model=list[MeetingResponse])
def get_meetings():
    db = SessionLocal()

    try:
        return db.query(Meeting).order_by(
            Meeting.meeting_date.desc()
        ).all()
    finally:
        db.close()


@app.get("/meetings/{meeting_id}", response_model=MeetingResponse)
def get_meeting(meeting_id: int):
    db = SessionLocal()

    try:
        return get_meeting_or_404(db, meeting_id)
    finally:
        db.close()


@app.post("/meetings", response_model=MeetingResponse)
def create_meeting(meeting: MeetingCreate):
    db = SessionLocal()

    try:
        new_meeting = Meeting(
            title=meeting.title,
            meeting_date=meeting.meeting_date,
            duration_seconds=meeting.duration_seconds,
            participants=meeting.participants,
            summary=meeting.summary,
        )

        db.add(new_meeting)
        db.commit()
        db.refresh(new_meeting)

        return new_meeting
    finally:
        db.close()


@app.post("/meetings/upload", response_model=MeetingResponse)
async def create_meeting_with_upload(
    video: UploadFile = File(...),
    transcript: UploadFile = File(...),
    duration_seconds: int = Form(0),
):
    """Create a meeting by uploading a video and a transcript."""
    video_name = video.filename or ""
    transcript_name = transcript.filename or ""

    video_extension = Path(video_name).suffix.lower()
    transcript_extension = Path(transcript_name).suffix.lower()

    if video_extension not in VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported video format. "
                "Allowed formats: mp4, webm, mov, m4v, mkv."
            ),
        )

    if transcript_extension not in TRANSCRIPT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported transcript format. "
                "Allowed formats: txt, srt, vtt."
            ),
        )

    try:
        transcript_bytes = await transcript.read()

        try:
            transcript_content = transcript_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="Transcript must be a UTF-8 text file.",
            ) from exc

        transcript_records = parse_transcript(
            transcript_content,
            transcript_extension,
        )

        if not transcript_records:
            raise HTTPException(
                status_code=400,
                detail="No readable transcript lines were found in the uploaded file.",
            )

        # --------------------------------------------------
        # NEW: Validate that the transcript includes timestamps
        # --------------------------------------------------

        has_timestamped_cues = bool(
            TIMED_CUE_RE.search(transcript_content)
        )

        has_timestamped_txt_lines = any(
            INLINE_TIMESTAMP_RE.match(line.strip())
            or TIMESTAMP_RE.fullmatch(line.strip("[]() "))
            for line in transcript_content.splitlines()
            if line.strip()
        )

        if not has_timestamped_cues and not has_timestamped_txt_lines:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This transcript does not contain timestamps. "
                    "Upload a timestamped .txt file or an SRT/VTT subtitle "
                    "file to synchronize it with the video."
                ),
            )

        # --------------------------------------------------
        # Save the uploaded video
        # --------------------------------------------------

        generated_filename = f"{uuid4().hex}{video_extension}"
        video_path = UPLOAD_DIR / generated_filename

        try:
            with video_path.open("wb") as destination:
                while True:
                    chunk = await video.read(1024 * 1024)

                    if not chunk:
                        break

                    destination.write(chunk)

        except Exception as exc:
            if video_path.exists():
                video_path.unlink()

            raise HTTPException(
                status_code=500,
                detail="Failed to save uploaded video.",
            ) from exc

        if not video_path.exists() or video_path.stat().st_size == 0:
            if video_path.exists():
                video_path.unlink()

            raise HTTPException(
                status_code=400,
                detail="The uploaded video is empty.",
            )

        # --------------------------------------------------
        # Determine meeting duration
        # --------------------------------------------------

        detected_duration = extract_video_duration(video_path)

        latest_transcript_time = max(
            int(record["start_time"])
            for record in transcript_records
        )

        final_duration = max(
            detected_duration,
            max(0, duration_seconds),
            latest_transcript_time,
        )

        speaker_names = list(
            dict.fromkeys(
                str(record["speaker"])
                for record in transcript_records
            )
        )

        # --------------------------------------------------
        # Save meeting and transcript records to the database
        # --------------------------------------------------

        db = SessionLocal()

        try:
            new_meeting = Meeting(
                title=meeting_title_from_filename(video_name),
                meeting_date=datetime.now().replace(microsecond=0),
                duration_seconds=final_duration,
                participants=", ".join(speaker_names),
                summary="",
                video_filename=generated_filename,
            )

            db.add(new_meeting)
            db.flush()

            for record in transcript_records:
                db.add(
                    TranscriptLine(
                        meeting_id=new_meeting.id,
                        speaker=str(record["speaker"]),
                        text=str(record["text"]),
                        start_time=int(record["start_time"]),
                    )
                )

            db.commit()
            db.refresh(new_meeting)

            return new_meeting

        except Exception:
            db.rollback()

            if video_path.exists():
                video_path.unlink()

            raise

        finally:
            db.close()

    finally:
        await video.close()
        await transcript.close()


@app.put("/meetings/{meeting_id}", response_model=MeetingResponse)
def update_meeting(meeting_id: int, meeting: MeetingUpdate):
    db = SessionLocal()

    try:
        existing = get_meeting_or_404(db, meeting_id)
        update_data = meeting.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(existing, field, value)

        db.commit()
        db.refresh(existing)

        return existing
    finally:
        db.close()


@app.delete("/meetings/{meeting_id}")
def delete_meeting(meeting_id: int):
    db = SessionLocal()

    try:
        meeting = get_meeting_or_404(db, meeting_id)

        db.query(TranscriptLine).filter(
            TranscriptLine.meeting_id == meeting_id
        ).delete(synchronize_session=False)

        db.query(ActionItem).filter(
            ActionItem.meeting_id == meeting_id
        ).delete(synchronize_session=False)

        if meeting.video_filename:
            video_path = UPLOAD_DIR / meeting.video_filename

            if video_path.exists():
                video_path.unlink()

        db.delete(meeting)
        db.commit()

        return {"message": "Meeting deleted successfully"}

    finally:
        db.close()


# --------------------------------------------------
# Transcript
# --------------------------------------------------

@app.get(
    "/meetings/{meeting_id}/transcript",
    response_model=list[TranscriptLineResponse],
)
def get_transcript(meeting_id: int):
    db = SessionLocal()

    try:
        get_meeting_or_404(db, meeting_id)

        return (
            db.query(TranscriptLine)
            .filter(TranscriptLine.meeting_id == meeting_id)
            .order_by(
                TranscriptLine.start_time.asc(),
                TranscriptLine.id.asc(),
            )
            .all()
        )

    finally:
        db.close()


@app.post(
    "/meetings/{meeting_id}/transcript",
    response_model=TranscriptLineResponse,
)
def create_transcript_line(
    meeting_id: int,
    transcript: TranscriptLineCreate,
):
    db = SessionLocal()

    try:
        get_meeting_or_404(db, meeting_id)

        line = TranscriptLine(
            meeting_id=meeting_id,
            speaker=transcript.speaker,
            text=transcript.text,
            start_time=transcript.start_time,
        )

        db.add(line)
        db.commit()
        db.refresh(line)

        return line

    finally:
        db.close()


@app.put(
    "/meetings/{meeting_id}/transcript/{line_id}",
    response_model=TranscriptLineResponse,
)
def update_transcript_line(
    meeting_id: int,
    line_id: int,
    transcript: TranscriptLineCreate,
):
    db = SessionLocal()

    try:
        get_meeting_or_404(db, meeting_id)

        line = (
            db.query(TranscriptLine)
            .filter(
                TranscriptLine.id == line_id,
                TranscriptLine.meeting_id == meeting_id,
            )
            .first()
        )

        if not line:
            raise HTTPException(
                status_code=404,
                detail="Transcript line not found",
            )

        line.speaker = transcript.speaker
        line.text = transcript.text
        line.start_time = transcript.start_time

        db.commit()
        db.refresh(line)

        return line

    finally:
        db.close()


@app.delete("/meetings/{meeting_id}/transcript/{line_id}")
def delete_transcript_line(
    meeting_id: int,
    line_id: int,
):
    db = SessionLocal()

    try:
        get_meeting_or_404(db, meeting_id)

        line = (
            db.query(TranscriptLine)
            .filter(
                TranscriptLine.id == line_id,
                TranscriptLine.meeting_id == meeting_id,
            )
            .first()
        )

        if not line:
            raise HTTPException(
                status_code=404,
                detail="Transcript line not found",
            )

        db.delete(line)
        db.commit()

        return {"message": "Transcript line deleted successfully"}

    finally:
        db.close()


# --------------------------------------------------
# Action items
# --------------------------------------------------

@app.get(
    "/meetings/{meeting_id}/action-items",
    response_model=list[ActionItemResponse],
)
def get_action_items(meeting_id: int):
    db = SessionLocal()

    try:
        get_meeting_or_404(db, meeting_id)

        return (
            db.query(ActionItem)
            .filter(ActionItem.meeting_id == meeting_id)
            .order_by(ActionItem.id.asc())
            .all()
        )

    finally:
        db.close()


@app.post(
    "/meetings/{meeting_id}/action-items",
    response_model=ActionItemResponse,
)
def create_action_item(
    meeting_id: int,
    action_item: ActionItemCreate,
):
    db = SessionLocal()

    try:
        get_meeting_or_404(db, meeting_id)

        item = ActionItem(
            meeting_id=meeting_id,
            text=action_item.text,
            completed=0,
        )

        db.add(item)
        db.commit()
        db.refresh(item)

        return item

    finally:
        db.close()


@app.put(
    "/action-items/{action_item_id}",
    response_model=ActionItemResponse,
)
def update_action_item(
    action_item_id: int,
    action_item: ActionItemUpdate,
):
    db = SessionLocal()

    try:
        item = (
            db.query(ActionItem)
            .filter(ActionItem.id == action_item_id)
            .first()
        )

        if not item:
            raise HTTPException(
                status_code=404,
                detail="Action item not found",
            )

        update_data = action_item.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(item, field, value)

        db.commit()
        db.refresh(item)

        return item

    finally:
        db.close()


@app.delete("/action-items/{action_item_id}")
def delete_action_item(action_item_id: int):
    db = SessionLocal()

    try:
        item = (
            db.query(ActionItem)
            .filter(ActionItem.id == action_item_id)
            .first()
        )

        if not item:
            raise HTTPException(
                status_code=404,
                detail="Action item not found",
            )

        db.delete(item)
        db.commit()

        return {"message": "Action item deleted successfully"}

    finally:
        db.close()


# --------------------------------------------------
# Seed data
# --------------------------------------------------

def seed_database():
    db = SessionLocal()

    try:
        existing = (
            db.query(Meeting)
            .filter(Meeting.id == 1)
            .first()
        )

        if existing:
            return

        meeting = Meeting(
            id=1,
            title="Product Strategy Meeting",
            meeting_date=datetime(2026, 9, 20, 10, 30),
            duration_seconds=3120,
            participants="Aryan, Sarah, Michael, David",
            summary=(
                "The team discussed the upcoming product roadmap, customer feedback, "
                "engineering priorities, and the timeline for the next release."
            ),
        )

        db.add(meeting)
        db.flush()

        transcript = [
            TranscriptLine(
                meeting_id=meeting.id,
                speaker="Aryan",
                text="Thanks everyone for joining. Let's go through the product roadmap.",
                start_time=0,
            ),
            TranscriptLine(
                meeting_id=meeting.id,
                speaker="Sarah",
                text="The biggest priority from customers is improving the onboarding experience.",
                start_time=24,
            ),
            TranscriptLine(
                meeting_id=meeting.id,
                speaker="Michael",
                text="From engineering, we can have the new onboarding flow ready next sprint.",
                start_time=51,
            ),
            TranscriptLine(
                meeting_id=meeting.id,
                speaker="David",
                text="We should also make sure analytics are included in the first release.",
                start_time=84,
            ),
            TranscriptLine(
                meeting_id=meeting.id,
                speaker="Aryan",
                text="Agreed. Let's make analytics part of the acceptance criteria.",
                start_time=108,
            ),
        ]

        db.add_all(transcript)

        action_items = [
            ActionItem(
                meeting_id=meeting.id,
                text="Finalize onboarding requirements",
                completed=0,
            ),
            ActionItem(
                meeting_id=meeting.id,
                text="Prepare analytics implementation plan",
                completed=0,
            ),
            ActionItem(
                meeting_id=meeting.id,
                text="Review customer feedback",
                completed=1,
            ),
        ]

        db.add_all(action_items)
        db.commit()

    finally:
        db.close()


seed_database()