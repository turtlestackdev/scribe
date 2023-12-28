import secrets

import pytest
from fastapi.testclient import TestClient

import scribe.dependencies
import scribe.slack.slack
from scribe.main import app
from scribe.models.models import Channel, Team, User
from scribe.session.session import SessionStore

from . import mocks


@pytest.fixture(autouse=True)
def global_mocks(mocker):
    mocker.patch.object(scribe.slack.slack, "access_token", mocks.mock_access_token)
    mocker.patch.object(scribe.slack.slack, "SlackClient", mocks.MockSlackClient)


@pytest.fixture()
def api_client():
    client = TestClient(app)
    return client


@pytest.fixture()
def user():
    return User(
        id="U1234",
        real_name="John Smith",
        real_name_normalized="John Smith",
        first_name="John",
        last_name="Smith",
        display_name="John Smith",
    )


@pytest.fixture()
def team():
    return Team(id="T1234", name="The Super Friends")


@pytest.fixture()
def channel():
    return Channel(id="C1234", name="General")


@pytest.fixture()
def access_token(user, team, channel):
    token = secrets.token_urlsafe(16)
    mocks.token_resources[token] = {"user": user, "team": team, "channels": [channel]}
    return token


@pytest.fixture()
def oauth_code(access_token):
    code = secrets.token_urlsafe(16)
    mocks.code_tokens[code] = access_token
    return code


@pytest.fixture()
def session_store(mocker):
    session_store = SessionStore()
    mocker.patch.object(scribe.dependencies, "session_store", session_store)
    return session_store


@pytest.fixture()
def empty_session(session_store):
    return session_store.start_session()


@pytest.fixture()
def user_session(empty_session, user):
    token = "123456"
    empty_session.set("access_token", token)
    empty_session.set("user", user)

    return empty_session
