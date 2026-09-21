"""Lazy Google Sheets connection; offline imports need no credentials."""

import os

import gspread
from google.oauth2.service_account import Credentials


def get_sheet(sheet_id, sheet_name):
    info = {
        "type": "service_account",
        "project_id": os.getenv("GOOGLE_CREDS_PROJECT_ID"),
        "private_key_id": os.getenv("GOOGLE_CREDS_PRIVATE_KEY_ID"),
        "private_key": os.getenv("GOOGLE_CREDS_PRIVATE_KEY", "").replace("\\n", "\n"),
        "client_email": os.getenv("GOOGLE_CREDS_CLIENT_EMAIL"),
        "client_id": os.getenv("GOOGLE_CREDS_CLIENT_ID"),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    credentials = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(credentials).open_by_key(sheet_id).worksheet(sheet_name)
