"""
This module contains all the HTML based routes for the app.
"""
import datetime
import logging
import os
import secrets
from pathlib import Path
from typing import Annotated, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from starlette.responses import RedirectResponse

from scribe.config.settings import settings
from scribe.dependencies import (
    consume_notifications,
    get_session,
    session_user,
    slack_client,
)
from scribe.models.models import Notification, NotificationType, Recording, User
from scribe.session.session import Session
from scribe.slack import slack
from scribe.slack.slack import SlackClient, SlackError
from scribe.text import text
from scribe.text.text import TranslationException

# initialize the frontend router
router = APIRouter()

# configure the template engine
_templates = Jinja2Templates(directory="templates")
_templates.env.globals["now"] = datetime.datetime.utcnow()


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user: Annotated[str, Depends(session_user)],
    notifications: Annotated[List[Notification], Depends(consume_notifications)],
):
    """
    Renders the home page

    :param request: The request object
    :param user: The logged-in user
    :param notifications: Any user notifications
    :return: HTML Response
    """

    return _templates.TemplateResponse(
        "pages/home.html.j2",
        {
            "request": request,
            "user": user,
            "notifications": notifications,
        },
    )


@router.get("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    notifications: Annotated[List[Notification], Depends(consume_notifications)],
):
    """
    Renders the login page

    :param request: The HTTP Request
    :param session: The active Session object
    :param notifications: Any user notifications
    :return: HTML Response
    """

    state = secrets.token_urlsafe(16)
    session.set("state", state)
    nonce = secrets.token_urlsafe(16)
    session.set("nonce", nonce)

    openid_link = (
        f"{settings.SLACK_OPENID_URL}?scope=openid"
        f"&response_type=code"
        f"&state={ state }"
        f"&nonce={ nonce }"
        f"&redirect_uri={slack.redirect_uri}"
        f"&team={settings.SLACK_TEAM_ID}"
        f"&client_id={settings.SLACK_CLIENT_ID}"
    )

    auth_link = (
        f"{settings.SLACK_AUTH_URL}?scope="
        f"&user_scope={ ','.join(settings.SLACK_USER_SCOPES) }"
        f"&response_type=code"
        f"&state={ state }"
        f"&nonce={ nonce }"
        f"&redirect_uri={slack.redirect_uri}"
        f"&team={settings.SLACK_TEAM_ID}"
        f"&client_id={settings.SLACK_CLIENT_ID}"
    )

    return _templates.TemplateResponse(
        "pages/login.html.j2",
        {
            "request": request,
            "auth_link": auth_link,
            "openid_link": openid_link,
            "notifications": notifications,
        },
    )


@router.get("/auth/redirect")
async def read_code(
    code: str,
    state: str,
    session: Annotated[Session, Depends(get_session)],
):
    """
    Convert OAuth code to access token and redirect to home page.

    :param code: The OAuth code returned by Slack
    :param state: The state variable sent in the auth request
    :param session: The active Session object
    :return: RedirectResponse
    """
    session_state = session.get("state")
    if not session_state or session_state != state:
        session.delete("state")
        session.delete("nonce")

        session.append(
            "notifications",
            [
                Notification(
                    type="error",
                    title="Login error",
                    message="An error occurred logging in. Please try again.",
                )
            ],
        )
        return RedirectResponse("/login")

    try:
        access_token = slack.access_token(code)
        client = slack.SlackClient(access_token)
        # todo make calls asynchronously
        user = client.user()
        session.set("access_token", access_token)
        session.set("user", user)
        session.set("team", client.team())
        session.set("channels", client.channels())

        session.append(
            "notifications",
            [
                Notification(
                    type="success",
                    title="Welcome back!",
                    message=f"Welcome back to Scribe {user.first_name}.",
                )
            ],
        )

        return RedirectResponse("/")
    except SlackError as err:
        logging.warning(f"Invalid token: {err}")
        session.append(
            "notifications",
            [
                Notification(
                    type="error",
                    title="Login error",
                    message="An error occurred logging in. Please try again.",
                )
            ],
        )
        return RedirectResponse("/login")
    except Exception as err:
        logging.debug(f"uncaught exception: {err}")

        session.append(
            "notifications",
            [
                Notification(
                    type="error",
                    title="Login error",
                    message="An error occurred logging in. Please try again.",
                )
            ],
        )
        return RedirectResponse("/login")
    finally:
        session.delete("state")
        session.delete("nonce")


async def transcription_event_generator(request: Request, recording: Recording):
    """
    Server Side Event Generator: Transcribes a recording and sends a message when done.
    :param request: The HTTP Request
    :param recording: The audio recording to transcribe
    :returns: a message dict with a turbo stream for updating the UI
    """
    while True:
        if await request.is_disconnected():
            logging.debug("Request disconnected")
            break

        try:
            recording.transcription = text.transcribe(recording.file_path)
            logging.debug("Transcription completed. Disconnecting now")
            data = _templates.get_template(
                "streams/transcription_complete.html.j2"
            ).render({"request": request, "recording": recording})
            yield {"event": "message", "data": data}
        except RuntimeError as err:
            logging.warning(f"error transcribing audio: {err}")
            data = _templates.get_template("streams/send_notification.html.j2").render(
                {
                    "request": request,
                    "notification": Notification(
                        type="error",
                        title="Error transcribing audio",
                        message="We're sorry, your recording could not be processed.",
                    ),
                }
            )
            yield {"event": "message", "data": data}
        break


@router.post("/upload")
async def upload(
    audio_file: UploadFile,
    request: Request,
    _user: Annotated[User, Depends(session_user)],
    session: Annotated[Session, Depends(get_session)],
):
    """
    upload an audio file
    :param audio_file: mpeg, ogg, wan, or flac audio file
    :param request: the HTTP request
    :param _user: logged-in user, used to check user session exists
    :param session: the active Session object
    :return: HTML Response
    """
    allowed_mime_types = ["audio/mpeg", "audio/ogg", "audio/wav", "audio/flac"]
    if audio_file.content_type not in allowed_mime_types:
        logging.warning(f"invalid content-type: {audio_file.content_type}")
        return _templates.TemplateResponse(
            "streams/send_notification.html.j2",
            {
                "request": request,
                "notification": Notification(
                    type="error",
                    title="Invalid file type",
                    message="The wrong type of audio file was submitted.",
                ),
            },
            # Turbo needs post data that returns HTML to set a status of 303 redirect.
            # This is due to how browsers handle page history with form submissions.
            status_code=415,
            headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
        )

    try:
        file_id = uuid4()
        parent_dir = os.path.join(settings.UPLOAD_PATH, session.id)
        Path(parent_dir).mkdir(parents=True, exist_ok=True)
        file_path = os.path.join(parent_dir, f"{file_id }.ogg")

        file_content = await audio_file.read()
        # Save the content to the file
        with open(file_path, "wb") as f:
            f.write(file_content)

        recording = Recording(id=str(file_id), file_path=file_path)
        recordings = session.get("recordings", {})
        recordings[recording.id] = recording
        session.set("recordings", recordings)

        return _templates.TemplateResponse(
            "streams/upload_audio.html.j2",
            {"request": request, "recording": recording},
            # Turbo needs post data that returns HTML to set a status of 303 redirect.
            # This is due to how browsers handle page history with form submissions.
            status_code=303,
            headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
        )
    except Exception as err:
        logging.warning(f"Failed to upload audio: {err}")
        return _templates.TemplateResponse(
            "streams/send_notification.html.j2",
            {
                "request": request,
                "notification": Notification(
                    type="error",
                    title="Error processing recording",
                    message="An error occurred while uploading your recording.",
                ),
            },
            # Turbo needs post data that returns HTML to set a status of 303 redirect.
            # This is due to how browsers handle page history with form submissions.
            status_code=500,
            headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
        )


@router.get("/recordings/{recording_id}")
async def transcribe_recording(
    request: Request,
    recording_id: str,
    _user: Annotated[User, Depends(session_user)],
    session: Annotated[Session, Depends(get_session)],
):
    """
    Transcribe an audio recording

    :param request: the HTTP request object
    :param recording_id: The id of the audio recording
    :param _user: active session user
    :param session: active Session object
    :return: SSE EventSourceResponse
    """
    recordings = session.get("recordings", {})
    recording = recordings.get(recording_id, None)
    if not recording:
        return _templates.TemplateResponse(
            "streams/send_notification.html.j2",
            {
                "request": request,
                "notification": Notification(
                    type="error",
                    title="Recording not found",
                    message="We could not find the recording file requested.",
                ),
            },
            # Turbo needs post data that returns HTML to set a status of 303 redirect.
            # This is due to how browsers handle page history with form submissions.
            status_code=404,
            headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
        )

    event_generator = transcription_event_generator(request, recording)

    return EventSourceResponse(
        event_generator,
        headers={"Content-Type": "text/event-stream"},
    )


@router.post("/publish")
async def publish(
    formatted_message: Annotated[str, Form()],
    post_image: Optional[UploadFile],
    request: Request,
    _user: Annotated[User, Depends(session_user)],
    client: Annotated[SlackClient, Depends(slack_client)],
    pin_to_channel: Annotated[bool, Form()] = False,
    notify_channel: Annotated[bool, Form()] = False,
):
    """
    Translate a message and publish it to each language channel.

    :param formatted_message: message formatted as HTML
    :param post_image: image file to append to post
    :param request: the http request
    :param _user: active session user
    :param client: SlackClient object
    :param pin_to_channel: flag for pinning messages to Slack channels
    :param notify_channel: Begin post with Dear @channel?
    :return: HTML Response
    """
    try:
        message = formatted_message.strip()
        messages = {
            settings.SOURCE_LANGUAGE: message,
        } | {
            target: text.translate(message, target)
            for target in settings.TARGET_LANGUAGES
        }

        if post_image.size == 0:
            post_image = None

        client.publish(messages, post_image, pin_to_channel, notify_channel)

        return _templates.TemplateResponse(
            "streams/message_published.html.j2",
            {
                "request": request,
                "notification": Notification(
                    type="success",
                    title="Message published",
                    message="Your message has been translated and published",
                ),
            },
            status_code=303,
            headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
        )
    except TranslationException:
        return _templates.TemplateResponse(
            "streams/send_notification.html.j2",
            {
                "request": request,
                "notification": Notification(
                    type="error",
                    title="Translation Error",
                    message="An error occurred while translating your message.",
                ),
            },
            # Turbo needs post data that returns HTML to set a status of 303 redirect.
            # This is due to how browsers handle page history with form submissions.
            status_code=500,
            headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
        )
    except SlackError:
        return _templates.TemplateResponse(
            "streams/send_notification.html.j2",
            {
                "request": request,
                "notification": Notification(
                    type="error",
                    title="Posting Error",
                    message="An error occurred while posting your message to Slack.",
                ),
            },
            # Turbo needs post data that returns HTML to set a status of 303 redirect.
            # This is due to how browsers handle page history with form submissions.
            status_code=500,
            headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
        )

    except Exception as err:
        logging.warning(f"error posting message: {err}")
        return _templates.TemplateResponse(
            "streams/send_notification.html.j2",
            {
                "request": request,
                "notification": Notification(
                    type="error",
                    title="Unknown Error",
                    message="An unknown error occurred..",
                ),
            },
            # Turbo needs post data that returns HTML to set a status of 303 redirect.
            # This is due to how browsers handle page history with form submissions.
            status_code=500,
            headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
        )


@router.get("/notify")
def notify(
    request: Request,
    ntype: Annotated[NotificationType, Query(alias="type")],
    title: str,
    message: str,
):
    return _templates.TemplateResponse(
        "streams/send_notification.html.j2",
        {
            "request": request,
            "notification": Notification(
                type=ntype,
                title=title,
                message=message,
            ),
        },
        # Turbo needs post data that returns HTML to set a status of 303 redirect.
        # This is due to how browsers handle page history with form submissions.
        status_code=200,
        headers={"Content-Type": "text/vnd.turbo-stream.html; charset=utf-8"},
    )
