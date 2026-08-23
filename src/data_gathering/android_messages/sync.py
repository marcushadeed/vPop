"""Sync the messages table with the raw data."""

from data_gathering.android_messages.grab_raw_xml import download_raw_xml
from data_gathering.android_messages.xml_to_sqlite import xml_to_sqlite
from data_gathering.file_management.data_paths import (
    vpop_data_dir,
    android_messages_raw_dir,
)


def sync(db_path: str) -> None:
    """
    Sync the messages table with the raw data.
    """
    download_raw_xml()

    xml_to_sqlite(str(android_messages_raw_dir()), db_path)
