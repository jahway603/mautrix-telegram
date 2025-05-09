# mautrix-telegram - A Matrix-Telegram puppeting bridge
# Copyright (C) 2021 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from typing import Any
import asyncio
import io

from telethon.errors import (
    AccessTokenExpiredError,
    AccessTokenInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberAppSignupForbiddenError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

from mautrix.client import Client
from mautrix.types import (
    EventID,
    ImageInfo,
    MediaMessageEventContent,
    MessageType,
    TextMessageEventContent,
    UserID,
)
from mautrix.util import background_task
from mautrix.util.format_duration import format_duration as fmt_duration

from ... import user as u
from ...commands import SECTION_AUTH, CommandEvent, command_handler
from ...types import TelegramID

try:
    from telethon.tl.custom import QRLogin
    import PIL as _
    import qrcode
except ImportError:
    qrcode = None
    QRLogin = None


@command_handler(
    needs_auth=False, help_section=SECTION_AUTH, help_text="Check if you're logged into Telegram."
)
async def ping(evt: CommandEvent) -> EventID:
    if await evt.sender.is_logged_in():
        me = await evt.sender.get_me()
        if me:
            human_tg_id = f"@{me.username}" if me.username else f"+{me.phone}"
            return await evt.reply(f"You're logged in as {human_tg_id}")
        else:
            return await evt.reply("You were logged in, but there appears to have been an error.")
    else:
        return await evt.reply("You're not logged in.")


@command_handler(
    needs_auth=False,
    needs_puppeting=False,
    help_section=SECTION_AUTH,
    help_text="Get the info of the message relay Telegram bot.",
)
async def ping_bot(evt: CommandEvent) -> EventID:
    if not evt.tgbot:
        return await evt.reply("Telegram message relay bot not configured.")
    info, mxid = await evt.tgbot.get_me(use_cache=False)
    return await evt.reply(
        "Telegram message relay bot is active: "
        f"[{info.first_name}](https://matrix.to/#/{mxid}) (ID {info.id})\n\n"
        "To use the bot, simply invite it to a portal room."
    )


@command_handler(
    needs_auth=False,
    management_only=True,
    help_section=SECTION_AUTH,
    help_text="Log in by scanning a QR code.",
)
async def login_qr(evt: CommandEvent) -> EventID:
    login_as = evt.sender
    if len(evt.args) > 0 and evt.sender.is_admin:
        login_as = await u.User.get_by_mxid(UserID(evt.args[0]))
    if not qrcode or not QRLogin:
        return await evt.reply("This bridge instance does not support logging in with a QR code.")
    if await login_as.is_logged_in():
        return await evt.reply(f"You are already logged in as {login_as.human_tg_id}.")

    await login_as.ensure_started(even_if_no_session=True)
    qr_login = QRLogin(login_as.client, ignored_ids=[])
    qr_event_id: EventID | None = None

    async def upload_qr() -> None:
        nonlocal qr_event_id
        buffer = io.BytesIO()
        image = qrcode.make(qr_login.url)
        size = image.pixel_size
        image.save(buffer, "PNG")
        qr = buffer.getvalue()
        mxc = await evt.az.intent.upload_media(qr, "image/png", "login-qr.png", len(qr))
        content = MediaMessageEventContent(
            body=qr_login.url,
            filename="login-qr.png",
            url=mxc,
            msgtype=MessageType.IMAGE,
            info=ImageInfo(mimetype="image/png", size=len(qr), width=size, height=size),
        )
        if qr_event_id:
            content.set_edit(qr_event_id)
            await evt.az.intent.send_message(evt.room_id, content)
        else:
            content.set_reply(evt.event_id)
            qr_event_id = await evt.az.intent.send_message(evt.room_id, content)

    retries = 4
    while retries > 0:
        await qr_login.recreate()
        await upload_qr()
        try:
            user = await qr_login.wait()
            break
        except asyncio.TimeoutError:
            retries -= 1
        except SessionPasswordNeededError:
            evt.sender.command_status = {
                "next": enter_password,
                "login_as": login_as if login_as != evt.sender else None,
                "action": "Login (password entry)",
            }
            return await evt.reply(
                "Your account has two-factor authentication. Please send your password here."
            )
        try:
            await evt.main_intent.redact(evt.room_id, qr_event_id, reason="QR code scanned")
        except Exception:
            pass
    else:
        timeout = TextMessageEventContent(body="Login timed out", msgtype=MessageType.TEXT)
        timeout.set_edit(qr_event_id)
        return await evt.az.intent.send_message(evt.room_id, timeout)

    return await _finish_sign_in(evt, user, login_as=login_as)


@command_handler(
    needs_auth=False,
    management_only=True,
    help_section=SECTION_AUTH,
    help_text="Get instructions on how to log in.",
)
async def login(evt: CommandEvent) -> EventID:
    override_sender = False
    if len(evt.args) > 0 and evt.sender.is_admin and evt.args[0]:
        override_user_id = UserID(evt.args[0])
        try:
            Client.parse_user_id(override_user_id)
        except ValueError:
            return await evt.reply(
                f"**Usage:** `$cmdprefix+sp login [override user ID]`\n\n"
                f"{override_user_id!r} is not a valid Matrix user ID"
            )
        orig_user_id = evt.sender.mxid
        evt.sender = await u.User.get_and_start_by_mxid(override_user_id)
        override_sender = True
        if orig_user_id != evt.sender:
            await evt.reply(
                f"Admin override: logging in as {evt.sender.mxid} instead of {orig_user_id}"
            )

    if await evt.sender.is_logged_in():
        return await evt.reply(f"You are already logged in as {evt.sender.human_tg_id}.")

    allow_matrix_login = evt.config["bridge.allow_matrix_login"]
    if allow_matrix_login and not override_sender:
        evt.sender.command_status = {
            "next": enter_phone_or_token,
            "action": "Login",
        }

    nb = "**N.B. Logging in grants the bridge full access to your Telegram account.**"
    if evt.config["appservice.public.enabled"]:
        prefix = evt.config["appservice.public.external"]
        url = f"{prefix}/login?token={evt.public_website.make_token(evt.sender.mxid, '/login')}"
        if override_sender:
            return await evt.reply(
                f"[Click here to log in]({url}) as "
                f"[{evt.sender.mxid}](https://matrix.to/#/{evt.sender.mxid})."
            )
        elif allow_matrix_login:
            return await evt.reply(
                f"[Click here to log in]({url}). Alternatively, send your phone"
                f" number (or bot auth token) here to log in.\n\n{nb}"
            )
        return await evt.reply(f"[Click here to log in]({url}).\n\n{nb}")
    elif allow_matrix_login:
        if override_sender:
            return await evt.reply(
                "This bridge instance does not allow you to log in outside of Matrix. "
                "Logging in as another user inside Matrix is not currently possible."
            )
        return await evt.reply(
            "Please send your phone number (or bot auth token) here to start "
            f"the login process.\n\n{nb}"
        )
    return await evt.reply("This bridge instance has been configured to not allow logging in.")


async def _request_code(
    evt: CommandEvent, phone_number: str, next_status: dict[str, Any]
) -> EventID:
    ok = False
    try:
        await evt.sender.ensure_started(even_if_no_session=True)
        await evt.sender.client.sign_in(phone_number)
        ok = True
        return await evt.reply(f"Login code sent to {phone_number}. Please send the code here.")
    except PhoneNumberAppSignupForbiddenError:
        return await evt.reply("Your phone number does not allow 3rd party apps to sign in.")
    except PhoneNumberFloodError:
        return await evt.reply(
            "Your phone number has been temporarily blocked for flooding. "
            "The ban is usually applied for around a day."
        )
    except FloodWaitError as e:
        return await evt.reply(
            "Your phone number has been temporarily blocked for flooding. "
            f"Please wait for {fmt_duration(e.seconds)} before trying again."
        )
    except PhoneNumberBannedError:
        return await evt.reply("Your phone number has been banned from Telegram.")
    except PhoneNumberUnoccupiedError:
        return await evt.reply(
            "That phone number has not been registered. "
            "Please sign up to Telegram using an official mobile client first."
        )
    except PhoneNumberInvalidError:
        return await evt.reply("That phone number is not valid.")
    except Exception:
        evt.log.exception("Error requesting phone code")
        return await evt.reply(
            "Unhandled exception while requesting code. Check console for more details."
        )
    finally:
        evt.sender.command_status = next_status if ok else None


@command_handler(needs_auth=False)
async def enter_phone_or_token(evt: CommandEvent) -> EventID | None:
    if len(evt.args) == 0:
        return await evt.reply("**Usage:** `$cmdprefix+sp enter-phone-or-token <phone-or-token>`")
    elif not evt.config.get("bridge.allow_matrix_login", True):
        return await evt.reply(
            "This bridge instance does not allow in-Matrix login. "
            "Please use `$cmdprefix+sp login` to get login instructions"
        )

    # phone numbers don't contain colons but telegram bot auth tokens do
    if evt.args[0].find(":") > 0:
        try:
            await _sign_in(evt, bot_token=evt.args[0])
        except Exception:
            evt.log.exception("Error sending auth token")
            return await evt.reply(
                "Unhandled exception while sending auth token. Check console for more details."
            )
    else:
        await _request_code(evt, evt.args[0], {"next": enter_code, "action": "Login"})
    return None


@command_handler(needs_auth=False)
async def enter_code(evt: CommandEvent) -> EventID | None:
    if len(evt.args) == 0:
        return await evt.reply("**Usage:** `$cmdprefix+sp enter-code <code>`")
    elif not evt.config.get("bridge.allow_matrix_login", True):
        return await evt.reply(
            "This bridge instance does not allow in-Matrix login. "
            "Please use `$cmdprefix+sp login` to get login instructions"
        )
    try:
        await _sign_in(evt, code=evt.args[0])
    except Exception:
        evt.log.exception("Error sending phone code")
        return await evt.reply(
            "Unhandled exception while sending code. Check console for more details."
        )
    return None


@command_handler(needs_auth=False)
async def enter_password(evt: CommandEvent) -> EventID | None:
    if len(evt.args) == 0:
        return await evt.reply("**Usage:** `$cmdprefix+sp enter-password <password>`")
    elif not evt.config.get("bridge.allow_matrix_login", True):
        return await evt.reply(
            "This bridge instance does not allow in-Matrix login. "
            "Please use `$cmdprefix+sp login` to get login instructions"
        )
    await evt.redact()
    try:
        await _sign_in(
            evt,
            login_as=evt.sender.command_status.get("login_as", None),
            password=" ".join(evt.args),
        )
    except AccessTokenInvalidError:
        return await evt.reply("That bot token is not valid.")
    except AccessTokenExpiredError:
        return await evt.reply("That bot token has expired.")
    except Exception:
        evt.log.exception("Error sending password")
        return await evt.reply(
            "Unhandled exception while sending password. Check console for more details."
        )
    return None


async def _sign_in(evt: CommandEvent, login_as: u.User = None, **sign_in_info) -> EventID:
    login_as = login_as or evt.sender
    try:
        await login_as.ensure_started(even_if_no_session=True)
        user = await login_as.client.sign_in(**sign_in_info)
        await _finish_sign_in(evt, user)
    except PhoneCodeExpiredError:
        return await evt.reply("Phone code expired. Try again with `$cmdprefix+sp login`.")
    except PhoneCodeInvalidError:
        return await evt.reply("Invalid phone code.")
    except PasswordHashInvalidError:
        return await evt.reply("Incorrect password.")
    except SessionPasswordNeededError:
        evt.sender.command_status = {
            "next": enter_password,
            "action": "Login (password entry)",
        }
        return await evt.reply(
            "Your account has two-factor authentication. Please send your password here."
        )


async def _finish_sign_in(evt: CommandEvent, user: User, login_as: u.User = None) -> EventID:
    login_as = login_as or evt.sender
    existing_user = await u.User.get_by_tgid(TelegramID(user.id))
    if existing_user and existing_user != login_as:
        await existing_user.log_out()
        await evt.reply(
            f"[{existing_user.displayname}] (https://matrix.to/#/{existing_user.mxid})"
            " was logged out from the account."
        )
    background_task.create(login_as.post_login(user, first_login=True))
    evt.sender.command_status = None
    name = f"@{user.username}" if user.username else f"+{user.phone}"
    if login_as != evt.sender:
        msg = (
            f"Successfully logged in [{login_as.mxid}](https://matrix.to/#/{login_as.mxid})"
            f" as {name}"
        )
    else:
        msg = f"Successfully logged in as {name}"
    return await evt.reply(msg)


@command_handler(needs_auth=False, help_section=SECTION_AUTH, help_text="Log out from Telegram.")
async def logout(evt: CommandEvent) -> EventID:
    if not evt.sender.tgid:
        return await evt.reply("You're not logged in")
    if await evt.sender.log_out():
        return await evt.reply("Logged out successfully.")
    return await evt.reply("Failed to log out.")
