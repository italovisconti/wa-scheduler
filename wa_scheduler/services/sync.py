from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from wa_scheduler.models import Chat, Contact
from wa_scheduler.services.wacli import WacliClient
from wa_scheduler.timeutil import parse_iso_datetime, utcnow


def sync_contacts(session: Session, client: WacliClient) -> int:
    client.contacts_refresh()
    rows, _ = client.contacts_search(query=".", limit=5000)

    count = 0
    for row in rows:
        jid = row.get("JID")
        if not jid:
            continue
        contact = session.scalar(select(Contact).where(Contact.wa_jid == jid))
        if contact is None:
            contact = Contact(wa_jid=jid)
            session.add(contact)
        contact.phone = row.get("Phone") or ""
        contact.display_name = row.get("Name") or contact.display_name or ""
        contact.alias = row.get("Alias") or contact.alias or ""
        tags = row.get("Tags") or []
        if isinstance(tags, list):
            contact.tags = ",".join(tags)
        contact.last_synced_at = parse_iso_datetime(row.get("UpdatedAt")) or utcnow()
        count += 1

    session.commit()
    return count


def sync_chats(session: Session, client: WacliClient) -> int:
    chats, _ = client.chats_list(limit=5000)
    groups, _ = client.groups_list()

    group_by_jid = {group.get("JID"): group for group in groups if group.get("JID")}
    # chats list and groups list can return the same group JID, so we keep an in-memory
    # map for the current sync pass and update the same row instead of inserting twice.
    existing_by_jid = {
        chat.wa_jid: chat for chat in session.scalars(select(Chat)).all() if chat.wa_jid
    }

    count = 0
    for row in chats:
        jid = row.get("JID")
        if not jid:
            continue
        chat = existing_by_jid.get(jid)
        if chat is None:
            chat = Chat(wa_jid=jid)
            session.add(chat)
            existing_by_jid[jid] = chat

        extra = group_by_jid.get(jid, {})
        chat.kind = (
            row.get("Kind") or ("group" if jid.endswith("@g.us") else "chat") or "chat"
        ).lower()
        chat.name = extra.get("Name") or row.get("Name") or chat.name or jid
        chat.owner_jid = extra.get("OwnerJID") or chat.owner_jid or ""
        chat.last_message_at = parse_iso_datetime(row.get("LastMessageTS"))
        chat.raw_payload = {**row, **extra}
        count += 1

    for row in groups:
        jid = row.get("JID")
        if not jid:
            continue
        chat = existing_by_jid.get(jid)
        if chat is None:
            chat = Chat(wa_jid=jid)
            session.add(chat)
            existing_by_jid[jid] = chat
        chat.kind = "group"
        chat.name = row.get("Name") or chat.name or jid
        chat.owner_jid = row.get("OwnerJID") or chat.owner_jid or ""
        chat.raw_payload = row
        count += 1

    session.commit()
    return count
