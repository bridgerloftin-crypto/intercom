#!/bin/bash
# Example MemPalace mining script for Intercom logs.

MEMPALACE="${MEMPALACE_BIN:-mempalace}"
INTERCOM_LOGS="${INTERCOM_LOGS_DIR:-$HOME/.mempalace/intercom}"
WING="${MEMPALACE_WING:-memory}"

if [ -d "$INTERCOM_LOGS" ]; then
    "$MEMPALACE" mine "$INTERCOM_LOGS" --wing "$WING" --agent intercom 2>&1
fi
