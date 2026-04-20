#!/usr/bin/env python3
"""
Intercom Logger — Writes processed messages directly into MemPalace.
Each agent imports this and calls log_message() after processing.
No flat files, no nightly mining — straight into the vector store.

Usage:
    from intercom_logger import log_message
    log_message('forge', msg, response_text)
"""

import hashlib
from datetime import datetime

_collection = None


def _get_collection():
    """Lazy-load ChromaDB collection."""
    global _collection
    if _collection is None:
        try:
            import chromadb
            from mempalace.config import MempalaceConfig
            cfg = MempalaceConfig()
            client = chromadb.PersistentClient(path=str(cfg.palace_path))
            _collection = client.get_or_create_collection(cfg.collection_name)
        except Exception as e:
            print(f"[intercom_logger] ChromaDB init failed: {e}")
            return None
    return _collection


def log_message(agent_name: str, msg: dict, response: str = None):
    """
    Log an intercom message directly into MemPalace as a drawer.

    Args:
        agent_name: Which agent processed this (forge, lumino, claude, waverly)
        msg: The raw intercom message dict
        response: The agent's response text (if any)
    """
    col = _get_collection()
    if col is None:
        return

    now = datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    time_str = now.strftime('%H:%M')

    msg_id = msg.get('id', '?')
    from_agent = msg.get('from_agent', '?')
    to_agent = msg.get('to_agent', '?')
    msg_type = msg.get('msg_type', 'msg')
    body = msg.get('body', '')
    ref_id = msg.get('ref_id')

    ref = f' (re: #{ref_id})' if ref_id else ''
    content = f"{time_str} #{msg_id} [{msg_type}] {from_agent}→{to_agent}{ref}: {body}"
    if response:
        resp_short = response[:300] + '...' if len(response) > 300 else response
        content += f"\n↳ {agent_name} replied: {resp_short}"

    # Determine wing based on agent
    wing = 'forge' if agent_name == 'forge' else 'memory'
    room = 'sessions' if agent_name == 'forge' else 'intercom'

    drawer_id = f"intercom_{msg_id}_{agent_name}_{hashlib.md5(str(msg_id).encode()).hexdigest()[:8]}"

    try:
        col.add(
            documents=[content],
            ids=[drawer_id],
            metadatas=[{
                'wing': wing,
                'room': room,
                'source_file': f'intercom-{date_str}.md',
                'chunk_index': 0,
                'added_by': agent_name,
                'filed_at': now.isoformat(),
                'intercom_id': str(msg_id),
                'from_agent': from_agent,
                'to_agent': to_agent,
            }]
        )
    except Exception as e:
        if 'already exists' not in str(e).lower():
            print(f"[intercom_logger] Write failed: {e}")
