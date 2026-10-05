"""Sync the messages table with the raw data."""

from vpop.paths import android_messages_raw_dir
from vpop.sources.android_messages.fetch import download_raw_xml
from vpop.sources.android_messages.xml_to_sqlite import xml_to_sqlite


def sync() -> None:
    """
    Sync the messages table with the raw data.
    """
    download_raw_xml()
    for file in android_messages_raw_dir().glob("*.xml"):
        xml_to_sqlite(file)
