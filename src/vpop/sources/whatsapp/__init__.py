"""
WhatsApp messages from Android's end-to-end encrypted backups (`msgstore*.db.crypt15`),
uploaded to a Google Drive folder and synced into SQLite.

WhatsApp writes a full backup every few days (`msgstore.db.crypt15`, an SQLite database)
and a small increment each day in between (`msgstore-increment-N.db.crypt15`, a ZIP of
protobuf). vPop keeps only the latest full backup and its increments, so the database also
holds messages since deleted on the phone that no backup on disk has any more.
"""
