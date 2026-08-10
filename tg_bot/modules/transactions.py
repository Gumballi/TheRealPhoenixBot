import logging
import os
from time import sleep
from typing import List

import requests

from telegram import Update, Bot, ParseMode
from telegram.ext import CommandHandler
from telegram.ext.dispatcher import run_async

from tg_bot import dispatcher

LOGGER = logging.getLogger(__name__)

# verify.et backend. Get an API key from verify.et and export VERIFY_ET_API_KEY.
API_KEY = os.environ.get("VERIFY_ET_API_KEY", "")
BASE_URL = os.environ.get("VERIFY_ET_BASE_URL", "https://verify.et").rstrip("/")

WAIT_MS = 8000          # ask the API to hold the request until it completes
POLL_ATTEMPTS = 20      # fallback polling for 202-queued requests
POLL_DELAY = 1.0        # seconds between status polls
REQUEST_TIMEOUT = 30

session = requests.Session()
session.headers.update({"Content-Type": "application/json"})

BANK_SPECS = [
    ("Telebirr", "tb", "telebirr", ["transactionNumber"]),
    ("M-Pesa", "mpesa", "mpesa", ["transactionNumber"]),
    ("CBE", "cbe", "cbe", ["referenceNumber", "accountSuffix"]),
    ("Bank of Abyssinia", "boa", "boa", ["referenceNumber", "accountSuffix"]),
    ("CBE Birr", "cbebirr", "cbebirr", ["receiptNumber", "phone"]),
    ("Dashen Bank", "dashen", "dashen", ["referenceNumber"]),
    ("Awash Bank", "awash", "awash", ["referenceNumber"]),
    ("Siinqee Bank", "siinqee", "siinqee", ["referenceNumber"]),
    ("Kaafi eBirr", "kaafi", "kaafiebirr", ["referenceNumber", "phone"]),
]

BANK_NAMES = {spec[2]: spec[0] for spec in BANK_SPECS}


def _verify_transaction(payload: dict) -> dict:
    if not API_KEY:
        return {"error": "setup"}
    headers = {"x-api-key": API_KEY}
    try:
        resp = session.post(
            BASE_URL + "/api/verify",
            params={"waitMs": WAIT_MS},
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as err:
        LOGGER.warning("verify.et request failed: %s", err)
        return {"error": "network"}

    try:
        body = resp.json()
    except ValueError:
        LOGGER.warning("verify.et returned non-JSON (HTTP %s)", resp.status_code)
        return {"error": "bad_response"}

    if resp.status_code == 202 and body.get("statusUrl"):
        for _ in range(POLL_ATTEMPTS):
            sleep(POLL_DELAY)
            try:
                poll = session.get(
                    BASE_URL + body["statusUrl"], headers=headers, timeout=REQUEST_TIMEOUT
                )
                status_body = poll.json()
            except (requests.RequestException, ValueError):
                break
            if status_body.get("data", {}).get("processingStatus") in ("completed", "failed"):
                return status_body

    return body


def _format_result(provider: str, body: dict) -> str:
    if body.get("success") is False:
        message = body.get("message") or "Verification failed."
        return "*Verification failed:*\n`{}`".format(message)

    data = body.get("data") or []
    if not data:
        status = (body.get("verification") or {}).get("status", "unknown")
        if status == "pending":
            return "Transaction is *still processing*. Try again in a few seconds."
        return "*Transaction not found* or could not be verified."

    item = data[0]
    if not item.get("verified"):
        return "*Transaction not found* or could not be verified."

    lines = [
        "*{}* - VERIFIED".format(provider),
        "",
        "Status: Success",
    ]
    if item.get("amount") is not None:
        currency = item.get("currency") or "ETB"
        lines.append("Amount: `{} {}`".format(item["amount"], currency))
    if item.get("senderName"):
        lines.append("Sender: {}".format(item["senderName"]))
    if item.get("receiverName"):
        lines.append("Receiver: {}".format(item["receiverName"]))
    if item.get("receiverAccount"):
        lines.append("Receiver Account: `{}`".format(item["receiverAccount"]))
    if item.get("referenceNumber"):
        lines.append("Reference: `{}`".format(item["referenceNumber"]))
    if item.get("accountSuffix"):
        lines.append("Account Suffix: `{}`".format(item["accountSuffix"]))
    if item.get("timestamp"):
        lines.append("Date: `{}`".format(item["timestamp"]))

    confirmation = item.get("confirmationHistory") or {}
    if confirmation:
        if confirmation.get("confirmedBefore"):
            lines.append(
                "Reused receipt: yes (confirmed {}x before)".format(
                    confirmation.get("confirmationCount", 0)
                )
            )
        else:
            lines.append("Reused receipt: no (first confirmation)")

    return "\n".join(lines)


def _reply_result(message, provider: str, payload: dict) -> None:
    status = message.reply_text("Verifying {} transaction...".format(provider))
    body = _verify_transaction(payload)
    if body.get("error") == "setup":
        status.edit_text(
            "Verifier not configured. Set the `VERIFY_ET_API_KEY` environment variable and restart.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif body.get("error") == "network":
        status.edit_text("Could not reach the verification service. Try again later.")
    elif body.get("error") == "bad_response":
        status.edit_text("Unexpected response from the verification service.")
    else:
        status.edit_text(_format_result(provider, body), parse_mode=ParseMode.MARKDOWN)


def _make_verifier(provider: str, command: str, bank: str, fields: List[str]):
    @run_async
    def handler(bot: Bot, update: Update, args: List[str]) -> None:
        message = update.effective_message
        if not args or len(args) < len(fields):
            message.reply_text(
                "Usage: /{} <{}>".format(command, "> <".join(fields))
            )
            return
        payload = {"bank": bank}
        for index, field in enumerate(fields):
            payload[field] = args[index].strip()
        _reply_result(message, provider, payload)

    return command, handler


@run_async
def verify_universal(bot: Bot, update: Update, args: List[str]) -> None:
    message = update.effective_message
    if not args:
        message.reply_text("Usage: /verify <reference> [account-suffix] [phone-number]")
        return
    payload = {"reference": args[0].strip()}
    if len(args) > 1:
        payload["suffix"] = args[1].strip()
    if len(args) > 2:
        payload["phoneNumber"] = args[2].strip()
    _reply_result(message, "Universal", payload)


@run_async
def transaction_menu(bot: Bot, update: Update) -> None:
    message = update.effective_message
    lines = [
        "*Transaction Verifier*",
        "",
        "Verify Ethiopian bank and wallet transactions via verify.et.",
        "",
        "*Commands:*",
        " - `/tb <transaction>` Telebirr",
        " - `/mpesa <transaction>` M-Pesa",
        " - `/cbe <reference> <suffix8>` Commercial Bank of Ethiopia",
        " - `/boa <reference> <suffix5>` Bank of Abyssinia",
        " - `/cbebirr <receipt> <phone>` CBE Birr",
        " - `/dashen <reference>` Dashen Bank",
        " - `/awash <reference>` Awash Bank",
        " - `/siinqee <reference>` Siinqee Bank",
        " - `/kaafi <reference> [phone]` Kaafi eBirr",
        " - `/verify <reference> [suffix] [phone]` Universal",
    ]
    message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


dispatcher.add_handler(CommandHandler("transaction", transaction_menu))

UNIVERSAL_HANDLER = CommandHandler("verify", verify_universal, pass_args=True)
dispatcher.add_handler(UNIVERSAL_HANDLER)

for provider, command, bank, fields in BANK_SPECS:
    cmd, handler = _make_verifier(provider, command, bank, fields)
    dispatcher.add_handler(CommandHandler(cmd, handler, pass_args=True))

__help__ = """
*Transaction Verifier*

Verify Ethiopian bank and wallet payment receipts using the verify.et API.

*Commands:*
 - /transaction: Show all available verifiers.
 - /tb <transaction>: Verify a Telebirr transaction.
 - /mpesa <transaction>: Verify an M-Pesa transaction.
 - /cbe <reference> <suffix8>: Verify a CBE transfer.
 - /boa <reference> <suffix5>: Verify a Bank of Abyssinia transfer.
 - /cbebirr <receipt> <phone>: Verify a CBE Birr payment.
 - /dashen <reference>: Verify a Dashen Bank transfer.
 - /awash <reference>: Verify an Awash Bank transfer.
 - /siinqee <reference>: Verify a Siinqee Bank transfer.
 - /kaafi <reference> [phone]: Verify a Kaafi eBirr payment.
 - /verify <reference> [suffix] [phone]: Universal smart-router lookup.

Requires the VERIFY_ET_API_KEY environment variable.
"""

__mod_name__ = "Transactions"
