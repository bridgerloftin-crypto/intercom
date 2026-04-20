#!/bin/bash
# Nightly intercom log miner — mines daily logs into MemPalace
# Run via launchd at 11:30pm ET daily

MEMPALACE="/Users/Clawdio/Library/Python/3.9/bin/mempalace"
INTERCOM_LOGS="/Users/Clawdio/.mempalace/intercom"
FORGE_SESSIONS="/Users/Clawdio/.mempalace/forge/sessions"

# Mine shared intercom logs
if [ -d "$INTERCOM_LOGS" ] && [ "$(ls -A $INTERCOM_LOGS/*.md 2>/dev/null)" ]; then
    $MEMPALACE mine "$INTERCOM_LOGS" --wing memory --agent intercom 2>&1
fi

# Mine forge session logs
if [ -d "$FORGE_SESSIONS" ] && [ "$(ls -A $FORGE_SESSIONS/*.md 2>/dev/null)" ]; then
    $MEMPALACE mine "$FORGE_SESSIONS" --wing forge --agent forge 2>&1
fi

# Compress new drawers
$MEMPALACE compress --wing forge 2>&1
$MEMPALACE compress --wing memory 2>&1

# Rotate changelog — archive yesterday's, start fresh
CHANGELOG="/Users/Clawdio/.mempalace/changelog"
TODAY=$(date +%Y-%m-%d)
YESTERDAY=$(date -v-1d +%Y-%m-%d)
if [ -f "$CHANGELOG/today.md" ]; then
    mv "$CHANGELOG/today.md" "$CHANGELOG/$YESTERDAY.md" 2>/dev/null
    echo "# Changelog — $TODAY" > "$CHANGELOG/today.md"
fi
